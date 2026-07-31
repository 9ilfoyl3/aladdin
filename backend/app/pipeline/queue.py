"""持久化任务队列 - 基于 Redis Stream 的任务管理（文档入库特化）

提供：
- TaskMessage 任务消息数据结构
- QueueStats 队列统计响应模型（从泛型基类 re-export，保持导入路径不变）
- TaskMessageCodec 负责 TaskMessage 的（反）序列化
- TaskQueue 文档入库队列，基于泛型 ``RedisStreamQueue`` 的薄特化

设计见 session-upload-async-ws design C2：与业务无关的 Redis Stream 机制
（DLQ / 崩溃恢复 / 毒消息治理 / XAUTOCLAIM）收敛到 ``RedisStreamQueue``，
``TaskQueue`` 仅负责注入 ``TaskMessage`` 的 codec 与默认 key。对外方法签名与
行为保持完全不变（现有 ``PipelineWorker`` 无需改动，靠 fakeredis 测试守回归）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as aioredis

from app.pipeline.stream_queue import QueueStats, RedisStreamQueue

# QueueStats 从泛型基类 re-export，保持 ``from app.pipeline.queue import QueueStats``
# 的历史导入路径不变。
__all__ = ["TaskMessage", "QueueStats", "TaskMessageCodec", "TaskQueue"]

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


class TaskMessageCodec:
    """``TaskMessage`` 的 Redis Stream 编解码器（注入泛型队列以解耦）。

    - ``encode``：入队前补全 created_at / trace_id（原地修改传入消息，与历史
      行为一致），并以单一 ``data`` JSON 信封序列化。
    - ``decode``：反序列化回 ``TaskMessage``，损坏消息返回 ``None``。
    """

    def encode(self, task: TaskMessage) -> dict[str, str]:
        if task.created_at == 0.0:
            task.created_at = time.time()
        if not task.trace_id:
            task.trace_id = str(uuid.uuid4())
        # Redis Stream 要求所有值为 str/bytes
        return {"data": json.dumps(asdict(task))}

    def decode(
        self, msg_id: Any, fields: dict[Any, Any]
    ) -> tuple[str, TaskMessage] | None:
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


class TaskQueue(RedisStreamQueue[TaskMessage]):
    """Redis Stream 文档入库任务队列（``RedisStreamQueue`` 的薄特化）

    仅注入 ``TaskMessageCodec`` 与文档入库默认 key，全部通用机制
    （enqueue/consume/ack/move_to_dlq/claim_pending/get_stats/_ensure_group、
    投递次数治理、毒消息判定、XAUTOCLAIM 崩溃恢复）继承自泛型基类。
    对外方法签名与行为与重构前完全一致。
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream_key: str = "pipeline:tasks",
        dlq_key: str = "pipeline:dlq",
        group_name: str = "pipeline-workers",
        codec: TaskMessageCodec | None = None,
    ):
        # codec 参数供泛型基类 ``create`` 工厂回构造时注入；直接构造时用默认
        # TaskMessageCodec，保持历史调用方（无需传 codec）签名不变。
        super().__init__(
            redis_client=redis_client,
            codec=codec or TaskMessageCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )

    @classmethod
    async def create(
        cls,
        redis_url: str,
        stream_key: str = "pipeline:tasks",
        dlq_key: str = "pipeline:dlq",
        group_name: str = "pipeline-workers",
    ) -> "TaskQueue | None":
        """工厂方法，Redis 不可用时返回 None"""
        queue = await super().create(
            redis_url=redis_url,
            codec=TaskMessageCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )
        return queue  # type: ignore[return-value]
