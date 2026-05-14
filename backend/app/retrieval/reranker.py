"""Rerank 精排封装

将 RerankProvider 包装为检索层可直接使用的工具，
接收 RetrievalResult 列表并返回重排序后的结果。
"""

from app.models.provider import RerankProvider
from app.retrieval.base import RetrievalResult


class RerankerWrapper:
    """Reranker 封装，桥接 RerankProvider 与 RetrievalResult"""

    def __init__(self, rerank_provider: RerankProvider):
        self.provider = rerank_provider

    async def rerank_results(
        self, query: str, results: list[RetrievalResult], top_k: int = 10
    ) -> list[RetrievalResult]:
        """对 RetrievalResult 列表重排序

        Args:
            query: 查询文本
            results: 待重排序的检索结果列表
            top_k: 返回前 k 个结果

        Returns:
            按相关性分数降序排列的 RetrievalResult 列表
        """
        if not results:
            return []

        # 提取文档内容交给 provider 打分
        documents = [r.content for r in results]
        ranked = await self.provider.rerank(query, documents, top_k)

        # 用 provider 返回的分数构建新的 RetrievalResult
        return [
            RetrievalResult(
                chunk_id=results[idx].chunk_id,
                content=results[idx].content,
                score=score,
                doc_id=results[idx].doc_id,
                metadata=results[idx].metadata,
            )
            for idx, score in ranked
        ]
