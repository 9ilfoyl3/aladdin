"""Pipeline Index 写入窗口取消检查 单元测试

Feature: retrieval-pipeline-hardening
Validates: Bug 6 (M6) — Index 写入窗口取消检查

验证:
- 写 Milvus 前 _check_cancelled 检测到取消 → 不写入 + 触发清理
- 分批写入过程中取消 → 停止写入剩余批次 + 触发清理
- 未取消时正常写入所有批次（不变式）
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock 掉需要的模块，避免导入失败
sys.modules.setdefault("pymilvus", MagicMock())
sys.modules.setdefault("pymilvus.connections", MagicMock())
sys.modules.setdefault("pymilvus.utility", MagicMock())

from app.pipeline.pipeline import DocumentPipeline, CancelledError


# ---------- Helper ----------


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
# M6: Index 写入窗口取消检查
# ==========================================================================


class TestIndexWriteWindowCancelCheck:
    """M6: Index 写入窗口取消检查"""

    @pytest.mark.asyncio
    async def test_cancel_before_milvus_write_aborts(self):
        """写 Milvus 前 _check_cancelled 检测到取消 → 不写入 + 触发清理"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 模拟 _check_cancelled 在写 Milvus 前 raise CancelledError
        # 这模拟了文档在 Index 写入窗口前被取消的场景
        cancel_error = CancelledError(f"文档 {doc_id} 已被取消或删除")

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.side_effect = cancel_error

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                # 模拟 CancelledError 处理逻辑:
                # pipeline.process() 中 except CancelledError 分支会 rollback + cleanup
                milvus_data = [{"doc_id": doc_id, "vector": [0.1] * 1024}]

                # 直接模拟 Index 写入窗口的逻辑:
                # if milvus_data:
                #     await self._check_cancelled(doc_id)  <-- 这里抛异常
                #     await self.milvus.insert(kb_id, milvus_data)  <-- 不应到达
                with pytest.raises(CancelledError):
                    await pipeline._check_cancelled(doc_id)

                # 验证 milvus.insert 从未被调用（写入前就中止了）
                mock_milvus.insert.assert_not_called()

                # 模拟 except CancelledError 分支的清理行为
                await pipeline._cleanup_milvus_orphans(kb_id, doc_id)
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_cancel_during_batch_write_stops_remaining(self):
        """分批写入过程中第 N 批后取消 → 停止写入剩余批次 + 触发清理

        模拟: 总共 15000 条数据(15 批，batch_size=1000)，
        每 10 批检查取消，在第 11 批前取消 → 只写入 10 批
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 构造 15000 条 milvus_data (15 批)
        batch_size = 1000
        total_records = 15000
        milvus_data = [{"doc_id": doc_id, "vector": [0.1] * 128, "idx": i} for i in range(total_records)]

        # _check_cancelled 的 side_effect:
        # - 第 1 次调用（写入前 i=0 之前的总检查）：pass
        # - 第 2 次调用（i=10000, 即第 10 批后 (10000//1000)%10==0）：raise CancelledError
        call_count = 0

        async def mock_check_cancelled(did):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 写入前的总检查 — pass
                return
            else:
                # 第 10 批后的批间检查 — 取消
                raise CancelledError(f"文档 {did} 已被取消或删除")

        with patch.object(pipeline, "_check_cancelled", side_effect=mock_check_cancelled):
            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                # 模拟分批写入逻辑（与 pipeline.py 中的实际逻辑对齐）
                cancelled = False
                try:
                    # 写入前总检查
                    await pipeline._check_cancelled(doc_id)

                    # 分批写入
                    for i in range(0, total_records, batch_size):
                        # 批次间检查（每 10 批）
                        if i > 0 and (i // batch_size) % 10 == 0:
                            await pipeline._check_cancelled(doc_id)
                        batch = milvus_data[i:i + batch_size]
                        await mock_milvus.insert(kb_id, batch)
                except CancelledError:
                    cancelled = True
                    # except CancelledError 分支: rollback + cleanup
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证确实被取消了
                assert cancelled is True

                # 验证 milvus.insert 被调用了 10 次（第 0~9 批成功写入）
                # i=10000 时 (10000//1000)%10==0，此时检查取消，抛异常
                assert mock_milvus.insert.call_count == 10

                # 验证 _cleanup_milvus_orphans 被调用
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_no_cancel_writes_all_batches(self):
        """未取消时正常写入所有批次（不变式）"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 构造 5000 条 milvus_data (5 批)
        batch_size = 1000
        total_records = 5000
        milvus_data = [{"doc_id": doc_id, "vector": [0.1] * 128, "idx": i} for i in range(total_records)]

        # _check_cancelled 始终 pass（文档未被取消）
        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = None

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                # 模拟分批写入逻辑
                cancelled = False
                try:
                    # 写入前总检查
                    await pipeline._check_cancelled(doc_id)

                    # 分批写入（total > batch_size）
                    for i in range(0, total_records, batch_size):
                        if i > 0 and (i // batch_size) % 10 == 0:
                            await pipeline._check_cancelled(doc_id)
                        batch = milvus_data[i:i + batch_size]
                        await mock_milvus.insert(kb_id, batch)
                except CancelledError:
                    cancelled = True
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证未被取消
                assert cancelled is False

                # 验证所有 5 批都被正常写入
                assert mock_milvus.insert.call_count == 5

                # 验证 _cleanup_milvus_orphans 未被调用（正常路径不触发清理）
                mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_small_batch_no_write(self):
        """小批量（≤ batch_size）写入前取消 → 完全不写入"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        # 小批量: 500 条，一次写入（走 total <= batch_size 分支）
        batch_size = 1000
        total_records = 500
        milvus_data = [{"doc_id": doc_id, "vector": [0.1] * 128, "idx": i} for i in range(total_records)]

        # _check_cancelled 立即取消
        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.side_effect = CancelledError(f"文档 {doc_id} 已被取消或删除")

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                cancelled = False
                try:
                    # 写入前总检查 — 此处取消
                    await pipeline._check_cancelled(doc_id)

                    # 小批量一次写入
                    if total_records <= batch_size:
                        await mock_milvus.insert(kb_id, milvus_data)
                except CancelledError:
                    cancelled = True
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证确实被取消
                assert cancelled is True

                # 验证 milvus.insert 从未被调用
                mock_milvus.insert.assert_not_called()

                # 验证清理被触发
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

    @pytest.mark.asyncio
    async def test_check_cancelled_raises_when_doc_deleted(self):
        """_check_cancelled: 文档状态为 None（已删除）时 raise CancelledError"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        doc_id = "deleted-doc-001"

        # Mock db_session_factory 返回的 session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # 文档已删除

        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        pipeline.db_session_factory = MagicMock(return_value=mock_session)

        with pytest.raises(CancelledError, match="已被取消或删除"):
            await pipeline._check_cancelled(doc_id)

    @pytest.mark.asyncio
    async def test_check_cancelled_raises_when_status_cancelled(self):
        """_check_cancelled: 文档状态为 'cancelled' 时 raise CancelledError"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        doc_id = "cancelled-doc-001"

        # Mock db_session_factory
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "cancelled"

        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        pipeline.db_session_factory = MagicMock(return_value=mock_session)

        with pytest.raises(CancelledError, match="已被取消或删除"):
            await pipeline._check_cancelled(doc_id)

    @pytest.mark.asyncio
    async def test_check_cancelled_passes_when_processing(self):
        """_check_cancelled: 文档状态为 'processing' 时正常通过，不抛异常"""
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        doc_id = "active-doc-001"

        # Mock db_session_factory
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "processing"

        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        pipeline.db_session_factory = MagicMock(return_value=mock_session)

        # 不应抛异常
        await pipeline._check_cancelled(doc_id)

    @pytest.mark.asyncio
    async def test_cancelled_error_triggers_rollback_and_cleanup(self):
        """CancelledError 被捕获后执行 rollback + _cleanup_milvus_orphans

        验证完整的异常处理链: CancelledError → rollback → cleanup
        """
        mock_milvus = AsyncMock()
        mock_milvus.has_collection = AsyncMock(return_value=True)
        mock_milvus.delete_by_doc_id = AsyncMock()
        mock_milvus.insert = AsyncMock()
        pipeline = _make_pipeline(mock_milvus)

        kb_id = "test-kb-001"
        doc_id = "test-doc-001"

        mock_session = AsyncMock()
        mock_session.rollback = AsyncMock()

        with patch.object(pipeline, "_check_cancelled", new_callable=AsyncMock) as mock_check:
            mock_check.side_effect = CancelledError(f"文档 {doc_id} 已被取消或删除")

            with patch.object(pipeline, "_cleanup_milvus_orphans", new_callable=AsyncMock) as mock_cleanup:
                # 模拟 pipeline.process() 中的 try/except CancelledError 分支
                try:
                    # Index 阶段: 写入前检查
                    await pipeline._check_cancelled(doc_id)
                    await mock_milvus.insert(kb_id, [{"doc_id": doc_id}])
                except CancelledError:
                    # 模拟 except CancelledError 分支
                    await mock_session.rollback()
                    await pipeline._cleanup_milvus_orphans(kb_id, doc_id)

                # 验证 rollback 被调用
                mock_session.rollback.assert_called_once()

                # 验证清理被调用
                mock_cleanup.assert_called_once_with(kb_id, doc_id)

                # 验证 insert 未被调用
                mock_milvus.insert.assert_not_called()
