"""MultiKBRetriever 单元测试

测试并行检索、加权合并、部分失败容错。
"""

import sys
from unittest.mock import AsyncMock, MagicMock, call

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.multi_kb import (  # noqa: E402
    KBRetrievalConfig,
    MultiKBRetriever,
    MultiKBSearchResult,
)


# rerank_and_expand 的 mock side_effect：与生产签名一致
# （query, results, top_k, tenant_id=None），返回前 top_k 条。
def _rerank_passthrough(query, results, top_k, tenant_id=None):
    return results[:top_k]


# ============================================================
# KBRetrievalConfig 测试
# ============================================================


class TestKBRetrievalConfig:
    """KBRetrievalConfig 数据类测试"""

    def test_default_priority_is_1(self):
        """默认 priority 为 1.0"""
        config = KBRetrievalConfig(kb_id="kb-main")
        assert config.priority == 1.0

    def test_custom_priority(self):
        """自定义 priority 被正确设置"""
        config = KBRetrievalConfig(kb_id="kb-aux", priority=0.8)
        assert config.kb_id == "kb-aux"
        assert config.priority == 0.8


# ============================================================
# _weighted_merge 测试
# ============================================================


class TestWeightedMerge:
    """测试 _weighted_merge 加权合并逻辑"""

    def _make_retriever(self) -> MultiKBRetriever:
        """创建一个带 mock HybridRetriever 的 MultiKBRetriever"""
        mock_hybrid = MagicMock()
        return MultiKBRetriever(hybrid_retriever=mock_hybrid)

    def test_primary_kb_higher_boosted_score(self):
        """主库 (priority=1.0) 结果的加权分数高于辅助库 (priority=0.8)"""
        retriever = self._make_retriever()

        results_by_kb = {
            "kb-main": [
                RetrievalResult(
                    chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={}
                ),
            ],
            "kb-aux": [
                RetrievalResult(
                    chunk_id="c2", content="内容2", score=0.9, doc_id="d2", metadata={}
                ),
            ],
        }
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        merged = retriever._weighted_merge(results_by_kb, kb_configs)

        # c1 的加权分数 = 0.9 * 1.0 = 0.9
        # c2 的加权分数 = 0.9 * 0.8 = 0.72
        # c1 应排在 c2 前面
        assert merged[0].chunk_id == "c1"
        assert merged[1].chunk_id == "c2"

    def test_same_chunk_id_keeps_highest_boosted_score(self):
        """相同 chunk_id 从多个 KB 出现时，保留最高加权分数"""
        retriever = self._make_retriever()

        results_by_kb = {
            "kb-main": [
                RetrievalResult(
                    chunk_id="c1", content="内容1", score=0.5, doc_id="d1", metadata={}
                ),
            ],
            "kb-aux": [
                RetrievalResult(
                    chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={}
                ),
            ],
        }
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        merged = retriever._weighted_merge(results_by_kb, kb_configs)

        # c1 在 kb-main 中加权分数 = 0.5 * 1.0 = 0.5
        # c1 在 kb-aux 中加权分数 = 0.9 * 0.8 = 0.72
        # 应保留最高加权分数 0.72
        assert len(merged) == 1
        assert merged[0].chunk_id == "c1"
        # 保留的是 kb-aux 中的结果（因为加权分数更高）
        assert merged[0].score == 0.9

    def test_results_sorted_by_boosted_score_descending(self):
        """结果按加权分数降序排列"""
        retriever = self._make_retriever()

        results_by_kb = {
            "kb-main": [
                RetrievalResult(
                    chunk_id="c1", content="内容1", score=0.3, doc_id="d1", metadata={}
                ),
                RetrievalResult(
                    chunk_id="c2", content="内容2", score=0.9, doc_id="d1", metadata={}
                ),
            ],
            "kb-aux": [
                RetrievalResult(
                    chunk_id="c3", content="内容3", score=0.7, doc_id="d2", metadata={}
                ),
            ],
        }
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        merged = retriever._weighted_merge(results_by_kb, kb_configs)

        # c1: 0.3 * 1.0 = 0.3
        # c2: 0.9 * 1.0 = 0.9
        # c3: 0.7 * 0.8 = 0.56
        # 排序: c2 (0.9) > c3 (0.56) > c1 (0.3)
        assert merged[0].chunk_id == "c2"
        assert merged[1].chunk_id == "c3"
        assert merged[2].chunk_id == "c1"

    def test_empty_results_from_kb_no_error(self):
        """某个 KB 返回空结果不会导致错误"""
        retriever = self._make_retriever()

        results_by_kb = {
            "kb-main": [
                RetrievalResult(
                    chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={}
                ),
            ],
            "kb-aux": [],
        }
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        merged = retriever._weighted_merge(results_by_kb, kb_configs)

        assert len(merged) == 1
        assert merged[0].chunk_id == "c1"


