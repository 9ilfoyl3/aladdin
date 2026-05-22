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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
    async def test_search_calls_rerank_and_expand(self):
        """验证合并后调用 rerank_and_expand 进行统一 Rerank"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [self._make_result("c1", score=0.9)],
                [self._make_result("c2", score=0.7)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(
            side_effect=lambda query, results, top_k: results[:top_k]
        )

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
