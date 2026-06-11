"""Milvus 向量数据库操作封装

支持三路检索：
- Dense（稠密向量，COSINE 相似度）
- Sparse（BGE-M3 稀疏向量，IP 内积）
- BM25（全文检索，Milvus 2.5+ 原生支持）
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

# 会话文件库逻辑 kb_id：经 _collection_name 解析为物理 collection "kb_session_files"。
# 所有会话上传文件共用这一个常驻 collection，各会话靠 session_id 标量字段过滤隔离
# （参考 WeKnora 的"共享 collection + 标量过滤"模式）。
SESSION_FILES_KB_ID = "session_files"


# session_id 安全字符白名单：字母数字 + 连字符（UUID 是其子集）。用于 expr 拼接前的
# 纵深防御校验，杜绝引号 / 空格 / 等号 / 布尔运算符等注入 Milvus 过滤表达式的字符。
_SESSION_ID_SAFE_RE = re.compile(r"[0-9a-zA-Z_-]{1,64}")


def build_session_id_expr(session_id: str) -> str:
    """构造会话隔离用的 Milvus 标量过滤表达式 ``session_id == "<sid>"``（纵深防御）。

    底层检索链路（HybridRetriever / MilvusClient）以字符串 expr 工作、无 pymilvus
    模板参数（``WithTemplateParam``）通道，故 session_id 以字符串拼接进 expr。生产中
    session_id 恒为服务端生成的 UUID 且经 ``_verify_session_owner`` 校验后才会到达此处，
    本不可注入；本函数对其做**格式白名单校验**作为纵深防御（对齐 WeKnora 参数化的安全
    意图）：仅允许字母数字 + 下划线 + 连字符（UUID 是其子集），出现引号 / 空格 / 等号 /
    布尔运算符等任何其它字符即判为非法输入抛 ``ValueError``，杜绝注入 expr 的可能。

    Args:
        session_id: 会话 ID（期望为 UUID 字符串）。

    Returns:
        形如 ``session_id == "xxxxxxxx-...."`` 的过滤表达式。

    Raises:
        ValueError: session_id 含 UUID 字符集以外的字符（潜在注入）。
    """
    if not session_id or not _SESSION_ID_SAFE_RE.fullmatch(session_id):
        raise ValueError(f"非法 session_id（仅允许字母数字/下划线/连字符）: {session_id!r}")
    return f'session_id == "{session_id}"'

# Collection 字段定义（v2 schema，支持 BM25 全文检索）
_FIELDS = [
    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
    FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535,
               enable_analyzer=True,
               analyzer_params={"type": "chinese"}),
    FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=1024),
    FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
    # BM25 输出字段：由 Milvus Function 自动生成，不需要手动插入
    FieldSchema(name="bm25_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
    FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="chunk_index", dtype=DataType.INT64),
    # scalar 字段，用于 pre-filter 过滤检索
    FieldSchema(name="file_type", dtype=DataType.VARCHAR, max_length=20),
    FieldSchema(name="element_type", dtype=DataType.VARCHAR, max_length=20),
]

# 会话文件库专用 schema：在 _FIELDS 基础上追加 session_id 标量字段，用于会话级隔离过滤。
# 正式知识库 collection 仍用 _FIELDS（不含 session_id），互不影响。
_SESSION_FIELDS = _FIELDS + [
    FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=64),
]

# BM25 Function：自动将 content 文本转换为 BM25 稀疏向量
_BM25_FUNCTION = Function(
    name="bm25_fn",
    input_field_names=["content"],
    output_field_names=["bm25_vector"],
    function_type=FunctionType.BM25,
)

# 索引配置
# HNSW 建索引参数默认值（efConstruction 默认 128 对齐主流，M 默认 16）。
# 仅在新建 / 主动重建 collection 时按当前配置值生效，不为存量 collection 触发重建。
_DEFAULT_EF_CONSTRUCTION = 128
_DEFAULT_M = 16


def _build_dense_index_params(
    ef_construction: int = _DEFAULT_EF_CONSTRUCTION, m: int = _DEFAULT_M,
) -> dict:
    """构造稠密向量 HNSW 索引参数。

    Args:
        ef_construction: HNSW 建索引时的 efConstruction（候选队列大小），默认 128。
        m: HNSW 图每个节点的最大边数 M，默认 16。

    Returns:
        pymilvus ``create_index`` 所需的 dense 索引参数 dict。
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
    """Milvus 操作客户端，封装 collection 的增删查操作"""

    def __init__(self, host: str = "localhost", port: int = 19530, alias: str = "default"):
        self._host = host
        self._port = port
        self._alias = alias
        # Collection_Load_Cache（Req 15）：collection name -> 上次 load 的单调时间戳
        self._loaded_at: dict[str, float] = {}

    def _ensure_loaded(self, name: str, collection, load_cache_ttl: int) -> None:
        """跳过重复 load（Req 15）。

        - ``ttl <= 0``：每次都 load（关闭优化，Req 15.5）。
        - 标记存在且 ``now - ts < ttl``：跳过 load（Req 15.1）。
        - 否则 ``collection.load()`` 并记录当前 monotonic 时间戳（Req 15.2/15.3）。

        Args:
            name: collection 名称（作为 Collection_Load_Cache 的键）。
            collection: pymilvus Collection 实例。
            load_cache_ttl: 加载缓存有效期（秒）。<=0 表示每次都 load。
        """
        if load_cache_ttl > 0:
            ts = self._loaded_at.get(name)
            if ts is not None and (time.monotonic() - ts) < load_cache_ttl:
                return
        collection.load()
        self._loaded_at[name] = time.monotonic()

    @staticmethod
    def _is_not_loaded_error(e: Exception) -> bool:
        """判断异常是否为 collection 未加载（"not loaded"）类错误（Req 16）。"""
        s = str(e).lower()
        return "not loaded" in s or "not been loaded" in s

    def _connect(self) -> None:
        """建立连接（如果尚未连接），支持断线重连"""
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

    @staticmethod
    def _collection_name(kb_id: str) -> str:
        """根据知识库 ID 生成 collection 名称（替换连字符为下划线）"""
        return f"kb_{kb_id.replace('-', '_')}"

    # ------------------------------------------------------------------
    # 公开方法（均为 async，内部通过 asyncio.to_thread 调用同步 pymilvus）
    # ------------------------------------------------------------------

    async def create_collection(
        self, kb_id: str,
        ef_construction: int | None = None, m: int | None = None,
    ) -> None:
        """创建 collection 并建立索引

        Args:
            ef_construction: HNSW 建索引 efConstruction。None 时回落默认 128，
                保证未透传配置的旧调用点行为不变。
            m: HNSW 建索引 M。None 时回落默认 16。
        """
        await asyncio.to_thread(self._create_collection_sync, kb_id, ef_construction, m)

    async def ensure_session_files_collection(self) -> None:
        """确保会话文件共享 collection（kb_session_files）存在。

        用 ``_SESSION_FIELDS``（含 session_id 标量字段）建表 + 建索引，幂等：
        collection 已存在则跳过。所有会话上传文件共用这一个常驻 collection，
        各会话靠 session_id 标量过滤隔离。
        """
        await asyncio.to_thread(self._ensure_session_files_collection_sync)

    async def insert(self, kb_id: str, data: list[dict]) -> int:
        """插入数据，返回插入条数"""
        return await asyncio.to_thread(self._insert_sync, kb_id, data)

    async def search_dense(
        self, kb_id: str, vector: list[float], top_k: int = 10,
        expr: str | None = None, ef: int | None = None,
        load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """稠密向量相似度搜索

        Args:
            ef: HNSW 查询时的 ef 参数（候选队列大小）。None 时回落默认 128，
                保证未透传配置的旧调用点行为不变。
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 关闭跳过优化（每次都 load），保证旧调用点行为不变。
        """
        return await asyncio.to_thread(
            self._search_dense_sync, kb_id, vector, top_k, expr,
            ef if ef is not None else 128, load_cache_ttl,
        )

    async def search_sparse(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """稀疏向量搜索

        Args:
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 关闭跳过优化（每次都 load），保证旧调用点行为不变。
        """
        return await asyncio.to_thread(
            self._search_sparse_sync, kb_id, sparse_vector, top_k, expr, load_cache_ttl,
        )

    async def search_bm25(
        self, kb_id: str, query_text: str, top_k: int = 10,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """BM25 全文检索（Milvus 2.5+ 原生支持）

        直接传入文本查询，Milvus 内部自动分词并计算 BM25 分数。
        对于旧 schema（无 bm25_vector 字段）的 collection，返回空列表。

        Args:
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 关闭跳过优化（每次都 load），保证旧调用点行为不变。
        """
        return await asyncio.to_thread(
            self._search_bm25_sync, kb_id, query_text, top_k, expr, load_cache_ttl,
        )

    async def delete(self, kb_id: str, chunk_ids: list[str]) -> None:
        """按 chunk_id 列表删除数据"""
        await asyncio.to_thread(self._delete_sync, kb_id, chunk_ids)

    async def delete_by_doc_id(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 删除该文档的所有向量（用于取消/清理孤儿数据）"""
        await asyncio.to_thread(self._delete_by_doc_id_sync, kb_id, doc_id)

    async def delete_by_doc_ids(self, kb_id: str, doc_ids: list[str]) -> None:
        """批量按 doc_id 列表删除向量（合并表达式，只 load/flush 一次）"""
        if not doc_ids:
            return
        await asyncio.to_thread(self._delete_by_doc_ids_sync, kb_id, doc_ids)

    async def delete_session(self, session_id: str) -> None:
        """按 session_id 标量删除会话文件库中该会话的全部向量（删会话级联清理用）。

        作用于共享 collection ``kb_session_files``，expr 为
        ``session_id == "{session_id}"``，仅影响该会话的向量，其余会话不受影响。
        """
        await asyncio.to_thread(self._delete_session_sync, session_id)

    async def drop_collection(self, kb_id: str) -> None:
        """删除整个 collection"""
        await asyncio.to_thread(self._drop_collection_sync, kb_id)

    async def has_collection(self, kb_id: str) -> bool:
        """检查 collection 是否存在"""
        return await asyncio.to_thread(self._has_collection_sync, kb_id)

    async def check_schema_version(self, kb_id: str) -> dict:
        """检查 collection 的 schema 版本

        Returns:
            dict with keys:
            - exists: bool - collection 是否存在
            - has_new_fields: bool - 是否包含 file_type/element_type 字段
            - field_names: list[str] - 当前所有字段名
        """
        return await asyncio.to_thread(self._check_schema_version_sync, kb_id)

    # ------------------------------------------------------------------
    # 同步实现
    # ------------------------------------------------------------------

    def _create_collection_sync(
        self, kb_id: str,
        ef_construction: int | None = None, m: int | None = None,
    ) -> None:
        """同步创建 collection + 索引（v2 schema，含 BM25 全文检索）

        Args:
            ef_construction: HNSW 建索引 efConstruction。None 时回落默认 128。
            m: HNSW 建索引 M。None 时回落默认 16。
        """
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            logger.info("Collection %s 已存在，跳过创建", name)
            return

        ec = ef_construction if ef_construction is not None else _DEFAULT_EF_CONSTRUCTION
        mm = m if m is not None else _DEFAULT_M

        schema = CollectionSchema(
            fields=_FIELDS,
            description=f"知识库 {kb_id} 的向量集合",
            functions=[_BM25_FUNCTION],
        )
        collection = Collection(name=name, schema=schema, using=self._alias)

        # 创建稠密向量索引
        collection.create_index(
            field_name="dense_vector",
            index_params=_build_dense_index_params(ec, mm),
        )
        # 创建稀疏向量索引（BGE-M3 sparse）
        collection.create_index(
            field_name="sparse_vector",
            index_params=_SPARSE_INDEX_PARAMS,
        )
        # 创建 BM25 全文检索索引
        collection.create_index(
            field_name="bm25_vector",
            index_params=_BM25_INDEX_PARAMS,
        )
        # 创建 scalar 索引，用于 pre-filter 过滤检索
        collection.create_index(
            field_name="file_type",
            index_name="idx_file_type",
        )
        collection.create_index(
            field_name="element_type",
            index_name="idx_element_type",
        )

        logger.info("Collection %s 创建完成（v2 schema，含 BM25）", name)

    def _ensure_session_files_collection_sync(self) -> None:
        """同步确保会话文件共享 collection 存在（_SESSION_FIELDS + BM25，幂等）。

        与 ``_create_collection_sync`` 同构，但使用含 session_id 标量字段的
        ``_SESSION_FIELDS`` 建表，并为 session_id 建标量索引以加速会话级 expr 过滤。
        """
        self._connect()
        name = self._collection_name(SESSION_FILES_KB_ID)

        if utility.has_collection(name, using=self._alias):
            logger.info("会话文件 Collection %s 已存在，跳过创建", name)
            return

        schema = CollectionSchema(
            fields=_SESSION_FIELDS,
            description="会话级文件共享向量集合（各会话靠 session_id 标量过滤隔离）",
            functions=[_BM25_FUNCTION],
        )
        collection = Collection(name=name, schema=schema, using=self._alias)

        # 创建稠密向量索引
        collection.create_index(
            field_name="dense_vector",
            index_params=_build_dense_index_params(),
        )
        # 创建稀疏向量索引（BGE-M3 sparse）
        collection.create_index(
            field_name="sparse_vector",
            index_params=_SPARSE_INDEX_PARAMS,
        )
        # 创建 BM25 全文检索索引
        collection.create_index(
            field_name="bm25_vector",
            index_params=_BM25_INDEX_PARAMS,
        )
        # 创建 scalar 索引，用于 pre-filter 过滤检索
        collection.create_index(
            field_name="file_type",
            index_name="idx_file_type",
        )
        collection.create_index(
            field_name="element_type",
            index_name="idx_element_type",
        )
        # session_id 标量索引：会话级隔离过滤的主要字段
        collection.create_index(
            field_name="session_id",
            index_name="idx_session_id",
        )

        logger.info("会话文件 Collection %s 创建完成（含 session_id 标量字段）", name)

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

    def _insert_sync(self, kb_id: str, data: list[dict]) -> int:
        """同步插入数据，带轻量重试（避免网络瞬断浪费 embedding 计算）"""
        self._connect()
        name = self._collection_name(kb_id)
        # 截断 content 字段，按 UTF-8 字节数限制，防止超过 Milvus max_length（字节）限制
        for record in data:
            if "content" in record:
                record["content"] = self._truncate_to_byte_limit(record["content"], 60000)
        collection = Collection(name=name, using=self._alias)

        # 轻量重试：网络瞬断时最多重试 2 次
        last_error = None
        for attempt in range(3):
            try:
                result = collection.insert(data)
                # 注意：此处**不调用 flush()**。flush 会强制把增量数据封成 sealed
                # segment 落盘，是重操作（实测单次可达十余秒），在 ARM64 / 高并发 /
                # 资源受限环境下可能长时间阻塞甚至拖垮进程，导致上层 DB 事务挂成
                # idle-in-transaction、上传永久卡住。Milvus 会按自身策略自动 flush；
                # 检索可见性由 search 前的 collection.load()（见 _ensure_loaded +
                # 下方 _loaded_at 失效）保证，growing segment 中的新数据 load 后即可被
                # 搜索到，无需客户端逐批显式 flush。
                # 写入后清除该 collection 的加载标记（Req 15.4），
                # 使下次搜索强制重新 load，避免读到旧加载快照。
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
                        "Milvus insert 失败 (attempt %d/3): %s，重试中...",
                        attempt + 1, e,
                    )
                    import time
                    time.sleep(1)
                    # 重连后重试
                    self._connect()
                    collection = Collection(name=name, using=self._alias)
        raise last_error

    def _search_dense_sync(
        self, kb_id: str, vector: list[float], top_k: int,
        expr: str | None = None, ef: int = 128, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """同步稠密向量搜索

        Args:
            ef: HNSW 查询时的 ef 参数（候选队列大小）。None 时回落默认 128。
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 每次都 load（关闭跳过优化）。
        """
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)
        self._ensure_loaded(name, collection, load_cache_ttl)

        ef_val = ef if ef is not None else 128
        search_kwargs: dict[str, Any] = {
            "data": [vector],
            "anns_field": "dense_vector",
            "param": {"metric_type": "COSINE", "params": {"ef": ef_val}},
            "limit": top_k,
            "output_fields": ["chunk_id", "doc_id", "content", "parent_id", "chunk_index", "file_type", "element_type"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        try:
            results = collection.search(**search_kwargs)
        except Exception as e:  # not-loaded 降级重试（Req 16）
            if self._is_not_loaded_error(e):
                self._loaded_at.pop(name, None)
                collection.load()
                self._loaded_at[name] = time.monotonic()
                results = collection.search(**search_kwargs)
            else:
                raise

        return self._parse_search_results(results)

    def _search_sparse_sync(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """同步稀疏向量搜索

        Args:
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 每次都 load（关闭跳过优化）。
        """
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)
        self._ensure_loaded(name, collection, load_cache_ttl)

        search_kwargs: dict[str, Any] = {
            "data": [sparse_vector],
            "anns_field": "sparse_vector",
            "param": {"metric_type": "IP"},
            "limit": top_k,
            "output_fields": ["chunk_id", "doc_id", "content", "parent_id", "chunk_index", "file_type", "element_type"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        try:
            results = collection.search(**search_kwargs)
        except Exception as e:  # not-loaded 降级重试（Req 16）
            if self._is_not_loaded_error(e):
                self._loaded_at.pop(name, None)
                collection.load()
                self._loaded_at[name] = time.monotonic()
                results = collection.search(**search_kwargs)
            else:
                raise

        return self._parse_search_results(results)

    def _search_bm25_sync(
        self, kb_id: str, query_text: str, top_k: int,
        expr: str | None = None, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """同步 BM25 全文检索

        使用 Milvus 2.5 原生 BM25 功能，直接传入文本查询。
        对于旧 schema（无 bm25_vector 字段）的 collection，返回空列表。

        Args:
            load_cache_ttl: 加载缓存有效期（秒），透传给 ``_ensure_loaded``。
                默认 0 = 每次都 load（关闭跳过优化）。
        """
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return []

        collection = Collection(name=name, using=self._alias)

        # 检查 collection 是否有 bm25_vector 字段（兼容旧 schema）
        field_names = [f.name for f in collection.schema.fields]
        if "bm25_vector" not in field_names:
            logger.debug("Collection %s 无 bm25_vector 字段，跳过 BM25 检索", name)
            return []

        self._ensure_loaded(name, collection, load_cache_ttl)

        search_kwargs: dict[str, Any] = {
            "data": [query_text],
            "anns_field": "bm25_vector",
            "param": {"metric_type": "BM25"},
            "limit": top_k,
            "output_fields": ["chunk_id", "doc_id", "content", "parent_id", "chunk_index", "file_type", "element_type"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        try:
            try:
                results = collection.search(**search_kwargs)
            except Exception as e:  # not-loaded 降级重试（Req 16），内层优先尝试重载
                if self._is_not_loaded_error(e):
                    self._loaded_at.pop(name, None)
                    collection.load()
                    self._loaded_at[name] = time.monotonic()
                    results = collection.search(**search_kwargs)
                else:
                    raise
            return self._parse_search_results(results)
        except Exception as e:
            logger.warning("BM25 检索失败（可能是旧 schema collection）: %s", e)
            return []

    def _delete_sync(self, kb_id: str, chunk_ids: list[str]) -> None:
        """同步删除指定 chunk"""
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)

        # 构造删除表达式
        ids_str = ", ".join(f'"{cid}"' for cid in chunk_ids)
        expr = f"chunk_id in [{ids_str}]"
        collection.delete(expr)
        # 不 flush（同 _insert_sync 的理由，避免重操作阻塞）：delete 返回后即通过
        # delete buffer 在查询中生效，下次 load（由 _loaded_at 失效强制触发）带上删除标记。
        # 删除后清除加载标记（Req 15.4），下次搜索强制重新 load。
        self._loaded_at.pop(name, None)

        logger.info("Collection %s 删除 %d 条记录", name, len(chunk_ids))

    def _delete_by_doc_id_sync(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 删除该文档的所有向量"""
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return

        collection = Collection(name=name, using=self._alias)
        # 确保 collection 已加载（delete 操作需要 loaded 状态）
        try:
            collection.load()
        except Exception:
            pass  # 可能已经 loaded，忽略错误

        expr = f'doc_id == "{doc_id}"'
        collection.delete(expr)
        # 不 flush：此方法在上传写索引前的孤儿清理热路径上被调用（_cleanup_milvus_orphans），
        # flush 阻塞会直接拖垮上传。delete 经 delete buffer 即时生效，可见性由 load 保证。
        # 删除后清除加载标记（Req 15.4），下次搜索强制重新 load。
        self._loaded_at.pop(name, None)

        logger.info("Collection %s 按 doc_id=%s 删除向量", name, doc_id)

    def _delete_by_doc_ids_sync(self, kb_id: str, doc_ids: list[str]) -> None:
        """批量按 doc_id 列表删除向量

        优化：合并为 IN 表达式，只 load 一次，只 flush 一次。
        """
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return

        collection = Collection(name=name, using=self._alias)
        try:
            collection.load()
        except Exception:
            pass

        batch_size = 100
        for i in range(0, len(doc_ids), batch_size):
            batch = doc_ids[i:i + batch_size]
            ids_str = ", ".join(f'"{did}"' for did in batch)
            expr = f"doc_id in [{ids_str}]"
            collection.delete(expr)

        # 不 flush（同上）：delete 经 delete buffer 即时生效，可见性由下次 load 保证。
        # 删除后清除加载标记（Req 15.4），下次搜索强制重新 load。
        self._loaded_at.pop(name, None)
        logger.info("Collection %s 批量删除 %d 个文档的向量", name, len(doc_ids))

    def _delete_session_sync(self, session_id: str) -> None:
        """同步按 session_id 删除会话文件库中该会话的全部向量。"""
        self._connect()
        name = self._collection_name(SESSION_FILES_KB_ID)

        if not utility.has_collection(name, using=self._alias):
            return

        collection = Collection(name=name, using=self._alias)
        # 确保 collection 已加载（delete 操作需要 loaded 状态）
        try:
            collection.load()
        except Exception:
            pass  # 可能已经 loaded，忽略错误

        expr = build_session_id_expr(session_id)
        collection.delete(expr)
        # 不 flush（同上）：delete 经 delete buffer 即时生效，可见性由下次 load 保证。
        # 删除后清除加载标记（Req 15.4），下次搜索强制重新 load。
        self._loaded_at.pop(name, None)

        logger.info("会话文件 Collection %s 按 session_id=%s 删除向量", name, session_id)

    def _drop_collection_sync(self, kb_id: str) -> None:
        """同步删除 collection"""
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            utility.drop_collection(name, using=self._alias)
            logger.info("Collection %s 已删除", name)

    def _has_collection_sync(self, kb_id: str) -> bool:
        """同步检查 collection 是否存在"""
        self._connect()
        name = self._collection_name(kb_id)
        return utility.has_collection(name, using=self._alias)

    def _check_schema_version_sync(self, kb_id: str) -> dict:
        """同步检查 schema 版本

        Returns:
            dict with keys:
            - exists: bool - collection 是否存在
            - has_new_fields: bool - 是否包含 file_type/element_type 字段
            - field_names: list[str] - 当前所有字段名
        """
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return {"exists": False, "has_new_fields": False, "field_names": []}

        collection = Collection(name=name, using=self._alias)
        field_names = [field.name for field in collection.schema.fields]
        has_new_fields = "file_type" in field_names and "element_type" in field_names

        return {
            "exists": True,
            "has_new_fields": has_new_fields,
            "field_names": field_names,
        }

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_search_results(results) -> list[dict[str, Any]]:
        """将 pymilvus 搜索结果转换为字典列表"""
        hits = []
        for result in results:
            for hit in result:
                hits.append({
                    "chunk_id": hit.entity.get("chunk_id"),
                    "doc_id": hit.entity.get("doc_id"),
                    "content": hit.entity.get("content"),
                    "parent_id": hit.entity.get("parent_id"),
                    "chunk_index": hit.entity.get("chunk_index"),
                    "file_type": hit.entity.get("file_type"),
                    "element_type": hit.entity.get("element_type"),
                    "score": hit.score,
                })
        return hits


# ------------------------------------------------------------------
# 进程内单例（Req 14）
# ------------------------------------------------------------------

_client: "MilvusClient | None" = None


def get_milvus_client() -> "MilvusClient":
    """进程内 MilvusClient 单例（Req 14）。

    host/port 取 get_settings()。多次调用返回同一实例，使 B3 的 collection 加载标记
    跨请求存活、Milvus 连接复用。API 进程与 Worker 进程各自持有独立实例（进程级）。
    """
    global _client
    if _client is None:
        from app.config import get_settings
        s = get_settings()
        _client = MilvusClient(host=s.milvus_host, port=s.milvus_port)
    return _client
