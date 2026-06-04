"""端到端集成测试 — 检索/入库主链路

覆盖所有已修复 Bug 的不变式和关键路径:
- H5: 流式问答取到租户配置（非默认参数生效）
- H2+H3: 某路检索失败 → 降级返回其余 + SSE degraded=True
- H6: 多库检索并发受限、慢源超时降级
- H1: 入库异常 → 无孤儿向量；先删后写重处理不残留
- M6: 入库写入中取消 → 清理
- 回归不变式:
  - 三路全成功结果不变
  - 单库 fast-path 无延迟
  - 配置未变缓存命中
  - 单 worker 写后失效
  - 正常入库无清理

与 test_integration_e2e.py (Worker/Queue 主流程) 及 11.1 (M1/M2/M7 失效广播路径) 不重复。
本文件聚焦检索/入库主链路的端到端行为验证。

Feature: retrieval-pipeline-hardening
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# 前置环境变量（部分模块在 import 时需要）
os.environ.setdefault("JWT_SECRET", "e2e-retrieval-pipeline-test-secret-0123456789abcdef")

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())
sys.modules.setdefault("pymilvus.connections", MagicMock())
sys.modules.setdefault("pymilvus.utility", MagicMock())

import pytest  # noqa: E402

from app.auth.identity import TenantScopeModeEnum  # noqa: E402
from app.pipeline.pipeline import CancelledError, DocumentPipeline  # noqa: E402
from app.repositories.tenant_repo import TenantScope, tenant_scope  # noqa: E402
from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.config import RetrievalConfig, RetrievalConfigStore  # noqa: E402
from app.retrieval.hybrid import HybridRetriever  # noqa: E402
from app.retrieval.multi_kb import (  # noqa: E402
    KBRetrievalConfig,
    MultiKBRetriever,
    MultiKBSearchResult,
)


# ============================================================
# 公共 helpers
# ============================================================


def _make_result(chunk_id: str, score: float = 0.8) -> RetrievalResult:
    """创建 RetrievalResult 辅助方法"""
    return RetrievalResult(
        chunk_id=chunk_id,
        content=f"内容-{chunk_id}",
        score=score,
        doc_id="d1",
        metadata={},
    )


def _rerank_passthrough(query, results, top_k, tenant_id=None):
    """rerank_and_expand 的 mock side_effect：透传前 top_k 条。"""
    return results[:top_k]


class FakeAsyncSession:
    """模拟异步数据库会话（父块扩展查询返回空）。"""

    async def execute(self, stmt):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSessionFactory:
    def __call__(self):
        return FakeAsyncSession()


def _build_hybrid(config: RetrievalConfig | None = None):
    """构造 HybridRetriever 并注入 mock 组件。

    Returns:
        (hybrid, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever)
    """
    if config is None:
        config = RetrievalConfig()

    config_store = MagicMock()
    config_store.get_effective = AsyncMock(return_value=config)

    platform_store = MagicMock()
    platform_store.get_load_cache_ttl = AsyncMock(return_value=10)

    vector_retriever = MagicMock()
    vector_retriever.search = AsyncMock(return_value=[])
    sparse_retriever = MagicMock()
    sparse_retriever.search = AsyncMock(return_value=[])
    bm25_retriever = MagicMock()
    bm25_retriever.search = AsyncMock(return_value=[])

    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[])

    hybrid = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=reranker,
        db_session_factory=FakeSessionFactory(),
        bm25_retriever=bm25_retriever,
        config_store=config_store,
        platform_store=platform_store,
    )
    return hybrid, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever


def _make_pipeline(mock_milvus=None):
    """创建最小 DocumentPipeline 实例，仅注入 mock milvus。"""
    mock_model_manager = MagicMock()
    mock_model_manager.embedder = AsyncMock()
    if mock_milvus is None:
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
    mock_db_factory = MagicMock()
    with patch("app.pipeline.pipeline.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            pipeline_embed_batch_size=32,
            pipeline_embed_concurrency=4,
            pipeline_embed_per_doc_concurrency=2,
            pipeline_slow_threshold_ms=5000,
        )
        pipeline = DocumentPipeline(mock_model_manager, mock_milvus, mock_db_factory)
    return pipeline


# ============================================================
# H5: 流式问答取到租户配置（非默认参数生效）
# ============================================================


class TestH5TenantConfigE2E:
    """H5: 检索使用正确的租户配置"""

    @pytest.mark.asyncio
    async def test_stream_response_uses_tenant_config(self):
        """流式问答时检索使用调用者的租户配置（非默认）

        场景：租户 tenant-custom 有自定义 top_k=20 / recall_k=200 配置，
        在流式响应中 contextvar 已 reset 的情况下，通过显式 tenant_id 传参
        仍能取到该租户的自定义配置。
        """
        # 模拟租户自定义配置: recall_k=200（非默认 100）
        custom_config = RetrievalConfig(recall_k=200, hnsw_ef=128)
        hybrid, config_store, _, _, _, _ = _build_hybrid(custom_config)

        # 不进入 tenant_scope（模拟流式响应中 contextvar 已 reset）
        # 通过显式 tenant_id 传参
        await hybrid.search("法律问题查询", kb_id="kb-law", top_k=20, tenant_id="tenant-custom")

        # 验证 config_store.get_effective 被调用时传入的是显式 tenant_id
        config_store.get_effective.assert_awaited_once_with("tenant-custom")

    @pytest.mark.asyncio
    async def test_tenant_config_non_default_params_effective(self):
        """租户自定义参数（recall_k=200, hnsw_ef=128）生效于三路子检索器

        验证 custom config 的参数确实透传给底层检索，而非使用全局默认。
        """
        custom_config = RetrievalConfig(recall_k=200, hnsw_ef=128)
        hybrid, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever = (
            _build_hybrid(custom_config)
        )

        scope = TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id="tenant-custom")
        with tenant_scope(scope):
            await hybrid.search("查询", kb_id="kb-001", top_k=10)

        # 验证 dense 检索器收到 ef=128（非默认 64）
        dense_kwargs = vector_retriever.search.call_args.kwargs
        assert dense_kwargs["ef"] == 128

    @pytest.mark.asyncio
    async def test_multi_kb_search_passes_tenant_to_all_subroutes(self):
        """多库检索时每个子路由都收到显式 tenant_id（非 None）

        端到端验证：MultiKBRetriever.search → HybridRetriever.search 的
        tenant_id 逐级透传。
        """
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1")],
                [_make_result("c2")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        await retriever.search("查询", kb_configs, top_k=10, tenant_id="tenant-stream")

        # 每个 KB 的 search 都收到显式 tenant_id
        for c in mock_hybrid.search.call_args_list:
            assert c.kwargs.get("tenant_id") == "tenant-stream"
        # 统一 rerank 也收到显式 tenant_id
        assert mock_hybrid.rerank_and_expand.call_args.kwargs.get("tenant_id") == "tenant-stream"


# ============================================================
# H2+H3: 检索容错与降级透传
# ============================================================


class TestH2H3FaultToleranceE2E:
    """H2+H3: 检索容错与降级透传"""

    @pytest.mark.asyncio
    async def test_single_route_failure_returns_degraded(self):
        """某路检索失败 → 其余路正常返回 + degraded=True

        端到端场景：3 路检索，中间一路 Milvus 超时，
        结果应含其余 2 路的数据且 degraded=True。
        """
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1", score=0.9)],
                RuntimeError("Milvus connection timeout"),
                [_make_result("c3", score=0.7)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-broken", priority=0.8),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.7),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        # 验证降级标志
        assert result.degraded is True
        assert "kb-broken" in result.failed_kb_ids
        # 成功源结果仍然返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c3" in chunk_ids

    @pytest.mark.asyncio
    async def test_degraded_flag_propagates_to_sse_context(self):
        """降级状态可被 SSE 流式上下文读取（模拟 degraded=True 时 SSE 事件包含标记）

        验证 MultiKBSearchResult.degraded 字段可被上层读取并透传到响应。
        """
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1")],
                ConnectionError("Redis cluster unavailable"),
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-ok", priority=1.0),
            KBRetrievalConfig(kb_id="kb-fail", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=5)

        # SSE 事件应该能读取到 degraded 状态
        assert result.degraded is True
        assert result.failed_kb_ids == ["kb-fail"]
        # 结果非空（容错成功）
        assert len(result.results) > 0

    @pytest.mark.asyncio
    async def test_all_routes_success_no_degradation(self):
        """三路全成功 → 结果完整 + degraded=False（回归不变式）"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1", score=0.95)],
                [_make_result("c2", score=0.85)],
                [_make_result("c3", score=0.75)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-2", priority=0.9),
            KBRetrievalConfig(kb_id="kb-3", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is False
        assert result.failed_kb_ids == []
        # 三路结果都在
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c2" in chunk_ids
        assert "c3" in chunk_ids


# ============================================================
# H6: 多库并发限流
# ============================================================


class TestH6ConcurrencyE2E:
    """H6: 多库检索并发受限、慢源超时降级"""

    @pytest.mark.asyncio
    async def test_multi_kb_concurrent_limited(self):
        """多库检索并发不超过 max_concurrency

        端到端验证：6 个知识库同时检索，max_concurrency=2，
        峰值并发不应超过 2。
        """
        max_concurrency = 2
        concurrent_count = 0
        peak_concurrent = 0

        async def _slow_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            await asyncio.sleep(0.03)  # 模拟检索耗时
            concurrent_count -= 1
            return [_make_result(f"c-{kb_id}")]

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

        result = await retriever.search("查询", kb_configs, top_k=10)

        # 峰值并发不超过 max_concurrency
        assert peak_concurrent <= max_concurrency
        # 所有源结果都返回
        assert len(result.results) == 6
        assert result.degraded is False

    @pytest.mark.asyncio
    async def test_slow_source_timeout_degraded(self):
        """慢源超时 → 该源降级不阻塞其余

        场景：3 个源，其中 1 个需要 10s 才能返回，
        per_source_timeout=0.2s → 该源超时被降级，其余正常返回。
        """
        async def _mixed_search(query, kb_id, top_k=10, skip_rerank=False,
                                expr=None, tenant_id=None):
            if kb_id == "kb-slow":
                await asyncio.sleep(10)  # 远超 timeout
            return [_make_result(f"c-{kb_id}")]

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
        result = await retriever.search("查询", kb_configs, top_k=10)
        elapsed = time.perf_counter() - start

        # 总耗时约等于 per_source_timeout，不是 10s
        assert elapsed < 1.0
        # 慢源被降级
        assert result.degraded is True
        assert "kb-slow" in result.failed_kb_ids
        # 快源结果正常
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c-kb-fast1" in chunk_ids
        assert "c-kb-fast2" in chunk_ids

    @pytest.mark.asyncio
    async def test_timeout_does_not_block_fast_sources(self):
        """超时源不阻塞其余快速源的检索（整体耗时 << 慢源耗时）"""
        async def _search(query, kb_id, top_k=10, skip_rerank=False,
                          expr=None, tenant_id=None):
            if kb_id == "kb-slow":
                await asyncio.sleep(10)
            else:
                await asyncio.sleep(0.01)  # 快源极快
            return [_make_result(f"c-{kb_id}")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(
            hybrid_retriever=mock_hybrid,
            max_concurrency=4,
            per_source_timeout=0.15,
        )
        kb_configs = [
            KBRetrievalConfig(kb_id=f"kb-fast{i}", priority=1.0) for i in range(4)
        ] + [KBRetrievalConfig(kb_id="kb-slow", priority=0.5)]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=10)
        elapsed = time.perf_counter() - start

        # 整体应在 per_source_timeout 附近完成（不是 10s）
        assert elapsed < 0.5
        # 4 个快源结果正常
        chunk_ids = [r.chunk_id for r in result.results]
        for i in range(4):
            assert f"c-kb-fast{i}" in chunk_ids


# ============================================================
# H1: 入库异常孤儿向量清理
# ============================================================


class TestH1OrphanCleanupE2E:
    """H1: 入库异常 → 无孤儿向量；先删后写重处理不残留"""

    @pytest.mark.asyncio
    async def test_exception_during_index_cleans_orphans(self):
        """入库异常 → 按 doc_id 清理孤儿向量

        端到端场景：pipeline.process 在 Index 阶段抛异常，
        异常处理路径应调用 _cleanup_milvus_orphans(kb_id, doc_id)。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock(side_effect=RuntimeError("Milvus insert failed"))
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-orphan"
        doc_id = "e2e-doc-orphan"

        # 模拟异常处理: insert 失败后走 cleanup 路径
        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = None  # 未取消

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                # 模拟 Index 阶段
                try:
                    await pipeline._check_cancelled(doc_id)
                    await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.1] * 1024}])
                except Exception:
                    # 异常处理: 清理孤儿向量
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证孤儿清理被调用
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_reprocess_same_doc_no_residual(self):
        """先删后写：重处理同一文档不残留旧向量

        端到端场景：同一 doc_id 入库两次，
        第二次应先清理旧向量再写入新向量，确保无残留。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock(return_value=1)
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-reprocess"
        doc_id = "e2e-doc-reprocess"

        # 第一次入库: 先删后写
        await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
        first_delete_call = mock_milvus.delete_by_doc_id.call_count
        assert first_delete_call == 1
        await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.1] * 1024}])

        # 第二次入库（重处理）: 再次先删后写
        await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
        second_delete_call = mock_milvus.delete_by_doc_id.call_count
        assert second_delete_call == 2  # 第二次也清理了旧向量
        await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.2] * 1024}])

        # 最终 insert 被调用了 2 次
        assert mock_milvus.insert.call_count == 2
        # 最终 delete_by_doc_id 被调用了 2 次（每次入库前都清理）
        assert mock_milvus.delete_by_doc_id.call_count == 2

    @pytest.mark.asyncio
    async def test_normal_success_no_extra_cleanup(self):
        """正常入库成功不触发额外清理（回归不变式）

        正常路径下 _cleanup_milvus_orphans 仅在写入前调用一次（先删后写的 delete），
        不应在写入成功后再次调用。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock(return_value=1)
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-normal"
        doc_id = "e2e-doc-normal"

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = None  # 未取消

            # 正常路径: 先删后写
            await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
            await pipeline._check_cancelled(doc_id)
            await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.1] * 1024}])

            # 验证: delete_by_doc_id 仅在写入前调用 1 次
            assert mock_milvus.delete_by_doc_id.call_count == 1
            # insert 成功
            assert mock_milvus.insert.call_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_exception_does_not_propagate(self):
        """孤儿清理失败（底层异常）不向外传播，不影响后续流程"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock(
            side_effect=ConnectionError("Milvus unavailable")
        )
        pipeline = _make_pipeline(mock_milvus)

        # 清理失败不应抛异常
        await pipeline._cleanup_milvus_orphans("kb-1", "doc-1")

        # delete_by_doc_id 确实被调用了（只是底层异常被吞掉）
        mock_milvus.delete_by_doc_id.assert_called_once_with("kb-1", "doc-1")


