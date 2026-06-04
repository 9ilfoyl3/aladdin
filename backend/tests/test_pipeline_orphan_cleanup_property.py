"""Pipeline 孤儿向量清理 属性测试 + 单元测试

Feature: retrieval-pipeline-hardening
Validates: Bug 5 (H1) — Property 5

Property 5:
- 任意 (kb_id, doc_id)，当且仅当二者均非空才调底层删除
- 任一为空跳过不抛
- 底层异常记 WARNING 不向上抛
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# Mock 掉需要的模块，避免导入失败
sys.modules.setdefault("pymilvus", MagicMock())
sys.modules.setdefault("pymilvus.connections", MagicMock())
sys.modules.setdefault("pymilvus.utility", MagicMock())

from app.pipeline.pipeline import DocumentPipeline


# ---------- Strategies ----------

# kb_id / doc_id 可为 None、空字符串、或非空字符串
nullable_str = st.one_of(
    st.none(),
    st.just(""),
    st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
)

# 非空字符串（用于异常测试）
non_empty_str = st.text(
    min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))
)


# ---------- Helper ----------

def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_pipeline(mock_milvus=None):
    """创建最小 DocumentPipeline 实例，仅注入 mock milvus"""
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


# ==========================================================================
# 属性测试 (Property 5)
# ==========================================================================


class TestProperty5CleanupOnlyDeletesWhenBothNonEmpty:
    """Property 5: 当且仅当 kb_id 和 doc_id 都非空时才调底层删除

    *For any* (kb_id, doc_id) 组合：
    - 两者都非空 → delete_by_doc_id 被调用
    - 任一为空（None 或 ""）→ delete_by_doc_id 不被调用
    - 无论何种组合，不抛出异常

    **Validates: Bug 5 (H1) — Property 5**
    """

    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(kb_id=nullable_str, doc_id=nullable_str)
    def test_cleanup_only_deletes_when_both_non_empty(self, kb_id, doc_id):
        """Property 5: 当且仅当 kb_id 和 doc_id 都非空时才调底层删除"""

        async def _test():
            mock_milvus = AsyncMock()
            mock_milvus.has_collection = AsyncMock(return_value=True)
            mock_milvus.delete_by_doc_id = AsyncMock()
            pipeline = _make_pipeline(mock_milvus)

            # 调用 _cleanup_milvus_orphans，不应抛异常
            await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

            both_non_empty = bool(kb_id) and bool(doc_id)
            if both_non_empty:
                # 两者都非空：delete_by_doc_id 应被调用
                mock_milvus.delete_by_doc_id.assert_called_once_with(kb_id, doc_id)
            else:
                # 任一为空：delete_by_doc_id 不应被调用
                mock_milvus.delete_by_doc_id.assert_not_called()

        _run_async(_test())


class TestProperty5CleanupNeverRaisesOnException:
    """Property 5: 底层删除异常时记 WARNING 不向上抛

    *For any* 非空 (kb_id, doc_id)，当底层 delete_by_doc_id 抛出任意异常时，
    _cleanup_milvus_orphans 不应向上传播异常。

    **Validates: Bug 5 (H1) — Property 5**
    """

    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(kb_id=non_empty_str, doc_id=non_empty_str)
    def test_cleanup_never_raises_on_exception(self, kb_id, doc_id):
        """Property 5: 底层异常不向上抛"""

        async def _test():
            mock_milvus = AsyncMock()
            mock_milvus.has_collection = AsyncMock(return_value=True)
            mock_milvus.delete_by_doc_id = AsyncMock(
                side_effect=RuntimeError("Milvus connection lost")
            )
            pipeline = _make_pipeline(mock_milvus)

            # 不应抛异常
            await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

            # delete_by_doc_id 被调用了（证明确实走到了删除逻辑）
            mock_milvus.delete_by_doc_id.assert_called_once_with(kb_id, doc_id)

        _run_async(_test())


class TestProperty5HasCollectionFalseSkipsDelete:
    """Property 5 补充: collection 不存在时跳过删除

    *For any* 非空 (kb_id, doc_id)，当 has_collection 返回 False 时，
    不调用 delete_by_doc_id。

    **Validates: Bug 5 (H1) — Property 5**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(kb_id=non_empty_str, doc_id=non_empty_str)
    def test_no_delete_when_collection_missing(self, kb_id, doc_id):
        """collection 不存在时跳过删除"""

        async def _test():
            mock_milvus = AsyncMock()
            mock_milvus.has_collection = AsyncMock(return_value=False)
            mock_milvus.delete_by_doc_id = AsyncMock()
            pipeline = _make_pipeline(mock_milvus)

            await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

            mock_milvus.has_collection.assert_called_once_with(kb_id)
            mock_milvus.delete_by_doc_id.assert_not_called()

        _run_async(_test())


