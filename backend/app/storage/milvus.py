"""Milvus 向量存储封装（共享 Collection + Partition Key + 按维度分表）

拓扑
----
物理 collection 名 = ``<base>_<dim>``，例如 ``artoo_chunks_1024``。两套 base：

- ``settings.milvus_collection``（默认 ``artoo_chunks``）—— 全部正式知识库共用，
  Partition Key = ``kb_id``。
- ``settings.milvus_session_collection``（默认 ``artoo_session_chunks``）—— 会话附件，
  Partition Key = ``session_id``。会话是短生命周期、高基数维度，若与正式库合表会全部
  落进同一个 ``kb_id`` 分桶形成热点分区。

三条设计目标
------------
1. **知识库数量无上限**：collection 数量只随「向量维度种类」增长，与知识库数量完全解耦。
   ``kb_id`` 只是一个标量值 + hash 分桶键，新建知识库零物理开销。
   （对比旧拓扑「每库一个 collection」：不仅有 65536 硬上限，更会因 vchannel 数量随库数
   线性增长而打爆 ``maxDispatcherNumPerPchannel``，本项目曾据此产生过日志风暴事故。）
2. **检索可裁剪**：所有读写都带 ``kb_id == "..."`` / ``kb_id in [...]``，Milvus 依
   Partition Key 只扫相关分区，而非全表扫描后过滤。
3. **换 embedding 模型不停机**：不同维度天然落不同 collection，新模型写新表、旧数据仍可查。

安全红线
--------
``kb_id`` 从「路由参数」变成了「过滤条件」，漏注入在读侧是跨库串数据、在写侧（删除）是
跨库误删。注入收敛在 ``_resolve()`` / ``_merge_expr()`` / ``_delete_with_expr()`` 三处，
公开方法一律经它们取 expr，不允许旁路。

删除的跨维度语义
----------------
读写按「当前维度」定位单张表，**删除则遍历该 base 下全部维度的表**。这是有意为之：
换过 embedding 模型后，同一份业务数据可能残留在旧维度表里，只删当前维度会留下永远查不到
也删不掉的孤儿向量（WeKnora 的 Milvus driver 正有此问题）。删除是低频操作，多扫几张表的
代价远小于孤儿数据带来的存储泄漏与合规风险。

三路检索
--------
- Dense（稠密向量，COSINE）
- Sparse（BGE-M3 稀疏向量，IP 内积）
- BM25（全文检索，Milvus 2.5+ 原生 Function）
"""

import asyncio
import logging
import re
import time
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    Function,
    FunctionType,
    connections,
    utility,
)

logger = logging.getLogger(__name__)

# 会话文件库的逻辑 kb_id 哨兵值：经 ``_resolve`` 路由到会话 collection。
SESSION_FILES_KB_ID = "session_files"

# 默认稠密向量维度（BGE-M3）。实际维度由 settings.embed_dim / 运行时向量长度决定。
DEFAULT_DENSE_DIM = 1024
# 兼容旧名（曾作为固定维度常量导出，milvus_event_store 等模块可能引用）
DENSE_DIM = DEFAULT_DENSE_DIM

# ID 安全字符白名单：字母数字 + 下划线 + 连字符（UUID 是其子集）。用于 expr 拼接前的
# 纵深防御校验，杜绝引号 / 空格 / 等号 / 布尔运算符等注入 Milvus 过滤表达式的字符。
_ID_SAFE_RE = re.compile(r"[0-9a-zA-Z_-]{1,64}")


def _validate_id(value: str, label: str) -> str:
    """校验 ID 是否落在白名单字符集内，返回原值。

    底层检索链路以字符串 expr 工作、无 pymilvus 模板参数（``WithTemplateParam``）通道，
    故 ID 以字符串拼接进 expr。生产中这些 ID 恒为服务端生成的 UUID 且已经过授权校验才会
    到达此处，本不可注入；本函数做**格式白名单校验**作为纵深防御：仅允许字母数字 + 下划线
    + 连字符（UUID 是其子集），出现引号 / 空格 / 等号 / 布尔运算符等任何其它字符即判非法。

    Raises:
        ValueError: value 含白名单以外的字符（潜在注入）。
    """
    if not value or not _ID_SAFE_RE.fullmatch(value):
        raise ValueError(f"非法 {label}（仅允许字母数字/下划线/连字符）: {value!r}")
    return value


def _build_id_expr(field: str, value: str, label: str) -> str:
    """构造 ``<field> == "<value>"``，value 经白名单校验。"""
    return f'{field} == "{_validate_id(value, label)}"'


def _build_in_expr(field: str, values: list[str], label: str) -> str:
    """构造 ``<field> in ["a", "b"]``，每个值经白名单校验。"""
    joined = ", ".join(f'"{_validate_id(v, label)}"' for v in values)
    return f"{field} in [{joined}]"


def build_session_id_expr(session_id: str) -> str:
    """构造会话隔离表达式 ``session_id == "<sid>"``。

    ``session_id`` 是会话 collection 的 Partition Key，故该表达式既是隔离条件、
    也是分区裁剪条件。
    """
    return _build_id_expr("session_id", session_id, "session_id")


def build_kb_id_expr(kb_id: str) -> str:
    """构造知识库隔离表达式 ``kb_id == "<kb_id>"``（单库）。"""
    return _build_id_expr("kb_id", kb_id, "kb_id")


def build_kb_ids_expr(kb_ids: list[str]) -> str:
    """构造多知识库表达式：单个退化为 ``==``，多个用 ``in [...]``。

    ``kb_id`` 是 Partition Key，``in`` 表达式同样触发分区裁剪（只扫这些 kb_id 命中的
    分桶），因此跨库检索可以一次 search 完成，不必逐库 load + search。
    """
    if not kb_ids:
        raise ValueError("kb_ids 不能为空")
    if len(kb_ids) == 1:
        return build_kb_id_expr(kb_ids[0])
    return _build_in_expr("kb_id", kb_ids, "kb_id")