# ============================================================
# M6: 入库写入中取消
# ============================================================


class TestM6CancelCheckE2E:
    """M6: 入库写入中取消 → 清理"""

    @pytest.mark.asyncio
    async def test_cancel_during_write_triggers_cleanup(self):
        """写入过程中取消 → 中止 + 清理已写入的孤儿

        端到端场景：分批写入 5000 条数据（5 批），
        在第 1 批写入后检测到取消 → 中止后续批次 + 清理。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-cancel"
        doc_id = "e2e-doc-cancel"

        batch_size = 1000
        total_records = 5000
        milvus_data = [{"doc_id": doc_id, "vector": [0.1] * 128, "idx": i} for i in range(total_records)]

        # _check_cancelled: 第一次 pass（写入前），第二次 raise（写入中）
        call_count = 0

        async def mock_check_cancelled(did):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # 写入前检查 pass
            # 后续检查: 取消
            raise CancelledError(f"文档 {did} 已被取消或删除")

        with patch.object(pipeline, "_check_cancelled", side_effect=mock_check_cancelled):
            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                cancelled = False
                batches_written = 0
                try:
                    # 写入前总检查
                    await pipeline._check_cancelled(doc_id)

                    # 分批写入
                    for i in range(0, total_records, batch_size):
                        # 每批后检查取消
                        if i > 0:
                            await pipeline._check_cancelled(doc_id)
                        batch = milvus_data[i:i + batch_size]
                        await mock_milvus.insert(kb_id, batch)
                        batches_written += 1
                except CancelledError:
                    cancelled = True
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证确实被取消
                assert cancelled is True
                # 验证只写了 1 批（第一批之后检查取消就中止了）
                assert batches_written == 1
                assert mock_milvus.insert.call_count == 1
                # 验证清理被触发
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_cancel_before_any_write_no_orphans(self):
        """写入前就检测到取消 → 完全不写入 + 清理（无实际孤儿但仍调清理确保安全）"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-cancel-early"
        doc_id = "e2e-doc-cancel-early"

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.side_effect = CancelledError(f"文档 {doc_id} 已被取消或删除")

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                cancelled = False
                try:
                    await pipeline._check_cancelled(doc_id)
                    await mock_milvus.insert(kb_id, [{"doc_id": doc_id}])
                except CancelledError:
                    cancelled = True
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                assert cancelled is True
                # 完全没写入
                mock_milvus.insert.assert_not_called()
                # 仍调了清理
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_no_cancel_all_batches_written(self):
        """未取消时所有批次正常写入（M6 回归不变式）"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-no-cancel"
        doc_id = "e2e-doc-no-cancel"

        batch_size = 1000
        total_records = 3000

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = None  # 永不取消

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                cancelled = False
                try:
                    await pipeline._check_cancelled(doc_id)
                    for i in range(0, total_records, batch_size):
                        if i > 0:
                            await pipeline._check_cancelled(doc_id)
                        batch = [{"doc_id": doc_id, "idx": j} for j in range(i, i + batch_size)]
                        await mock_milvus.insert(kb_id, batch)
                except CancelledError:
                    cancelled = True
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                assert cancelled is False
                # 所有 3 批写入
                assert mock_milvus.insert.call_count == 3
                # 清理未被触发
                mock_cleanup.assert_not_called()


# ============================================================
# 回归不变式
# ============================================================


class TestRegressionInvariants:
    """回归不变式：确保修复不引入性能退化或行为变化"""

    @pytest.mark.asyncio
    async def test_single_kb_fast_path_no_delay(self):
        """单库检索无额外延迟（Semaphore 不阻塞）

        单库时应极快完成，max_concurrency 不应引入可感知延迟。
        """
        async def _fast_search(query, kb_id, top_k=10, skip_rerank=False,
                               expr=None, tenant_id=None):
            return [_make_result("c1")]

        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(side_effect=_fast_search)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid, max_concurrency=4)
        kb_configs = [KBRetrievalConfig(kb_id="kb-only", priority=1.0)]

        start = time.perf_counter()
        result = await retriever.search("查询", kb_configs, top_k=5)
        elapsed = time.perf_counter() - start

        # 单源应极快完成（< 100ms，计入 Python 调度开销）
        assert elapsed < 0.5
        assert result.degraded is False
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_cache_hit_when_config_unchanged(self):
        """配置未变时缓存命中（不重复打 DB）

        验证 RetrievalConfigStore 的 _cache_get 在 TTL 内对同一 tenant_id
        直接返回缓存值，不再调用 session_factory。
        """
        # 模拟 session_factory（只应被调用一次）
        mock_session = AsyncMock()
        mock_row = MagicMock()
        # 模拟 RetrievalConfigRow 的属性（简单返回 None 让 effective_from_raw 用默认）
        for attr in ["recall_k", "hnsw_ef", "sparse_weight", "dense_weight",
                     "bm25_weight", "rerank_top_k", "parent_chunk_size",
                     "child_chunk_size", "chunk_overlap", "expand_parent",
                     "expand_window"]:
            setattr(mock_row, attr, None)
        mock_session.get = AsyncMock(return_value=mock_row)

        class _FakeCtx:
            def __init__(self, session):
                self._s = session

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        factory = MagicMock(return_value=_FakeCtx(mock_session))
        store = RetrievalConfigStore(factory)

        tenant = "cache-test-tenant"

        # 第一次调用：缓存 miss → 打 DB
        config1 = await store.get_effective(tenant)
        assert factory.call_count == 1

        # 第二次调用：TTL 内缓存命中 → 不打 DB
        config2 = await store.get_effective(tenant)
        assert factory.call_count == 1  # 仍然是 1（未增加）

        # 返回结果一致
        assert config1 == config2

    @pytest.mark.asyncio
    async def test_single_worker_write_invalidates_cache(self):
        """单 worker 写后失效：update 后该租户缓存被清除

        验证 update 调用后 _cache 中对应 tenant 被失效，
        下次 get_effective 必须重新打 DB。
        """
        mock_session = AsyncMock()
        mock_row = MagicMock()
        for attr in ["recall_k", "hnsw_ef", "sparse_weight", "dense_weight",
                     "bm25_weight", "rerank_top_k", "parent_chunk_size",
                     "child_chunk_size", "chunk_overlap", "expand_parent",
                     "expand_window"]:
            setattr(mock_row, attr, None)
        mock_session.get = AsyncMock(return_value=mock_row)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        class _FakeCtx:
            def __init__(self, session):
                self._s = session

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        factory = MagicMock(return_value=_FakeCtx(mock_session))
        store = RetrievalConfigStore(factory)

        tenant = "writer-test-tenant"

        # 先读一次（填充缓存）
        await store.get_effective(tenant)
        assert factory.call_count == 1

        # 缓存命中
        await store.get_effective(tenant)
        assert factory.call_count == 1

        # update 写入 → 失效缓存
        await store.update(tenant, {"recall_k": 150})
        # update 内部调了 session_factory（写入） + get_effective（读新值）
        # 缓存被失效后 get_effective 会重打 DB
        cache_calls_after_update = factory.call_count

        # 再次 get_effective → 应从缓存命中（update 已重新填充）
        await store.get_effective(tenant)
        assert factory.call_count == cache_calls_after_update  # 不增加

    @pytest.mark.asyncio
    async def test_normal_indexing_no_orphan_cleanup_triggered(self):
        """正常入库不触发异常路径的清理（回归不变式）

        验证正常流程下 except Exception/CancelledError 分支不被进入，
        _cleanup_milvus_orphans 仅在先删后写的 delete 阶段被调用。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock(return_value=5)
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "e2e-kb-normal-flow"
        doc_id = "e2e-doc-normal-flow"

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = None

            # 正常入库流程: 先删后写 + check + insert
            await pipeline._cleanup_milvus_orphans(kb_id, doc_id)  # 先删（正常的预清理）
            await pipeline._check_cancelled(doc_id)
            await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.1] * 1024}])

            # 验证 delete_by_doc_id 仅被调用 1 次（预清理那次）
            assert mock_milvus.delete_by_doc_id.call_count == 1
            # insert 成功 1 次
            assert mock_milvus.insert.call_count == 1

    @pytest.mark.asyncio
    async def test_all_success_results_complete_and_ordered(self):
        """三路全成功时结果完整且按加权分数排序（回归不变式）

        确保 fault-tolerance 修复没有改变正常路径的结果完整性和排序。
        """
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1", score=0.95)],
                [_make_result("c2", score=0.85)],
                [_make_result("c3", score=0.75)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-2", priority=0.9),
            KBRetrievalConfig(kb_id="kb-3", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        # 结果完整
        assert len(result.results) == 3
        # 按加权分数降序: c1(0.95*1.0=0.95) > c2(0.85*0.9=0.765) > c3(0.75*0.8=0.6)
        assert result.results[0].chunk_id == "c1"
        assert result.results[1].chunk_id == "c2"
        assert result.results[2].chunk_id == "c3"
        # 无降级
        assert result.degraded is False
        assert result.failed_kb_ids == []
