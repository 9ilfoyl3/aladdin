"""PipelineWorker 属性测试 - 使用 Hypothesis 验证 Worker 核心正确性属性

Feature: pipeline-production-optimization
Validates: Requirements 1.7, 4.1, 4.2, 4.3
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import fakeredis.aioredis
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from app.pipeline.queue import TaskMessage, TaskQueue
from app.pipeline.worker import PipelineWorker


# ---------- Strategies ----------

# 生成有效的 doc_id（UUID 格式字符串）
uuid_str = st.from_type(uuid.UUID).map(str)

# 生成 max_concurrent 值 (1-10)
max_concurrent_st = st.integers(min_value=1, max_value=10)

# 并发任务数量 (2-20)
task_count_st = st.integers(min_value=2, max_value=20)


# ---------- Helper ----------

def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_db_factory_completed():
    """创建 mock 数据库会话工厂，返回文档状态为 completed"""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "completed"
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_db_factory_pending():
    """创建 mock 数据库会话工厂，返回文档状态为 pending"""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "pending"
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


async def _make_queue() -> tuple[fakeredis.aioredis.FakeRedis, TaskQueue]:
    """创建隔离的 fakeredis 客户端和 TaskQueue 实例"""
    client = fakeredis.aioredis.FakeRedis()
    suffix = uuid.uuid4().hex[:8]
    queue = TaskQueue(
        redis_client=client,
        stream_key=f"test:worker:{suffix}",
        dlq_key=f"test:dlq:worker:{suffix}",
        group_name="pipeline-workers",
    )
    await queue._ensure_group()
    return client, queue


# ---------- Property 4: 已完成文档被幂等跳过 ----------
# Feature: pipeline-production-optimization, Property 4: 已完成文档被幂等跳过


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(doc_id=uuid_str)
def test_completed_document_is_idempotently_skipped(doc_id):
    """Property 4: 对于任意状态为 completed 的文档，
    Worker 消费到该文档的任务时，应直接 ACK 消息且不触发 Pipeline 处理逻辑。

    **Validates: Requirements 1.7**
    """

    async def _test():
        client, queue = await _make_queue()
        try:
            mock_pipeline = AsyncMock()
            mock_pipeline.process = AsyncMock()
            db_factory = _make_db_factory_completed()

            worker = PipelineWorker(
                queue=queue,
                pipeline=mock_pipeline,
                db_session_factory=db_factory,
            )

            # 入队任务
            msg = TaskMessage(
                doc_id=doc_id,
                kb_id="kb-test",
                file_path="test.pdf",
            )
            await queue.enqueue(msg)

            # 消费任务
            results = await queue.consume("test-worker", count=1, block_ms=100)
            assert len(results) == 1
            message_id, consumed_msg = results[0]

            # 处理任务
            await worker._process_task(message_id, consumed_msg)

            # 验证 pipeline.process 未被调用（幂等跳过）
            mock_pipeline.process.assert_not_called()

            # 验证消息已被 ACK（pending 数为 0）
            stats = await queue.get_stats()
            assert stats.pending_count == 0
        finally:
            await client.flushall()
            await client.aclose()

    _run_async(_test())


# ---------- Property 10: 并发数不超过 Semaphore 上限 ----------
# Feature: pipeline-production-optimization, Property 10: 并发数不超过 Semaphore 上限


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    max_concurrent=max_concurrent_st,
    num_tasks=task_count_st,
)
def test_concurrency_never_exceeds_semaphore_limit(max_concurrent, num_tasks):
    """Property 10: 对于任意 max_concurrent 值 N 和任意数量的并发任务，
    在任意时刻同时执行 Pipeline.process 的任务数应 ≤ N。

    **Validates: Requirements 4.1, 4.2, 4.3**
    """

    async def _test():
        client, queue = await _make_queue()
        try:
            concurrent_count = 0
            max_observed = 0
            lock = asyncio.Lock()

            async def tracked_process(file_path, doc_id, kb_id):
                nonlocal concurrent_count, max_observed
                async with lock:
                    concurrent_count += 1
                    max_observed = max(max_observed, concurrent_count)
                # 模拟处理耗时，让并发有机会重叠
                await asyncio.sleep(0.01)
                async with lock:
                    concurrent_count -= 1

            mock_pipeline = AsyncMock()
            mock_pipeline.process = tracked_process
            db_factory = _make_db_factory_pending()

            worker = PipelineWorker(
                queue=queue,
                pipeline=mock_pipeline,
                db_session_factory=db_factory,
                max_concurrent=max_concurrent,
            )

            # 入队多个任务
            for i in range(num_tasks):
                msg = TaskMessage(
                    doc_id=f"doc-{i}",
                    kb_id="kb-1",
                    file_path=f"file-{i}.pdf",
                )
                await queue.enqueue(msg)

            # 消费所有任务
            results = await queue.consume(
                "test-worker", count=num_tasks, block_ms=100
            )

            # 并发处理所有任务
            tasks = []
            for message_id, msg in results:
                tasks.append(
                    asyncio.create_task(worker._process_task(message_id, msg))
                )

            await asyncio.gather(*tasks)

            # 验证最大并发数不超过 semaphore 上限
            assert max_observed <= max_concurrent

            # 验证 semaphore 被正确释放（所有任务完成后 semaphore 恢复满值）
            assert worker.semaphore._value == max_concurrent
        finally:
            await client.flushall()
            await client.aclose()

    _run_async(_test())
