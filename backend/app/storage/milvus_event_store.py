"""Milvus 事件向量集合操作封装（事件中心图谱）

事件（Event）是事件中心图谱的一等检索单元。本模块为每个知识库维护一个独立的
**事件向量集合** ``kb_event_<kb_id>``，承载事件 ``content`` 的稠密向量，用于事件
向量召回入口（与现有 chunk collection ``kb_<kb_id>`` 互相独立）。

设计对齐 ``storage/milvus.py``：
- 复用 ``_build_dense_index_params``（HNSW + COSINE），维度 1024 对齐现有 dense（BGE-M3），
  保证 query 与 event 在同一向量空间。
- 复用 ``MilvusClient`` 的连接 / collection 命名 / 字节截断 / not-loaded 降级语义。
- ``content_vector`` 建 HNSW+COSINE 索引；``doc_id`` 建 scalar 索引（按 doc 删除用）。

字段：``event_id``(pk) / ``kb_id`` / ``doc_id`` / ``chunk_id`` / ``content`` / ``content_vector``。
方法：``create_collection`` / ``upsert`` / ``search`` / ``delete_by_doc`` / ``delete_by_kb``。
"""

import asyncio
import logging
import time
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.storage.milvus import (
    MilvusClient,
    _build_dense_index_params,
)

logger = logging.getLogger(__name__)

# 事件向量维度，对齐现有 dense（BGE-M3 = 1024），保证 query/event 同向量空间。
_EVENT_VECTOR_DIM = 1024

