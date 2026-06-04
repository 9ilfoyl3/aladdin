"""MultiKBRetriever 并发限流 属性测试 + 单元测试 (H6)

Feature: retrieval-pipeline-hardening
Validates: Bug 4 (H6) — Property 3

Property 3: 任意源数 N 与上限 M，同时执行源检索数 ≤ M；
N=1 不被阻塞；任一源超时表现为该源失败不阻塞其余。
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
from hypothesis import given, settings, HealthCheck  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.multi_kb import (  # noqa: E402
    KBRetrievalConfig,
    MultiKBRetriever,
)


# ---------- Helpers ----------


def _make_result(chunk_id: str, score: float = 0.8) -> RetrievalResult:
    """创建简单的 RetrievalResult 用于测试"""
    return RetrievalResult(
        chunk_id=chunk_id, content=f"内容-{chunk_id}", score=score,
        doc_id="d1", metadata={},
    )


def _rerank_passthrough(query, results, top_k, tenant_id=None):
    """rerank_and_expand 的 mock：直接返回前 top_k 条"""
    return results[:top_k]


def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# Property 3: 并发限流属性测试
# ============================================================
# Feature: retrieval-pipeline-hardening, Property 3: 并发限流


@settings(max_examples=200, deadline=2000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    num_sources=st.integers(min_value=1, max_value=20),
    max_concurrency=st.integers(min_value=1, max_value=8),
)
def test_concurrency_never_exceeds_limit(num_sources, max_concurrency):
    """Property 3a: 对于任意源数 N 和上限 M，同时执行的源检索数 SHALL ≤ M。

    **Validates: Bug 4 (H6) — Property 3**
    """

    async def _test():
        concurrent_count = 0
        peak_concurrent = 0

        async def _slow_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            await asyncio.sleep(0.01)  # 模拟耗时，确保并发窗口重叠
            concurrent_count -= 1
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_slow_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=max_concurrency,
            per_source_timeout=5.0,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-{i}", priority=1.0)
            for i in range(num_sources)
        ]

        result = await retriever.search("查询", kb_configs, top_k=5)

        # 核心断言：峰值并发不超过 max_concurrency
        assert peak_concurrent <= max_concurrency
        # 所有源都被检索（成功的）
        assert mock_hybrid.search.call_count == num_sources
        # 结果正常返回
        assert result.degraded is False

    _run_async(_test())


@settings(max_examples=200, deadline=2000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    num_sources=st.integers(min_value=1, max_value=1),
    max_concurrency=st.integers(min_value=1, max_value=8),
)
def test_single_source_never_blocked(num_sources, max_concurrency):
    """Property 3b: N=1 时，无论 M 取何值，单源不被 Semaphore 阻塞。

    **Validates: Bug 4 (H6) — Property 3**
    """

    async def _test():
        async def _instant_search(query, kb_id, top_k=10, skip_rerank=False,
                                  expr=None, tenant_id=None):
            return [_make_result("c-single")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_instant_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=max_concurrency,
            per_source_timeout=5.0,
        )
        kb_configs = [KBRetrievalConfig(kb_id="kb-only", priority=1.0)]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        # 单源应极快完成，不被 Semaphore 阻塞
        assert elapsed < 1.0
        assert result.degraded is False
        assert len(result.results) == 1

    _run_async(_test())


@settings(max_examples=100, deadline=2000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    num_sources=st.integers(min_value=2, max_value=10),
    max_concurrency=st.integers(min_value=1, max_value=8),
    timeout_source_idx=st.integers(min_value=0, max_value=9),
)
def test_timeout_source_does_not_block_others(num_sources, max_concurrency, timeout_source_idx):
    """Property 3c: 任一源超时表现为该源失败，不阻塞其余源。

    **Validates: Bug 4 (H6) — Property 3**
    """
    # 确保 timeout_source_idx 在有效范围内
    timeout_source_idx = timeout_source_idx % num_sources

    async def _test():
        timeout_kb_id = f"kb-{timeout_source_idx}"

        async def _mixed_search(query, kb_id, top_k=10, skip_rerank=False,
                                expr=None, tenant_id=None):
            if kb_id == timeout_kb_id:
                await asyncio.sleep(10)  # 远超 timeout
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_mixed_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=max_concurrency,
            per_source_timeout=0.1,  # 100ms 超时
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-{i}", priority=1.0)
            for i in range(num_sources)
        ]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        # 总耗时应远小于超时源的 sleep(10)，说明不被阻塞
        assert elapsed < 2.0
        # 超时源被标记为失败
        assert result.degraded is True
        assert timeout_kb_id in result.failed_kb_ids
        # 其余源结果正常（至少有部分成功）
        # 非超时源的数量 = num_sources - 1
        successful_count = num_sources - 1
        # rerank_and_expand 被调用说明流程正常完成
        assert mock_hybrid.rerank_and_expand.call_count == 1

    _run_async(_test())


# ============================================================
# 单元测试：超时降级
# ============================================================


class TestTimeoutDegradation:
    """测试超时源的降级行为"""

    @pytest.mark.asyncio
    async def test_timeout_degrades_source(self):
        """某源超时(per_source_timeout) → 该源失败 + degraded=True + 其余正常返回"""

        async def _search_with_slow(query, kb_id, top_k=10, skip_rerank=False,
                                    expr=None, tenant_id=None):
            if kb_id == "kb-slow":
                await asyncio.sleep(10)  # 远超 timeout
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_search_with_slow)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=0.1,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-fast1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-fast2", priority=0.9),
            KBRetrievalConfig(kb_id="kb-slow", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=5)

        # 超时源被标记为失败
        assert result.degraded is True
        assert "kb-slow" in result.failed_kb_ids
        # 快速源结果正常返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c-kb-fast1" in chunk_ids
        assert "c-kb-fast2" in chunk_ids
        # 超时源的结果不在返回结果中
        assert "c-kb-slow" not in chunk_ids

    @pytest.mark.asyncio
    async def test_multiple_timeouts_all_marked_failed(self):
        """多个源同时超时，都被标记为失败"""

        async def _all_slow(query, kb_id, top_k=10, skip_rerank=False,
                            expr=None, tenant_id=None):
            if kb_id in ("kb-slow1", "kb-slow2"):
                await asyncio.sleep(10)
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_all_slow)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=0.1,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-ok", priority=1.0),
            KBRetrievalConfig(kb_id="kb-slow1", priority=0.8),
            KBRetrievalConfig(kb_id="kb-slow2", priority=0.7),
        ]

        result = await retriever.search("查询", kb_configs, top_k=5)

        assert result.degraded is True
        assert "kb-slow1" in result.failed_kb_ids
        assert "kb-slow2" in result.failed_kb_ids
        assert "kb-ok" not in result.failed_kb_ids


# ============================================================
# 单元测试：并发计数
# ============================================================


class TestConcurrencyCount:
    """测试并发计数不超过 max_concurrency"""

    @pytest.mark.asyncio
    async def test_peak_concurrency_with_many_sources(self):
        """10 源 max_concurrency=2，峰值并发不超过 2"""
        concurrent_count = 0
        peak_concurrent = 0

        async def _tracked_search(query, kb_id, top_k=10, skip_rerank=False,
                                  expr=None, tenant_id=None):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_tracked_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=2,
            per_source_timeout=5.0,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-{i}", priority=1.0)
            for i in range(10)
        ]

        await retriever.search("查询", kb_configs, top_k=5)

        assert peak_concurrent <= 2
        # 所有源都被检索
        assert mock_hybrid.search.call_count == 10

    @pytest.mark.asyncio
    async def test_peak_concurrency_with_max_1(self):
        """max_concurrency=1 时等价于串行"""
        concurrent_count = 0
        peak_concurrent = 0

        async def _tracked_search(query, kb_id, top_k=10, skip_rerank=False,
                                  expr=None, tenant_id=None):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            concurrent_count -= 1
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_tracked_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=1,
            per_source_timeout=5.0,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-{i}", priority=1.0)
            for i in range(5)
        ]

        await retriever.search("查询", kb_configs, top_k=5)

        # max_concurrency=1 → 峰值必为 1
        assert peak_concurrent == 1

    @pytest.mark.asyncio
    async def test_single_source_not_blocked_by_semaphore(self):
        """N=1 时不被 Semaphore 阻塞"""

        async def _fast_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            return [_make_result("c1")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_fast_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=5.0,
        )
        kb_configs = [KBRetrievalConfig(kb_id="kb-only", priority=1.0)]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0
        assert result.degraded is False
        assert len(result.results) == 1


# ============================================================
# 单元测试：_compute_source_top_k 收敛公式
# ============================================================


class TestComputeSourceTopK:
    """_compute_source_top_k 收敛公式验证"""

    def test_single_source_full_topk_times_3(self):
        """单库 = top_k*3"""
        assert MultiKBRetriever._compute_source_top_k(10, 1) == 30

    def test_four_sources_full_topk_times_3(self):
        """4库 = top_k*3（临界点，不收敛）"""
        assert MultiKBRetriever._compute_source_top_k(10, 4) == 30

    def test_eight_sources_converges(self):
        """8库 = top_k*3*4//8 = 15（适当收敛但 ≥ top_k）"""
        result = MultiKBRetriever._compute_source_top_k(10, 8)
        assert result == 15
        assert result >= 10  # 不低于 top_k

    def test_twenty_sources_floors_at_top_k(self):
        """20库：收敛到 top_k*3*4//20=6 → 兜底 max(10,6) = 10"""
        result = MultiKBRetriever._compute_source_top_k(10, 20)
        assert result >= 10  # 兜底不低于 top_k

    def test_hundred_sources_floors_at_top_k(self):
        """100库：极端收敛 → 兜底 top_k"""
        result = MultiKBRetriever._compute_source_top_k(10, 100)
        assert result == 10

    def test_never_below_top_k_for_any_input(self):
        """结果永远不低于 top_k（广泛验证）"""
        for num_sources in range(1, 50):
            for top_k in [5, 10, 20, 50, 100]:
                result = MultiKBRetriever._compute_source_top_k(top_k, num_sources)
                assert result >= top_k, (
                    f"_compute_source_top_k({top_k}, {num_sources}) = {result} < {top_k}"
                )

    def test_monotonically_non_increasing_with_more_sources(self):
        """源数增多时每源召回量单调不增（或持平）"""
        top_k = 10
        prev = MultiKBRetriever._compute_source_top_k(top_k, 1)
        for n in range(2, 30):
            curr = MultiKBRetriever._compute_source_top_k(top_k, n)
            assert curr <= prev, (
                f"_compute_source_top_k({top_k}, {n})={curr} > "
                f"_compute_source_top_k({top_k}, {n-1})={prev}"
            )
            prev = curr

    def test_top_k_20_with_six_sources(self):
        """top_k=20, 6库: 20*3*4//6=40"""
        result = MultiKBRetriever._compute_source_top_k(20, 6)
        assert result == 40

    def test_boundary_at_4_sources(self):
        """4库是 fast-path 临界点，5库开始收敛"""
        top_k = 10
        assert MultiKBRetriever._compute_source_top_k(top_k, 4) == 30
        assert MultiKBRetriever._compute_source_top_k(top_k, 5) == 24  # 10*3*4//5=24
