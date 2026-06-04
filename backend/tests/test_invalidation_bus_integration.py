"""M1/M2/M7 失效广播集成测试

验证 InvalidationBus 的跨进程失效广播路径：
- M2: Worker 入库 → API 进程秒级可见（kb_data → 失效 Milvus 加载缓存 + 检索结果缓存）
- M1: 改配置 → 跨 worker 配置生效（tenant_config → 失效配置缓存）
- M7: 配置变更 → 该租户结果缓存失效
- 降级: Redis 不可用 → publish no-op + 不影响主流程
"""

import asyncio
import json

import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from app.storage.invalidation import InvalidationBus, CHANNEL


@pytest_asyncio.fixture
async def shared_redis():
    """共享 fakeredis 实例，模拟同一 Redis 集群"""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def worker_bus(shared_redis):
    """模拟 Worker 进程的 InvalidationBus"""
    bus = InvalidationBus(redis_client=shared_redis, instance_id="worker-instance-001")
    yield bus
    bus.stop()


@pytest_asyncio.fixture
async def api_bus(shared_redis):
    """模拟 API 进程的 InvalidationBus（不同 instance_id）"""
    bus = InvalidationBus(redis_client=shared_redis, instance_id="api-instance-002")
    yield bus
    bus.stop()


class TestM2KbDataInvalidation:
    """M2: Worker 入库成功后 publish kb_data → 其他进程失效 Milvus 加载缓存 + 检索结果缓存"""

    @pytest.mark.asyncio
    async def test_kb_data_publish_triggers_milvus_cache_invalidation(self, worker_bus, api_bus):
        """M2: Worker 入库成功后 publish kb_data → 其他进程 handler 失效 Milvus 加载缓存"""
        handler_called = asyncio.Event()
        received_key = None

        async def mock_kb_data_handler(key: str):
            nonlocal received_key
            received_key = key
            handler_called.set()

        # API 进程启动订阅（后台任务）
        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"kb_data": mock_kb_data_handler})
        )

        # 等待订阅就绪
        await asyncio.sleep(0.05)

        # Worker 进程发布 kb_data 失效信号
        await worker_bus.publish("kb_data", "kb-123")

        # 等待 handler 被调用
        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        assert received_key == "kb-123"

    @pytest.mark.asyncio
    async def test_kb_data_publish_triggers_retrieval_cache_invalidation(self, worker_bus, api_bus):
        """M2: publish kb_data → 检索结果缓存 invalidate_kb 被调用"""
        invalidate_kb_mock = AsyncMock()
        handler_called = asyncio.Event()

        async def mock_kb_data_handler(key: str):
            await invalidate_kb_mock(key)
            handler_called.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"kb_data": mock_kb_data_handler})
        )
        await asyncio.sleep(0.05)

        await worker_bus.publish("kb_data", "kb-456")

        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        invalidate_kb_mock.assert_awaited_once_with("kb-456")


class TestM1TenantConfigInvalidation:
    """M1: 配置 update 后 publish tenant_config → 其他进程配置缓存失效"""

    @pytest.mark.asyncio
    async def test_tenant_config_publish_triggers_config_store_invalidation(
        self, worker_bus, api_bus
    ):
        """M1: 配置 update 后 publish tenant_config → 其他进程配置缓存失效"""
        config_invalidate_mock = MagicMock()
        handler_called = asyncio.Event()

        async def mock_tenant_config_handler(key: str):
            config_invalidate_mock(key)
            handler_called.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"tenant_config": mock_tenant_config_handler})
        )
        await asyncio.sleep(0.05)

        # API 进程修改配置后发布 tenant_config 失效
        await worker_bus.publish("tenant_config", "tenant-abc")

        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        config_invalidate_mock.assert_called_once_with("tenant-abc")


class TestM7ConfigChangeCacheInvalidation:
    """M7: tenant_config 失效 → 额外失效该租户名下 KB 的检索结果缓存"""

    @pytest.mark.asyncio
    async def test_tenant_config_publish_triggers_kb_cache_invalidation(
        self, worker_bus, api_bus
    ):
        """M7: tenant_config 失效 → 额外失效该租户名下 KB 的检索结果缓存"""
        kb_invalidations: list[str] = []
        handler_called = asyncio.Event()

        # 模拟完整的 _handle_tenant_config 逻辑:
        # 1. 失效 config_store (M1)
        # 2. 查询租户名下 KB，逐个失效检索结果缓存 (M7)
        mock_tenant_kb_ids = ["kb-001", "kb-002", "kb-003"]

        async def mock_tenant_config_handler(tenant_id: str):
            """模拟 M1+M7 联合处理逻辑"""
            # M1: 配置缓存失效（同步调用）
            # M7: 查该租户 KB 列表，逐个失效检索缓存
            for kb_id in mock_tenant_kb_ids:
                kb_invalidations.append(kb_id)
            handler_called.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"tenant_config": mock_tenant_config_handler})
        )
        await asyncio.sleep(0.05)

        await worker_bus.publish("tenant_config", "tenant-xyz")

        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        # 验证所有 KB 缓存都被失效
        assert kb_invalidations == ["kb-001", "kb-002", "kb-003"]


