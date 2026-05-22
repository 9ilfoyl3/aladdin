"""多知识库联合检索模块

提供 KBRetrievalConfig 配置和 MultiKBRetriever 检索器，
支持并行检索多个知识库并按优先级加权合并结果，最后统一 Rerank。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.retrieval.base import RetrievalResult
from app.retrieval.filter import RetrievalFilter
from app.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)


@dataclass
class KBRetrievalConfig:
    """知识库检索配置"""

    kb_id: str
    priority: float = 1.0  # 优先级权重 (主库1.0, 辅助库0.8)


@dataclass
class MultiKBSearchResult:
    """多知识库联合检索结果，包含检索元数据"""

    results: list[RetrievalResult]
    degraded: bool = False  # 是否有知识库检索失败
    failed_kb_ids: list[str] = field(default_factory=list)  # 失败的知识库 ID 列表


class MultiKBRetriever:
    """多知识库联合检索器

    并行检索多个知识库，按优先级加权合并 RRF 分数后统一 Rerank。
    """

    def __init__(
        self,
        hybrid_retriever: HybridRetriever,
        max_concurrency: int = 5,
    ):
        self.retriever = hybrid_retriever
        self.max_concurrency = max_concurrency

    async def search(
        self,
        query: str,
        kb_configs: list[KBRetrievalConfig],
        top_k: int = 10,
        filters: RetrievalFilter | None = None,
    ) -> MultiKBSearchResult:
        """并行检索多个知识库，加权合并后统一 Rerank

        单个知识库检索失败时返回空结果，不影响其他库的正常检索。
        当任何知识库检索失败时，返回结果中 degraded=True。

        Args:
            query: 用户查询文本
            kb_configs: 知识库配置列表，包含 kb_id 和优先级
            top_k: 最终返回结果数量
            filters: 可选的检索过滤条件

        Returns:
            MultiKBSearchResult，包含检索结果和降级状态信息
        """
        expr = filters.to_milvus_expr() if filters else None

        # 并行检索所有知识库（skip_rerank=True，合并后统一 rerank）
        tasks = [
            self.retriever.search(
                query, cfg.kb_id, top_k=top_k * 3, skip_rerank=True, expr=expr
            )
            for cfg in kb_configs
        ]
        results_by_kb = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理结果（异常的库返回空列表，记录失败信息）
        valid_results: dict[str, list[RetrievalResult]] = {}
        failed_kb_ids: list[str] = []

        for cfg, result in zip(kb_configs, results_by_kb):
            if isinstance(result, Exception):
                logger.warning(
                    "知识库 '%s' 检索失败，返回空结果: %s",
                    cfg.kb_id,
                    str(result),
                )
                valid_results[cfg.kb_id] = []
                failed_kb_ids.append(cfg.kb_id)
            else:
                valid_results[cfg.kb_id] = result

        degraded = len(failed_kb_ids) > 0

        # 加权合并
        merged = self._weighted_merge(valid_results, kb_configs)

        # 统一 Rerank + 父块扩展
        results = await self.retriever.rerank_and_expand(query, merged, top_k)

        return MultiKBSearchResult(
            results=results,
            degraded=degraded,
            failed_kb_ids=failed_kb_ids,
        )

    def _weighted_merge(
        self,
        results_by_kb: dict[str, list[RetrievalResult]],
        kb_configs: list[KBRetrievalConfig],
    ) -> list[RetrievalResult]:
        """按知识库优先级加权合并 RRF 分数

        Args:
            results_by_kb: 各知识库的检索结果，key 为 kb_id
            kb_configs: 知识库配置列表

        Returns:
            按加权分数降序排列的合并结果
        """
        merged: dict[str, RetrievalResult] = {}
        merged_scores: dict[str, float] = {}

        for cfg in kb_configs:
            results = results_by_kb.get(cfg.kb_id, [])
            for item in results:
                key = item.chunk_id
                boosted_score = item.score * cfg.priority
                if key not in merged or boosted_score > merged_scores[key]:
                    merged[key] = item
                    merged_scores[key] = boosted_score

        # 按加权分数降序排列
        sorted_items = sorted(
            merged.values(),
            key=lambda x: merged_scores[x.chunk_id],
            reverse=True,
        )
        return sorted_items