def merge_exprs(*exprs: str | None) -> str | None:
    """用 ``and`` 合并标量过滤表达式，各子表达式加括号保留自身优先级。

    ``None`` / 空串被跳过；全空返回 ``None``（不过滤）。单个非空表达式原样返回
    （不多包一层括号），使日志与测试断言更直观。
    """
    parts = [e for e in exprs if e]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return " and ".join(f"({p})" for p in parts)


def collection_name(base: str, dim: int) -> str:
    """物理 collection 名 = ``<base>_<dim>``。"""
    return f"{base}_{int(dim)}"


def parse_dim(name: str, base: str) -> int | None:
    """从物理 collection 名解析维度；不严格匹配 ``<base>_<纯数字>`` 则返回 ``None``。

    严格匹配（后缀必须全是数字）是必要的：若只判前缀，当两个 base 互为前缀时
    （如 ``artoo_chunks`` 与 ``artoo_chunks_session``）会互相误判，导致删除/统计
    跨错了表。
    """
    prefix = base + "_"
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix):]
    return int(suffix) if suffix.isdigit() else None


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------

# 检索结果需要回读的标量字段（三路 search 共用，与 ``_parse_search_results`` 一一对应）。
_OUTPUT_FIELDS = [
    "chunk_id",
    "kb_id",
    "doc_id",
    "content",
    "parent_id",
    "chunk_index",
    "file_type",
    "element_type",
]


def _build_fields(partition_key: str, dim: int) -> list[FieldSchema]:
    """构造统一 schema 字段列表。

    两套 collection 共用同一套字段（只是 Partition Key 与向量维度不同），使写入 record
    形状、``_OUTPUT_FIELDS`` 与 ``_parse_search_results`` 全链路统一。

    每次调用都新建 ``FieldSchema`` 实例：``FieldSchema`` 是可变对象，模块级共享会让
    ``is_partition_key`` / ``dim`` 在两次建表间互相污染。

    Args:
        partition_key: ``"kb_id"``（正式库）或 ``"session_id"``（会话库）。
        dim: 稠密向量维度。
    """
    if partition_key not in ("kb_id", "session_id"):
        raise ValueError(f"不支持的 partition_key: {partition_key!r}")
    if dim <= 0:
        raise ValueError(f"非法向量维度: {dim!r}")
    return [
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
        # Partition Key 候选之一：正式库按 kb_id 分区裁剪。
        FieldSchema(
            name="kb_id", dtype=DataType.VARCHAR, max_length=64,
            is_partition_key=(partition_key == "kb_id"),
        ),
        # Partition Key 候选之二：会话库按 session_id 分区裁剪。
        # 正式库记录该字段恒为空串（占位，保持两表 record 形状一致）。
        FieldSchema(
            name="session_id", dtype=DataType.VARCHAR, max_length=64,
            is_partition_key=(partition_key == "session_id"),
        ),
        # 租户维度：权限判定在 PostgreSQL 侧完成（app/auth/kb_authz.py），此字段不参与
        # 鉴权，仅供运维排障、按租户统计与按租户批量清理。
        FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(
            name="content", dtype=DataType.VARCHAR, max_length=65535,
            enable_analyzer=True, analyzer_params={"type": "chinese"},
        ),
        FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=int(dim)),
        FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
        # BM25 输出字段：由 Milvus Function 自动生成，不需要手动插入
        FieldSchema(name="bm25_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
        FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_index", dtype=DataType.INT64),
        # scalar 字段，用于 pre-filter 过滤检索
        FieldSchema(name="file_type", dtype=DataType.VARCHAR, max_length=20),
        FieldSchema(name="element_type", dtype=DataType.VARCHAR, max_length=20),
    ]


def _build_bm25_function() -> Function:
    """BM25 Function：自动把 content 文本转成 BM25 稀疏向量（每次新建实例，避免复用污染）。"""
    return Function(
        name="bm25_fn",
        input_field_names=["content"],
        output_field_names=["bm25_vector"],
        function_type=FunctionType.BM25,
    )


# 除 Partition Key 外需要额外建标量索引的字段。
# Partition Key 字段由 Milvus 自身按分区裁剪，无需也不应再建普通标量索引。
_SCALAR_INDEXES = {
    "doc_id": "idx_doc_id",          # 按文档删除 / doc_id 过滤检索的主字段
    "tenant_id": "idx_tenant_id",    # 按租户统计 / 批量清理
    "file_type": "idx_file_type",
    "element_type": "idx_element_type",
}

# ------------------------------------------------------------------
# 索引配置
# ------------------------------------------------------------------

# HNSW 建索引参数默认值。efConstruction 默认 200：相较 128 可提升召回，
# 相较 400 建索引耗时不翻倍，是生产环境的性价比拐点（M=16 为通用最优）。
_DEFAULT_EF_CONSTRUCTION = 200
_DEFAULT_M = 16


def _build_dense_index_params(
    ef_construction: int = _DEFAULT_EF_CONSTRUCTION, m: int = _DEFAULT_M,
) -> dict:
    """构造稠密向量 HNSW 索引参数。

    Args:
        ef_construction: 建索引时的候选队列大小，默认 200。
        m: HNSW 图每个节点的最大边数，默认 16。
    """
    return {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "params": {"M": m, "efConstruction": ef_construction},
    }


_SPARSE_INDEX_PARAMS = {
    "index_type": "SPARSE_INVERTED_INDEX",
    "metric_type": "IP",
}
_BM25_INDEX_PARAMS = {
    "index_type": "AUTOINDEX",
    "metric_type": "BM25",
}


