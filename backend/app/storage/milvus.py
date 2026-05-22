"""Milvus 向量数据库操作封装"""

import asyncio
import logging
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

logger = logging.getLogger(__name__)

# Collection 字段定义
_FIELDS = [
    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
    FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
    FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=1024),
    FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
    FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="chunk_index", dtype=DataType.INT64),
    # scalar 字段，用于 pre-filter 过滤检索
    FieldSchema(name="file_type", dtype=DataType.VARCHAR, max_length=20),
    FieldSchema(name="element_type", dtype=DataType.VARCHAR, max_length=20),
]

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


class MilvusClient:
    """Milvus 操作客户端，封装 collection 的增删查操作"""

    def __init__(self, host: str = "localhost", port: int = 19530, alias: str = "default"):
        self._host = host
        self._port = port
        self._alias = alias

    def _connect(self) -> None:
        """建立连接（如果尚未连接）"""
        if not connections.has_connection(self._alias):
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

    async def delete(self, kb_id: str, chunk_ids: list[str]) -> None:
        """按 chunk_id 列表删除数据"""
        await asyncio.to_thread(self._delete_sync, kb_id, chunk_ids)

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
        """同步创建 collection + 索引"""
        self._connect()
        name = self._collection_name(kb_id)

        if utility.has_collection(name, using=self._alias):
            logger.info("Collection %s 已存在，跳过创建", name)
            return

        schema = CollectionSchema(fields=_FIELDS, description=f"知识库 {kb_id} 的向量集合")
        collection = Collection(name=name, schema=schema, using=self._alias)

        # 创建稠密向量索引
        collection.create_index(
            field_name="dense_vector",
            index_params=_DENSE_INDEX_PARAMS,
        )
        # 创建稀疏向量索引
        collection.create_index(
            field_name="sparse_vector",
            index_params=_SPARSE_INDEX_PARAMS,
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

        logger.info("Collection %s 创建完成", name)

    def _insert_sync(self, kb_id: str, data: list[dict]) -> int:
        """同步插入数据"""
        self._connect()
        name = self._collection_name(kb_id)
        collection = Collection(name=name, using=self._alias)
        result = collection.insert(data)
        collection.flush()
        return result.insert_count

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
