"""Trace ID 链路追踪属性测试

使用 Hypothesis 验证 trace_id 在所有阶段日志中的一致性。

Feature: pipeline-production-optimization, Property 11: Trace ID 在所有阶段日志中一致
"""

import logging
import re
import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

from app.pipeline.logging import PipelineLogger


# --- Strategies ---

# 合理的耗时范围 (0ms ~ 120_000ms)
duration_ms_st = st.integers(min_value=0, max_value=120_000)

# 合理的数据大小
size_st = st.integers(min_value=0, max_value=1_000_000)

# doc_id
doc_id_st = st.uuids().map(str)

# UUID4 正则
UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# 所有管道阶段
ALL_STAGES = ["load", "ocr", "chunk", "embed", "index"]


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


class TestProperty11TraceIdConsistency:
    """Property 11: Trace ID 在所有阶段日志中一致

    *For any* 文档处理过程，所有输出的结构化日志条目中的 trace_id 字段值
    应相同，且为有效的 UUID4 格式。

    **Validates: Requirements 5.1**
    """

    @settings(max_examples=100)
    @given(
        trace_id=st.uuids(version=4).map(str),
        doc_id=doc_id_st,
        load_duration=duration_ms_st,
        ocr_duration=duration_ms_st,
        chunk_duration=duration_ms_st,
        embed_duration=duration_ms_st,
        index_duration=duration_ms_st,
        load_input=size_st,
        load_output=size_st,
        ocr_input=size_st,
        ocr_output=size_st,
        chunk_input=size_st,
        chunk_output=size_st,
        embed_input=size_st,
        embed_output=size_st,
        index_input=size_st,
        index_output=size_st,
    )
    def test_trace_id_consistent_across_all_stages(
        self,
        trace_id: str,
        doc_id: str,
        load_duration: int,
        ocr_duration: int,
        chunk_duration: int,
        embed_duration: int,
        index_duration: int,
        load_input: int,
        load_output: int,
        ocr_input: int,
        ocr_output: int,
        chunk_input: int,
        chunk_output: int,
        embed_input: int,
        embed_output: int,
        index_input: int,
        index_output: int,
    ):
        """Property 11: 所有阶段日志和 summary 日志中 trace_id 一致且为有效 UUID4"""
        handler = _make_capture_handler()
        try:
            logger = PipelineLogger(
                trace_id=trace_id,
                doc_id=doc_id,
            )

            handler.clear()

            # 模拟完整文档处理：调用所有阶段的 stage_complete
            durations = [load_duration, ocr_duration, chunk_duration, embed_duration, index_duration]
            inputs = [load_input, ocr_input, chunk_input, embed_input, index_input]
            outputs = [load_output, ocr_output, chunk_output, embed_output, index_output]

            for i, stage in enumerate(ALL_STAGES):
                logger.stage_complete(
                    stage=stage,
                    duration_ms=durations[i],
                    input_size=inputs[i],
                    output_size=outputs[i],
                )

            # 调用 summary
            total_duration_ms = sum(durations)
            logger.summary(total_duration_ms=total_duration_ms)

            # 验证：应有 5 个阶段日志 + 1 个 summary 日志 = 6 条记录
            assert len(handler.records) == 6

            # 验证所有日志条目的 trace_id 一致且为有效 UUID4
            for record in handler.records:
                assert hasattr(record, "structured_data")
                data = record.structured_data

                # trace_id 字段存在
                assert "trace_id" in data

                # trace_id 值与传入的一致
                assert data["trace_id"] == trace_id

                # trace_id 为有效的 UUID4 格式
                assert UUID4_PATTERN.match(data["trace_id"]) is not None, (
                    f"trace_id '{data['trace_id']}' is not a valid UUID4"
                )
        finally:
            _remove_capture_handler(handler)
