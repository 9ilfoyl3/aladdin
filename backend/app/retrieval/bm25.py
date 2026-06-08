"""BM25 全文检索器

基于 Milvus 2.5+ 原生 BM25 全文检索能力，直接传入文本查询，
Milvus 内部自动分词并计算 BM25 分数。

对于精确关键词匹配（条款编号、人名、案号等）效果显著优于语义检索。
对于旧 schema（无 bm25_vector 字段）的 collection，自动降级返回空结果。
"""

import logging

from app.retrieval.base import BaseRetriever, RetrievalResult
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)


class BM25Retriever(BaseRetriever):
    """BM25 全文检索器，使用 Milvus 2.5 原生 BM25 功能"""

    def __init__(self, milvus_client: MilvusClient):
        self.milvus = milvus_client

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None, **kwargs
    ) -> list[RetrievalResult]:
        """执行 BM25 全文检索

        直接传入文本查询，Milvus 内部自动分词并计算 BM25 分数。
        对于旧 schema 的 collection，返回空列表（优雅降级）。
        """
        # load_cache_ttl 从 kwargs 取出透传给 milvus（与 vector 侧 ef 处理一致），默认 0 = 每次 load
        load_cache_ttl = kwargs.pop("load_cache_ttl", 0)
        hits = await self.milvus.search_bm25(
            kb_id, query, top_k, expr=expr, load_cache_ttl=load_cache_ttl
        )

        results = [
            RetrievalResult(
                chunk_id=hit["chunk_id"],
                content=hit["content"],
                score=hit["score"],
                doc_id=hit["doc_id"],
                metadata={
                    "parent_id": hit.get("parent_id", ""),
                    "chunk_index": hit.get("chunk_index", 0),
                    "element_type": hit.get("element_type", "text"),
                },
            )
            for hit in hits
        ]

        results.sort(key=lambda r: r.score, reverse=True)

        logger.debug("BM25Retriever 在 kb=%s 中检索到 %d 条结果", kb_id, len(results))
        return results
