"""泛型 Redis Stream 队列基类 - 与业务无关的通用传输层

把与业务无关的 Redis Stream 机制收敛到一处（主流做法：通用传输 + 任务特化），
避免 DLQ / 崩溃恢复 / 毒消息治理逻辑在多份队列间漂移。

提供：
- ``StreamCodec`` 业务 payload 编解码协议（注入解耦）
- ``QueueStats`` 队列统计响应模型
- ``RedisStreamQueue`` 泛型队列：enqueue/consume/ack/move_to_dlq/claim_pending/
  get_stats/create/_ensure_group，承载投递次数治理、毒消息判定与 XAUTOCLAIM 崩溃恢复

``TaskQueue``（文档入库）与 ``SessionUploadQueue``（会话上传）均为其薄特化：
stream/group/DLQ key 由构造参数决定，业务 payload 的序列化交给注入的 codec。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry
from pydantic import BaseModel

logger = logging.getLogger("pipeline.stream_queue")

# 业务任务类型（TaskMessage / SessionUploadTask 等），由特化子类/codec 决定
T = TypeVar("T")

# DLQ 采用的标准 JSON 信封字段名。codec 约定将业务 payload 序列化到该字段的
# JSON 里；move_to_dlq 据此在不感知业务结构的前提下注入 error/failed_at 元信息。
_ENVELOPE_FIELD = "data"


class StreamCodec(Protocol[T]):
    """业务 payload 编解码协议（注入以解耦泛型队列与具体任务结构）。

    - ``encode(task)``：把业务任务序列化为 Redis Stream 字段（值须为 str）。
      约定使用单一 ``data`` 字段承载 JSON（便于 DLQ 元信息注入），
      并可在此处对任务做入队前的补全（如 created_at / trace_id）。
    - ``decode(msg_id, fields)``：反序列化为 ``(message_id, task)``；
      对损坏 / 无法解析的消息返回 ``None``（调用方会自动 ACK 跳过）。
    """

    def encode(self, task: T) -> dict[str, str]:
        ...

    def decode(
        self, msg_id: Any, fields: dict[Any, Any]
    ) -> tuple[str, T] | None:
        ...


class QueueStats(BaseModel):
    """队列统计信息响应模型"""

    stream_length: int = 0       # Stream 总消息数
    pending_count: int = 0       # 未 ACK 的消息数
    active_workers: int = 0      # 活跃 consumer 数
    dlq_length: int = 0          # 死信队列长度


class RedisStreamQueue(Generic[T]):
    """泛型 Redis Stream 任务队列

    基于 Redis Stream + Consumer Group 实现任务持久化、自动恢复、重试与死信处理。
    业务 payload 的（反）序列化通过注入的 ``StreamCodec`` 完成，stream/group/DLQ
    key 由构造参数决定，因此本类与具体任务结构完全无关。
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        codec: StreamCodec[T],
        stream_key: str,
        dlq_key: str,
        group_name: str,
    ):
        self._redis = redis_client
        self._codec = codec
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

    async def enqueue(self, task: T) -> str:
        """写入任务到 Stream，返回 message_id

        业务字段补全（如 created_at / trace_id）由 codec.encode 负责。
        """
        payload = self._codec.encode(task)
        message_id: bytes = await self._redis.xadd(self._stream_key, payload)
        # message_id 可能是 bytes 或 str
        return message_id.decode() if isinstance(message_id, bytes) else message_id

    async def consume(
        self, consumer_name: str, count: int = 1, block_ms: int = 5000
    ) -> list[tuple[str, T]]:
        """消费消息，返回 [(message_id, task), ...]

        使用 XREADGROUP 从 consumer group 中读取新消息。
        """
        results: list[tuple[str, T]] = []
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
                parsed = self._codec.decode(msg_id, fields)
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

    async def move_to_dlq(self, message_id: str, task: T, error: str) -> None:
        """将失败任务移入死信队列

        将 codec 编码后的 payload 追加 error/failed_at 元信息写入 DLQ stream，
        然后 ACK 原消息。为保持与业务无关，元信息注入遵循标准 ``data`` JSON 信封
        约定：若 payload 使用 ``data`` 字段承载 JSON，则把 error/failed_at 合并进
        该 JSON；否则作为独立字段附加。
        """
        payload = self._codec.encode(task)
        failed_at = time.time()
        raw = payload.get(_ENVELOPE_FIELD)
        if raw is not None:
            try:
                obj = json.loads(raw)
                obj["error"] = error
                obj["failed_at"] = failed_at
                payload = {**payload, _ENVELOPE_FIELD: json.dumps(obj)}
            except (json.JSONDecodeError, TypeError):
                payload = {**payload, "error": error, "failed_at": str(failed_at)}
        else:
            payload = {**payload, "error": error, "failed_at": str(failed_at)}

        await self._redis.xadd(self._dlq_key, payload)
        await self.ack(message_id)

    async def claim_pending(
        self,
        consumer_name: str,
        min_idle_ms: int = 60000,
        max_delivery_count: int | None = None,
        on_poison_pill: "Callable[[T, str], Awaitable[None]] | None" = None,
    ) -> list[tuple[str, T]]:
        """认领超时的 pending 消息（用于崩溃恢复）

        使用 XAUTOCLAIM 认领 idle 超过 min_idle_ms 的消息。这里的 idle 是
        "距离消息上次被投递的时间"，因此 min_idle_ms 必须大于单任务最大处理
        时长，否则会抢走正在被合法处理的消息，导致同一任务重复处理。

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
            on_poison_pill: 毒消息移入 DLQ 后的回调（async），接收 (task, 原因)。
                            队列层不依赖 DB，由调用方传入以将任务标记为 failed，
                            避免毒任务状态停留在 processing。回调异常不影响主流程。

        Returns:
            可处理的 [(message_id, task), ...]（已剔除毒消息）
        """
        results: list[tuple[str, T]] = []
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
                parsed = self._codec.decode(msg_id, fields)
                if not parsed:
                    continue

                mid, task = parsed

                # 去重：fakeredis 排干后会重复返回同一批消息，避免重复处理
                if mid in claimed_ids:
                    continue
                claimed_ids.add(mid)

                # 毒消息兜底：投递次数超阈值直接进 DLQ，避免无限重投
                if max_delivery_count is not None:
                    delivery_count = await self._get_delivery_count(mid)
                    if delivery_count > max_delivery_count:
                        reason = (
                            f"poison-pill: delivery_count={delivery_count} "
                            f"exceeded max={max_delivery_count}"
                        )
                        logger.error(
                            "Message %s exceeded max delivery count "
                            "(%d > %d), moving to DLQ as poison-pill",
                            mid, delivery_count, max_delivery_count,
                        )
                        await self.move_to_dlq(mid, task, reason)
                        # 通知调用方将该任务标记为 failed（队列层不直接碰 DB）
                        if on_poison_pill is not None:
                            try:
                                await on_poison_pill(task, reason)
                            except Exception as cb_err:
                                logger.warning(
                                    "on_poison_pill callback failed: %s", cb_err,
                                )
                        continue

                results.append((mid, task))

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
        codec: StreamCodec[T],
        stream_key: str,
        dlq_key: str,
        group_name: str,
    ) -> "RedisStreamQueue[T] | None":
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
            codec=codec,
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )
        await queue._ensure_group()
        return queue