# 事件集合字段定义：event_id 为主键，content_vector 为稠密向量（HNSW/COSINE），
# doc_id 建 scalar 索引用于按文档删除。
_EVENT_FIELDS = [
    FieldSchema(name="event_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
    FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
    FieldSchema(name="content_vector", dtype=DataType.FLOAT_VECTOR, dim=_EVENT_VECTOR_DIM),
]


class MilvusEventStore:
    """事件向量集合操作客户端，封装 ``kb_event_<kb_id>`` 的建/写/查/删。

    与 ``MilvusClient`` 同构（async 公开方法 + ``asyncio.to_thread`` 包同步 pymilvus），
    并复用其连接管理与 collection 加载缓存机制，避免重复实现连接/重连逻辑。
    """

    def __init__(self, host: str = "localhost", port: int = 19530, alias: str = "default"):
        self._host = host
        self._port = port
        self._alias = alias
        # collection name -> 上次 load 的单调时间戳（与 MilvusClient 一致的加载缓存）
        self._loaded_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 连接 / 命名（复用 MilvusClient 的实现，避免重复造轮子）
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """建立连接（如果尚未连接），支持断线重连。复用 MilvusClient 逻辑。"""
        if not connections.has_connection(self._alias):
            connections.connect(alias=self._alias, host=self._host, port=self._port)
        else:
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
        """根据知识库 ID 生成事件集合名称 ``kb_event_<kb_id>``（替换连字符为下划线）。"""
        return f"kb_event_{kb_id.replace('-', '_')}"

    def _ensure_loaded(self, name: str, collection, load_cache_ttl: int) -> None:
        """跳过重复 load（对齐 MilvusClient._ensure_loaded 语义）。"""
        if load_cache_ttl > 0:
            ts = self._loaded_at.get(name)
            if ts is not None and (time.monotonic() - ts) < load_cache_ttl:
                return
        collection.load()
        self._loaded_at[name] = time.monotonic()

    @staticmethod
    def _is_not_loaded_error(e: Exception) -> bool:
        """判断异常是否为 collection 未加载类错误（复用 MilvusClient 语义）。"""
        s = str(e).lower()
        return "not loaded" in s or "not been loaded" in s

    # ------------------------------------------------------------------
    # 公开方法（均为 async）
    # ------------------------------------------------------------------

    async def create_collection(
        self, kb_id: str,
        ef_construction: int | None = None, m: int | None = None,
    ) -> None:
        """创建事件集合并建立索引（content_vector HNSW/COSINE + doc_id scalar）。

        Args:
            ef_construction: HNSW 建索引 efConstruction。None 时回落 milvus 默认 128。
            m: HNSW 建索引 M。None 时回落 milvus 默认 16。
        """
        await asyncio.to_thread(self._create_collection_sync, kb_id, ef_construction, m)

    async def upsert(
        self, kb_id: str, rows: list[dict], vectors: list[list[float]],
    ) -> int:
        """幂等写入事件向量（按 event_id upsert），返回写入条数。

        rows 中每条须含 ``event_id``/``doc_id``/``chunk_id``/``content``，``kb_id`` 字段
        缺省时以入参 kb_id 兜底；``vectors`` 与 rows 一一对应。collection 不存在时自动建。
        """
        if not rows:
            return 0
        if len(rows) != len(vectors):
            raise ValueError(
                f"rows 与 vectors 数量不一致: {len(rows)} != {len(vectors)}"
            )
        return await asyncio.to_thread(self._upsert_sync, kb_id, rows, vectors)

    async def search(
        self, kb_id: str, query_vector: list[float], top_k: int = 10,
        expr: str | None = None, ef: int | None = None,
        load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """事件 content 向量召回（COSINE）。collection 不存在时返回 ``[]``。

        Args:
            ef: HNSW 查询 ef。None 时回落默认 128。
            load_cache_ttl: 加载缓存有效期（秒），默认 0 = 每次都 load。
        """
        return await asyncio.to_thread(
            self._search_sync, kb_id, query_vector, top_k, expr,
            ef if ef is not None else 128, load_cache_ttl,
        )

    async def delete_by_doc(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 删除该文档贡献的事件向量（重处理「先删后写」幂等）。"""
        await asyncio.to_thread(self._delete_by_doc_sync, kb_id, doc_id)

    async def delete_by_kb(self, kb_id: str) -> None:
        """删除整个事件集合（删 KB 时级联清理，不留孤儿）。"""
        await asyncio.to_thread(self._delete_by_kb_sync, kb_id)

    async def has_collection(self, kb_id: str) -> bool:
        """检查事件集合是否存在。"""
        return await asyncio.to_thread(self._has_collection_sync, kb_id)

    # ------------------------------------------------------------------
    # 同步实现
    # ------------------------------------------------------------------

    def _create_collection_sync(
        self, kb_id: str,
        ef_construction: int | None = None, m: int | None = None,
    ) -> None:
        """同步创建事件集合 + 索引（幂等：已存在则跳过）。"""
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            logger.info("事件 Collection %s 已存在，跳过创建", name)
            return

        schema = CollectionSchema(
            fields=_EVENT_FIELDS,
            description=f"知识库 {kb_id} 的事件向量集合",
        )
        collection = Collection(name=name, schema=schema, using=self._alias)

        # content_vector 稠密向量索引：HNSW + COSINE（复用现有 dense 索引参数构造器）
        if ef_construction is not None and m is not None:
            dense_params = _build_dense_index_params(ef_construction, m)
        else:
            dense_params = _build_dense_index_params()
        collection.create_index(
            field_name="content_vector",
            index_params=dense_params,
        )
        # doc_id scalar 索引：按 doc 删除用
        collection.create_index(
            field_name="doc_id",
            index_name="idx_event_doc_id",
        )

        logger.info("事件 Collection %s 创建完成（HNSW/COSINE + doc_id scalar）", name)

    def _upsert_sync(
        self, kb_id: str, rows: list[dict], vectors: list[list[float]],
    ) -> int:
        """同步 upsert 事件向量（按 event_id 主键覆盖），带轻量重试。"""
        self._connect()
        name = self._collection_name(kb_id)

        # collection 不存在则先建（与 chunk 写入前确保 collection 存在的语义一致）
        if not utility.has_collection(name, using=self._alias):
            self._create_collection_sync(kb_id)

        # 组装写入数据：content 按 UTF-8 字节截断，防止超过 Milvus max_length（字节）限制
        data: list[dict] = []
        for row, vec in zip(rows, vectors):
            content = MilvusClient._truncate_to_byte_limit(row.get("content", "") or "", 60000)
            data.append({
                "event_id": row["event_id"],
                "kb_id": row.get("kb_id") or kb_id,
                "doc_id": row.get("doc_id", "") or "",
                "chunk_id": row.get("chunk_id", "") or "",
                "content": content,
                "content_vector": vec,
            })

        collection = Collection(name=name, using=self._alias)

        last_error = None
        for attempt in range(3):
            try:
                result = collection.upsert(data)
                # 不显式 flush（对齐 MilvusClient 写入：避免重操作阻塞），可见性由下次
                # search 前的 load 保证。写入后清除加载标记，强制下次重新 load。
                self._loaded_at.pop(name, None)
                # pymilvus upsert 返回 upsert_count（部分版本无该字段则回落写入条数）
                return getattr(result, "upsert_count", None) or len(data)
            except Exception as e:
                last_error = e
                error_str = str(e)
                if "invalid parameter" in error_str or "type mismatch" in error_str:
                    raise
                if attempt < 2:
                    logger.warning(
                        "Milvus 事件 upsert 失败 (attempt %d/3): %s，重试中...",
                        attempt + 1, e,
                    )
                    time.sleep(1)
                    self._connect()
                    collection = Collection(name=name, using=self._alias)
        raise last_error

    def _search_sync(
        self, kb_id: str, query_vector: list[float], top_k: int,
        expr: str | None = None, ef: int = 128, load_cache_ttl: int = 0,
    ) -> list[dict[str, Any]]:
        """同步事件向量搜索（COSINE）。collection 不存在时返回 ``[]``。"""
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return []

        collection = Collection(name=name, using=self._alias)
        self._ensure_loaded(name, collection, load_cache_ttl)

        ef_val = ef if ef is not None else 128
        search_kwargs: dict[str, Any] = {
            "data": [query_vector],
            "anns_field": "content_vector",
            "param": {"metric_type": "COSINE", "params": {"ef": ef_val}},
            "limit": top_k,
            "output_fields": ["event_id", "kb_id", "doc_id", "chunk_id", "content"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        try:
            results = collection.search(**search_kwargs)
        except Exception as e:  # not-loaded 降级重试（对齐 MilvusClient）
            if self._is_not_loaded_error(e):
                self._loaded_at.pop(name, None)
                collection.load()
                self._loaded_at[name] = time.monotonic()
                results = collection.search(**search_kwargs)
            else:
                raise

        return self._parse_search_results(results)

    def _delete_by_doc_sync(self, kb_id: str, doc_id: str) -> None:
        """同步按 doc_id 删除事件向量（collection 不存在则跳过）。"""
        self._connect()
        name = self._collection_name(kb_id)

        if not utility.has_collection(name, using=self._alias):
            return

        collection = Collection(name=name, using=self._alias)
        try:
            collection.load()
        except Exception:
            pass  # 可能已 loaded，忽略

        expr = f'doc_id == "{doc_id}"'
        collection.delete(expr)
        # 不 flush：delete 经 delete buffer 即时生效，可见性由下次 load 保证。
        self._loaded_at.pop(name, None)

        logger.info("事件 Collection %s 按 doc_id=%s 删除向量", name, doc_id)

    def _delete_by_kb_sync(self, kb_id: str) -> None:
        """同步删除整个事件集合。"""
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            utility.drop_collection(name, using=self._alias)
            self._loaded_at.pop(name, None)
            logger.info("事件 Collection %s 已删除", name)

    def _has_collection_sync(self, kb_id: str) -> bool:
        """同步检查事件集合是否存在。"""
        self._connect()
        name = self._collection_name(kb_id)
        return utility.has_collection(name, using=self._alias)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_search_results(results) -> list[dict[str, Any]]:
        """将 pymilvus 搜索结果转换为字典列表。"""
        hits = []
        for result in results:
            for hit in result:
                hits.append({
                    "event_id": hit.entity.get("event_id"),
                    "kb_id": hit.entity.get("kb_id"),
                    "doc_id": hit.entity.get("doc_id"),
                    "chunk_id": hit.entity.get("chunk_id"),
                    "content": hit.entity.get("content"),
                    "score": hit.score,
                })
        return hits


# ------------------------------------------------------------------
# 进程内单例（对齐 MilvusClient 的 get_milvus_client）
# ------------------------------------------------------------------

_event_store: "MilvusEventStore | None" = None


def get_milvus_event_store() -> "MilvusEventStore":
    """进程内 MilvusEventStore 单例。

    host/port 取 get_settings()。多次调用返回同一实例，使 collection 加载标记跨请求
    存活、Milvus 连接复用。API 进程与 Worker 进程各自持有独立实例（进程级）。
    """
    global _event_store
    if _event_store is None:
        from app.config import get_settings
        s = get_settings()
        _event_store = MilvusEventStore(host=s.milvus_host, port=s.milvus_port)
    return _event_store
