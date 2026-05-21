"""TaskQueue 单元测试 - 验证 Redis Stream 任务队列基本功能"""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.pipeline.queue import TaskMessage, TaskQueue, QueueStats


@pytest_asyncio.fixture
async def redis_client():
    """创建 fakeredis 异步客户端"""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def task_queue(redis_client):
    """创建 TaskQueue 实例"""
    queue = TaskQueue(
        redis_client=redis_client,
        stream_key="pipeline:tasks",
        dlq_key="pipeline:dlq",
        group_name="pipeline-workers",
    )
    await queue._ensure_group()
    return queue


@pytest.fixture
def sample_message():
    """创建示例 TaskMessage"""
    return TaskMessage(
        doc_id="doc-123",
        kb_id="kb-456",
        file_path="data/uploads/test.pdf",
    )


@pytest.mark.asyncio
async def test_enqueue_returns_message_id(task_queue, sample_message):
    """enqueue 应返回有效的 message_id"""
    msg_id = await task_queue.enqueue(sample_message)
    assert msg_id is not None
    assert "-" in msg_id  # Redis Stream ID 格式: timestamp-sequence


@pytest.mark.asyncio
async def test_enqueue_fills_created_at_and_trace_id(task_queue):
    """enqueue 应自动填充 created_at 和 trace_id"""
    msg = TaskMessage(doc_id="d1", kb_id="k1", file_path="f1.pdf")
    assert msg.created_at == 0.0
    assert msg.trace_id == ""

    await task_queue.enqueue(msg)

    assert msg.created_at > 0
    assert len(msg.trace_id) > 0


@pytest.mark.asyncio
async def test_enqueue_and_consume(task_queue, sample_message):
    """enqueue 后 consume 应能读取到相同消息"""
    await task_queue.enqueue(sample_message)

    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    assert len(results) == 1

    msg_id, task_msg = results[0]
    assert task_msg.doc_id == "doc-123"
    assert task_msg.kb_id == "kb-456"
    assert task_msg.file_path == "data/uploads/test.pdf"
    assert task_msg.retry_count == 0


@pytest.mark.asyncio
async def test_ack_removes_from_pending(task_queue, sample_message):
    """ack 后消息应从 pending 列表中移除"""
    await task_queue.enqueue(sample_message)
    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    msg_id, _ = results[0]

    await task_queue.ack(msg_id)

    # 再次 consume 不应获取到消息
    results2 = await task_queue.consume("worker-1", count=1, block_ms=100)
    assert len(results2) == 0


@pytest.mark.asyncio
async def test_move_to_dlq(task_queue, sample_message):
    """move_to_dlq 应将消息写入 DLQ 并 ACK 原消息"""
    await task_queue.enqueue(sample_message)
    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    msg_id, task_msg = results[0]

    await task_queue.move_to_dlq(msg_id, task_msg, "max retries exceeded")

    # DLQ 应有一条消息
    stats = await task_queue.get_stats()
    assert stats.dlq_length == 1


@pytest.mark.asyncio
async def test_get_stats(task_queue, sample_message):
    """get_stats 应返回正确的队列统计"""
    # 初始状态
    stats = await task_queue.get_stats()
    assert stats.stream_length == 0
    assert stats.dlq_length == 0

    # 入队 2 条消息
    await task_queue.enqueue(sample_message)
    msg2 = TaskMessage(doc_id="d2", kb_id="k2", file_path="f2.pdf")
    await task_queue.enqueue(msg2)

    stats = await task_queue.get_stats()
    assert stats.stream_length == 2

    # 消费 1 条（变为 pending）
    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    stats = await task_queue.get_stats()
    assert stats.pending_count == 1


@pytest.mark.asyncio
async def test_create_returns_none_when_redis_unavailable():
    """Redis 不可用时 create() 应返回 None"""
    result = await TaskQueue.create("redis://localhost:59999/0")
    assert result is None


@pytest.mark.asyncio
async def test_consume_empty_queue(task_queue):
    """空队列 consume 应返回空列表"""
    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    assert results == []


@pytest.mark.asyncio
async def test_enqueue_preserves_retry_count(task_queue):
    """enqueue 应保留 retry_count 值（用于重试入队）"""
    msg = TaskMessage(
        doc_id="d1", kb_id="k1", file_path="f1.pdf", retry_count=2
    )
    await task_queue.enqueue(msg)

    results = await task_queue.consume("worker-1", count=1, block_ms=100)
    _, task_msg = results[0]
    assert task_msg.retry_count == 2
