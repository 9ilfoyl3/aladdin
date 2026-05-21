"""Pipeline 结构化日志模块

提供 JSON 格式的结构化日志输出，支持链路追踪和慢阶段检测。
所有 pipeline 相关 logger 使用 `pipeline.*` 命名空间。
"""

import json
import logging
from typing import Any


class JSONFormatter(logging.Formatter):
    """JSON 格式化器，将日志记录输出为单行 JSON"""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 合并结构化字段（通过 extra 传入）
        if hasattr(record, "structured_data"):
            log_data.update(record.structured_data)

        return json.dumps(log_data, ensure_ascii=False)


def get_pipeline_logger(name: str = "pipeline") -> logging.Logger:
    """获取 pipeline 命名空间的 logger，配置 JSONFormatter 输出到 stdout"""
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

    return logger


class PipelineLogger:
    """管道结构化日志器

    为单个文档处理流程提供结构化日志输出，包含：
    - 各阶段完成日志（含耗时、输入输出大小）
    - embed 阶段额外字段（batch_count、total_chunks、avg_batch_duration_ms）
    - 慢阶段检测（超过阈值时提升为 WARNING）
    - 处理汇总日志
    """

    def __init__(self, trace_id: str, doc_id: str, slow_threshold_ms: int = 30000):
        self.trace_id = trace_id
        self.doc_id = doc_id
        self.slow_threshold_ms = slow_threshold_ms
        self._stage_timings: dict[str, int] = {}
        self._logger = get_pipeline_logger("pipeline.trace")

    def stage_complete(
        self,
        stage: str,
        duration_ms: int,
        input_size: int,
        output_size: int,
        **extra: Any,
    ) -> None:
        """输出阶段完成日志

        Args:
            stage: 阶段名称（load/ocr/chunk/embed/index）
            duration_ms: 阶段耗时（毫秒）
            input_size: 输入数据大小
            output_size: 输出数据大小
            **extra: 额外字段（如 embed 阶段的 batch_count 等）
        """
        self._stage_timings[stage] = duration_ms

        log_data: dict[str, Any] = {
            "trace_id": self.trace_id,
            "doc_id": self.doc_id,
            "stage": stage,
            "duration_ms": duration_ms,
            "input_size": input_size,
            "output_size": output_size,
            "status": "success",
        }

        # embed 阶段额外字段
        if stage == "embed":
            for key in ("batch_count", "total_chunks", "avg_batch_duration_ms"):
                if key in extra:
                    log_data[key] = extra[key]

        # 慢阶段检测
        is_slow = duration_ms > self.slow_threshold_ms
        if is_slow:
            log_data["slow"] = True

        # 选择日志级别
        level = logging.WARNING if is_slow else logging.INFO

        self._logger.log(
            level,
            f"Stage {stage} completed in {duration_ms}ms",
            extra={"structured_data": log_data},
        )

    def summary(self, total_duration_ms: int) -> None:
        """输出处理汇总日志

        Args:
            total_duration_ms: 总处理耗时（毫秒）
        """
        log_data: dict[str, Any] = {
            "trace_id": self.trace_id,
            "doc_id": self.doc_id,
            "stage": "summary",
            "total_duration_ms": total_duration_ms,
            "stages": dict(self._stage_timings),
        }

        self._logger.info(
            f"Pipeline completed in {total_duration_ms}ms",
            extra={"structured_data": log_data},
        )