# ============================================================
# search() 测试（mock HybridRetriever）
# ============================================================


class TestMultiKBRetrieverSearch:
    """测试 MultiKBRetriever.search() 方法"""

    def _make_result(self, chunk_id: str, score: float = 0.8) -> RetrievalResult:
        """辅助方法：创建 RetrievalResult"""
        return RetrievalResult(
            chunk_id=chunk_id,
            content=f"内容-{chunk_id}",
            score=score,
            doc_id="d1",
            metadata={},
        )

    @pytest.mark.asyncio
    async def test_parallel_retrieval_all_kbs_searched(self):
        """并行检索：所有 KB 都被搜索"""
        mock_hybrid = AsyncMock()
        # search 被调用时返回不同结果
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1")],
                [self._make_result("c2")],
                [self._make_result("c3")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-2", priority=0.8),
            KBRetrievalConfig(kb_id="kb-3", priority=0.7),
        ]

        await retriever.search("查询", kb_configs, top_k=10)

        # 验证 search 被调用了 3 次（每个 KB 一次）
        assert mock_hybrid.search.call_count == 3
        # 验证每个 KB 都被搜索
        called_kb_ids = [
            call.kwargs.get("kb_id", call.args[1] if len(call.args) > 1 else None)
            for call in mock_hybrid.search.call_args_list
        ]
        # search(query, kb_id, top_k=..., skip_rerank=True, expr=...)
        actual_kb_ids = [call.args[1] for call in mock_hybrid.search.call_args_list]
        assert set(actual_kb_ids) == {"kb-1", "kb-2", "kb-3"}

    @pytest.mark.asyncio
    async def test_results_merged_and_reranked(self):
        """结果被合并并经过 rerank"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1", score=0.9)],
                [self._make_result("c2", score=0.7)],
            ]
        )
        # rerank_and_expand 返回传入的结果（模拟 rerank 不改变顺序）
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        # rerank_and_expand 应被调用一次
        assert mock_hybrid.rerank_and_expand.call_count == 1
        # 结果应包含两个 KB 的内容
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c2" in chunk_ids

    @pytest.mark.asyncio
    async def test_kb_failure_sets_degraded_true(self):
        """当某个 KB 检索失败时，degraded=True"""
        mock_hybrid = AsyncMock()
        # 第一个 KB 正常返回，第二个 KB 抛出异常
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1")],
                RuntimeError("Milvus connection timeout"),
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-broken", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_kb_failure_other_results_still_returned(self):
        """当某个 KB 失败时，其他 KB 的结果仍然正常返回"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1"), self._make_result("c2")],
                RuntimeError("Collection not found"),
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-missing", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        # 主库的结果应正常返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c2" in chunk_ids

    @pytest.mark.asyncio
    async def test_failed_kb_ids_contains_failed_kbs(self):
        """failed_kb_ids 包含失败的知识库 ID"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1")],
                RuntimeError("timeout"),
                RuntimeError("not found"),
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-ok", priority=1.0),
            KBRetrievalConfig(kb_id="kb-timeout", priority=0.8),
            KBRetrievalConfig(kb_id="kb-missing", priority=0.7),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert set(result.failed_kb_ids) == {"kb-timeout", "kb-missing"}
        assert result.degraded is True


    @pytest.mark.asyncio
    async def test_search_passes_skip_rerank_true(self):
        """验证每个 KB 的 search 调用都传递了 skip_rerank=True"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1")],
                [self._make_result("c2")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        await retriever.search("查询", kb_configs, top_k=5)

        # 验证每次 search 调用都传递了 skip_rerank=True
        for c in mock_hybrid.search.call_args_list:
            assert c.kwargs.get("skip_rerank") is True

    @pytest.mark.asyncio
    async def test_search_passes_expr_from_filters(self):
        """验证 filters 转换为 expr 并传递给 retriever.search"""
        from app.retrieval.filter import RetrievalFilter

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(return_value=[self._make_result("c1")])
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [KBRetrievalConfig(kb_id="kb-main", priority=1.0)]
        filters = RetrievalFilter(doc_ids=["doc-001", "doc-002"])

        await retriever.search("查询", kb_configs, top_k=5, filters=filters)

        # 验证 expr 被传递给 search
        passed_expr = mock_hybrid.search.call_args_list[0].kwargs.get("expr")
        assert passed_expr is not None
        assert "doc_id" in passed_expr
        assert "doc-001" in passed_expr
        assert "doc-002" in passed_expr

    @pytest.mark.asyncio
    async def test_search_passes_tenant_id_to_subcalls(self):
        """显式 tenant_id 透传给每个 hybrid.search 与 rerank_and_expand（H5）。"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1")],
                [self._make_result("c2")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        await retriever.search("查询", kb_configs, top_k=5, tenant_id="tenant-A")

        # 每个 KB 的 search 都收到显式 tenant_id
        for c in mock_hybrid.search.call_args_list:
            assert c.kwargs.get("tenant_id") == "tenant-A"
        # 统一 rerank 也收到显式 tenant_id
        assert mock_hybrid.rerank_and_expand.call_args.kwargs.get("tenant_id") == "tenant-A"

    @pytest.mark.asyncio
    async def test_search_calls_rerank_and_expand(self):
        """验证合并后调用 rerank_and_expand 进行统一 Rerank"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1", score=0.9)],
                [self._make_result("c2", score=0.7)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        await retriever.search("测试查询", kb_configs, top_k=3)

        # rerank_and_expand 应被调用一次
        mock_hybrid.rerank_and_expand.assert_called_once()
        # 验证参数
        args = mock_hybrid.rerank_and_expand.call_args[0]
        assert args[0] == "测试查询"  # query
        assert len(args[1]) == 2  # merged results (c1 + c2)
        assert args[2] == 3  # top_k


# ============================================================
# H6 并发限流 单元测试
# ============================================================


class TestMultiKBConcurrencyLimit:
    """测试 MultiKBRetriever 的并发限流（H6）"""

    def _make_result(self, chunk_id: str, score: float = 0.8) -> RetrievalResult:
        return RetrievalResult(
            chunk_id=chunk_id, content=f"内容-{chunk_id}", score=score,
            doc_id="d1", metadata={},
        )

    @pytest.mark.asyncio
    async def test_concurrency_does_not_exceed_max(self):
        """同时执行的源检索数不超过 max_concurrency"""
        import asyncio

        max_concurrency = 2
        concurrent_count = 0
        peak_concurrent = 0

        async def _slow_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            await asyncio.sleep(0.05)  # 模拟耗时
            concurrent_count -= 1
            return [self._make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_slow_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=max_concurrency,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-{i}", priority=1.0) for i in range(6)
        ]

        await retriever.search("查询", kb_configs, top_k=5)

        # 峰值并发不超过 max_concurrency
        assert peak_concurrent <= max_concurrency

    @pytest.mark.asyncio
    async def test_single_source_not_blocked(self):
        """单源时 Semaphore 不构成阻塞（fast-path）"""
        import asyncio
        import time

        async def _fast_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            return [self._make_result("c1")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_fast_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid, max_concurrency=4)
        kb_configs = [KBRetrievalConfig(kb_id="kb-only", priority=1.0)]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        # 单源应极快完成，无可感知延迟
        assert elapsed < 1.0
        assert result.degraded is False

    @pytest.mark.asyncio
    async def test_timeout_source_treated_as_failure(self):
        """超时的源被当作失败处理（degraded + failed_kb_ids）"""
        import asyncio

        async def _timeout_search(query, kb_id, top_k=10, skip_rerank=False,
                                  expr=None, tenant_id=None):
            if kb_id == "kb-slow":
                await asyncio.sleep(10)  # 远超 timeout
            return [self._make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_timeout_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=0.1,  # 100ms 超时
        )
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-fast", priority=1.0),
            KBRetrievalConfig(kb_id="kb-slow", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=5)

        assert result.degraded is True
        assert "kb-slow" in result.failed_kb_ids
        # 快速源的结果仍然返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c-kb-fast" in chunk_ids

    @pytest.mark.asyncio
    async def test_timeout_does_not_block_other_sources(self):
        """一个源超时不阻塞其他源的检索"""
        import asyncio
        import time

        async def _mixed_search(query, kb_id, top_k=10, skip_rerank=False,
                                expr=None, tenant_id=None):
            if kb_id == "kb-slow":
                await asyncio.sleep(10)  # 远超 timeout
            return [self._make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_mixed_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=0.2,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-fast1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-fast2", priority=0.9),
            KBRetrievalConfig(kb_id="kb-slow", priority=0.8),
        ]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        # 总耗时应约等于 per_source_timeout（超时源的耗时），不是 10s
        assert elapsed < 1.0
        # 快速源结果正常
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c-kb-fast1" in chunk_ids
        assert "c-kb-fast2" in chunk_ids

    @pytest.mark.asyncio
    async def test_default_max_concurrency_is_4(self):
        """默认 max_concurrency 为 4"""
        mock_hybrid = AsyncMock()
        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        assert retriever.max_concurrency == 4

    @pytest.mark.asyncio
    async def test_default_per_source_timeout_is_30(self):
        """默认 per_source_timeout 为 30s"""
        mock_hybrid = AsyncMock()
        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        assert retriever.per_source_timeout == 30.0


# ============================================================
# H6 召回量收敛公式 单元测试
# ============================================================


class TestSourceTopKScaling:
    """测试 _compute_source_top_k 召回量收敛公式"""

    def test_single_source_gets_full_topk_times_3(self):
        """单源获得 top_k*3（不劣化）"""
        assert MultiKBRetriever._compute_source_top_k(10, 1) == 30

    def test_four_sources_gets_full_topk_times_3(self):
        """4 库时仍获得 top_k*3（fast-path 临界点）"""
        assert MultiKBRetriever._compute_source_top_k(10, 4) == 30

    def test_eight_sources_converges(self):
        """8 库时每库召回收敛（top_k*3*4//8 = top_k*1.5）"""
        result = MultiKBRetriever._compute_source_top_k(10, 8)
        # 10 * 3 * 4 // 8 = 15
        assert result == 15

    def test_many_sources_floors_at_top_k(self):
        """库数很多时，每库召回不低于 top_k"""
        result = MultiKBRetriever._compute_source_top_k(10, 100)
        # 10 * 3 * 4 // 100 = 1 → 兜底 max(10, 1) = 10
        assert result == 10

    def test_top_k_20_with_six_sources(self):
        """top_k=20, 6 库: 20*3*4//6=40"""
        result = MultiKBRetriever._compute_source_top_k(20, 6)
        assert result == 40

    def test_never_below_top_k(self):
        """结果永远不低于 top_k"""
        for num_sources in range(1, 50):
            for top_k in [5, 10, 20, 50]:
                result = MultiKBRetriever._compute_source_top_k(top_k, num_sources)
                assert result >= top_k
