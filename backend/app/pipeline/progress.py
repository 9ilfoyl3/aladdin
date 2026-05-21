"""进度追踪器 - 文档处理管道各阶段加权进度更新

提供：
- PipelineStage 枚举（load/ocr/chunk/embed/index）
- STAGE_WEIGHTS 阶段权重映射
- ProgressTracker 类：管理文档处理进度的实时更新
"""

import logging
from enum import Enum

from sqlalchemy import update

from app.schema.db import Document

logger = logging.getLogger("pipeline.progress")


class PipelineStage(Enum):
    """管道处理阶段"""

    LOAD = "load"       # 权重 10%
    OCR = "ocr"         # 权重 20%
    CHUNK = "chunk"     # 权重 20%
    EMBED = "embed"     # 权重 40%
    INDEX = "index"     # 权重 10%


# 各阶段对应的进度区间 (start%, end%)
STAGE_WEIGHTS = {
    PipelineStage.LOAD: (0, 10),    # 0% - 10%
    PipelineStage.OCR: (10, 30),    # 10% - 30%
    PipelineStage.CHUNK: (30, 50),  # 30% - 50%
    PipelineStage.EMBED: (50, 90),  # 50% - 90%
    PipelineStage.INDEX: (90, 100), # 90% - 100%
}


class ProgressTracker:
    """文档处理进度追踪器

    通过数据库直接更新 Document 表的 progress 和 progress_message 字段。
    进度更新失败时仅记录 WARNING，不影响主流程。
    """

    def __init__(self, doc_id: str, db_session_factory):
        """初始化进度追踪器

        Args:
            doc_id: 文档 ID
            db_session_factory: 异步数据库会话工厂（async_sessionmaker）
        """
        self.doc_id = doc_id
        self._db_session_factory = db_session_factory

    async def _update_db(self, progress: int, message: str) -> None:
        """更新数据库中的进度信息

        失败时仅记录 WARNING，不抛出异常。
        """
        # 确保进度值在 [0, 100] 范围内
        progress = max(0, min(100, progress))
        try:
            async with self._db_session_factory() as session:
                stmt = (
                    update(Document)
                    .where(Document.id == self.doc_id)
                    .values(progress=progress, progress_message=message)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning(
                "进度更新失败 doc_id=%s progress=%d: %s",
                self.doc_id, progress, str(e)
            )

    async def start_stage(self, stage: PipelineStage, message: str = "") -> None:
        """标记阶段开始

        将进度设置为该阶段的起始值。

        Args:
            stage: 管道阶段
            message: 可选的进度描述信息
        """
        start, _ = STAGE_WEIGHTS[stage]
        if not message:
            message = f"正在处理: {stage.value}"
        await self._update_db(start, message)

    async def complete_stage(self, stage: PipelineStage) -> None:
        """标记阶段完成

        将进度更新到该阶段的终点值。

        Args:
            stage: 管道阶段
        """
        _, end = STAGE_WEIGHTS[stage]
        message = f"{stage.value} 阶段完成"
        await self._update_db(end, message)

    async def skip_stage(self, stage: PipelineStage) -> None:
        """跳过阶段

        直接将进度累加到该阶段的终点值（跳过中间更新）。

        Args:
            stage: 管道阶段
        """
        _, end = STAGE_WEIGHTS[stage]
        message = f"{stage.value} 阶段已跳过"
        await self._update_db(end, message)

    async def update_sub_progress(
        self, stage: PipelineStage, completed: int, total: int, message: str = ""
    ) -> None:
        """阶段内子进度更新

        使用线性插值计算当前进度。例如 embed 阶段处理 batch 时，
        按已完成 batch 数 / 总 batch 数在该阶段权重区间内插值。

        Args:
            stage: 管道阶段
            completed: 已完成的子任务数
            total: 总子任务数
            message: 可选的进度描述信息
        """
        progress = self.interpolate(stage, completed, total)
        if not message:
            message = f"正在处理 {stage.value} ({completed}/{total})"
        await self._update_db(progress, message)

    async def fail(self, stage: PipelineStage, error_message: str) -> None:
        """标记失败

        progress 保持不变（不更新进度值），仅更新 progress_message 记录失败信息。

        Args:
            stage: 失败的管道阶段
            error_message: 错误信息
        """
        message = f"{stage.value} 阶段失败: {error_message}"
        try:
            async with self._db_session_factory() as session:
                stmt = (
                    update(Document)
                    .where(Document.id == self.doc_id)
                    .values(progress_message=message)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning(
                "进度更新失败 doc_id=%s stage=%s: %s",
                self.doc_id, stage.value, str(e)
            )

    async def complete(self) -> None:
        """标记处理完成

        将 progress 设为 100，progress_message 设为 "处理完成"。
        """
        await self._update_db(100, "处理完成")

    @staticmethod
    def interpolate(stage: PipelineStage, completed: int, total: int) -> int:
        """计算阶段内线性插值进度

        在阶段的 [start, end] 区间内，按 completed/total 比例计算当前进度值。

        Args:
            stage: 管道阶段
            completed: 已完成的子任务数
            total: 总子任务数

        Returns:
            int: 插值后的进度值（0-100）
        """
        start, end = STAGE_WEIGHTS[stage]
        if total <= 0:
            return end
        # 确保 completed 不超过 total
        completed = max(0, min(completed, total))
        return start + int((completed / total) * (end - start))