class MilvusClient:
    """Milvus 操作客户端。

    公开方法的第一个参数仍是 ``kb_id``（逻辑知识库 ID），但它已不是 collection 名的来源，
    而是：① 决定路由到哪套 collection（正式库 / 会话库）；② 作为 Partition Key 过滤条件。
    具体落到哪张物理表由「base + 向量维度」共同决定。
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 19530,
        alias: str = "default",
        collection: str = "artoo_chunks",
        session_collection: str = "artoo_session_chunks",
        num_partitions: int = 64,
        session_num_partitions: int = 16,
        dim: int = DEFAULT_DENSE_DIM,
        shards_num: int = 0,
        replica_number: int = 0,
    ):
        """
        Args:
            host / port / alias: Milvus 连接参数。
            collection: 正式知识库的 collection base 名（Partition Key = kb_id）。
            session_collection: 会话附件的 collection base 名（Partition Key = session_id）。
            num_partitions: 正式库的物理分区数（建表时固定）。**不是知识库数量上限**，
                无上限个 kb_id 会 hash 进这 N 个分桶；它只决定检索最少扫描比例（≈1/N）。
            session_num_partitions: 会话库的物理分区数（建表时固定）。
            dim: 当前 embedding 维度，决定读写默认落到哪张 ``<base>_<dim>`` 表。
            shards_num: 写入分片数（建表时固定）。0 = 用 Milvus 默认。
            replica_number: 读副本数（load 时生效，可随时改）。0 = 用 Milvus 默认。
        """
        self._host = host
        self._port = port
        self._alias = alias
        self._collection = collection
        self._session_collection = session_collection
        self._num_partitions = num_partitions
        self._session_num_partitions = session_num_partitions
        self._dim = dim
        self._shards_num = shards_num
        self._replica_number = replica_number
        # Collection_Load_Cache：collection name -> 上次 load 的单调时间戳。
        # 同一物理表内该缓存是**全局**的：任一知识库写入都会清掉标记，使下一次检索强制
        # 重新 load。这是「正确性优先」的取舍——load 对已加载的 collection 是廉价的幂等
        # 调用，而漏 load 会读到旧快照。
        self._loaded_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 路由 / expr 注入（全部读写方法的唯一入口，不允许旁路）
    # ------------------------------------------------------------------

    def _base_of(self, kb_id: str) -> str:
        """逻辑 kb_id -> collection base 名。"""
        return self._session_collection if kb_id == SESSION_FILES_KB_ID else self._collection

    def _topology_of(self, base: str) -> tuple[str, int]:
        """collection base -> ``(partition_key_field, num_partitions)``。"""
        if base == self._session_collection:
            return "session_id", self._session_num_partitions
        return "kb_id", self._num_partitions

    def _scope_expr(self, kb_ids: list[str]) -> str | None:
        """构造 Partition Key scope 条件。

        - 会话库：返回 ``None``。会话库的 Partition Key 是 ``session_id``，裁剪条件由
          调用方以 ``build_session_id_expr`` 传入（``delete_session`` 自行构造）。
          两套表物理隔离，故此处为空不会造成跨源串数据。
        - 正式库：返回 ``kb_id == "..."`` 或 ``kb_id in [...]``（隔离 + 分区裁剪）。
        """
        if len(kb_ids) == 1 and kb_ids[0] == SESSION_FILES_KB_ID:
            return None
        if SESSION_FILES_KB_ID in kb_ids:
            # 会话库与正式库是两张物理表，无法在一次 search 里混合检索。
            raise ValueError("会话附件源不能与正式知识库合并为一次检索（物理表不同）")
        return build_kb_ids_expr(kb_ids)

    def _resolve(
        self, kb_id: str, dim: int | None = None,
    ) -> tuple[str, str | None]:
        """把逻辑 ``kb_id`` 解析为「物理 collection 名 + Partition Key 过滤表达式」。

        Raises:
            ValueError: kb_id 为空或含非法字符。
        """
        return self._resolve_many([kb_id], dim)

    def _resolve_many(
        self, kb_ids: list[str], dim: int | None = None,
    ) -> tuple[str, str | None]:
        """多 kb_id 版本的 ``_resolve``，用于跨库单次合并检索。"""
        if not kb_ids:
            raise ValueError("kb_ids 不能为空")
        bases = {self._base_of(k) for k in kb_ids}
        if len(bases) > 1:
            raise ValueError("会话附件源不能与正式知识库合并为一次检索（物理表不同）")
        base = bases.pop()
        name = collection_name(base, dim if dim is not None else self._dim)
        return name, self._scope_expr(kb_ids)

    @staticmethod
    def _merge_expr(scope: str | None, caller: str | None) -> str | None:
        """合并「Partition Key scope 条件」与「调用方过滤条件」。

        scope 放在最左侧，便于日志排查。
        """
        return merge_exprs(scope, caller)

    # ------------------------------------------------------------------
    # 连接 / 加载
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """建立连接（如果尚未连接），支持断线重连。"""
        if not connections.has_connection(self._alias):
            connections.connect(alias=self._alias, host=self._host, port=self._port)
        else:
            # 验证连接是否仍然有效，无效则重连
            try:
                utility.list_collections(using=self._alias, timeout=5)
            except Exception:
                logger.warning("Milvus 连接失效，尝试重连...")
                try:
                    connections.disconnect(self._alias)
                except Exception:
                    pass
                connections.connect(alias=self._alias, host=self._host, port=self._port)

    def _existing_collections(self, base: str) -> list[str]:
        """列出该 base 下**已存在**的全部维度 collection（按维度升序）。

        供删除 / 统计 / 拓扑描述使用：换过 embedding 模型后同一份业务数据可能散落在多个
        维度表里，这些操作必须覆盖全部维度才不留孤儿。
        """
        names = utility.list_collections(using=self._alias)
        with_dim = [(parse_dim(n, base), n) for n in names]
        return [n for d, n in sorted((d, n) for d, n in with_dim if d is not None)]

    def _load(self, collection) -> None:
        """load collection，按配置带上读副本数。"""
        if self._replica_number > 0:
            collection.load(replica_number=self._replica_number)
        else:
            collection.load()

    def _ensure_loaded(self, name: str, collection, load_cache_ttl: int) -> None:
        """跳过重复 load。

        - ``ttl <= 0``：每次都 load（关闭优化）。
        - 标记存在且 ``now - ts < ttl``：跳过 load。
        - 否则 load 并记录当前 monotonic 时间戳。
        """
        if load_cache_ttl > 0:
            ts = self._loaded_at.get(name)
            if ts is not None and (time.monotonic() - ts) < load_cache_ttl:
                return
        self._load(collection)
        self._loaded_at[name] = time.monotonic()

    def _reload(self, name: str, collection) -> None:
        """强制重新 load 并刷新标记（not-loaded 降级重试用）。"""
        self._loaded_at.pop(name, None)
        self._load(collection)
        self._loaded_at[name] = time.monotonic()

    @staticmethod
    def _is_not_loaded_error(e: Exception) -> bool:
        """判断异常是否为 collection 未加载（"not loaded"）类错误。"""
        s = str(e).lower()
        return "not loaded" in s or "not been loaded" in s

    def invalidate_load_cache(self, kb_id: str | None = None) -> None:
        """清除加载缓存标记，使下次检索强制重新 load。

        供跨进程失效广播（InvalidationBus 的 ``kb_data`` 信号）调用。

        Args:
            kb_id: 指定知识库时清其所属 base 下**全部维度**表的标记；``None`` 清全部。
                注意共享 collection 拓扑下「清某个 KB」实际等于清该物理表的全局标记——
                这是共享表的固有取舍，宁可多 load 不可漏 load。
        """
        if kb_id is None:
            self._loaded_at.clear()
            return
        try:
            # 显式校验：非法 kb_id 说明调用方状态已不可信，保守清全部而不是只清主表，
            # 避免「以为清了、其实残留旧加载快照」这种最难查的失效遗漏。
            if kb_id != SESSION_FILES_KB_ID:
                _validate_id(kb_id, "kb_id")
            base = self._base_of(kb_id)
        except ValueError:
            self._loaded_at.clear()
            return
        for name in [n for n in self._loaded_at if parse_dim(n, base) is not None]:
            self._loaded_at.pop(name, None)

    # ------------------------------------------------------------------
    # 公开方法（均为 async，内部通过 asyncio.to_thread 调用同步 pymilvus）
    # ------------------------------------------------------------------

    async def ensure_collections(
        self, ef_construction: int | None = None, m: int | None = None,
        dim: int | None = None,
    ) -> None:
        """幂等确保当前维度的**两张**物理表都存在（启动期调用）。

        Args:
            ef_construction: HNSW 建索引 efConstruction。None 时回落默认 200。
            m: HNSW 建索引 M。None 时回落默认 16。
            dim: 目标维度。None 时用客户端默认（``settings.embed_dim``）。
        """
        d = dim if dim is not None else self._dim
        for base in (self._collection, self._session_collection):
            await asyncio.to_thread(
                self._ensure_collection_sync, collection_name(base, d), ef_construction, m, d,
            )

    async def ensure_collection(
        self, kb_id: str, ef_construction: int | None = None, m: int | None = None,
        dim: int | None = None,
    ) -> None:
        """幂等确保 ``kb_id`` 所路由到的物理表存在（写入前的懒建兜底）。"""
        name, _ = self._resolve(kb_id, dim)
        await asyncio.to_thread(
            self._ensure_collection_sync, name, ef_construction, m,
            dim if dim is not None else self._dim,
        )

    async def ensure_session_files_collection(self) -> None:
        """幂等确保会话附件表存在（``ensure_collection`` 的语义别名）。"""
        await self.ensure_collection(SESSION_FILES_KB_ID)

    async def insert(self, kb_id: str, data: list[dict]) -> int:
        """插入数据，返回插入条数。

        按每条记录 ``dense_vector`` 的**实际长度**分组落到对应维度的表，因此写入永远落在
        正确的物理表上，即使配置的 ``embed_dim`` 与模型实际输出漂移。
        同时为每条 record 兜底补齐 Partition Key 与租户字段（见 ``_stamp_records``）。
        """
        return await asyncio.to_thread(self._insert_sync, kb_id, data)

    async def search_dense(
        self, kb_id: str, vector: list[float], top_k: int = 10,
        expr: str | None = None, ef: int | None = None,
        load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """稠密向量检索（自动注入 ``kb_id`` 分区裁剪条件，维度按查询向量长度定位表）。

        Args:
            ef: HNSW 查询 ef（候选队列大小）。None 时回落默认 128。
            load_cache_ttl: 加载缓存有效期（秒）。默认 0 = 每次都 load。
        """
        return await self.search_dense_multi(
            [kb_id], vector, top_k, expr=expr, ef=ef, load_cache_ttl=load_cache_ttl,
        )

    async def search_dense_multi(
        self, kb_ids: list[str], vector: list[float], top_k: int = 10,
        expr: str | None = None, ef: int | None = None,
        load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """跨知识库稠密检索：一次 search 覆盖多个 kb_id（``kb_id in [...]``）。

        ``kb_id`` 是 Partition Key，``in`` 同样触发分区裁剪，因此这比逐库 search 少
        ``N-1`` 次网络往返与 ``N-1`` 次 load，是多库场景最主要的延迟来源优化。
        """
        return await asyncio.to_thread(
            self._search_dense_sync, kb_ids, vector, top_k, expr,
            ef if ef is not None else 128, load_cache_ttl,
        )

    async def search_sparse(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """稀疏向量检索（自动注入 ``kb_id`` 分区裁剪条件）。"""
        return await self.search_sparse_multi(
            [kb_id], sparse_vector, top_k, expr=expr, load_cache_ttl=load_cache_ttl,
        )

    async def search_sparse_multi(
        self, kb_ids: list[str], sparse_vector: dict[int, float], top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
        dim: int | None = None,
    ) -> list[dict[str, Any]]:
        """跨知识库稀疏检索：一次 search 覆盖多个 kb_id。

        稀疏向量本身无维度信息，故物理表由 ``dim``（默认客户端配置）定位——稀疏与稠密
        同表，只要与写入时的 dense 维度一致即可。
        """
        return await asyncio.to_thread(
            self._search_sparse_sync, kb_ids, sparse_vector, top_k, expr, load_cache_ttl, dim,
        )

    async def search_bm25(
        self, kb_id: str, query_text: str, top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """BM25 全文检索（Milvus 2.5+ 原生，自动注入 ``kb_id`` 分区裁剪条件）。"""
        return await self.search_bm25_multi(
            [kb_id], query_text, top_k, expr=expr, load_cache_ttl=load_cache_ttl,
        )

    async def search_bm25_multi(
        self, kb_ids: list[str], query_text: str, top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
        dim: int | None = None,
    ) -> list[dict[str, Any]]:
        """跨知识库 BM25 检索：一次 search 覆盖多个 kb_id。

        表不存在或任何异常都降级为空列表——BM25 是「有则用之」的增强路，不应打断
        dense/sparse 两路。
        """
        return await asyncio.to_thread(
            self._search_bm25_sync, kb_ids, query_text, top_k, expr, load_cache_ttl, dim,
        )

    async def delete(self, kb_id: str, chunk_ids: list[str]) -> None:
        """按 chunk_id 列表删除（限定在该 kb_id 范围内，跨全部维度表执行）。"""
        if not chunk_ids:
            return
        await asyncio.to_thread(self._delete_sync, kb_id, chunk_ids)

    async def delete_by_doc_id(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 删除该文档的所有向量（限定在该 kb_id 范围内，跨全部维度表执行）。"""
        await asyncio.to_thread(self._delete_by_doc_id_sync, kb_id, doc_id)

    async def delete_by_doc_ids(self, kb_id: str, doc_ids: list[str]) -> None:
        """批量按 doc_id 删除（限定在该 kb_id 范围内，跨全部维度表执行）。"""
        if not doc_ids:
            return
        await asyncio.to_thread(self._delete_by_doc_ids_sync, kb_id, doc_ids)

    async def delete_by_kb(self, kb_id: str) -> None:
        """删除某个知识库的**全部**向量（删库场景，跨全部维度表执行）。

        取代旧拓扑的 ``drop_collection(kb_id)``：共享 collection 下 drop 物理表会清掉
        所有知识库，因此删库改为按 Partition Key ``kb_id == "..."`` 删除。
        """
        await asyncio.to_thread(self._delete_by_kb_sync, kb_id)

    async def delete_by_tenant(self, tenant_id: str) -> None:
        """删除某租户的全部向量（SaaS 退租 / 数据清除，跨全部维度表 + 两套 base 执行）。

        ``tenant_id`` 不是 Partition Key（有标量索引），因此这是一次全表条件删除，
        属低频运维操作。
        """
        await asyncio.to_thread(self._delete_by_tenant_sync, tenant_id)

    async def delete_session(self, session_id: str) -> None:
        """按 session_id 删除会话表中该会话的全部向量（跨全部维度表执行）。

        ``session_id`` 是会话表的 Partition Key，该删除会被分区裁剪。
        """
        await asyncio.to_thread(self._delete_session_sync, session_id)

    async def has_collection(self, kb_id: str, dim: int | None = None) -> bool:
        """检查 ``kb_id`` 当前维度的**物理表**是否存在。

        注意语义：这不表示「该知识库有数据」，而是「承载该知识库的物理表是否已建」。
        调用方把它当作「跳过清理」的守卫仍然正确（未建表则无可删），但不可用它判断某个
        知识库是否为空——需要计数请用 ``count``。
        """
        return await asyncio.to_thread(self._has_collection_sync, kb_id, dim)

    async def count(self, kb_id: str, expr: str | None = None) -> int:
        """统计某知识库（可叠加 expr）的向量条数，跨全部维度表累加。"""
        return await asyncio.to_thread(self._count_sync, kb_id, expr)

    async def describe(self) -> dict:
        """返回两套 base 下全部维度表的拓扑描述（供健康检查 / 运维排障使用）。"""
        return await asyncio.to_thread(self._describe_sync)

    async def drop_all_collections(self) -> list[str]:
        """删除本客户端管理的两套 base 下**全部维度**的物理表，返回被删除的名字列表。

        **破坏性操作**：清空全部知识库与会话附件向量。仅供初始化 / 重置脚本调用，
        业务代码不得使用（删单个知识库请用 ``delete_by_kb``）。
        """
        return await asyncio.to_thread(self._drop_all_collections_sync)

    async def drop_other_dims(self, keep_dim: int | None = None) -> list[str]:
        """删除除 ``keep_dim`` 外的其它维度表（换模型且确认旧数据无用后清理）。"""
        return await asyncio.to_thread(
            self._drop_other_dims_sync, keep_dim if keep_dim is not None else self._dim,
        )

    # ------------------------------------------------------------------
    # 同步实现
    # ------------------------------------------------------------------

    def _ensure_collection_sync(
        self, name: str, ef_construction: int | None = None, m: int | None = None,
        dim: int | None = None,
    ) -> None:
        """同步创建指定物理表 + 索引（幂等：已存在则跳过）。"""
        self._connect()

        if utility.has_collection(name, using=self._alias):
            logger.debug("Collection %s 已存在，跳过创建", name)
            return

        ec = ef_construction if ef_construction is not None else _DEFAULT_EF_CONSTRUCTION
        mm = m if m is not None else _DEFAULT_M
        # 由表名反解 base 与维度，保证 schema 与表名恒一致。
        # 按 base 名长度降序尝试，避免两个 base 互为前缀时误判。
        base, parsed_dim = None, None
        for candidate in sorted(
            (self._collection, self._session_collection), key=len, reverse=True,
        ):
            pd = parse_dim(name, candidate)
            if pd is not None:
                base, parsed_dim = candidate, pd
                break
        if base is None:
            raise ValueError(f"collection 名 {name!r} 不属于本客户端管理的任何 base")
        d = dim if dim is not None else parsed_dim
        partition_key, num_partitions = self._topology_of(base)

        schema = CollectionSchema(
            fields=_build_fields(partition_key, d),
            description=f"Artoo 向量集合（Partition Key={partition_key}, dim={d}）",
            functions=[_build_bm25_function()],
        )
        create_kwargs: dict[str, Any] = {
            "name": name,
            "schema": schema,
            "using": self._alias,
            "num_partitions": num_partitions,
        }
        if self._shards_num > 0:
            create_kwargs["num_shards"] = self._shards_num
        collection = Collection(**create_kwargs)

        # 稠密向量索引（HNSW + COSINE）
        collection.create_index(
            field_name="dense_vector",
            index_params=_build_dense_index_params(ec, mm),
        )
        # 稀疏向量索引（BGE-M3 sparse，IP 内积）
        collection.create_index(
            field_name="sparse_vector", index_params=_SPARSE_INDEX_PARAMS,
        )
        # BM25 全文检索索引
        collection.create_index(
            field_name="bm25_vector", index_params=_BM25_INDEX_PARAMS,
        )
        # 标量索引（Partition Key 字段由分区裁剪覆盖，不重复建）
        for field_name, index_name in _SCALAR_INDEXES.items():
            collection.create_index(field_name=field_name, index_name=index_name)

        logger.info(
            "Collection %s 创建完成（Partition Key=%s, dim=%d, num_partitions=%d, "
            "num_shards=%s, HNSW M=%d efConstruction=%d）",
            name, partition_key, d, num_partitions,
            self._shards_num or "default", mm, ec,
        )

    @staticmethod
    def _truncate_to_byte_limit(text: str, max_bytes: int = 60000) -> str:
        """按 UTF-8 字节数截断字符串，确保不超过 Milvus VarChar 字节限制。

        Milvus 的 max_length 实际按 UTF-8 字节数计算，中文字符占 3 字节，
        因此不能简单按 Python 字符数截断。
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        # 截断字节后解码，errors='ignore' 避免截断到多字节字符中间
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _stamp_records(self, kb_id: str, base: str, data: list[dict]) -> None:
        """就地补齐每条 record 的 Partition Key 与租户字段，并截断 content。

        共享 collection 拓扑下 ``kb_id`` / ``session_id`` 从「路由参数」变成了「数据字段」，
        漏填会写出检索不到、也删不掉的孤儿向量。此处统一兜底：

        - ``kb_id``：正式库填传入的 kb_id；会话库填 ``SESSION_FILES_KB_ID`` 哨兵。
          强制盖写（而非 setdefault），使生产者写错也不会落到别的分区。
        - ``session_id``：正式库补空串占位；会话库必须由生产者提供（缺失即抛错，
          因为它是会话库的 Partition Key，补空串会让该记录永远检索不到）。
        - ``tenant_id``：缺失补空串（不参与鉴权，仅供排障 / 统计 / 按租户清理）。

        Raises:
            ValueError: 写会话表但 record 缺 ``session_id``。
        """
        is_session = base == self._session_collection
        stamped_kb_id = SESSION_FILES_KB_ID if is_session else kb_id
        for record in data:
            if "content" in record:
                record["content"] = self._truncate_to_byte_limit(record["content"], 60000)
            record["kb_id"] = stamped_kb_id
            if is_session:
                if not record.get("session_id"):
                    raise ValueError("写入会话向量集合时 record 缺少 session_id（Partition Key）")
            else:
                record.setdefault("session_id", "")
            record.setdefault("tenant_id", "")

    def _insert_sync(self, kb_id: str, data: list[dict]) -> int:
        """同步插入，按向量实际维度分组落表，带轻量重试。"""
        if not data:
            return 0
        self._connect()
        base = self._base_of(kb_id)
        self._stamp_records(kb_id, base, data)

        # 按 dense_vector 实际长度分组：写入永远落在与向量维度匹配的表上，
        # 即使 settings.embed_dim 与模型实际输出漂移也不会写坏 schema。
        by_dim: dict[int, list[dict]] = {}
        for record in data:
            vec = record.get("dense_vector")
            d = len(vec) if vec is not None else self._dim
            if d <= 0:
                raise ValueError(f"record {record.get('chunk_id')!r} 的 dense_vector 为空")
            by_dim.setdefault(d, []).append(record)

        total = 0
        for d, rows in by_dim.items():
            name = collection_name(base, d)
            # 懒建：换模型后首批数据会自动建出新维度表，无需人工干预
            self._ensure_collection_sync(name, dim=d)
            total += self._insert_rows_sync(name, rows)
        return total

    def _insert_rows_sync(self, name: str, rows: list[dict]) -> int:
        """向单张物理表写入，带轻量重试（避免网络瞬断浪费 embedding 计算）。"""
        collection = Collection(name=name, using=self._alias)
        last_error = None
        for attempt in range(3):
            try:
                result = collection.insert(rows)
                # 注意：此处**不调用 flush()**。flush 会强制把增量数据封成 sealed segment
                # 落盘，是重操作（实测单次可达十余秒），在 ARM64 / 高并发 / 资源受限环境下
                # 可能长时间阻塞甚至拖垮进程，导致上层 DB 事务挂成 idle-in-transaction、
                # 上传永久卡住。Milvus 会按自身策略自动 flush；检索可见性由 search 前的
                # load（见 _ensure_loaded + 下方 _loaded_at 失效）保证。
                self._loaded_at.pop(name, None)
                return result.insert_count
            except Exception as e:
                last_error = e
                error_str = str(e)
                # 数据层面的错误（如字段超长）不重试，直接抛出
                if "invalid parameter" in error_str or "type mismatch" in error_str:
                    raise
                if attempt < 2:
                    logger.warning(
                        "Milvus insert 到 %s 失败 (attempt %d/3): %s，重试中...",
                        name, attempt + 1, e,
                    )
                    time.sleep(1)
                    self._connect()
                    collection = Collection(name=name, using=self._alias)
        raise last_error

    def _search_sync(
        self, kb_ids: list[str], search_kwargs: dict[str, Any],
        expr: str | None, load_cache_ttl: int, dim: int | None,
    ) -> list[dict[str, Any]]:
        """三路 search 的共同骨架：路由 → 注入 scope expr → load → search → 降级重试。

        把 collection 解析、expr 注入、not-loaded 降级重试收敛到一处，避免三路各写一遍
        导致某一路漏注入 ``kb_id`` 条件（跨库串数据）。
        """
        self._connect()
        name, scope = self._resolve_many(kb_ids, dim)

        if not utility.has_collection(name, using=self._alias):
            logger.debug("Collection %s 不存在，检索返回空", name)
            return []

        collection = Collection(name=name, using=self._alias)
        self._ensure_loaded(name, collection, load_cache_ttl)

        effective_expr = self._merge_expr(scope, expr)
        if effective_expr is not None:
            search_kwargs["expr"] = effective_expr
        search_kwargs["output_fields"] = list(_OUTPUT_FIELDS)

        try:
            results = collection.search(**search_kwargs)
        except Exception as e:  # not-loaded 降级重试
            if self._is_not_loaded_error(e):
                self._reload(name, collection)
                results = collection.search(**search_kwargs)
            else:
                raise

        return self._parse_search_results(results)

    def _search_dense_sync(
        self, kb_ids: list[str], vector: list[float], top_k: int,
        expr: str | None = None, ef: int = 128, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """同步稠密检索。维度直接取查询向量长度——最可靠的定位方式。"""
        ef_val = ef if ef is not None else 128
        return self._search_sync(
            kb_ids,
            {
                "data": [vector],
                "anns_field": "dense_vector",
                "param": {"metric_type": "COSINE", "params": {"ef": ef_val}},
                "limit": top_k,
            },
            expr, load_cache_ttl, dim=len(vector) or None,
        )

    def _search_sparse_sync(
        self, kb_ids: list[str], sparse_vector: dict[int, float], top_k: int,
        expr: str | None = None, load_cache_ttl: int = 0, dim: int | None = None,
    ) -> list[dict[str, Any]]:
        """同步稀疏检索。"""
        return self._search_sync(
            kb_ids,
            {
                "data": [sparse_vector],
                "anns_field": "sparse_vector",
                "param": {"metric_type": "IP"},
                "limit": top_k,
            },
            expr, load_cache_ttl, dim,
        )

    def _search_bm25_sync(
        self, kb_ids: list[str], query_text: str, top_k: int,
        expr: str | None = None, load_cache_ttl: int = 0, dim: int | None = None,
    ) -> list[dict[str, Any]]:
        """同步 BM25 检索。任何异常降级为空，不打断三路混合检索。"""
        try:
            return self._search_sync(
                kb_ids,
                {
                    "data": [query_text],
                    "anns_field": "bm25_vector",
                    "param": {"metric_type": "BM25"},
                    "limit": top_k,
                },
                expr, load_cache_ttl, dim,
            )
        except Exception as e:
            logger.warning("BM25 检索失败，降级为空结果: %s", e)
            return []

    def _delete_across_dims(
        self, base: str, scope: str | None, exprs: list[str], *, what: str,
    ) -> None:
        """在 base 下**全部维度**表上按表达式删除。

        跨维度是有意为之：换过 embedding 模型后同一份业务数据可能残留在旧维度表里，
        只删当前维度会留下永远查不到也删不掉的孤儿向量。删除是低频操作，多扫几张表的
        代价远小于存储泄漏与合规风险。
        """
        self._connect()
        names = self._existing_collections(base)
        if not names:
            return

        # 先算出最终表达式：任何一条为空都说明「无条件删除整表」，必须在触碰 Milvus 前拒绝
        effective_exprs = []
        for caller_expr in exprs:
            effective = self._merge_expr(scope, caller_expr)
            if effective is None:
                raise ValueError(f"拒绝执行无过滤条件的删除（{what}）")
            effective_exprs.append(effective)

        for name in names:
            collection = Collection(name=name, using=self._alias)
            # delete 需要 collection 处于 loaded 状态；失败忽略（可能已 loaded）
            try:
                self._load(collection)
            except Exception:
                pass
            for effective in effective_exprs:
                collection.delete(effective)
            # 不 flush（同写入侧的理由）：delete 经 delete buffer 即时生效，
            # 可见性由下次 load（由 _loaded_at 失效强制触发）保证。
            self._loaded_at.pop(name, None)

        logger.info("Collection %s(全部维度: %s) %s", base, ",".join(names), what)

    def _delete_with_expr(
        self, kb_id: str, exprs: list[str], *, what: str,
    ) -> None:
        """按 kb_id 路由后跨维度删除（统一注入 scope 条件）。"""
        base = self._base_of(kb_id)
        scope = self._scope_expr([kb_id])
        self._delete_across_dims(base, scope, exprs, what=what)

    @staticmethod
    def _batched(values: list[str], size: int = 100) -> list[list[str]]:
        return [values[i:i + size] for i in range(0, len(values), size)]

    def _delete_sync(self, kb_id: str, chunk_ids: list[str]) -> None:
        """按 chunk_id 删除（限定 kb_id 范围）。"""
        exprs = [
            _build_in_expr("chunk_id", batch, "chunk_id")
            for batch in self._batched(chunk_ids)
        ]
        self._delete_with_expr(
            kb_id, exprs, what=f"删除 {len(chunk_ids)} 条记录（kb={kb_id}）",
        )

    def _delete_by_doc_id_sync(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 删除该文档的所有向量（限定 kb_id 范围）。"""
        self._delete_with_expr(
            kb_id, [_build_id_expr("doc_id", doc_id, "doc_id")],
            what=f"按 doc_id={doc_id} 删除向量（kb={kb_id}）",
        )

    def _delete_by_doc_ids_sync(self, kb_id: str, doc_ids: list[str]) -> None:
        """批量按 doc_id 删除（限定 kb_id 范围）。"""
        exprs = [
            _build_in_expr("doc_id", batch, "doc_id")
            for batch in self._batched(doc_ids)
        ]
        self._delete_with_expr(
            kb_id, exprs, what=f"批量删除 {len(doc_ids)} 个文档的向量（kb={kb_id}）",
        )

    def _delete_by_kb_sync(self, kb_id: str) -> None:
        """删除某知识库的全部向量。

        scope 条件本身（``kb_id == "..."``）就是全部删除范围，故调用方条件传空串占位，
        由 ``_merge_expr`` 归约成仅 scope 一条。
        """
        if kb_id == SESSION_FILES_KB_ID:
            # 会话库没有「按 kb_id 删」的语义（其 Partition Key 是 session_id），
            # 误用会退化成清空整个会话集合，直接拒绝。
            raise ValueError("delete_by_kb 不适用于会话向量集合，请使用 delete_session")
        self._delete_with_expr(kb_id, [""], what=f"删除知识库 {kb_id} 的全部向量")

    def _delete_by_tenant_sync(self, tenant_id: str) -> None:
        """删除某租户的全部向量（两套 base × 全部维度）。"""
        expr = _build_id_expr("tenant_id", tenant_id, "tenant_id")
        for base in (self._collection, self._session_collection):
            self._delete_across_dims(
                base, None, [expr], what=f"删除租户 {tenant_id} 的全部向量",
            )

    def _delete_session_sync(self, session_id: str) -> None:
        """按 session_id 删除会话表中该会话的全部向量。"""
        self._delete_with_expr(
            SESSION_FILES_KB_ID, [build_session_id_expr(session_id)],
            what=f"按 session_id={session_id} 删除向量",
        )

    def _has_collection_sync(self, kb_id: str, dim: int | None = None) -> bool:
        """同步检查物理表是否存在。"""
        self._connect()
        name, _ = self._resolve(kb_id, dim)
        return utility.has_collection(name, using=self._alias)

    def _count_sync(self, kb_id: str, expr: str | None = None) -> int:
        """同步统计条数，跨该 base 下全部维度表累加。"""
        self._connect()
        base = self._base_of(kb_id)
        scope = self._scope_expr([kb_id])
        effective = self._merge_expr(scope, expr)

        total = 0
        for name in self._existing_collections(base):
            collection = Collection(name=name, using=self._alias)
            try:
                self._load(collection)
            except Exception:
                pass
            rows = collection.query(
                expr=effective if effective is not None else "",
                output_fields=["count(*)"],
            )
            if rows:
                total += int(rows[0].get("count(*)", 0))
        return total

    def _describe_sync(self) -> dict:
        """同步返回两套 base 下全部维度表的拓扑描述。"""
        self._connect()
        out: dict[str, Any] = {}
        for base in (self._collection, self._session_collection):
            partition_key, num_partitions = self._topology_of(base)
            names = self._existing_collections(base)
            entry: dict[str, Any] = {
                "base": base,
                "partition_key": partition_key,
                "configured_num_partitions": num_partitions,
                "configured_dim": self._dim,
                "collections": [],
            }
            for name in names:
                collection = Collection(name=name, using=self._alias)
                entry["collections"].append({
                    "name": name,
                    "dim": parse_dim(name, base),
                    "num_partitions": len(collection.partitions),
                    "num_shards": getattr(collection, "num_shards", None),
                    "field_names": [f.name for f in collection.schema.fields],
                })
            out[base] = entry
        return out

    def _drop_all_collections_sync(self) -> list[str]:
        """同步删除两套 base 下全部维度表（破坏性，仅重置脚本用）。"""
        self._connect()
        dropped: list[str] = []
        for base in (self._collection, self._session_collection):
            for name in self._existing_collections(base):
                utility.drop_collection(name, using=self._alias)
                self._loaded_at.pop(name, None)
                dropped.append(name)
                logger.warning("Collection %s 已删除（全量重置）", name)
        return dropped

    def _drop_other_dims_sync(self, keep_dim: int) -> list[str]:
        """同步删除除 keep_dim 外的其它维度表。"""
        self._connect()
        dropped: list[str] = []
        for base in (self._collection, self._session_collection):
            for name in self._existing_collections(base):
                if parse_dim(name, base) == keep_dim:
                    continue
                utility.drop_collection(name, using=self._alias)
                self._loaded_at.pop(name, None)
                dropped.append(name)
                logger.warning("Collection %s 已删除（清理旧维度）", name)
        return dropped

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_search_results(results) -> list[dict[str, Any]]:
        """将 pymilvus 搜索结果转换为字典列表（键与 ``_OUTPUT_FIELDS`` 对应 + score）。"""
        hits = []
        for result in results:
            for hit in result:
                item = {field: hit.entity.get(field) for field in _OUTPUT_FIELDS}
                item["score"] = hit.score
                hits.append(item)
        return hits


# ------------------------------------------------------------------
# 进程内单例
# ------------------------------------------------------------------

_client: "MilvusClient | None" = None


def get_milvus_client() -> "MilvusClient":
    """进程内 MilvusClient 单例。

    连接参数与拓扑配置均取 ``get_settings()``。多次调用返回同一实例，使 collection
    加载标记跨请求存活、Milvus 连接复用。API 进程与 Worker 进程各持有独立实例。
    """
    global _client
    if _client is None:
        from app.config import get_settings
        s = get_settings()
        _client = MilvusClient(
            host=s.milvus_host,
            port=s.milvus_port,
            collection=s.milvus_collection,
            session_collection=s.milvus_session_collection,
            num_partitions=s.milvus_num_partitions,
            session_num_partitions=s.milvus_session_num_partitions,
            dim=s.embed_dim,
            shards_num=s.milvus_shards_num,
            replica_number=s.milvus_replica_number,
        )
    return _client
