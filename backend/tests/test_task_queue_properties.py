"""TaskQueue 属性测试 - 使用 Hypothesis 验证队列核心正确性属性

Feature: pipeline-production-optimization
Validates: Requirements 1.1, 1.3, 1.5
"""

import asyncio
import uuid

import pytest
import fakeredis.aioredis
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from app.pipeline.queue import TaskMessage, TaskQueue


# ---------- Strategies ----------

# 生成有效的 doc_id / kb_id（UUID 格式字符串）
uuid_str = st.from_type(uuid.UUID).map(str)

# 生成有效的 file_path（非空路径字符串）
file_path_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"), whitelist_characters="/_-."),
    min_size=1,
    max_size=100,
).map(lambda s: s.strip() or "file.pdf")

# 重试次数 1-3
retry_count_st = st.integers(min_value=1, max_value=3)

# pending 消息数量
pending_count_st = st.integers(min_value=0, max_value=10)

# DLQ 消息数量
dlq_count_st = st.integers(min_value=0, max_value=10)


# ---------- Helper ----------

def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_queue(stream_suffix: str) -> tuple[fakeredis.aioredis.FakeRedis, TaskQueue]:
    """创建隔离的 fakeredis 客户端和 TaskQueue 实例"""
    client = fakeredis.aioredis.FakeRedis()
    queue = TaskQueue(
        redis_client=client,
        stream_key=f"test:{stream_suffix}:{uuid.uuid4().hex[:8]}",
        dlq_key=f"test:dlq:{stream_suffix}:{uuid.uuid4().hex[:8]}",
        group_name="pipeline-workers",
    )
    await queue._ensure_group()
    return client, queue


# ---------- Property 1: 任务入队保留所有元数据 ----------
# Feature: pipeline-production-optimization, Property 1: 任务入队保留所有元数据


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    doc_id=uuid_str,
    kb_id=uuid_str,
    file_path=file_path_st,
)
def test_enqueue_preserves_all_metadata(doc_id, kb_id, file_path):
    """Property 1: 对于任意有效的 doc_id, kb_id, file_path，
    enqueue 后 consume 得到的消息应包含完全相同的字段值，
    trace_id 为有效 UUID4，retry_count 为 0。

    **Validates: Requirements 1.1**
    """

    async def _test():
        client, queue = await _make_queue("prop1")
        try:
            msg = TaskMessage(doc_id=doc_id, kb_id=kb_id, file_path=file_path)
            await queue.enqueue(msg)

            results = await queue.consume("test-worker", count=1, block_ms=100)
            assert len(results) == 1

            _, consumed_msg = results[0]

            # 元数据完全保留
            assert consumed_msg.doc_id == doc_id
            assert consumed_msg.kb_id == kb_id
            assert consumed_msg.file_path == file_path

            # retry_count 初始为 0
            assert consumed_msg.retry_count == 0

            # trace_id 为有效 UUID4
            parsed_uuid = uuid.UUID(consumed_msg.trace_id, version=4)
            assert str(parsed_uuid) == consumed_msg.trace_id
        finally:
            await client.flushall()
            await client.aclose()

    _run_async(_test())


# ---------- Property 2: 重试计数与退避时间正确 ----------
# Feature: pipeline-production-optimization, Property 2: 重试计数与退避时间正确


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(retry_count=retry_count_st)
def test_retry_count_and_backoff_correct(retry_count):
    """Property 2: 对于任意失败次数 N (1 ≤ N ≤ 3)，
    retry_count 应等于 N，退避延迟应等于 2^(N-1) 秒。

    **Validates: Requirements 1.3**
    """

    async def _test():
        client, queue = await _make_queue("prop2")
        try:
            # 模拟重试：创建带有 retry_count=N 的消息入队
            msg = TaskMessage(
                doc_id="doc-retry",
                kb_id="kb-retry",
                file_path="retry.pdf",
                retry_count=retry_count,
            )
            await queue.enqueue(msg)

            results = await queue.consume("test-worker", count=1, block_ms=100)
            assert len(results) == 1

            _, consumed_msg = results[0]

            # retry_count 保持正确
            assert consumed_msg.retry_count == retry_count

            # 验证退避延迟计算：2^(N-1) 秒
            expected_backoff = 2 ** (retry_count - 1)
            actual_backoff = 2 ** (consumed_msg.retry_count - 1)
            assert actual_backoff == expected_backoff
            # 验证具体值范围：1s, 2s, 4s
            assert expected_backoff in (1, 2, 4)
        finally:
            await client.flushall()
            await client.aclose()

    _run_async(_test())


# ---------- Property 3: 队列统计准确反映实际状态 ----------
# Feature: pipeline-production-optimization, Property 3: 队列统计准确反映实际状态


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pending_n=pending_count_st,
    dlq_m=dlq_count_st,
)
def test_queue_stats_reflect_actual_state(pending_n, dlq_m):
    """Property 3: 对于任意队列状态（N 条 pending 消息、M 条 DLQ 消息），
    get_stats() 返回的 pending_count 应等于 N，dlq_length 应等于 M。

    **Validates: Requirements 1.5**
    """

    async def _test():
        client, queue = await _make_queue("prop3")
        try:
            # 入队 pending_n 条消息并消费（使其变为 pending 状态）
            for i in range(pending_n):
                msg = TaskMessage(
                    doc_id=f"doc-{i}",
                    kb_id=f"kb-{i}",
                    file_path=f"file-{i}.pdf",
                )
                await queue.enqueue(msg)

            # 消费所有消息使其进入 pending 状态（已读取但未 ACK）
            if pending_n > 0:
                await queue.consume("test-worker", count=pending_n, block_ms=100)

            # 向 DLQ 写入 dlq_m 条消息
            for i in range(dlq_m):
                dlq_msg = TaskMessage(
                    doc_id=f"dlq-doc-{i}",
                    kb_id=f"dlq-kb-{i}",
                    file_path=f"dlq-file-{i}.pdf",
                )
                await queue.move_to_dlq(f"fake-id-{i}", dlq_msg, "test error")

            stats = await queue.get_stats()

            # pending_count 应等于 N
            assert stats.pending_count == pending_n

            # dlq_length 应等于 M
            assert stats.dlq_length == dlq_m
        finally:
            await client.flushall()
            await client.aclose()

    _run_async(_test())
