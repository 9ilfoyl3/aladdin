"""PipelineLogger 单元测试"""

import json
import logging

import pytest

from app.pipeline.logging import JSONFormatter, PipelineLogger, get_pipeline_logger


class TestJSONFormatter:
    """JSONFormatter 格式化器测试"""

    def test_format_outputs_valid_json(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pipeline.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "pipeline.test"
        assert data["message"] == "test message"

    def test_format_includes_structured_data(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pipeline.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=None,
            exc_info=None,
        )
        record.structured_data = {"trace_id": "abc-123", "stage": "load"}
        output = formatter.format(record)
        data = json.loads(output)
        assert data["trace_id"] == "abc-123"
        assert data["stage"] == "load"


class TestGetPipelineLogger:
    """get_pipeline_logger 测试"""

    def test_returns_logger_with_pipeline_namespace(self):
        logger = get_pipeline_logger("pipeline.test_ns")
        assert logger.name == "pipeline.test_ns"

    def test_logger_has_json_formatter(self):
        logger = get_pipeline_logger("pipeline.test_fmt")
        assert len(logger.handlers) > 0
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)

    def test_no_duplicate_handlers(self):
        """多次调用不会重复添加 handler"""
        logger = get_pipeline_logger("pipeline.test_dup")
        handler_count = len(logger.handlers)
        get_pipeline_logger("pipeline.test_dup")
        assert len(logger.handlers) == handler_count


class TestPipelineLogger:
    """PipelineLogger 结构化日志器测试"""

    def setup_method(self):
        self.logger = PipelineLogger(
            trace_id="test-trace-001",
            doc_id="test-doc-001",
            slow_threshold_ms=5000,
        )

    def test_stage_complete_outputs_required_fields(self, caplog):
        """阶段日志包含所有必需字段"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="load",
                duration_ms=100,
                input_size=1024,
                output_size=512,
            )

        # 解析 JSON 输出
        assert len(caplog.records) == 1
        record = caplog.records[0]
        data = record.structured_data

        assert data["trace_id"] == "test-trace-001"
        assert data["doc_id"] == "test-doc-001"
        assert data["stage"] == "load"
        assert data["duration_ms"] == 100
        assert data["input_size"] == 1024
        assert data["output_size"] == 512
        assert data["status"] == "success"

    def test_stage_complete_embed_extra_fields(self, caplog):
        """embed 阶段包含额外字段"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="embed",
                duration_ms=2000,
                input_size=50,
                output_size=50,
                batch_count=10,
                total_chunks=1280,
                avg_batch_duration_ms=200,
            )

        record = caplog.records[0]
        data = record.structured_data

        assert data["batch_count"] == 10
        assert data["total_chunks"] == 1280
        assert data["avg_batch_duration_ms"] == 200

    def test_stage_complete_non_embed_ignores_extra_embed_fields(self, caplog):
        """非 embed 阶段不包含 embed 额外字段"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="load",
                duration_ms=100,
                input_size=10,
                output_size=10,
                batch_count=5,  # 应被忽略
            )

        record = caplog.records[0]
        data = record.structured_data
        assert "batch_count" not in data

    def test_slow_stage_detection_warning(self, caplog):
        """慢阶段检测：超过阈值时级别为 WARNING 且包含 slow=true"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="embed",
                duration_ms=6000,  # > 5000 阈值
                input_size=100,
                output_size=100,
            )

        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        data = record.structured_data
        assert data["slow"] is True

    def test_normal_stage_info_level(self, caplog):
        """正常阶段：级别为 INFO 且不包含 slow 字段"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="load",
                duration_ms=100,  # < 5000 阈值
                input_size=10,
                output_size=10,
            )

        record = caplog.records[0]
        assert record.levelno == logging.INFO
        data = record.structured_data
        assert "slow" not in data

    def test_slow_threshold_boundary_equal(self, caplog):
        """边界条件：duration_ms == slow_threshold_ms 时不触发慢阶段"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete(
                stage="chunk",
                duration_ms=5000,  # == 阈值，不触发
                input_size=10,
                output_size=10,
            )

        record = caplog.records[0]
        assert record.levelno == logging.INFO
        assert "slow" not in record.structured_data

    def test_summary_outputs_all_stage_timings(self, caplog):
        """汇总日志包含所有阶段耗时"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete("load", 100, 10, 10)
            self.logger.stage_complete("ocr", 200, 10, 10)
            self.logger.stage_complete("chunk", 300, 10, 10)
            self.logger.stage_complete("embed", 400, 10, 10)
            self.logger.stage_complete("index", 500, 10, 10)
            self.logger.summary(total_duration_ms=1500)

        # 最后一条是 summary
        summary_record = caplog.records[-1]
        data = summary_record.structured_data

        assert data["trace_id"] == "test-trace-001"
        assert data["doc_id"] == "test-doc-001"
        assert data["stage"] == "summary"
        assert data["total_duration_ms"] == 1500
        assert data["stages"] == {
            "load": 100,
            "ocr": 200,
            "chunk": 300,
            "embed": 400,
            "index": 500,
        }

    def test_trace_id_consistent_across_all_logs(self, caplog):
        """trace_id 在所有日志中保持一致"""
        with caplog.at_level(logging.DEBUG, logger="pipeline.trace"):
            self.logger.stage_complete("load", 100, 10, 10)
            self.logger.stage_complete("chunk", 200, 10, 10)
            self.logger.summary(total_duration_ms=300)

        for record in caplog.records:
            assert record.structured_data["trace_id"] == "test-trace-001"
