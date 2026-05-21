"""持久化任务队列 - 基于 Redis Stream 的任务管理

提供：
- TaskMessage 任务消息数据结构
- QueueStats 队列统计响应模型
- TaskQueue Redis Stream 任务队列
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel

logger = logging.getLogger("pipeline.queue")


@dataclass
class TaskMessage:
    """任务消息结构"""

    doc_id: str
    kb_id: str
    file_path: str
    retry_count: int = 0
    created_at: float = 0.0  # timestamp
    trace_id: str = ""  # UUID4


class QueueStats(BaseModel):
    """队列统计信息响应模型"""

    stream_length: int = 0       # Stream 总消息数
    pending_count: int = 0       # 未 ACK 的消息数
    active_workers: int = 0      # 活跃 consumer 数
    dlq_length: int = 0          # 死信队列长度


class TaskQueue:
    """Redis Stream 任务队列

    基于 Redis Stream + Consumer Group 实现任务持久化、
    自动恢复、重试与死信处理。
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream_key: str = "pipeline:tasks",
        dlq_key: str = "pipeline:dlq",
        group_name: str = "pipeline-workers",
    ):
        self._redis = redis_client
        self._stream_key = stream_key
        self._dlq_key = dlq_key
        self._group_name = group_name

    async def _ensure_group(self) -> None:
        """确保 consumer group 存在，不存在则创建"""
        try:
            await self._redis.xgroup_create(
                self._stream_key, self._group_name, id="0", mkstream=True
            )
        except aioredis.ResponseError as e:
            # BUSYGROUP 表示 group 已存在，忽略
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, msg: TaskMessage) -> str:
        """写入任务到 Stream，返回 message_id

        自动填充 created_at 和 trace_id（如果未设置）。
        """
        if msg.created_at == 0.0:
            msg.created_at = time.time()
        if not msg.trace_id:
            msg.trace_id = str(uuid.uuid4())

        data = asdict(msg)
        # Redis Stream 要求所有值为 str/bytes
        payload = {"data": json.dumps(data)}
        message_id: bytes = await self._redis.xadd(self._stream_key, payload)
        # message_id 可能是 bytes 或 str
        return message_id.decode() if isinstance(message_id, bytes) else message_id

    async def consume(
        self, consumer_name: str, count: int = 1, block_ms: int = 5000
    ) -> list[tuple[str, TaskMessage]]:
        """消费消息，返回 [(message_id, TaskMessage), ...]

        使用 XREADGROUP 从 consumer group 中读取新消息。
        """
        results: list[tuple[str, TaskMessage]] = []
        try:
            response = await self._redis.xreadgroup(
                groupname=self._group_name,
                consumername=consumer_name,
                streams={self._stream_key: ">"},
                count=count,
                block=block_ms,
            )
        except aioredis.ResponseError:
            return results

        if not response:
            return results

        for _stream_name, messages in response:
            for msg_id, fields in messages:
                parsed = self._parse_message(msg_id, fields)
                if parsed:
                    results.append(parsed)
                else:
                    # 反序列化失败，直接 ACK 避免阻塞队列
                    mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    logger.error(
                        "Failed to deserialize message %s, ACKing to avoid blocking",
                        mid,
                    )
                    await self.ack(mid)

        return results

    async def ack(self, message_id: str) -> None:
        """确认消息处理完成"""
        await self._redis.xack(self._stream_key, self._group_name, message_id)

    async def move_to_dlq(
        self, message_id: str, msg: TaskMessage, error: str
    ) -> None:
        """将失败任务移入死信队列

        将消息数据和错误信息写入 DLQ stream，然后 ACK 原消息。
        """
        data = asdict(msg)
        data["error"] = error
        data["failed_at"] = time.time()
        payload = {"data": json.dumps(data)}
        await self._redis.xadd(self._dlq_key, payload)
        await self.ack(message_id)

    async def claim_pending(
        self, consumer_name: str, min_idle_ms: int = 60000
    ) -> list[tuple[str, TaskMessage]]:
        """认领超时的 pending 消息（用于启动恢复）

        使用 XAUTOCLAIM 认领空闲超过 min_idle_ms 的消息。
        """
        results: list[tuple[str, TaskMessage]] = []
        try:
            # XAUTOCLAIM 返回 (next_start_id, [(msg_id, fields), ...], deleted_ids)
            response = await self._redis.xautoclaim(
                name=self._stream_key,
                groupname=self._group_name,
                consumername=consumer_name,
                min_idle_time=min_idle_ms,
                start_id="0-0",
            )
        except (aioredis.ResponseError, aioredis.ConnectionError):
            return results

        if not response or len(response) < 2:
            return results

        messages = response[1]
        for msg_id, fields in messages:
            if fields is None:
                # 消息已被删除
                continue
            parsed = self._parse_message(msg_id, fields)
            if parsed:
                results.append(parsed)

        return results

    async def get_stats(self) -> QueueStats:
        """获取队列统计信息"""
        stream_length = 0
        pending_count = 0
        active_workers = 0
        dlq_length = 0

        try:
            # Stream 总长度
            stream_length = await self._redis.xlen(self._stream_key)
        except (aioredis.ResponseError, aioredis.ConnectionError):
            pass

        try:
            # Pending 消息数和活跃 consumer 数
            info = await self._redis.xinfo_groups(self._stream_key)
            for group in info:
                name = group.get("name", b"")
                if isinstance(name, bytes):
                    name = name.decode()
                if name == self._group_name:
                    pending_count = group.get("pending", 0)
                    active_workers = group.get("consumers", 0)
                    break
        except (aioredis.ResponseError, aioredis.ConnectionError):
            pass

        try:
            # DLQ 长度
            dlq_length = await self._redis.xlen(self._dlq_key)
        except (aioredis.ResponseError, aioredis.ConnectionError):
            pass

        return QueueStats(
            stream_length=stream_length,
            pending_count=pending_count,
            active_workers=active_workers,
            dlq_length=dlq_length,
        )

    @classmethod
    async def create(
        cls,
        redis_url: str,
        stream_key: str = "pipeline:tasks",
        dlq_key: str = "pipeline:dlq",
        group_name: str = "pipeline-workers",
    ) -> TaskQueue | None:
        """工厂方法，Redis 不可用时返回 None"""
        try:
            client = aioredis.from_url(
                redis_url, decode_responses=False, socket_connect_timeout=5
            )
            # 测试连接
            await client.ping()
        except (
            aioredis.ConnectionError,
            aioredis.TimeoutError,
            OSError,
            ConnectionRefusedError,
        ):
            logger.warning("Redis unavailable, falling back to in-process task")
            return None

        queue = cls(
            redis_client=client,
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )
        await queue._ensure_group()
        return queue

    def _parse_message(
        self, msg_id: Any, fields: dict[Any, Any]
    ) -> tuple[str, TaskMessage] | None:
        """解析 Redis Stream 消息为 TaskMessage"""
        try:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data is None:
                return None
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode()
            data = json.loads(raw_data)
            task_msg = TaskMessage(
                doc_id=data["doc_id"],
                kb_id=data["kb_id"],
                file_path=data["file_path"],
                retry_count=int(data.get("retry_count", 0)),
                created_at=float(data.get("created_at", 0.0)),
                trace_id=data.get("trace_id", ""),
            )
            return (mid, task_msg)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error("Failed to parse message: %s", e)
            return None
