"""检索执行器

并行执行多个查询（跳过 rerank），合并去重结果后统一 rerank，
避免多次 rerank 锁争用导致的串行瓶颈。

支持查询级去重：通过 embedding cosine similarity 跳过与已执行查询高度相似的新查询，
减少无效检索开销。
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from app.models.provider import EmbedProvider
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)

# 查询去重阈值：cosine similarity > 此值的查询视为重复
_QUERY_DEDUP_THRESHOLD = 0.92


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    dot = np.dot(va, vb)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    if norm < 1e-10:
        return 0.0
    return float(dot / norm)


class RetrievalExecutor:
    """检索执行器：查询去重 → 并行检索 → 合并去重 → 统一 rerank"""

    def __init__(self, retriever: BaseRetriever, embedder: EmbedProvider | None = None):
        self.retriever = retriever
        self.embedder = embedder
        # 查询缓存：记录已执行查询的 embedding，用于跨迭代去重
        self._query_cache: list[list[float]] = []

    def reset_cache(self):
        """重置查询缓存（每次新的 agent 编排开始时调用）"""
        self._query_cache = []

    async def _dedup_queries(self, queries: list[str]) -> list[str]:
        """基于 embedding 相似度去重查询

        去重逻辑：
        1. 批量计算所有查询的 embedding
        2. 与已缓存的历史查询比较，跳过相似度 > 阈值的查询
        3. 查询间互相比较，跳过重复的后续查询
        4. 将新查询的 embedding 加入缓存

        Returns:
            去重后的查询列表（至少保留一个）
        """
        if not self.embedder or len(queries) <= 1:
            return queries

        # 批量 embed 所有候选查询
        embeddings = await self.embedder.embed(queries)

        unique_queries: list[str] = []
        unique_embeddings: list[list[float]] = []

        for i, (q, emb) in enumerate(zip(queries, embeddings)):
            is_duplicate = False

            # 与历史缓存比较
            for cached_emb in self._query_cache:
                if _cosine_similarity(emb, cached_emb) > _QUERY_DEDUP_THRESHOLD:
                    is_duplicate = True
                    print(f"[Executor] 查询去重（与历史重复）: {q!r}")
                    break

            # 与本批次已保留的查询比较
            if not is_duplicate:
                for kept_emb in unique_embeddings:
                    if _cosine_similarity(emb, kept_emb) > _QUERY_DEDUP_THRESHOLD:
                        is_duplicate = True
                        print(f"[Executor] 查询去重（批次内重复）: {q!r}")
                        break

            if not is_duplicate:
                unique_queries.append(q)
                unique_embeddings.append(emb)

        # 更新缓存
        self._query_cache.extend(unique_embeddings)

        # 至少保留一个查询
        if not unique_queries:
            unique_queries = [queries[0]]
            self._query_cache.append(embeddings[0])

        if len(unique_queries) < len(queries):
            print(f"[Executor] 查询去重: {len(queries)} -> {len(unique_queries)}")

        return unique_queries

    async def execute(
        self, queries: list[str], kb_id: str, top_k: int = 30, expr: str | None = None,
    ) -> list[RetrievalResult]:
        """并行执行多个查询，合并去重后统一 rerank

        优化策略：
        - 查询级去重：跳过与已执行查询高度相似的新查询
        - 子查询阶段跳过 rerank（纯向量检索+RRF，完全并行无锁）
        - 合并去重后只做一次 rerank（消除锁争用）

        Args:
            queries: 待检索的查询列表（可能由 Rewriter 生成）
            kb_id: 知识库 ID
            top_k: 最终返回的最大结果数
            expr: Milvus pre-filter 表达式

        Returns:
            按分数降序排列的去重结果列表
        """
        # 查询去重
        deduped_queries = await self._dedup_queries(queries)

        # 判断 retriever 是否支持 skip_rerank 优化
        use_batch_rerank = isinstance(self.retriever, HybridRetriever)

        if use_batch_rerank:
            # 并行执行所有子查询，跳过 rerank（只做稠密+稀疏+RRF）
            tasks = [
                self.retriever.search(q, kb_id, top_k, skip_rerank=True, expr=expr)
                for q in deduped_queries
            ]
            all_results = await asyncio.gather(*tasks)

            # 合并去重（按 chunk_id，保留最高分）
            merged: dict[str, RetrievalResult] = {}
            for results in all_results:
                for r in results:
                    if r.chunk_id not in merged or r.score > merged[r.chunk_id].score:
                        merged[r.chunk_id] = r

            # 按 RRF 分数降序排列
            merged_list = sorted(merged.values(), key=lambda x: x.score, reverse=True)

            # 统一 rerank + 父块扩展（只调用一次，无锁争用）
            combined_query = " ".join(deduped_queries)
            return await self.retriever.rerank_and_expand(combined_query, merged_list, top_k)
        else:
            # 回退：非 HybridRetriever 时保持原有逻辑
            tasks = [self.retriever.search(q, kb_id, top_k, expr=expr) for q in deduped_queries]
            all_results = await asyncio.gather(*tasks)

            merged: dict[str, RetrievalResult] = {}
            for results in all_results:
                for r in results:
                    if r.chunk_id not in merged or r.score > merged[r.chunk_id].score:
                        merged[r.chunk_id] = r

            return sorted(merged.values(), key=lambda x: x.score, reverse=True)
