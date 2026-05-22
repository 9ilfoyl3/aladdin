"""Agent 编排主控 - Plan-Execute-Reflect 流程

完整编排流程：路由 → 改写 → 迭代检索+反思
异常时回退到快路径直接检索。
支持进度回调，用于流式推送 Agent 思考过程。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field

from app.agent.executor import RetrievalExecutor
from app.agent.reflector import Reflector
from app.agent.rewriter import QueryRewriter
from app.agent.router import QueryRouter
from app.retrieval.base import BaseRetriever, RetrievalResult

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Agent 编排结果"""

    chunks: list[RetrievalResult]  # 检索结果列表
    iterations: int  # 迭代次数（快路径为 0）
    degraded: bool = False  # 是否降级


# 进度回调类型：接收 (step_name, detail) 参数
ProgressCallback = Callable[[str, str], Awaitable[None]]


class AgentOrchestrator:
    """Agent 编排器：路由 → 改写 → 迭代检索+反思，异常时降级"""

    def __init__(
        self,
        router: QueryRouter,
        rewriter: QueryRewriter,
        executor: RetrievalExecutor,
        reflector: Reflector,
        retriever: BaseRetriever,  # 快路径直接检索
        max_iterations: int = 3,
        timeout: float = 0,  # 保留参数兼容性，不再使用
    ):
        self.router = router
        self.rewriter = rewriter
        self.executor = executor
        self.reflector = reflector
        self.retriever = retriever
        self.max_iterations = max_iterations

    async def run(
        self, query: str, kb_id: str, on_progress: ProgressCallback | None = None,
        expr: str | None = None,
    ) -> AgentResult:
        """执行完整 Agent 编排流程，异常时降级到快路径"""
        try:
            return await self._agent_flow(query, kb_id, on_progress, expr=expr)
        except Exception as e:
            print(f"[Agent] 异常降级: query={query!r}, error={e}")
            return await self._fast_path(query, kb_id, degraded=True, expr=expr)

    async def _emit(self, on_progress: ProgressCallback | None, step: str, detail: str):
        """发送进度通知"""
        if on_progress:
            await on_progress(step, detail)

    async def _agent_flow(
        self, query: str, kb_id: str, on_progress: ProgressCallback | None,
        expr: str | None = None,
    ) -> AgentResult:
        """完整 Agent 编排逻辑"""
        # 重置 executor 查询缓存（每次新编排开始时）
        self.executor.reset_cache()

        # 1. 路由 + 改写并行执行
        await self._emit(on_progress, "routing", "正在分析问题类型...")

        route_task = asyncio.create_task(self.router.classify(query))
        rewrite_task = asyncio.create_task(self.rewriter.rewrite(query))

        route = await route_task
        print(f"[Agent] 路由判定: query={query!r} -> route={route}")

        if route == "simple":
            # simple 路由不需要改写结果，取消任务
            rewrite_task.cancel()
            await self._emit(on_progress, "routing_done", "简单查询，直接检索")
            return await self._fast_path(query, kb_id, expr=expr)

        await self._emit(on_progress, "routing_done", "判定为复杂查询，启动深度检索")

        # 等待改写结果（大概率已经完成了）
        rewritten_queries = await rewrite_task
        print(f"[Agent] 查询改写: {query!r} -> {rewritten_queries}")
        queries_text = "、".join(f"「{q}」" for q in rewritten_queries)
        await self._emit(on_progress, "rewriting_done", f"查询改写为：{queries_text}")

        # 3. 迭代检索+反思
        all_chunk_ids: set[str] = set()
        results: list[RetrievalResult] = []
        iterations = 0
        prev_coverage: float = 0.0

        for i in range(self.max_iterations):
            await self._emit(on_progress, "retrieving", f"第 {i+1} 轮检索中...")
            new_results = await self.executor.execute(rewritten_queries, kb_id, expr=expr)
            print(f"[Agent] 第 {i+1} 轮检索: 查询={rewritten_queries}, 返回 {len(new_results)} 条结果")

            # 去重：只保留之前没见过的结果
            novel_results = [r for r in new_results if r.chunk_id not in all_chunk_ids]
            for r in new_results:
                all_chunk_ids.add(r.chunk_id)
            results.extend(novel_results)

            await self._emit(on_progress, "retrieving_done", f"第 {i+1} 轮检索完成，获得 {len(new_results)} 条结果（新增 {len(novel_results)} 条）")

            # 无新增结果 → 知识库已穷尽，终止
            if not novel_results and i > 0:
                iterations = i + 1
                await self._emit(on_progress, "done", f"未检索到新信息，终止迭代")
                print(f"[Agent] 第 {i+1} 轮无新增结果，提前终止")
                break

            # 最后一轮：不调用 Reflector，直接返回结果
            if i == self.max_iterations - 1:
                iterations = i + 1
                await self._emit(on_progress, "done", f"达到最大迭代次数，返回当前结果")
                break

            await self._emit(on_progress, "reflecting", f"正在评估检索结果质量...")
            verdict = await self.reflector.evaluate(query, results)
            print(
                f"[Agent] 第 {i+1} 轮反思: sufficient={verdict.is_sufficient}, "
                f"relevance={verdict.relevance_score:.2f}, coverage={verdict.coverage_score:.2f}, "
                f"reasoning={verdict.reasoning!r}, follow_up={verdict.follow_up_queries}"
            )
            iterations = i + 1

            if verdict.is_sufficient:
                await self._emit(
                    on_progress, "done",
                    f"结果充分（相关性 {verdict.relevance_score:.0%}，覆盖度 {verdict.coverage_score:.0%}）：{verdict.reasoning}"
                )
                break

            # 置信度阈值：覆盖度提升不足 10% → 继续迭代无意义，终止
            coverage_gain = verdict.coverage_score - prev_coverage
            prev_coverage = verdict.coverage_score

            if i > 0 and coverage_gain < 0.1:
                await self._emit(
                    on_progress, "done",
                    f"覆盖度无明显提升（{verdict.coverage_score:.0%}，增幅 {coverage_gain:+.0%}），终止迭代"
                )
                print(f"[Agent] 覆盖度增幅不足 ({coverage_gain:.2f})，提前终止")
                break

            # 继续迭代
            follow_up_text = "、".join(f"「{q}」" for q in verdict.follow_up_queries)
            await self._emit(
                on_progress, "reflecting_done",
                f"结果不充分（相关性 {verdict.relevance_score:.0%}，覆盖度 {verdict.coverage_score:.0%}），追加查询：{follow_up_text}"
            )
            rewritten_queries = verdict.follow_up_queries

        await self._emit(on_progress, "done", f"检索完成，共 {iterations} 轮迭代，最终 {len(results)} 条结果")
        print(f"[Agent] 完成: 共 {iterations} 轮迭代, 最终 {len(results)} 条结果")

        # 按分数降序排列，截断到 top-30
        results.sort(key=lambda x: x.score, reverse=True)
        results = results[:30]

        return AgentResult(chunks=results, iterations=iterations)

    async def _fast_path(
        self, query: str, kb_id: str, degraded: bool = False, expr: str | None = None,
    ) -> AgentResult:
        """快路径：直接使用 retriever 检索"""
        results = await self.retriever.search(query, kb_id, top_k=30, expr=expr)
        return AgentResult(chunks=results, iterations=0, degraded=degraded)
