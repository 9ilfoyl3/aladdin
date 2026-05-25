"""Agent 编排主控 - Plan-Execute-Reflect 流程

完整编排流程：规划（意图拆分）→ 分组检索 → 迭代反思
异常时回退到快路径直接检索。
支持进度回调，用于流式推送 Agent 思考过程。

v2 架构（参考 WeKnora ReAct Agent 思路）：
- Planner 替代 Router + Rewriter，一次性完成意图拆分和查询生成
- 按意图组并行检索，确保每个意图都有独立的检索路径
- 结果合并时保证多样性（每个意图组保留最低配额）
- Reflector 感知意图覆盖度
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field

from app.agent.executor import RetrievalExecutor
from app.agent.planner import QueryPlanner, QueryPlan, IntentGroup
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


# 每个意图组在最终结果中的最低保留数量
_MIN_RESULTS_PER_INTENT = 5


class AgentOrchestrator:
    """Agent 编排器：规划 → 分组检索 → 迭代反思，异常时降级

    v2 架构支持两种模式：
    - 有 Planner 时：使用意图拆分 + 分组检索（推荐）
    - 无 Planner 时：回退到 Router + Rewriter 旧模式（兼容）
    """

    def __init__(
        self,
        router: QueryRouter,
        rewriter: QueryRewriter,
        executor: RetrievalExecutor,
        reflector: Reflector,
        retriever: BaseRetriever,  # 快路径直接检索
        max_iterations: int = 3,
        timeout: float = 0,  # 保留参数兼容性，不再使用
        planner: QueryPlanner | None = None,  # v2: 意图规划器
    ):
        self.router = router
        self.rewriter = rewriter
        self.executor = executor
        self.reflector = reflector
        self.retriever = retriever
        self.max_iterations = max_iterations
        self.planner = planner

    async def run(
        self, query: str, kb_id: str, on_progress: ProgressCallback | None = None,
        expr: str | None = None,
    ) -> AgentResult:
        """执行完整 Agent 编排流程，异常时降级到快路径"""
        try:
            if self.planner:
                return await self._agent_flow_v2(query, kb_id, on_progress, expr=expr)
            return await self._agent_flow_v1(query, kb_id, on_progress, expr=expr)
        except Exception as e:
            print(f"[Agent] 异常降级: query={query!r}, error={e}")
            return await self._fast_path(query, kb_id, degraded=True, expr=expr)

    async def _emit(self, on_progress: ProgressCallback | None, step: str, detail: str):
        """发送进度通知"""
        if on_progress:
            await on_progress(step, detail)

    # ================================================================
    # v2 流程：Planner 意图拆分 + 分组检索
    # ================================================================

    async def _agent_flow_v2(
        self, query: str, kb_id: str, on_progress: ProgressCallback | None,
        expr: str | None = None,
    ) -> AgentResult:
        """v2 编排逻辑：意图拆分 → 分组并行检索 → 意图感知反思"""
        self.executor.reset_cache()

        # 1. 规划：意图拆分 + 查询生成
        await self._emit(on_progress, "routing", "正在分析问题类型...")

        plan = await self.planner.plan(query)
        print(f"[Agent-v2] 规划完成: complexity={plan.complexity}, "
              f"intent_groups={len(plan.intent_groups)}")

        if plan.complexity == "simple":
            await self._emit(on_progress, "routing_done", "简单查询，直接检索")
            return await self._fast_path(query, kb_id, expr=expr)

        # 展示规划结果
        intents_text = "、".join(f"「{g.intent}」" for g in plan.intent_groups)
        await self._emit(on_progress, "routing_done", f"判定为复杂查询，识别到 {len(plan.intent_groups)} 个意图：{intents_text}")

        all_queries = []
        for g in plan.intent_groups:
            all_queries.extend(g.queries)
        queries_text = "、".join(f"「{q}」" for q in all_queries)
        await self._emit(on_progress, "rewriting_done", f"查询改写为：{queries_text}")

        # 2. 分组并行检索
        all_chunk_ids: set[str] = set()
        # 按意图组存储结果，用于最终合并时保证多样性
        intent_results: dict[str, list[RetrievalResult]] = {
            g.intent: [] for g in plan.intent_groups
        }
        all_results: list[RetrievalResult] = []
        iterations = 0

        for i in range(self.max_iterations):
            await self._emit(on_progress, "retrieving", f"第 {i+1} 轮检索中...")

            # 按意图组并行检索
            group_tasks = []
            for group in plan.intent_groups:
                if group.queries:  # 只检索有查询的意图组
                    group_tasks.append(
                        self._retrieve_for_intent(group, kb_id, expr)
                    )

            group_results_list = await asyncio.gather(*group_tasks)

            # 收集本轮新增结果
            round_novel_count = 0
            for group, group_results in zip(plan.intent_groups, group_results_list):
                for r in group_results:
                    if r.chunk_id not in all_chunk_ids:
                        all_chunk_ids.add(r.chunk_id)
                        intent_results[group.intent].append(r)
                        all_results.append(r)
                        round_novel_count += 1

            total_results = len(all_results)
            print(f"[Agent-v2] 第 {i+1} 轮检索完成: 新增 {round_novel_count} 条，累计 {total_results} 条")

            # 展示各意图组的结果数
            group_stats = ", ".join(
                f"{g.intent}({len(intent_results[g.intent])}条)"
                for g in plan.intent_groups
            )
            await self._emit(
                on_progress, "retrieving_done",
                f"第 {i+1} 轮检索完成，新增 {round_novel_count} 条（{group_stats}）"
            )

            # 无新增结果 → 知识库已穷尽
            if round_novel_count == 0 and i > 0:
                iterations = i + 1
                await self._emit(on_progress, "done", "未检索到新信息，终止迭代")
                break

            # 最后一轮不反思
            if i == self.max_iterations - 1:
                iterations = i + 1
                await self._emit(on_progress, "done", "达到最大迭代次数，返回当前结果")
                break

            # 3. 意图感知反思
            await self._emit(on_progress, "reflecting", "正在评估检索结果质量...")

            # 传入意图组信息，让 Reflector 检查每个意图的覆盖情况
            intent_names = [g.intent for g in plan.intent_groups]
            verdict = await self.reflector.evaluate(
                query, all_results, intent_names=intent_names
            )
            print(
                f"[Agent-v2] 第 {i+1} 轮反思: sufficient={verdict.is_sufficient}, "
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

            # 用 follow_up_queries 更新对应意图组的查询
            if verdict.follow_up_queries:
                # 将追加查询分配到覆盖不足的意图组
                self._update_intent_queries(plan, verdict.follow_up_queries, intent_results)
                follow_up_text = "、".join(f"「{q}」" for q in verdict.follow_up_queries)
                await self._emit(
                    on_progress, "reflecting_done",
                    f"结果不充分（相关性 {verdict.relevance_score:.0%}，覆盖度 {verdict.coverage_score:.0%}），追加查询：{follow_up_text}"
                )
            else:
                iterations = i + 1
                await self._emit(on_progress, "done", "无追加查询，终止迭代")
                break

        await self._emit(on_progress, "done", f"检索完成，共 {iterations} 轮迭代，最终 {len(all_results)} 条结果")
        print(f"[Agent-v2] 完成: 共 {iterations} 轮迭代, 最终 {len(all_results)} 条结果")

        # 4. 多样性合并：确保每个意图组都有最低配额的结果
        final_results = self._merge_with_diversity(plan.intent_groups, intent_results, top_k=30)

        return AgentResult(chunks=final_results, iterations=iterations)

    async def _retrieve_for_intent(
        self, group: IntentGroup, kb_id: str, expr: str | None
    ) -> list[RetrievalResult]:
        """为单个意图组执行检索"""
        return await self.executor.execute(group.queries, kb_id, expr=expr)

    def _update_intent_queries(
        self, plan: QueryPlan, follow_up_queries: list[str],
        intent_results: dict[str, list[RetrievalResult]],
    ):
        """将追加查询分配到覆盖不足的意图组

        策略：找到结果最少的意图组，将追加查询分配给它
        """
        # 找到结果最少的意图组
        min_group = min(plan.intent_groups, key=lambda g: len(intent_results[g.intent]))
        # 将追加查询替换到该意图组
        min_group.queries = follow_up_queries
        # 其他意图组清空查询（避免重复检索已有结果）
        for g in plan.intent_groups:
            if g is not min_group:
                g.queries = []

    def _merge_with_diversity(
        self, intent_groups: list[IntentGroup],
        intent_results: dict[str, list[RetrievalResult]],
        top_k: int = 30,
    ) -> list[RetrievalResult]:
        """多样性合并：确保每个意图组都有最低配额的结果进入最终输出

        策略（参考 WeKnora MMR 思路）：
        1. 每个意图组按分数排序，保留 top-N 作为保底
        2. 剩余名额按全局分数排序填充
        """
        num_groups = len(intent_groups)
        if num_groups == 0:
            return []

        # 每个意图组的保底配额
        min_per_group = min(_MIN_RESULTS_PER_INTENT, top_k // num_groups)

        # 第一步：每个意图组取 top-N 保底
        reserved: list[RetrievalResult] = []
        reserved_ids: set[str] = set()
        remaining_pool: list[RetrievalResult] = []

        for group in intent_groups:
            group_sorted = sorted(
                intent_results[group.intent],
                key=lambda x: x.score, reverse=True
            )
            # 取保底配额
            for r in group_sorted[:min_per_group]:
                if r.chunk_id not in reserved_ids:
                    reserved.append(r)
                    reserved_ids.add(r.chunk_id)
            # 剩余放入全局池
            for r in group_sorted[min_per_group:]:
                if r.chunk_id not in reserved_ids:
                    remaining_pool.append(r)

        # 第二步：剩余名额按全局分数填充
        remaining_pool.sort(key=lambda x: x.score, reverse=True)
        remaining_quota = top_k - len(reserved)

        for r in remaining_pool[:remaining_quota]:
            reserved.append(r)

        # 最终按分数排序
        reserved.sort(key=lambda x: x.score, reverse=True)
        return reserved[:top_k]

    # ================================================================
    # v1 流程：Router + Rewriter（兼容旧模式）
    # ================================================================

    async def _agent_flow_v1(
        self, query: str, kb_id: str, on_progress: ProgressCallback | None,
        expr: str | None = None,
    ) -> AgentResult:
        """v1 编排逻辑（兼容旧模式）"""
        self.executor.reset_cache()

        # 1. 路由 + 改写并行执行
        await self._emit(on_progress, "routing", "正在分析问题类型...")

        route_task = asyncio.create_task(self.router.classify(query))
        rewrite_task = asyncio.create_task(self.rewriter.rewrite(query))

        route = await route_task
        print(f"[Agent] 路由判定: query={query!r} -> route={route}")

        if route == "simple":
            rewrite_task.cancel()
            await self._emit(on_progress, "routing_done", "简单查询，直接检索")
            return await self._fast_path(query, kb_id, expr=expr)

        await self._emit(on_progress, "routing_done", "判定为复杂查询，启动深度检索")

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

            novel_results = [r for r in new_results if r.chunk_id not in all_chunk_ids]
            for r in new_results:
                all_chunk_ids.add(r.chunk_id)
            results.extend(novel_results)

            await self._emit(on_progress, "retrieving_done", f"第 {i+1} 轮检索完成，获得 {len(new_results)} 条结果（新增 {len(novel_results)} 条）")

            if not novel_results and i > 0:
                iterations = i + 1
                await self._emit(on_progress, "done", "未检索到新信息，终止迭代")
                print(f"[Agent] 第 {i+1} 轮无新增结果，提前终止")
                break

            if i == self.max_iterations - 1:
                iterations = i + 1
                await self._emit(on_progress, "done", "达到最大迭代次数，返回当前结果")
                break

            await self._emit(on_progress, "reflecting", "正在评估检索结果质量...")
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

            coverage_gain = verdict.coverage_score - prev_coverage
            prev_coverage = verdict.coverage_score

            if i > 0 and coverage_gain < 0.1:
                await self._emit(
                    on_progress, "done",
                    f"覆盖度无明显提升（{verdict.coverage_score:.0%}，增幅 {coverage_gain:+.0%}），终止迭代"
                )
                print(f"[Agent] 覆盖度增幅不足 ({coverage_gain:.2f})，提前终止")
                break

            follow_up_text = "、".join(f"「{q}」" for q in verdict.follow_up_queries)
            await self._emit(
                on_progress, "reflecting_done",
                f"结果不充分（相关性 {verdict.relevance_score:.0%}，覆盖度 {verdict.coverage_score:.0%}），追加查询：{follow_up_text}"
            )
            rewritten_queries = verdict.follow_up_queries

        await self._emit(on_progress, "done", f"检索完成，共 {iterations} 轮迭代，最终 {len(results)} 条结果")
        print(f"[Agent] 完成: 共 {iterations} 轮迭代, 最终 {len(results)} 条结果")

        results.sort(key=lambda x: x.score, reverse=True)
        results = results[:30]

        return AgentResult(chunks=results, iterations=iterations)

    async def _fast_path(
        self, query: str, kb_id: str, degraded: bool = False, expr: str | None = None,
    ) -> AgentResult:
        """快路径：直接使用 retriever 检索"""
        results = await self.retriever.search(query, kb_id, top_k=30, expr=expr)
        return AgentResult(chunks=results, iterations=0, degraded=degraded)
