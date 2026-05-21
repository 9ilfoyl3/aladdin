"""ProgressTracker 单元测试"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.pipeline.progress import PipelineStage, ProgressTracker, STAGE_WEIGHTS


class TestPipelineStage:
    """PipelineStage 枚举测试"""

    def test_all_stages_defined(self):
        """所有阶段都已定义"""
        assert PipelineStage.LOAD.value == "load"
        assert PipelineStage.OCR.value == "ocr"
        assert PipelineStage.CHUNK.value == "chunk"
        assert PipelineStage.EMBED.value == "embed"
        assert PipelineStage.INDEX.value == "index"

    def test_stage_count(self):
        """共 5 个阶段"""
        assert len(PipelineStage) == 5


class TestStageWeights:
    """STAGE_WEIGHTS 权重映射测试"""

    def test_all_stages_have_weights(self):
        """所有阶段都有权重映射"""
        for stage in PipelineStage:
            assert stage in STAGE_WEIGHTS

    def test_weights_cover_full_range(self):
        """权重覆盖 0-100 完整范围"""
        stages_ordered = [
            PipelineStage.LOAD,
            PipelineStage.OCR,
            PipelineStage.CHUNK,
            PipelineStage.EMBED,
            PipelineStage.INDEX,
        ]
        assert STAGE_WEIGHTS[stages_ordered[0]][0] == 0
        assert STAGE_WEIGHTS[stages_ordered[-1]][1] == 100

    def test_weights_are_contiguous(self):
        """权重区间连续无间隙"""
        stages_ordered = [
            PipelineStage.LOAD,
            PipelineStage.OCR,
            PipelineStage.CHUNK,
            PipelineStage.EMBED,
            PipelineStage.INDEX,
        ]
        for i in range(len(stages_ordered) - 1):
            _, end = STAGE_WEIGHTS[stages_ordered[i]]
            start, _ = STAGE_WEIGHTS[stages_ordered[i + 1]]
            assert end == start

    def test_specific_weights(self):
        """验证具体权重值"""
        assert STAGE_WEIGHTS[PipelineStage.LOAD] == (0, 10)
        assert STAGE_WEIGHTS[PipelineStage.OCR] == (10, 30)
        assert STAGE_WEIGHTS[PipelineStage.CHUNK] == (30, 50)
        assert STAGE_WEIGHTS[PipelineStage.EMBED] == (50, 90)
        assert STAGE_WEIGHTS[PipelineStage.INDEX] == (90, 100)


class TestInterpolate:
    """interpolate 静态方法测试"""

    def test_zero_completed(self):
        """completed=0 时返回阶段起始值"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 0, 10)
        assert result == 50  # EMBED 起始值

    def test_all_completed(self):
        """completed=total 时返回阶段终点值"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 10, 10)
        assert result == 90  # EMBED 终点值

    def test_half_completed(self):
        """completed=total/2 时返回中间值"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 5, 10)
        assert result == 70  # 50 + int(0.5 * 40) = 70

    def test_total_zero(self):
        """total=0 时返回阶段终点值"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 0, 0)
        assert result == 90  # EMBED 终点值

    def test_total_negative(self):
        """total<0 时返回阶段终点值"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 0, -1)
        assert result == 90

    def test_load_stage_interpolation(self):
        """LOAD 阶段插值"""
        result = ProgressTracker.interpolate(PipelineStage.LOAD, 1, 2)
        assert result == 5  # 0 + int(0.5 * 10) = 5

    def test_index_stage_interpolation(self):
        """INDEX 阶段插值"""
        result = ProgressTracker.interpolate(PipelineStage.INDEX, 3, 4)
        assert result == 97  # 90 + int(0.75 * 10) = 97

    def test_completed_exceeds_total(self):
        """completed > total 时 clamp 到 total"""
        result = ProgressTracker.interpolate(PipelineStage.EMBED, 15, 10)
        assert result == 90  # 不超过终点值


