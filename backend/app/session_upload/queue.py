"""会话上传任务队列 - 基于 Redis Stream 的任务管理（会话上传特化）

提供：
- SessionUploadTask 会话上传任务消息数据结构
- SessionUploadTaskCodec 负责 SessionUploadTask 的（反）序列化
- SessionUploadQueue 会话上传队列，基于泛型 ``RedisStreamQueue`` 的薄特化

设计见 session-upload-async-ws design C2：与业务无关的 Redis Stream 机制
（DLQ / 崩溃恢复 / 毒消息治理 / XAUTOCLAIM）收敛到 ``RedisStreamQueue``，
``SessionUploadQueue`` 仅负责注入 ``SessionUploadTask`` 的 codec 与默认 key，
使用独立 stream ``session_upload:tasks`` / DLQ ``session_upload:dlq`` /
group ``session-upload-workers``，与文档入库快/慢道队列物理隔离。

注意：任务 payload 不带文件内容，也不依赖本地临时文件——worker 从 MinIO
按 ``object_key`` 下载（``materialized_file``），与文档 worker 的权威存储路径一致。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as aioredis

from app.pipeline.stream_queue import RedisStreamQueue

__all__ = [
    "SessionUploadTask",
    "SessionUploadTaskCodec",
    "SessionUploadQueue",
    "get_session_upload_queue",
    "set_session_upload_queue",
]

logger = logging.getLogger("session_upload.queue")


@dataclass
class SessionUploadTask:
    """会话上传建索引任务消息结构

    payload 不含文件内容：worker 从 MinIO 按 ``object_key`` 下载原件处理，
    跨进程无共享文件系统假设。
    """

    file_id: str
    session_id: str
    tenant_id: str | None
    owner_user_id: str | None
    object_key: str        # MinIO 原件 key
    ext: str
    filename: str
    retry_count: int = 0
    created_at: float = 0.0  # timestamp
    trace_id: str = ""       # UUID4


class SessionUploadTaskCodec:
    """``SessionUploadTask`` 的 Redis Stream 编解码器（注入泛型队列以解耦）。

    - ``encode``：入队前补全 created_at / trace_id（原地修改传入消息，与
      ``TaskMessageCodec`` 行为一致），并以单一 ``data`` JSON 信封序列化。
    - ``decode``：反序列化回 ``SessionUploadTask``，损坏消息返回 ``None``。
    """

    def encode(self, task: SessionUploadTask) -> dict[str, str]:
        if task.created_at == 0.0:
            task.created_at = time.time()
        if not task.trace_id:
            task.trace_id = str(uuid.uuid4())
        # Redis Stream 要求所有值为 str/bytes
        return {"data": json.dumps(asdict(task))}

    def decode(
        self, msg_id: Any, fields: dict[Any, Any]
    ) -> tuple[str, SessionUploadTask] | None:
        try:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data is None:
                return None
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode()
            data = json.loads(raw_data)
            task_msg = SessionUploadTask(
                file_id=data["file_id"],
                session_id=data["session_id"],
                tenant_id=data.get("tenant_id"),
                owner_user_id=data.get("owner_user_id"),
                object_key=data["object_key"],
                ext=data["ext"],
                filename=data["filename"],
                retry_count=int(data.get("retry_count", 0)),
                created_at=float(data.get("created_at", 0.0)),
                trace_id=data.get("trace_id", ""),
            )
            return (mid, task_msg)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error("Failed to parse message: %s", e)
            return None


class SessionUploadQueue(RedisStreamQueue[SessionUploadTask]):
    """Redis Stream 会话上传任务队列（``RedisStreamQueue`` 的薄特化）

    仅注入 ``SessionUploadTaskCodec`` 与会话上传默认 key，全部通用机制
    （enqueue/consume/ack/move_to_dlq/claim_pending/get_stats/_ensure_group、
    投递次数治理、毒消息判定、XAUTOCLAIM 崩溃恢复）继承自泛型基类。
    独立 stream / DLQ / group 与文档入库队列物理隔离。
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream_key: str = "session_upload:tasks",
        dlq_key: str = "session_upload:dlq",
        group_name: str = "session-upload-workers",
        codec: SessionUploadTaskCodec | None = None,
    ):
        # codec 参数供泛型基类 ``create`` 工厂回构造时注入；直接构造时用默认
        # SessionUploadTaskCodec，保持调用方（无需传 codec）签名简洁。
        super().__init__(
            redis_client=redis_client,
            codec=codec or SessionUploadTaskCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )

    @classmethod
    async def create(
        cls,
        redis_url: str,
        stream_key: str = "session_upload:tasks",
        dlq_key: str = "session_upload:dlq",
        group_name: str = "session-upload-workers",
    ) -> "SessionUploadQueue | None":
        """工厂方法，Redis 不可用时返回 None"""
        queue = await super().create(
            redis_url=redis_url,
            codec=SessionUploadTaskCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )
        return queue  # type: ignore[return-value]


# ---------- 进程内单例访问器 ----------
#
# 会话上传队列为跨请求共享的进程级资源（持有 Redis 连接）。API 进程在启动
# （main.py lifespan，任务 8）时通过 ``set_session_upload_queue`` 注入创建好的
# 队列实例；``enqueue_upload`` 等调用方通过 ``get_session_upload_queue`` 读取。
#
# 未初始化 / Redis 不可用时返回 None——调用方据此快速失败（路由转 503），
# 不做进程内降级（避免重启丢任务，见 design C7 / REQ-1）。

_queue_instance: "SessionUploadQueue | None" = None


def get_session_upload_queue() -> "SessionUploadQueue | None":
    """获取进程内 ``SessionUploadQueue`` 单例（未初始化 / 不可用时返回 None）。"""
    return _queue_instance


def set_session_upload_queue(queue: "SessionUploadQueue | None") -> None:
    """设置进程内 ``SessionUploadQueue`` 单例（应用启动时注入，测试可重置）。"""
    global _queue_instance
    _queue_instance = queue