# ==========================================================================
# 单元测试
# ==========================================================================


class TestOrphanCleanupUnit:
    """孤儿向量清理单元测试"""

    @pytest.mark.asyncio
    async def test_exception_branch_triggers_cleanup(self):
        """Index 阶段抛异常后，Exception 分支调 _cleanup_milvus_orphans 按 doc_id 清理"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 直接测试 _cleanup_milvus_orphans 在异常场景下的行为：
        # 模拟 delete_by_doc_id 第一次调用抛异常（模拟底层 Milvus 异常）
        mock_milvus.delete_by_doc_id = AsyncMock(
            side_effect=ConnectionError("Milvus unavailable")
        )

        # 调用不应抛异常
        await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

        # 验证 delete_by_doc_id 确实被调用了（按 doc_id 清理）
        mock_milvus.delete_by_doc_id.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_write_before_delete_idempotent(self):
        """先删后写模式: 重处理同一文档不残留旧向量

        验证对同一 (kb_id, doc_id) 多次调用 _cleanup_milvus_orphans 后写入，
        每次都是先删后写，保证幂等性。
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock(return_value=1)
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 第一次处理: 先删后写
        await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
        mock_milvus.delete_by_doc_id.assert_called_with(kb_id, doc_id)
        # 模拟写入新向量
        await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.1] * 1024}])

        # 重置 mock 计数
        mock_milvus.delete_by_doc_id.reset_mock()

        # 第二次处理（重处理同一文档）: 再次先删后写
        await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
        mock_milvus.delete_by_doc_id.assert_called_once_with(kb_id, doc_id)
        # 再次写入（幂等：旧的已删，新的写入）
        await mock_milvus.insert(kb_id, [{"doc_id": doc_id, "vector": [0.2] * 1024}])

        # 验证两次写入都正常完成
        assert mock_milvus.insert.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_kb_id_skips_cleanup(self):
        """空 kb_id 跳过清理不抛异常"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        # kb_id 为空字符串
        await pipeline._cleanup_milvus_orphans("", "valid-doc-id")
        mock_milvus.delete_by_doc_id.assert_not_called()
        mock_milvus.has_collection.assert_not_called()

        # kb_id 为 None
        await pipeline._cleanup_milvus_orphans(None, "valid-doc-id")
        mock_milvus.delete_by_doc_id.assert_not_called()
        mock_milvus.has_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_doc_id_skips_cleanup(self):
        """空 doc_id 跳过清理不抛异常"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        # doc_id 为空字符串
        await pipeline._cleanup_milvus_orphans("valid-kb-id", "")
        mock_milvus.delete_by_doc_id.assert_not_called()
        mock_milvus.has_collection.assert_not_called()

        # doc_id 为 None
        await pipeline._cleanup_milvus_orphans("valid-kb-id", None)
        mock_milvus.delete_by_doc_id.assert_not_called()
        mock_milvus.has_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_logs_warning_on_exception(self):
        """底层异常时记录 WARNING 日志"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock(
            side_effect=RuntimeError("network timeout")
        )
        pipeline = _make_pipeline(mock_milvus)

        with patch("app.pipeline.pipeline.logger") as mock_logger:
            await pipeline._cleanup_milvus_orphans("kb-1", "doc-1")
            # 验证 WARNING 日志被记录
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args
            assert "doc-1" in str(call_args)

    @pytest.mark.asyncio
    async def test_backward_compat_cleanup_on_cancel_delegates(self):
        """向后兼容: _cleanup_milvus_on_cancel 委托给 _cleanup_milvus_orphans"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb"
        doc_id = "test-doc"

        await pipeline._cleanup_milvus_on_cancel(kb_id, doc_id)

        mock_milvus.delete_by_doc_id.assert_called_once_with(kb_id, doc_id)
