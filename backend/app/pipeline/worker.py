"""PipelineWorker - 管道工作进程，消费 Redis Stream 任务

提供：
- PipelineWorker 类：从 TaskQueue 消费任务，通过 Semaphore 控制并发，
  执行 DocumentPipeline 处理，支持失败重试和死信队列。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.pipeline.queue import TaskMessage, TaskQueue
from app.pipeline.pipeline import CancelledError
from app.schema.db import Document

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.pipeline.pipeline import DocumentPipeline

logger = logging.getLogger("pipeline.worker")

# 不可重试的错误类型：文件不存在、参数错误、权限不足、任务取消
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, PermissionError, CancelledError)


class PipelineWorker:
    """管道工作进程，消费 Redis Stream 任务

    启动时先 claim_pending() 恢复中断任务，再循环 consume() 新任务。
    使用 asyncio.Semaphore 控制并发处理文件数。
    """

    def __init__(
        self,
        queue: TaskQueue,
        pipeline: DocumentPipeline,
        db_session_factory: "async_sessionmaker[AsyncSession]",
        max_concurrent: int = 3,
        max_retries: int = 3,
    ):
        """初始化 PipelineWorker

        Args:
            queue: Redis Stream 任务队列
            pipeline: 文档处理管道实例
            db_session_factory: 异步数据库会话工厂
            max_concurrent: 最大并发处理文件数
            max_retries: 最大重试次数
        """
        self._queue = queue
        self._pipeline = pipeline
        self._db_session_factory = db_session_factory
        self._max_concurrent = max_concurrent
        self._max_retries = max_retries
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False
        self._consumer_name = f"worker-{socket.gethostname()}"
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """启动 Worker 循环：claim pending → consume new

        1. 输出启动日志
        2. 恢复中断的 pending 任务
        3. 循环消费新任务
        """
        self._running = True
        logger.info(
            "Pipeline worker started, max_concurrent=%d", self._max_concurrent
        )

        # 恢复中断的 pending 任务
        try:
            pending = await self._queue.claim_pending(
                self._consumer_name, min_idle_ms=60000
            )
            for message_id, msg in pending:
                task = asyncio.create_task(
                    self._process_task(message_id, msg)
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception as e:
            logger.warning("Failed to claim pending tasks: %s", e)

        # 主消费循环
        while self._running:
            try:
                messages = await self._queue.consume(
                    self._consumer_name, count=1, block_ms=5000
                )
                for message_id, msg in messages:
                    task = asyncio.create_task(
                        self._process_task(message_id, msg)
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error consuming tasks: %s, retrying in 5s", e)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 Worker

        设置 running=False，等待所有正在处理的任务完成。
        """
        self._running = False
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _process_task(self, message_id: str, msg: TaskMessage) -> None:
        """处理单个任务

        流程：
        1. 检查文档状态，已 completed 则跳过（幂等）
        2. acquire semaphore 控制并发
        3. 执行 pipeline.process()
        4. 成功则 ACK，失败则调用 _handle_failure

        Args:
            message_id: Redis Stream 消息 ID
            msg: 任务消息
        """
        # 幂等检查：文档已完成则跳过
        if await self._is_document_completed(msg.doc_id):
            print(f"[Worker] 文档 {msg.doc_id} 已完成/取消/删除，跳过")
            logger.info(
                "Document %s already completed, skipping (trace_id=%s)",
                msg.doc_id, msg.trace_id,
            )
            await self._queue.ack(message_id)
            return

        print(f"[Worker] 开始处理文档 {msg.doc_id} (trace_id={msg.trace_id})")

        # 通过 semaphore 控制并发
        async with self.semaphore:
            try:
                await self._pipeline.process(
                    file_path=msg.file_path,
                    doc_id=msg.doc_id,
                    kb_id=msg.kb_id,
                )
                # 处理成功，ACK 消息
                await self._queue.ack(message_id)
                logger.info(
                    "Task completed: doc_id=%s (trace_id=%s)",
                    msg.doc_id, msg.trace_id,
                )
            except Exception as e:
                await self._handle_failure(message_id, msg, e)

    async def _handle_failure(
        self, message_id: str, msg: TaskMessage, error: Exception
    ) -> None:
        """失败处理

        - 不可重试错误（FileNotFoundError、ValueError、PermissionError）直接进 DLQ
        - retry_count < max_retries 时指数退避重新入队
        - 否则移入 DLQ

        Args:
            message_id: Redis Stream 消息 ID
            msg: 任务消息
            error: 异常对象
        """
        error_str = f"{type(error).__name__}: {error}"

        # 不可重试错误直接进 DLQ
        if isinstance(error, NON_RETRYABLE_ERRORS):
            logger.error(
                "Non-retryable error for doc_id=%s, moving to DLQ: %s (trace_id=%s)",
                msg.doc_id, error_str, msg.trace_id,
            )
            await self._queue.move_to_dlq(message_id, msg, error_str)
            return

        # 检查重试次数
        next_retry = msg.retry_count + 1
        if next_retry <= self._max_retries:
            # 指数退避：delay = 2^(retry_count - 1)，retry_count 从 1 开始
            delay = 2 ** (next_retry - 1)  # 1s, 2s, 4s
            logger.warning(
                "Task failed for doc_id=%s (retry %d/%d), "
                "retrying in %ds: %s (trace_id=%s)",
                msg.doc_id, next_retry, self._max_retries,
                delay, error_str, msg.trace_id,
            )
            # ACK 当前消息
            await self._queue.ack(message_id)
            # 等待退避时间后重新入队
            await asyncio.sleep(delay)
            # 重新入队，增加 retry_count
            retry_msg = TaskMessage(
                doc_id=msg.doc_id,
                kb_id=msg.kb_id,
                file_path=msg.file_path,
                retry_count=next_retry,
                created_at=msg.created_at,
                trace_id=msg.trace_id,
            )
            await self._queue.enqueue(retry_msg)
        else:
            # 超过最大重试次数，移入 DLQ
            logger.error(
                "Max retries exceeded for doc_id=%s, moving to DLQ: %s (trace_id=%s)",
                msg.doc_id, error_str, msg.trace_id,
            )
            await self._queue.move_to_dlq(message_id, msg, error_str)

    async def _is_document_completed(self, doc_id: str) -> bool:
        """检查文档是否应跳过处理

        跳过条件：文档已完成、已取消、或已被删除（不存在）。

        Args:
            doc_id: 文档 ID

        Returns:
            True 如果文档应跳过处理
        """
        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    select(Document.status).where(Document.id == doc_id)
                )
                status = result.scalar_one_or_none()
                # 文档不存在 = 已被删除，应跳过
                if status is None:
                    return True
                return status in ("completed", "cancelled")
        except Exception as e:
            print(f"[Worker] ❌ 查询文档状态失败 {doc_id}: {e}")
            logger.warning(
                "Failed to check document status for %s: %s", doc_id, e
            )
            # 无法确认状态时，继续处理（不跳过）
            return False
            return False
