"""PipelineLogger 属性测试

使用 Hypothesis 验证 PipelineLogger 的正确性属性。

Feature: pipeline-production-optimization
"""

import logging
import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

from app.pipeline.logging import PipelineLogger


# --- Strategies ---

# 管道阶段名称
pipeline_stages = st.sampled_from(["load", "ocr", "chunk", "embed", "index"])

# 合理的耗时范围 (0ms ~ 120_000ms)
duration_ms_st = st.integers(min_value=0, max_value=120_000)

# 合理的数据大小
size_st = st.integers(min_value=0, max_value=1_000_000)

# 慢阶段阈值
slow_threshold_st = st.integers(min_value=1, max_value=60_000)

# trace_id 和 doc_id
trace_id_st = st.from_type(uuid.UUID).map(str)
doc_id_st = st.uuids().map(str)


class _RecordCapture(logging.Handler):
    """轻量级日志捕获 handler，用于属性测试中替代 caplog"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def clear(self) -> None:
        self.records.clear()


def _make_capture_handler() -> _RecordCapture:
    """创建并挂载到 pipeline.trace logger 的捕获 handler"""
    handler = _RecordCapture()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("pipeline.trace")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


def _remove_capture_handler(handler: _RecordCapture) -> None:
    """移除捕获 handler"""
    logger = logging.getLogger("pipeline.trace")
    logger.removeHandler(handler)


class TestProperty12StageLogRequiredFields:
    """Property 12: 阶段日志包含所有必需字段

    *For any* 管道阶段（load/ocr/chunk/embed/index）的完成日志，
    输出的 JSON 应包含字段：trace_id、doc_id、stage、duration_ms、
    input_size、output_size、status，且所有字段值类型正确。

    **Validates: Requirements 5.2**
    """

    @settings(max_examples=100)
    @given(
        trace_id=trace_id_st,
        doc_id=doc_id_st,
        stage=pipeline_stages,
        duration_ms=duration_ms_st,
        input_size=size_st,
        output_size=size_st,
        slow_threshold_ms=slow_threshold_st,
    )
    def test_stage_log_contains_all_required_fields(
        self,
        trace_id: str,
        doc_id: str,
        stage: str,
        duration_ms: int,
        input_size: int,
        output_size: int,
        slow_threshold_ms: int,
    ):
        """Property 12: 阶段日志包含所有必需字段"""
        handler = _make_capture_handler()
        try:
            logger = PipelineLogger(
                trace_id=trace_id,
                doc_id=doc_id,
                slow_threshold_ms=slow_threshold_ms,
            )

            handler.clear()
            logger.stage_complete(
                stage=stage,
                duration_ms=duration_ms,
                input_size=input_size,
                output_size=output_size,
            )

            assert len(handler.records) == 1
            record = handler.records[0]
            data = record.structured_data

            # 验证所有必需字段存在
            assert "trace_id" in data
            assert "doc_id" in data
            assert "stage" in data
            assert "duration_ms" in data
            assert "input_size" in data
            assert "output_size" in data
            assert "status" in data

            # 验证字段值正确
            assert data["trace_id"] == trace_id
            assert data["doc_id"] == doc_id
            assert data["stage"] == stage
            assert data["duration_ms"] == duration_ms
            assert data["input_size"] == input_size
            assert data["output_size"] == output_size
            assert data["status"] == "success"

            # 验证字段类型正确
            assert isinstance(data["trace_id"], str)
            assert isinstance(data["doc_id"], str)
            assert isinstance(data["stage"], str)
            assert isinstance(data["duration_ms"], int)
            assert isinstance(data["input_size"], int)
            assert isinstance(data["output_size"], int)
            assert isinstance(data["status"], str)
        finally:
            _remove_capture_handler(handler)


class TestProperty13SummaryDurationSum:
    """Property 13: 汇总日志各阶段耗时之和等于总耗时

    *For any* 完成的文档处理，汇总日志中 stages 字典各值之和
    应等于 total_duration_ms（允许 ±10ms 误差）。

    **Validates: Requirements 5.4**
    """

    @settings(max_examples=100)
    @given(
        trace_id=trace_id_st,
        doc_id=doc_id_st,
        load_ms=st.integers(min_value=0, max_value=30_000),
        ocr_ms=st.integers(min_value=0, max_value=30_000),
        chunk_ms=st.integers(min_value=0, max_value=30_000),
        embed_ms=st.integers(min_value=0, max_value=30_000),
        index_ms=st.integers(min_value=0, max_value=30_000),
    )
    def test_summary_stages_sum_equals_total(
        self,
        trace_id: str,
        doc_id: str,
        load_ms: int,
        ocr_ms: int,
        chunk_ms: int,
        embed_ms: int,
        index_ms: int,
    ):
        """Property 13: 汇总日志各阶段耗时之和等于总耗时"""
        handler = _make_capture_handler()
        try:
            logger = PipelineLogger(
                trace_id=trace_id,
                doc_id=doc_id,
            )

            handler.clear()
            logger.stage_complete("load", load_ms, 10, 10)
            logger.stage_complete("ocr", ocr_ms, 10, 10)
            logger.stage_complete("chunk", chunk_ms, 10, 10)
            logger.stage_complete("embed", embed_ms, 10, 10)
            logger.stage_complete("index", index_ms, 10, 10)

            total_duration_ms = load_ms + ocr_ms + chunk_ms + embed_ms + index_ms
            logger.summary(total_duration_ms=total_duration_ms)

            # 找到 summary 日志记录
            summary_records = [
                r for r in handler.records
                if hasattr(r, "structured_data")
                and r.structured_data.get("stage") == "summary"
            ]
            assert len(summary_records) == 1

            data = summary_records[0].structured_data
            stages = data["stages"]

            # 验证 stages 字典各值之和等于 total_duration_ms（允许 ±10ms 误差）
            stages_sum = sum(stages.values())
            assert abs(stages_sum - data["total_duration_ms"]) <= 10
        finally:
            _remove_capture_handler(handler)


class TestProperty14SlowStageDetection:
    """Property 14: 慢阶段检测阈值正确

    *For any* 阶段耗时 D 和阈值 T，当 D > T 时日志级别应为 WARNING
    且包含 `"slow": true`；当 D ≤ T 时日志级别应为 INFO 且不包含 slow 字段。

    **Validates: Requirements 5.6**
    """

    @settings(max_examples=100)
    @given(
        trace_id=trace_id_st,
        doc_id=doc_id_st,
        stage=pipeline_stages,
        duration_ms=duration_ms_st,
        slow_threshold_ms=slow_threshold_st,
        input_size=size_st,
        output_size=size_st,
    )
    def test_slow_stage_threshold_correct(
        self,
        trace_id: str,
        doc_id: str,
        stage: str,
        duration_ms: int,
        slow_threshold_ms: int,
        input_size: int,
        output_size: int,
    ):
        """Property 14: 慢阶段检测阈值正确"""
        handler = _make_capture_handler()
        try:
            logger = PipelineLogger(
                trace_id=trace_id,
                doc_id=doc_id,
                slow_threshold_ms=slow_threshold_ms,
            )

            handler.clear()
            logger.stage_complete(
                stage=stage,
                duration_ms=duration_ms,
                input_size=input_size,
                output_size=output_size,
            )

            assert len(handler.records) == 1
            record = handler.records[0]
            data = record.structured_data

            if duration_ms > slow_threshold_ms:
                # 超过阈值：WARNING 级别 + slow=true
                assert record.levelno == logging.WARNING
                assert data.get("slow") is True
            else:
                # 未超过阈值：INFO 级别 + 无 slow 字段
                assert record.levelno == logging.INFO
                assert "slow" not in data
        finally:
            _remove_capture_handler(handler)
