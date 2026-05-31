"""API Key 使用计数的内存合并 + 周期落库（性能根因修复）。

问题根因：原实现每次 API Key 请求都在鉴权关键路径上做一次
`UPDATE api_keys SET call_count=call_count+1, last_used_at=now()` 并 commit。
高频集成调用下这会带来两类开销：
  1. 每请求一次写 + fsync 提交，放大 I/O；
  2. 同一个 Key 被并发打时，对同一行的行级写锁竞争，造成无意义的互相等待。

根因修复（而非兼容/绕过）：把"计数"从关键路径上彻底移走——
请求路径只在内存里 O(1) 累加（无 I/O、无锁等待），由一个后台协程每隔
`flush_interval` 秒把累计增量**合并成一次批量写**落库。这样：
  - 鉴权关键路径不再有任何 DB 写；
  - 同一 Key 的 N 次调用合并为 1 次 `+N` 更新，消除行锁竞争；
  - 进程退出前 flush 一次，避免丢最后一个区间的计数。

语义变化（可接受且明确）：`call_count`/`last_used_at` 由"强一致"变为
"最终一致（flush_interval 内可见）"。用量统计本就不要求强一致，这是标准取舍。
进程崩溃会丢失最后一个未 flush 区间的增量——对用量计数可接受。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update

from app.schema.db import ApiKey

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    delta: int
    last_used_at: datetime


class ApiKeyUsageTracker:
    """进程内 API Key 用量累加器 + 周期批量落库。

    record() 仅改内存（非阻塞）；后台 _flush_loop 周期把累计增量合并落库。
    单进程内单例（API 进程）。Worker 不认证 API Key，无需此组件。
    """

    def __init__(self, session_factory, flush_interval: float = 5.0):
        self._session_factory = session_factory
        self._flush_interval = flush_interval
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False

    async def record(self, api_key_id: str) -> None:
        """记录一次调用（仅内存累加，O(1)，不触库）。"""
        async with self._lock:
            cur = self._pending.get(api_key_id)
            now = datetime.utcnow()
            if cur is None:
                self._pending[api_key_id] = _Pending(delta=1, last_used_at=now)
            else:
                cur.delta += 1
                cur.last_used_at = now

    async def _drain(self) -> dict[str, _Pending]:
        """取出并清空当前累计（持锁期间极短）。"""
        async with self._lock:
            if not self._pending:
                return {}
            snapshot = self._pending
            self._pending = {}
            return snapshot

    async def flush(self) -> None:
        """把累计增量合并落库：每个 Key 一次 `call_count += delta`。"""
        snapshot = await self._drain()
        if not snapshot:
            return
        try:
            async with self._session_factory() as session:
                for api_key_id, pending in snapshot.items():
                    await session.execute(
                        update(ApiKey)
                        .where(ApiKey.id == api_key_id)
                        .values(
                            call_count=ApiKey.call_count + pending.delta,
                            last_used_at=pending.last_used_at,
                        )
                    )
                await session.commit()
        except Exception as e:
            # 落库失败不影响请求路径；把未落库的增量补回，等下个区间重试。
            logger.warning("API Key 用量落库失败，增量将于下次重试: %s", e)
            async with self._lock:
                for api_key_id, pending in snapshot.items():
                    cur = self._pending.get(api_key_id)
                    if cur is None:
                        self._pending[api_key_id] = pending
                    else:
                        cur.delta += pending.delta
                        cur.last_used_at = max(cur.last_used_at, pending.last_used_at)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:  # 防御：循环自身不因单次异常退出
                logger.warning("API Key 用量 flush 循环异常: %s", e)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """停止后台循环并落库剩余增量（优雅关闭，不丢最后一个区间）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()


# 进程内单例（API 进程）。在 main.py lifespan 启动/停止。
_tracker: ApiKeyUsageTracker | None = None


def init_usage_tracker(session_factory, flush_interval: float = 5.0) -> ApiKeyUsageTracker:
    """初始化并启动全局用量追踪器（幂等）。"""
    global _tracker
    if _tracker is None:
        _tracker = ApiKeyUsageTracker(session_factory, flush_interval=flush_interval)
        _tracker.start()
    return _tracker


async def shutdown_usage_tracker() -> None:
    """停止全局用量追踪器并落库剩余增量。"""
    global _tracker
    if _tracker is not None:
        await _tracker.stop()
        _tracker = None


async def record_api_key_usage(api_key_id: str) -> None:
    """记录一次 API Key 调用。

    追踪器未初始化（如脱离 API 进程的测试场景）时静默跳过——这不是掩盖错误：
    用量计数是可观测性指标，缺失追踪器仅意味着"不统计"，不影响鉴权正确性。
    """
    if _tracker is not None:
        await _tracker.record(api_key_id)