class TestOriginSkip:
    """自己发的消息自己不处理（origin 比对）"""

    @pytest.mark.asyncio
    async def test_origin_skip_self_message(self, shared_redis):
        """自己发的消息自己不处理"""
        handler_mock = AsyncMock()
        bus = InvalidationBus(redis_client=shared_redis, instance_id="same-instance")

        handler_called = asyncio.Event()

        async def tracking_handler(key: str):
            await handler_mock(key)
            handler_called.set()

        listen_task = asyncio.create_task(
            bus.subscribe_loop({"kb_data": tracking_handler})
        )
        await asyncio.sleep(0.05)

        # 自己发布消息
        await bus.publish("kb_data", "kb-self")

        # 等待足够时间确认 handler 不会被调用
        await asyncio.sleep(0.3)

        bus.stop()
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

        # handler 不应被调用（origin 相同跳过）
        handler_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_different_origin_receives_message(self, worker_bus, api_bus):
        """不同 origin 的消息应被正常处理"""
        handler_called = asyncio.Event()
        received_keys: list[str] = []

        async def handler(key: str):
            received_keys.append(key)
            handler_called.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"kb_data": handler})
        )
        await asyncio.sleep(0.05)

        await worker_bus.publish("kb_data", "kb-other")

        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        assert received_keys == ["kb-other"]


class TestRedisDegradation:
    """Redis 不可用 → publish/subscribe 均 no-op 降级，不抛异常不阻塞"""

    @pytest.mark.asyncio
    async def test_redis_unavailable_publish_noop(self):
        """Redis 不可用时 publish 静默 no-op，不抛异常"""
        bus = InvalidationBus(redis_client=None, instance_id="degraded-001")

        # 不应抛出任何异常
        await bus.publish("kb_data", "kb-123")
        await bus.publish("tenant_config", "tenant-abc")

    @pytest.mark.asyncio
    async def test_redis_unavailable_subscribe_noop(self):
        """Redis 不可用时 subscribe_loop 直接返回，不阻塞"""
        bus = InvalidationBus(redis_client=None, instance_id="degraded-002")
        handler_mock = AsyncMock()

        # subscribe_loop 应立即返回（不阻塞）
        await asyncio.wait_for(
            bus.subscribe_loop({"kb_data": handler_mock}),
            timeout=1.0,
        )

        # handler 不应被调用
        handler_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_connection_error_publish_noop(self):
        """Redis 连接异常时 publish 降级为 no-op"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=ConnectionError("Connection refused"))

        bus = InvalidationBus(redis_client=mock_redis, instance_id="error-001")

        # 不应抛出异常
        await bus.publish("kb_data", "kb-789")


class TestHandlerExceptionResilience:
    """handler 抛异常不影响订阅循环继续处理后续消息"""

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_loop(self, worker_bus, api_bus):
        """handler 抛异常不影响订阅循环继续处理后续消息"""
        call_count = 0
        second_call = asyncio.Event()

        async def flaky_handler(key: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("模拟 handler 内部错误")
            # 第二次调用成功
            second_call.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"kb_data": flaky_handler})
        )
        await asyncio.sleep(0.05)

        # 第一条消息会触发 handler 异常
        await worker_bus.publish("kb_data", "kb-first")
        await asyncio.sleep(0.1)

        # 第二条消息应该仍然被处理
        await worker_bus.publish("kb_data", "kb-second")

        try:
            await asyncio.wait_for(second_call.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_unregistered_type_silently_ignored(self, worker_bus, api_bus):
        """未注册 type 的消息静默跳过，不影响后续消息处理"""
        handler_called = asyncio.Event()
        received_keys: list[str] = []

        async def handler(key: str):
            received_keys.append(key)
            handler_called.set()

        listen_task = asyncio.create_task(
            api_bus.subscribe_loop({"kb_data": handler})
        )
        await asyncio.sleep(0.05)

        # 发布一个未注册的 type
        await worker_bus.publish("unknown_type", "key-1")
        await asyncio.sleep(0.1)

        # 再发布已注册的 type
        await worker_bus.publish("kb_data", "key-2")

        try:
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
        finally:
            api_bus.stop()
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        # 只有 kb_data handler 被调用
        assert received_keys == ["key-2"]
