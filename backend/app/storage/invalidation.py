"""跨进程失效广播基础设施

采用「广播失效信号」范式：
- 每进程一个 instance_id（uuid，构造时生成）。
- publish: 写路径调用，只发 key + origin，不发 value（消息仅作失效信号）。
- subscribe_loop: 各进程启动时后台订阅；收到消息比对 origin——
  自己发的跳过，否则执行本地失效 handler。
- Redis 不可用（redis_url 未配/连接失败）→ publish/subscribe 均 no-op 降级。
- 断线指数退避重连（上限 30s）。
"""

import asyncio
import json
import logging
import uuid

logger = logging.getLogger(__name__)

CHANNEL = "aladdin:invalidate"


class InvalidationBus:
    """跨进程失效广播。采用「广播失效信号」范式:
    - 每进程一个 instance_id(uuid，构造时生成)。
    - publish: 写路径调用，只发 key + origin，不发 value。
    - subscribe_loop: 各进程启动时后台订阅；收到消息比对 origin——
      自己发的跳过，否则执行本地失效 handler。
    - Redis 不可用(redis_url 未配/连接失败) → publish/subscribe 均 no-op 降级。
    - 断线指数退避重连(上限 30s)。
    """

    def __init__(self, redis_client=None, instance_id: str | None = None):
        self._redis = redis_client
        self._instance_id = instance_id or uuid.uuid4().hex
        self._running = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def publish(self, type_: str, key: str) -> None:
        """发布失效信号。Redis 不可用时 no-op。"""
        if self._redis is None:
            return
        try:
            message = json.dumps({"type": type_, "key": key, "origin": self._instance_id})
            await self._redis.publish(CHANNEL, message)
        except Exception as e:
            logger.warning("失效广播发布失败（降级 no-op）: %s", e)

    async def subscribe_loop(self, handlers: dict[str, callable]) -> None:
        """订阅失效消息并分发给 handler。断线指数退避重连（上限 30s）。"""
        if self._redis is None:
            logger.info("InvalidationBus: Redis 未配置，跳过订阅（降级 TTL 兜底）")
            return

        self._running = True
        retry_delay = 1.0
        max_delay = 30.0

        while self._running:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(CHANNEL)
                retry_delay = 1.0  # 连接成功重置
                logger.info("InvalidationBus: 订阅成功 (instance=%s)", self._instance_id[:8])

                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message["type"] != "message":
                        continue
                    try:
                        data = json.loads(message["data"])
                        # 自己发的跳过
                        if data.get("origin") == self._instance_id:
                            continue
                        msg_type = data.get("type")
                        msg_key = data.get("key")
                        handler = handlers.get(msg_type)
                        if handler:
                            await handler(msg_key)
                    except Exception as e:
                        logger.warning("InvalidationBus: 消息处理异常: %s", e)

            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                logger.warning("InvalidationBus: 连接断开，%0.1fs 后重连: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

        logger.info("InvalidationBus: 订阅循环结束")

    def stop(self):
        """停止订阅循环"""
        self._running = False


# ---------- 全局单例 ----------

_bus_instance: InvalidationBus | None = None


def get_invalidation_bus() -> InvalidationBus | None:
    """获取全局 InvalidationBus 单例"""
    return _bus_instance


async def init_invalidation_bus() -> InvalidationBus | None:
    """初始化全局 InvalidationBus。Redis 不可用时返回 None。"""
    global _bus_instance
    if _bus_instance is not None:
        return _bus_instance

    try:
        import redis.asyncio as aioredis
        from app.config import get_settings

        settings = get_settings()
        redis_url = getattr(settings, "redis_url", "")

        if not redis_url:
            logger.info("InvalidationBus: redis_url 未配置，降级 no-op")
            _bus_instance = InvalidationBus(redis_client=None)
            return _bus_instance

        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        _bus_instance = InvalidationBus(redis_client=client)
        logger.info("InvalidationBus: 初始化成功 (instance=%s)", _bus_instance.instance_id[:8])
        return _bus_instance
    except Exception as e:
        logger.warning("InvalidationBus: 初始化失败，降级 no-op: %s", e)
        _bus_instance = InvalidationBus(redis_client=None)
        return _bus_instance