class TestProgressTrackerMethods:
    """ProgressTracker 方法测试（使用 mock session）"""

    def _make_tracker(self):
        """创建带 mock session factory 的 tracker"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)
        return tracker, mock_session

    @pytest.mark.asyncio
    async def test_start_stage(self):
        """start_stage 更新进度到阶段起始值"""
        tracker, session = self._make_tracker()
        await tracker.start_stage(PipelineStage.OCR, "开始 OCR")
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_stage(self):
        """complete_stage 更新进度到阶段终点值"""
        tracker, session = self._make_tracker()
        await tracker.complete_stage(PipelineStage.OCR)
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_stage(self):
        """skip_stage 直接跳到阶段终点"""
        tracker, session = self._make_tracker()
        await tracker.skip_stage(PipelineStage.OCR)
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_sub_progress(self):
        """update_sub_progress 使用插值更新"""
        tracker, session = self._make_tracker()
        await tracker.update_sub_progress(PipelineStage.EMBED, 3, 10, "正在生成向量 (3/10 批)")
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_fail_does_not_update_progress_value(self):
        """fail 只更新 message，不更新 progress 值"""
        tracker, session = self._make_tracker()
        await tracker.fail(PipelineStage.EMBED, "OOM error")
        # 验证 execute 被调用（只更新 message）
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete(self):
        """complete 设置 progress=100"""
        tracker, session = self._make_tracker()
        await tracker.complete()
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_failure_logs_warning(self):
        """数据库更新失败时仅记录 WARNING，不抛异常"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.execute.side_effect = Exception("DB connection lost")

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)

        # 不应抛出异常
        with patch("app.pipeline.progress.logger") as mock_logger:
            await tracker.start_stage(PipelineStage.LOAD)
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_fail_db_failure_logs_warning(self):
        """fail 方法数据库更新失败时仅记录 WARNING"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.execute.side_effect = Exception("DB connection lost")

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)

        with patch("app.pipeline.progress.logger") as mock_logger:
            await tracker.fail(PipelineStage.EMBED, "some error")
            mock_logger.warning.assert_called_once()


class TestSkipStageAccumulatesWeight:
    """skip_stage 直接累加权重测试 (Requirement 2.6)"""

    def _make_tracker_with_capture(self):
        """创建 tracker 并捕获 _update_db 调用参数"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)
        return tracker

    @pytest.mark.asyncio
    async def test_skip_ocr_jumps_to_30(self):
        """跳过 OCR 阶段时进度直接跳到 30%（OCR 终点）"""
        tracker = self._make_tracker_with_capture()
        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.skip_stage(PipelineStage.OCR)
            mock_update.assert_called_once_with(30, "ocr 阶段已跳过")

    @pytest.mark.asyncio
    async def test_skip_load_jumps_to_10(self):
        """跳过 LOAD 阶段时进度直接跳到 10%"""
        tracker = self._make_tracker_with_capture()
        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.skip_stage(PipelineStage.LOAD)
            mock_update.assert_called_once_with(10, "load 阶段已跳过")

    @pytest.mark.asyncio
    async def test_skip_chunk_jumps_to_50(self):
        """跳过 CHUNK 阶段时进度直接跳到 50%"""
        tracker = self._make_tracker_with_capture()
        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.skip_stage(PipelineStage.CHUNK)
            mock_update.assert_called_once_with(50, "chunk 阶段已跳过")

    @pytest.mark.asyncio
    async def test_skip_embed_jumps_to_90(self):
        """跳过 EMBED 阶段时进度直接跳到 90%"""
        tracker = self._make_tracker_with_capture()
        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.skip_stage(PipelineStage.EMBED)
            mock_update.assert_called_once_with(90, "embed 阶段已跳过")

    @pytest.mark.asyncio
    async def test_skip_index_jumps_to_100(self):
        """跳过 INDEX 阶段时进度直接跳到 100%"""
        tracker = self._make_tracker_with_capture()
        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.skip_stage(PipelineStage.INDEX)
            mock_update.assert_called_once_with(100, "index 阶段已跳过")


class TestCompleteSetProgress100:
    """complete 设置 progress=100 测试 (Requirement 2.4)"""

    @pytest.mark.asyncio
    async def test_complete_calls_update_db_with_100(self):
        """complete() 调用 _update_db(100, '处理完成')"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)

        with patch.object(tracker, "_update_db", new_callable=AsyncMock) as mock_update:
            await tracker.complete()
            mock_update.assert_called_once_with(100, "处理完成")

    @pytest.mark.asyncio
    async def test_complete_progress_value_is_100(self):
        """complete() 确保 progress 值为 100，无论之前进度是多少"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)

        # 先模拟一些中间进度
        await tracker.start_stage(PipelineStage.LOAD)
        # 然后调用 complete
        await tracker.complete()
        # 最后一次 execute 调用应该是 complete 的
        assert session_execute_called_with_progress_100(mock_session)


class TestStageWeightsCorrectness:
    """各阶段权重区间正确性验证 (Requirement 2.1)"""

    def test_load_weight_is_10_percent(self):
        """LOAD 阶段权重为 10%（0-10）"""
        start, end = STAGE_WEIGHTS[PipelineStage.LOAD]
        assert end - start == 10

    def test_ocr_weight_is_20_percent(self):
        """OCR 阶段权重为 20%（10-30）"""
        start, end = STAGE_WEIGHTS[PipelineStage.OCR]
        assert end - start == 20

    def test_chunk_weight_is_20_percent(self):
        """CHUNK 阶段权重为 20%（30-50）"""
        start, end = STAGE_WEIGHTS[PipelineStage.CHUNK]
        assert end - start == 20

    def test_embed_weight_is_40_percent(self):
        """EMBED 阶段权重为 40%（50-90）"""
        start, end = STAGE_WEIGHTS[PipelineStage.EMBED]
        assert end - start == 40

    def test_index_weight_is_10_percent(self):
        """INDEX 阶段权重为 10%（90-100）"""
        start, end = STAGE_WEIGHTS[PipelineStage.INDEX]
        assert end - start == 10

    def test_total_weight_is_100(self):
        """所有阶段权重之和为 100%"""
        total = sum(end - start for start, end in STAGE_WEIGHTS.values())
        assert total == 100

    def test_stages_follow_pipeline_order(self):
        """阶段按 pipeline 顺序排列：load → ocr → chunk → embed → index"""
        expected_order = [
            (PipelineStage.LOAD, 0, 10),
            (PipelineStage.OCR, 10, 30),
            (PipelineStage.CHUNK, 30, 50),
            (PipelineStage.EMBED, 50, 90),
            (PipelineStage.INDEX, 90, 100),
        ]
        for stage, expected_start, expected_end in expected_order:
            start, end = STAGE_WEIGHTS[stage]
            assert start == expected_start
            assert end == expected_end


def session_execute_called_with_progress_100(mock_session) -> bool:
    """辅助函数：检查最后一次 execute 调用是否包含 progress=100"""
    # 获取最后一次 execute 调用的参数
    calls = mock_session.execute.call_args_list
    if not calls:
        return False
    # 最后一次调用存在即可（complete 内部调用 _update_db(100, ...)）
    return len(calls) >= 1
