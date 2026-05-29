"""TaskQueue.claim_pending 崩溃恢复与毒消息兜底测试

覆盖机制 A 新增逻辑：
- min_idle_ms 过滤：未达 idle 阈值的消息不被认领
- delivery count 兜底：投递次数超阈值的毒消息直接进 DLQ
- cursor 分页：PEL 中超过单页（100 条）的消息能被完整认领
"""

import asyncio

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.pipeline.queue import TaskMessage, TaskQueue


@pytest_asyncio.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def task_queue(redis_client):
    queue = TaskQueue(
        redis_client=redis_client,
        stream_key="pipeline:tasks",
        dlq_key="pipeline:dlq",
        group_name="pipeline-workers",
    )
    await queue._ensure_group()
    return queue


def _msg(doc_id: str = "d1") -> TaskMessage:
    return TaskMessage(doc_id=doc_id, kb_id="k1", file_path=f"{doc_id}.pdf")


@pytest.mark.asyncio
async def test_claim_skips_messages_below_min_idle(task_queue):
    """idle 未达阈值的消息不应被认领（避免抢走正在处理的任务）"""
    await task_queue.enqueue(_msg("d1"))
    # worker-A 读取消息后未 ACK，消息进入 PEL（idle 从 0 开始）
    await task_queue.consume("worker-A", count=1, block_ms=100)

    # worker-B 用较大的 min_idle 认领，刚投递的消息 idle≈0，不应被认领
    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=60_000, max_delivery_count=3
    )
    assert claimed == []


@pytest.mark.asyncio
async def test_claim_reclaims_idle_message(task_queue):
    """idle 超过阈值的孤儿消息应被认领"""
    await task_queue.enqueue(_msg("d1"))
    await task_queue.consume("worker-A", count=1, block_ms=100)

    # min_idle=0 表示任何 PEL 消息都可认领
    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=0, max_delivery_count=3
    )
    assert len(claimed) == 1
    _, msg = claimed[0]
    assert msg.doc_id == "d1"


@pytest.mark.asyncio
async def test_poison_pill_moved_to_dlq(task_queue):
    """投递次数超阈值的毒消息应进 DLQ，不返回给调用方"""
    await task_queue.enqueue(_msg("poison"))
    await task_queue.consume("worker-A", count=1, block_ms=100)

    # 反复认领抬高 delivery count（每次 XAUTOCLAIM 投递次数 +1）
    # max_delivery_count 设很大，先把投递次数堆高
    for _ in range(4):
        await task_queue.claim_pending(
            "worker-B", min_idle_ms=0, max_delivery_count=999
        )

    stats_before = await task_queue.get_stats()
    assert stats_before.dlq_length == 0

    # 此时投递次数已远超 2，用阈值 2 认领应判定为毒消息并进 DLQ
    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=0, max_delivery_count=2
    )
    assert claimed == []

    stats_after = await task_queue.get_stats()
    assert stats_after.dlq_length == 1


@pytest.mark.asyncio
async def test_claim_paginates_over_100_messages(task_queue):
    """PEL 中超过单页（100 条）的消息应通过 cursor 分页被完整认领"""
    total = 150
    for i in range(total):
        await task_queue.enqueue(_msg(f"d{i}"))
    # 全部读入 PEL
    drained = 0
    while drained < total:
        batch = await task_queue.consume("worker-A", count=50, block_ms=100)
        if not batch:
            break
        drained += len(batch)
    assert drained == total

    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=0, max_delivery_count=999
    )
    # 认领结果不重复且覆盖全部消息
    claimed_doc_ids = {msg.doc_id for _, msg in claimed}
    assert len(claimed) == len(claimed_doc_ids)  # 无重复
    assert claimed_doc_ids == {f"d{i}" for i in range(total)}


@pytest.mark.asyncio
async def test_get_delivery_count_increments(task_queue):
    """_get_delivery_count 应随认领单调递增"""
    await task_queue.enqueue(_msg("d1"))
    results = await task_queue.consume("worker-A", count=1, block_ms=100)
    msg_id, _ = results[0]

    # 初次投递 delivery count >= 1
    first = await task_queue._get_delivery_count(msg_id)
    assert first >= 1

    await task_queue.claim_pending("worker-B", min_idle_ms=0, max_delivery_count=999)
    second = await task_queue._get_delivery_count(msg_id)
    assert second > first


@pytest.mark.asyncio
async def test_poison_pill_triggers_callback(task_queue):
    """毒消息进 DLQ 时应触发 on_poison_pill 回调，携带对应 TaskMessage"""
    await task_queue.enqueue(_msg("poison-cb"))
    await task_queue.consume("worker-A", count=1, block_ms=100)

    # 先把投递次数堆高
    for _ in range(4):
        await task_queue.claim_pending(
            "worker-B", min_idle_ms=0, max_delivery_count=999
        )

    called: list[tuple[str, str]] = []

    async def on_poison(msg: TaskMessage, reason: str) -> None:
        called.append((msg.doc_id, reason))

    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=0, max_delivery_count=2, on_poison_pill=on_poison
    )
    assert claimed == []
    assert len(called) == 1
    assert called[0][0] == "poison-cb"
    assert "poison-pill" in called[0][1]


@pytest.mark.asyncio
async def test_poison_pill_callback_exception_does_not_break(task_queue):
    """on_poison_pill 回调抛异常时不应中断 claim_pending（仍进 DLQ）"""
    await task_queue.enqueue(_msg("poison-err"))
    await task_queue.consume("worker-A", count=1, block_ms=100)

    for _ in range(4):
        await task_queue.claim_pending(
            "worker-B", min_idle_ms=0, max_delivery_count=999
        )

    async def bad_callback(msg: TaskMessage, reason: str) -> None:
        raise RuntimeError("callback boom")

    # 回调抛异常不应向上传播
    claimed = await task_queue.claim_pending(
        "worker-B", min_idle_ms=0, max_delivery_count=2, on_poison_pill=bad_callback
    )
    assert claimed == []
    # 消息仍应进入 DLQ
    stats = await task_queue.get_stats()
    assert stats.dlq_length == 1
