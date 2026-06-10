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
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry
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
    # tenant-auth：冗余/可观测字段。Chunk 盖章的权威来源仍是所属 KB 的 tenant_id
    # （见 pipeline 与 design 显式兼容清单 C4），此处仅便于追踪与日志。
    tenant_id: str | None = None
    # 对象存储 key：源文件在 MinIO 中的 key。Worker 据此下载到临时文件处理。
    # 为空时回退使用 file_path（兼容历史消息 / 对象存储不可用的降级路径）。
    object_key: str | None = None


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
        self,
        consumer_name: str,
        min_idle_ms: int = 60000,
        max_delivery_count: int | None = None,
        on_poison_pill: "Callable[[TaskMessage, str], Awaitable[None]] | None" = None,
    ) -> list[tuple[str, TaskMessage]]:
        """认领超时的 pending 消息（用于崩溃恢复）

        使用 XAUTOCLAIM 认领 idle 超过 min_idle_ms 的消息。这里的 idle 是
        "距离消息上次被投递的时间"，因此 min_idle_ms 必须大于单文档最大处理
        时长，否则会抢走正在被合法处理的消息，导致同一文档重复处理。

        通过 cursor 分页遍历整个 PEL，避免单次 XAUTOCLAIM（默认 COUNT=100）
        在 PEL 堆积时漏认领。

        毒消息（poison-pill）兜底：硬崩溃（OOM/SIGKILL）下消息从未走过失败
        重试逻辑，payload 里的 retry_count 永远是 0，只能靠 Redis 层的投递
        次数（delivery count）收敛。XAUTOCLAIM 每次认领都会使投递次数 +1，
        当某条消息投递次数超过 max_delivery_count 时直接移入 DLQ 不再处理。

        Args:
            consumer_name: 认领者名称
            min_idle_ms: 消息 idle 阈值（毫秒），必须 > task_timeout
            max_delivery_count: 投递次数上限，超过则进 DLQ；None 表示不限制
            on_poison_pill: 毒消息移入 DLQ 后的回调（async），接收 (TaskMessage, 原因)。
                            队列层不依赖 DB，由调用方传入以将文档标记为 failed，
                            避免毒文档状态停留在 processing。回调异常不影响主流程。

        Returns:
            可处理的 [(message_id, TaskMessage), ...]（已剔除毒消息）
        """
        results: list[tuple[str, TaskMessage]] = []
        start_id = "0-0"
        seen_cursors: set[str] = set()
        claimed_ids: set[str] = set()

        while True:
            try:
                # XAUTOCLAIM 返回 (next_start_id, [(msg_id, fields), ...], deleted_ids)
                response = await self._redis.xautoclaim(
                    name=self._stream_key,
                    groupname=self._group_name,
                    consumername=consumer_name,
                    min_idle_time=min_idle_ms,
                    start_id=start_id,
                    count=100,
                )
            except (aioredis.ResponseError, aioredis.ConnectionError):
                break

            if not response or len(response) < 2:
                break

            next_start_id = response[0]
            messages = response[1]

            for msg_id, fields in messages:
                if fields is None:
                    # 消息已被删除（XAUTOCLAIM 在 deleted_ids 中返回，fields 为 None）
                    continue
                parsed = self._parse_message(msg_id, fields)
                if not parsed:
                    continue

                mid, task_msg = parsed

                # 去重：fakeredis 排干后会重复返回同一批消息，避免重复处理
                if mid in claimed_ids:
                    continue
                claimed_ids.add(mid)

                # 毒消息兜底：投递次数超阈值直接进 DLQ，避免无限重投
                if max_delivery_count is not None:
                    delivery_count = await self._get_delivery_count(mid)
                    if delivery_count > max_delivery_count:
                        logger.error(
                            "Message %s (doc_id=%s) exceeded max delivery count "
                            "(%d > %d), moving to DLQ as poison-pill",
                            mid, task_msg.doc_id, delivery_count, max_delivery_count,
                        )
                        await self.move_to_dlq(
                            mid,
                            task_msg,
                            f"poison-pill: delivery_count={delivery_count} "
                            f"exceeded max={max_delivery_count}",
                        )
                        # 通知调用方将该文档标记为 failed（队列层不直接碰 DB）
                        if on_poison_pill is not None:
                            try:
                                await on_poison_pill(
                                    task_msg,
                                    f"poison-pill: delivery_count={delivery_count} "
                                    f"exceeded max={max_delivery_count}",
                                )
                            except Exception as cb_err:
                                logger.warning(
                                    "on_poison_pill callback failed for doc_id=%s: %s",
                                    task_msg.doc_id, cb_err,
                                )
                        continue

                results.append((mid, task_msg))

            # 终止判定（兼容真实 Redis 与 fakeredis 两种 cursor 语义）：
            # ① 本页无消息 → PEL 已遍历完
            # ② 真实 Redis 返回 cursor "0-0" → 完成
            # ③ cursor 不再前进（fakeredis 排干后会重复返回最后一个 ID）→ 完成
            if not messages:
                break
            next_id_str = (
                next_start_id.decode()
                if isinstance(next_start_id, bytes)
                else str(next_start_id)
            )
            if next_id_str == "0-0" or next_id_str in seen_cursors:
                break
            seen_cursors.add(next_id_str)
            start_id = next_id_str

        return results

    async def _get_delivery_count(self, message_id: str) -> int:
        """查询某条消息在 consumer group 中的投递次数（XPENDING）

        XPENDING 的 detail 形式返回每条消息的
        [message_id, consumer, idle_time, delivery_count]。

        Returns:
            投递次数；查询失败时返回 0（视为未超限，交由后续处理）
        """
        try:
            pending = await self._redis.xpending_range(
                name=self._stream_key,
                groupname=self._group_name,
                min=message_id,
                max=message_id,
                count=1,
            )
        except (aioredis.ResponseError, aioredis.ConnectionError):
            return 0

        if not pending:
            return 0

        entry = pending[0]
        # redis-py 返回 dict（key 可能是 str 或 bytes）
        if isinstance(entry, dict):
            for key in ("times_delivered", b"times_delivered"):
                if key in entry:
                    return int(entry[key])
            return 0
        # 兜底：序列形式 [id, consumer, idle, times_delivered]
        try:
            return int(entry[3])
        except (IndexError, TypeError, ValueError):
            return 0

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
                redis_url,
                decode_responses=False,
                socket_connect_timeout=5,
                # socket 读超时必须大于 consume() 的 block 时长（XREADGROUP block=5s 长轮询）。
                # 否则队列空闲时，客户端会在服务端长轮询返回前先判定读超时，
                # 持续抛出 "Timeout reading from redis"。取 15s 给足网络往返余量。
                socket_timeout=15,
                # 超时/断连后按退避自动重试，避免 stale 连接导致消费循环永久卡死。
                # redis-py 6.0+ 已废弃 retry_on_timeout，统一用 retry + retry_on_error。
                retry=Retry(ExponentialBackoff(cap=1.0, base=0.1), retries=3),
                retry_on_error=[RedisConnectionError, RedisTimeoutError],
                # 注意：health_check_interval 与 XREADGROUP BLOCK 命令在某些
                # redis-py 版本下冲突（保活 PING 在 block 期间触发导致协议错乱），
                # 禁用保活，由 retry 负责断线重连。
                health_check_interval=0,
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
                tenant_id=data.get("tenant_id"),
                object_key=data.get("object_key"),
            )
            return (mid, task_msg)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error("Failed to parse message: %s", e)
            return None
