"""ProgressTracker 属性测试

使用 Hypothesis 验证 ProgressTracker 的正确性属性。

Feature: pipeline-production-optimization
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app.pipeline.progress import PipelineStage, ProgressTracker, STAGE_WEIGHTS


# --- Strategies ---

# 合理的 total 值（> 0）
total_st = st.integers(min_value=1, max_value=10_000)

# 管道阶段
pipeline_stage_st = st.sampled_from(list(PipelineStage))

# 错误消息
error_message_st = st.text(min_size=1, max_size=200)


class TestProperty5EmbedInterpolation:
    """Property 5: Embed 阶段进度线性插值正确

    *For any* total > 0 和 0 ≤ completed ≤ total，
    interpolate(EMBED, completed, total) == 50 + int((completed / total) * 40)

    **Validates: Requirements 2.3**
    """

    @settings(max_examples=100)
    @given(
        total=total_st,
        data=st.data(),
    )
    def test_embed_interpolation_formula(self, total: int, data):
        """Property 5: Embed 阶段进度线性插值正确"""
        completed = data.draw(st.integers(min_value=0, max_value=total))

        result = ProgressTracker.interpolate(PipelineStage.EMBED, completed, total)
        expected = 50 + int((completed / total) * 40)

        assert result == expected


class TestProperty6FailDoesNotChangeProgress:
    """Property 6: 失败时进度值不变

    *For any* 管道阶段和错误消息，调用 fail() 后，
    数据库更新语句中不包含 progress 字段（仅更新 progress_message）。

    **Validates: Requirements 2.5**
    """

    @settings(max_examples=100)
    @given(
        stage=pipeline_stage_st,
        error_message=error_message_st,
    )
    @pytest.mark.asyncio
    async def test_fail_only_updates_message_not_progress(
        self, stage: PipelineStage, error_message: str
    ):
        """Property 6: 失败时进度值不变"""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=mock_session)
        tracker = ProgressTracker("test-doc-id", mock_factory)

        await tracker.fail(stage, error_message)

        # 验证 execute 被调用
        mock_session.execute.assert_called_once()

        # 获取传入 execute 的 SQL 语句
        stmt = mock_session.execute.call_args[0][0]

        # 验证 SQL UPDATE 语句的 values 中只有 progress_message，没有 progress
        # SQLAlchemy Update 对象的 _values 包含要更新的列
        compiled = stmt.compile()
        params = compiled.params

        # progress_message 应该存在于参数中
        assert "progress_message" in params

        # progress 不应该存在于参数中
        assert "progress" not in params
