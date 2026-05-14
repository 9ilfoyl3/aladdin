"""稠密向量检索器

基于 Milvus 的稠密向量相似度搜索，将查询文本嵌入为向量后
在指定知识库中执行 ANN 检索，返回按分数降序排列的结果。
"""

import logging

from app.models.provider import EmbedProvider
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)


class VectorRetriever(BaseRetriever):
    """稠密向量检索器，使用 EmbedProvider 嵌入查询后在 Milvus 中搜索"""

    def __init__(self, embed_provider: EmbedProvider, milvus_client: MilvusClient):
        self.embedder = embed_provider
        self.milvus = milvus_client

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, **kwargs
    ) -> list[RetrievalResult]:
        """执行稠密向量检索

        流程：嵌入查询 → Milvus 搜索 → 转换为 RetrievalResult 列表
        """
        # 1. 将查询文本嵌入为稠密向量
        vectors = await self.embedder.embed([query])
        query_vector = vectors[0]

        # 2. 在 Milvus 中执行稠密向量搜索
        hits = await self.milvus.search_dense(kb_id, query_vector, top_k)

        # 3. 转换为 RetrievalResult 并按分数降序排列
        results = [
            RetrievalResult(
                chunk_id=hit["chunk_id"],
                content=hit["content"],
                score=hit["score"],
                doc_id=hit["doc_id"],
                metadata={
                    "parent_id": hit.get("parent_id", ""),
                    "chunk_index": hit.get("chunk_index", 0),
                },
            )
            for hit in hits
        ]

        # 按分数降序排序
        results.sort(key=lambda r: r.score, reverse=True)

        logger.debug("VectorRetriever 在 kb=%s 中检索到 %d 条结果", kb_id, len(results))
        return results
