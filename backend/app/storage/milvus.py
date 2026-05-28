"""Milvus 向量数据库操作封装

支持三路检索：
- Dense（稠密向量，COSINE 相似度）
- Sparse（BGE-M3 稀疏向量，IP 内积）
- BM25（全文检索，Milvus 2.5+ 原生支持）
"""

import asyncio
import logging
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

# BM25 Function：自动将 content 文本转换为 BM25 稀疏向量
_BM25_FUNCTION = Function(
    name="bm25_fn",
    input_field_names=["content"],
    output_field_names=["bm25_vector"],
    function_type=FunctionType.BM25,
)

# 索引配置
_DENSE_INDEX_PARAMS = {
    "index_type": "HNSW",
    "metric_type": "COSINE",
    "params": {"M": 16, "efConstruction": 256},
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

    async def create_collection(self, kb_id: str) -> None:
        """创建 collection 并建立索引"""
        await asyncio.to_thread(self._create_collection_sync, kb_id)

    async def insert(self, kb_id: str, data: list[dict]) -> int:
        """插入数据，返回插入条数"""
        return await asyncio.to_thread(self._insert_sync, kb_id, data)

    async def search_dense(
        self, kb_id: str, vector: list[float], top_k: int = 10,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """稠密向量相似度搜索"""
        return await asyncio.to_thread(self._search_dense_sync, kb_id, vector, top_k, expr)

    async def search_sparse(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int = 10,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """稀疏向量搜索"""
        return await asyncio.to_thread(self._search_sparse_sync, kb_id, sparse_vector, top_k, expr)

    async def search_bm25(
        self, kb_id: str, query_text: str, top_k: int = 10,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 全文检索（Milvus 2.5+ 原生支持）

        直接传入文本查询，Milvus 内部自动分词并计算 BM25 分数。
        对于旧 schema（无 bm25_vector 字段）的 collection，返回空列表。
        """
        return await asyncio.to_thread(self._search_bm25_sync, kb_id, query_text, top_k, expr)

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

    def _create_collection_sync(self, kb_id: str) -> None:
        """同步创建 collection + 索引（v2 schema，含 BM25 全文检索）"""
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            logger.info("Collection %s 已存在，跳过创建", name)
            return

        schema = CollectionSchema(
            fields=_FIELDS,
            description=f"知识库 {kb_id} 的向量集合",
            functions=[_BM25_FUNCTION],
        )
        collection = Collection(name=name, schema=schema, using=self._alias)

        # 创建稠密向量索引
        collection.create_index(
            field_name="dense_vector",
            index_params=_DENSE_INDEX_PARAMS,
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
                collection.flush()
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
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """同步稠密向量搜索"""
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)
        collection.load()

        search_kwargs: dict[str, Any] = {
            "data": [vector],
            "anns_field": "dense_vector",
            "param": {"metric_type": "COSINE", "params": {"ef": 128}},
            "limit": top_k,
            "output_fields": ["chunk_id", "doc_id", "content", "parent_id", "chunk_index", "file_type", "element_type"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        results = collection.search(**search_kwargs)

        return self._parse_search_results(results)

    def _search_sparse_sync(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """同步稀疏向量搜索"""
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)
        collection.load()

        search_kwargs: dict[str, Any] = {
            "data": [sparse_vector],
            "anns_field": "sparse_vector",
            "param": {"metric_type": "IP"},
            "limit": top_k,
            "output_fields": ["chunk_id", "doc_id", "content", "parent_id", "chunk_index", "file_type", "element_type"],
        }
        if expr is not None:
            search_kwargs["expr"] = expr

        results = collection.search(**search_kwargs)

        return self._parse_search_results(results)

    def _search_bm25_sync(
        self, kb_id: str, query_text: str, top_k: int,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """同步 BM25 全文检索

        使用 Milvus 2.5 原生 BM25 功能，直接传入文本查询。
        对于旧 schema（无 bm25_vector 字段）的 collection，返回空列表。
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

        collection.load()

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
            results = collection.search(**search_kwargs)
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
        collection.flush()

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
        collection.flush()

        logger.info("Collection %s 按 doc_id=%s 删除向量", name, doc_id)

    def _delete_by_doc_ids_sync(self, kb_id: str, doc_ids: list[str]) -> None:
        """批量按 doc_id 列表删除向量

        优化：合并为 IN 表达式，只 load 一次，只 flush 一次。
        对于大量 doc_id，分批处理避免表达式过长。
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

        # 分批删除，每批最多 100 个 doc_id（避免表达式过长）
        batch_size = 100
        for i in range(0, len(doc_ids), batch_size):
            batch = doc_ids[i:i + batch_size]
            ids_str = ", ".join(f'"{did}"' for did in batch)
            expr = f"doc_id in [{ids_str}]"
            collection.delete(expr)

        # 所有批次删除完后一次性 flush
        collection.flush()
        logger.info("Collection %s 批量删除 %d 个文档的向量", name, len(doc_ids))

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
