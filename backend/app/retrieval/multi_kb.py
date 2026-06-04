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
    """知识库检索配置

    Attributes:
        kb_id: 知识库 ID（会话文件源固定为 ``"session_files"``，对应共享 collection
            ``kb_session_files``）。
        priority: 优先级权重，主库 ``1.0``、辅助库 ``0.8``、会话文件源 ``1.2``
            （高于普通辅助库，使刚上传的内容更易靠前）。
        expr: 该源专属的 Milvus 标量过滤表达式（如会话源用
            ``session_id == "{sid}"`` 强制会话隔离）。``None`` 时仅应用全局
            ``filters.expr``；非 None 时与全局 expr 用 `` and `` 合并，二者均存在
            则同时生效，单一存在则按非空者生效（与既有 ``filters.expr`` 行为兼容，
            默认 None 时行为不变）。
    """

    kb_id: str
    priority: float = 1.0  # 优先级权重 (主库1.0, 辅助库0.8, 会话文件源1.2)
    expr: str | None = None  # 该源专属过滤表达式（如会话源 session_id 隔离）


@dataclass
class MultiKBSearchResult:
    """多知识库联合检索结果，包含检索元数据"""

    results: list[RetrievalResult]
    degraded: bool = False  # 是否有知识库检索失败
    failed_kb_ids: list[str] = field(default_factory=list)  # 失败的知识库 ID 列表


class MultiKBRetriever:
    """多知识库联合检索器

    并行检索多个知识库，按优先级加权合并 RRF 分数后统一 Rerank。

    注意:应用层 asyncio.Semaphore 限制的是同时发起的源检索协程数，
    ≠ Milvus 连接池上限（后者由 pymilvus connection pool 参数单独配置）。
    两层都需要合理设置以避免资源耗尽。
    """

    def __init__(
        self,
        hybrid_retriever: HybridRetriever,
        max_concurrency: int = 4,
        per_source_timeout: float = 30.0,
    ):
        """
        Args:
            hybrid_retriever: 混合检索器实例
            max_concurrency: 同时检索的最大源数量（默认 4，对齐 weknora defaultMultiStoreFanoutLimit）
            per_source_timeout: 每个检索源的超时秒数（默认 30s，对齐 weknora defaultMultiStoreRetrieveTimeout）
        """
        self.retriever = hybrid_retriever
        self.max_concurrency = max_concurrency
        self.per_source_timeout = per_source_timeout

    @staticmethod
    def _compute_source_top_k(top_k: int, num_sources: int) -> int:
        """计算每个检索源的召回数量，随库数增多适当收敛以保护总召回量。

        公式设计:
        - 单库/少库（≤4）: 返回 top_k * 3（充分召回，不劣化）
        - 多库（>4）: top_k * 3 * 4 // num_sources，但不低于 top_k
          即把"4 库时的总召回量"作为软上界，多库时均摊。

        保证:
        - num_sources=1 → top_k*3（与修复前一致）
        - num_sources=4 → top_k*3（fast-path 无劣化临界点）
        - num_sources=8 → top_k*3*4//8 = top_k*1.5，但不低于 top_k
        - num_sources=20 → 约 top_k*0.6 → 兜底 top_k
        """
        if num_sources <= 4:
            return top_k * 3
        scaled = top_k * 3 * 4 // num_sources
        return max(top_k, scaled)

    async def search(
        self,
        query: str,
        kb_configs: list[KBRetrievalConfig],
        top_k: int = 10,
        filters: RetrievalFilter | None = None,
        tenant_id: str | None = None,
    ) -> MultiKBSearchResult:
        """并行检索多个知识库，加权合并后统一 Rerank

        单个知识库检索失败时返回空结果，不影响其他库的正常检索。
        当任何知识库检索失败时，返回结果中 degraded=True。

        并发控制:
        - asyncio.Semaphore(max_concurrency) 限制同时执行的源检索数
        - asyncio.wait_for(per_source_timeout) 限制单源最大耗时
        - 超时产生 TimeoutError，按异常处理（该源空 + failed_kb_ids）

        注意:此处 Semaphore 是应用层并发控制，≠ Milvus 连接池上限，
        后者需通过 pymilvus 连接池参数单独配置。

        Args:
            query: 用户查询文本
            kb_configs: 知识库配置列表，包含 kb_id 和优先级
            top_k: 最终返回结果数量
            filters: 可选的检索过滤条件
            tenant_id: 显式租户 ID（H5）。透传给每个 ``hybrid_retriever.search`` 与
                统一 ``rerank_and_expand``，使多库召回与 rerank 两个阶段都按该租户配置执行；
                未传（None）时各底层调用回退 contextvar（向后兼容）。

        Returns:
            MultiKBSearchResult，包含检索结果和降级状态信息
        """
        expr = filters.to_milvus_expr() if filters else None

        # 总召回量保护:库数多时每库召回数适当收敛
        num_sources = len(kb_configs)
        source_top_k = self._compute_source_top_k(top_k, num_sources)

        # 并发限流:Semaphore 限制同时打到 Milvus 的源检索并发数
        sem = asyncio.Semaphore(self.max_concurrency)

        async def _guarded_search(cfg: KBRetrievalConfig) -> list[RetrievalResult]:
            """带并发限流和超时保护的单源检索

            合并"全局 ``filters.expr``"与"该源 ``cfg.expr``"为最终下传到底层
            ``HybridRetriever.search`` 的 expr：

            - 二者均非 None → ``"({global}) and ({cfg})"``（保留各自子表达式优先级）
            - 仅一方非 None → 用非空者
            - 二者均为 None → 传 ``None``（保留既有行为不变）

            会话文件源（``cfg.expr = 'session_id == "..."'``）由此叠加在全局 doc_id
            过滤之上，跨会话不会泄露；正式 KB 源 ``cfg.expr`` 默认 None，行为与
            bugfix 改造前一致。
            """
            if expr is not None and cfg.expr is not None:
                effective_expr = f"({expr}) and ({cfg.expr})"
            else:
                effective_expr = expr if expr is not None else cfg.expr
            async with sem:
                return await asyncio.wait_for(
                    self.retriever.search(
                        query, cfg.kb_id, top_k=source_top_k, skip_rerank=True,
                        expr=effective_expr, tenant_id=tenant_id,
                    ),
                    timeout=self.per_source_timeout,
                )

        tasks = [_guarded_search(cfg) for cfg in kb_configs]
        results_by_kb = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理结果（异常/超时的库返回空列表，记录失败信息）
        valid_results: dict[str, list[RetrievalResult]] = {}
        failed_kb_ids: list[str] = []

        for cfg, result in zip(kb_configs, results_by_kb):
            if isinstance(result, Exception):
                if isinstance(result, TimeoutError):
                    logger.warning(
                        "知识库 '%s' 检索超时（超过 %.1fs），返回空结果",
                        cfg.kb_id,
                        self.per_source_timeout,
                    )
                else:
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

        # 统一 Rerank + 父块扩展（显式下传 tenant_id，避免 rerank 阶段丢租户配置）
        results = await self.retriever.rerank_and_expand(query, merged, top_k, tenant_id=tenant_id)

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
