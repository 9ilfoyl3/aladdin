"""PipelineWorker 单元测试 - 验证任务消费、并发控制、重试和 DLQ 逻辑"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.pipeline.queue import TaskMessage, TaskQueue
from app.pipeline.worker import PipelineWorker, NON_RETRYABLE_ERRORS


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
def mock_pipeline():
    """创建 mock DocumentPipeline"""
    pipeline = AsyncMock()
    pipeline.process = AsyncMock()
    return pipeline


@pytest.fixture
def mock_db_session_factory():
    """创建 mock 数据库会话工厂，默认返回文档状态为 pending"""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "pending"
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def mock_db_completed_factory():
    """创建 mock 数据库会话工厂，返回文档状态为 completed"""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "completed"
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def sample_message():
    """创建示例 TaskMessage"""
    return TaskMessage(
        doc_id="doc-123",
        kb_id="kb-456",
        file_path="data/uploads/test.pdf",
        retry_count=0,
        created_at=1700000000.0,
        trace_id="test-trace-id",
    )


class TestPipelineWorkerInit:
    """测试 PipelineWorker 初始化"""

    def test_init_default_values(self, task_queue, mock_pipeline, mock_db_session_factory):
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )
        assert worker._max_concurrent == 3
        assert worker._max_retries == 3
        assert worker._running is False

    def test_init_custom_values(self, task_queue, mock_pipeline, mock_db_session_factory):
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=5,
            max_retries=5,
        )
        assert worker._max_concurrent == 5
        assert worker._max_retries == 5


class TestProcessTask:
    """测试 _process_task 方法"""

    @pytest.mark.asyncio
    async def test_process_task_success(
        self, task_queue, mock_pipeline, mock_db_session_factory, sample_message
    ):
        """成功处理任务后应 ACK 消息"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        # 入队并消费
        msg_id = await task_queue.enqueue(sample_message)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        assert len(results) == 1
        consumed_id, consumed_msg = results[0]

        # 处理任务
        await worker._process_task(consumed_id, consumed_msg)

        # 验证 pipeline.process 被调用
        mock_pipeline.process.assert_called_once_with(
            file_path="data/uploads/test.pdf",
            doc_id="doc-123",
            kb_id="kb-456",
        )

    @pytest.mark.asyncio
    async def test_process_task_skips_completed_document(
        self, task_queue, mock_pipeline, mock_db_completed_factory, sample_message
    ):
        """已完成的文档应被跳过"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_completed_factory,
        )

        msg_id = await task_queue.enqueue(sample_message)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        await worker._process_task(consumed_id, consumed_msg)

        # pipeline.process 不应被调用
        mock_pipeline.process.assert_not_called()


class TestHandleFailure:
    """测试 _handle_failure 方法"""

    @pytest.mark.asyncio
    async def test_non_retryable_error_goes_to_dlq(
        self, task_queue, mock_pipeline, mock_db_session_factory, sample_message
    ):
        """不可重试错误应直接进 DLQ"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        msg_id = await task_queue.enqueue(sample_message)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        # FileNotFoundError 是不可重试错误
        error = FileNotFoundError("file not found")
        await worker._handle_failure(consumed_id, consumed_msg, error)

        # 验证进入 DLQ
        stats = await task_queue.get_stats()
        assert stats.dlq_length == 1

    @pytest.mark.asyncio
    async def test_value_error_goes_to_dlq(
        self, task_queue, mock_pipeline, mock_db_session_factory, sample_message
    ):
        """ValueError 应直接进 DLQ"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        msg_id = await task_queue.enqueue(sample_message)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        error = ValueError("unsupported file type")
        await worker._handle_failure(consumed_id, consumed_msg, error)

        stats = await task_queue.get_stats()
        assert stats.dlq_length == 1

    @pytest.mark.asyncio
    async def test_permission_error_goes_to_dlq(
        self, task_queue, mock_pipeline, mock_db_session_factory, sample_message
    ):
        """PermissionError 应直接进 DLQ"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        msg_id = await task_queue.enqueue(sample_message)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        error = PermissionError("access denied")
        await worker._handle_failure(consumed_id, consumed_msg, error)

        stats = await task_queue.get_stats()
        assert stats.dlq_length == 1

    @pytest.mark.asyncio
    async def test_retryable_error_requeues_with_incremented_count(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """可重试错误应重新入队，retry_count 递增"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_retries=3,
        )

        msg = TaskMessage(
            doc_id="doc-1", kb_id="kb-1", file_path="f.pdf",
            retry_count=0, created_at=1700000000.0, trace_id="trace-1",
        )
        msg_id = await task_queue.enqueue(msg)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        # RuntimeError 是可重试错误
        error = RuntimeError("connection timeout")

        # patch asyncio.sleep 避免实际等待
        with patch("app.pipeline.worker.asyncio.sleep", new_callable=AsyncMock):
            await worker._handle_failure(consumed_id, consumed_msg, error)

        # 验证重新入队的消息 retry_count 为 1
        results2 = await task_queue.consume("test-worker", count=1, block_ms=100)
        assert len(results2) == 1
        _, retry_msg = results2[0]
        assert retry_msg.retry_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_goes_to_dlq(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """超过最大重试次数应进 DLQ"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_retries=3,
        )

        # retry_count 已经是 3（等于 max_retries）
        msg = TaskMessage(
            doc_id="doc-1", kb_id="kb-1", file_path="f.pdf",
            retry_count=3, created_at=1700000000.0, trace_id="trace-1",
        )
        msg_id = await task_queue.enqueue(msg)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        error = RuntimeError("persistent failure")
        await worker._handle_failure(consumed_id, consumed_msg, error)

        # 验证进入 DLQ
        stats = await task_queue.get_stats()
        assert stats.dlq_length == 1

    @pytest.mark.asyncio
    async def test_exponential_backoff_delay(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """重试应使用指数退避延迟：2^(retry_count-1)"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_retries=3,
        )

        # retry_count=1 时，下一次 retry 是 2，delay = 2^(2-1) = 2s
        msg = TaskMessage(
            doc_id="doc-1", kb_id="kb-1", file_path="f.pdf",
            retry_count=1, created_at=1700000000.0, trace_id="trace-1",
        )
        msg_id = await task_queue.enqueue(msg)
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        consumed_id, consumed_msg = results[0]

        error = RuntimeError("timeout")

        with patch("app.pipeline.worker.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker._handle_failure(consumed_id, consumed_msg, error)
            # next_retry = 1 + 1 = 2, delay = 2^(2-1) = 2
            mock_sleep.assert_called_once_with(2)


class TestStartStop:
    """测试 start/stop 方法"""

    @pytest.mark.asyncio
    async def test_start_logs_message(
        self, task_queue, mock_pipeline, mock_db_session_factory, caplog
    ):
        """start 应输出启动日志"""
        import logging

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=5,
        )

        # Mock consume 使其不阻塞，在第一次调用后停止 worker
        call_count = 0
        original_consume = task_queue.consume

        async def mock_consume(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker._running = False
            return []

        with caplog.at_level(logging.INFO, logger="pipeline.worker"):
            with patch.object(task_queue, "consume", side_effect=mock_consume):
                await asyncio.wait_for(worker.start(), timeout=3.0)

        assert "Pipeline worker started, max_concurrent=5" in caplog.text

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """stop 应设置 _running 为 False"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        worker._running = True
        await worker.stop()
        assert worker._running is False


class TestStartRecoverPending:
    """测试启动恢复 pending 任务 (Requirement 1.2)"""

    @pytest.mark.asyncio
    async def test_start_claims_and_processes_pending_tasks(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """启动时应通过 claim_pending 恢复中断的 pending 任务并处理"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        # 模拟 claim_pending 返回 pending 任务
        pending_msg = TaskMessage(
            doc_id="pending-doc-1", kb_id="kb-1", file_path="pending.pdf",
            retry_count=0, created_at=1700000000.0, trace_id="pending-trace",
        )

        with patch.object(
            task_queue, "claim_pending",
            new_callable=AsyncMock,
            return_value=[("msg-id-pending", pending_msg)],
        ) as mock_claim:
            # Mock consume 使其不阻塞
            call_count = 0

            async def mock_consume(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    worker._running = False
                return []

            with patch.object(task_queue, "consume", side_effect=mock_consume):
                await asyncio.wait_for(worker.start(), timeout=3.0)

            # 验证 claim_pending 被调用
            mock_claim.assert_called_once()
            # 验证 pipeline.process 被调用处理 pending 任务
            # 等待内部 task 完成
            await asyncio.sleep(0.1)
            mock_pipeline.process.assert_called_with(
                file_path="pending.pdf",
                doc_id="pending-doc-1",
                kb_id="kb-1",
            )

    @pytest.mark.asyncio
    async def test_start_handles_claim_pending_failure(
        self, task_queue, mock_pipeline, mock_db_session_factory, caplog
    ):
        """claim_pending 失败时应记录 WARNING 并继续运行"""
        import logging

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        with patch.object(
            task_queue, "claim_pending",
            new_callable=AsyncMock,
            side_effect=Exception("Redis connection lost"),
        ):
            call_count = 0

            async def mock_consume(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    worker._running = False
                return []

            with caplog.at_level(logging.WARNING, logger="pipeline.worker"):
                with patch.object(task_queue, "consume", side_effect=mock_consume):
                    await asyncio.wait_for(worker.start(), timeout=3.0)

            # Worker 应继续运行（不崩溃）
            assert "Failed to claim pending tasks" in caplog.text


class TestRedisDisconnectReconnect:
    """测试 Redis 断连后等待重连 (Requirement 1.2)"""

    @pytest.mark.asyncio
    async def test_consume_error_waits_and_retries(
        self, task_queue, mock_pipeline, mock_db_session_factory, caplog
    ):
        """consume 抛出异常时应等待 5s 后重试，不崩溃"""
        import logging

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        call_count = 0

        async def mock_consume(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次调用模拟 Redis 断连
                raise ConnectionError("Redis connection lost")
            elif call_count == 2:
                # 第二次调用正常返回（模拟重连成功）
                worker._running = False
                return []
            return []

        with caplog.at_level(logging.ERROR, logger="pipeline.worker"):
            with patch.object(task_queue, "claim_pending", new_callable=AsyncMock, return_value=[]):
                with patch.object(task_queue, "consume", side_effect=mock_consume):
                    with patch("app.pipeline.worker.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        await asyncio.wait_for(worker.start(), timeout=3.0)

                        # 验证等待了 5s 后重试
                        mock_sleep.assert_called_with(5)

        # 验证错误日志
        assert "Error consuming tasks" in caplog.text
        # 验证 consume 被调用了 2 次（第一次失败，第二次成功后停止）
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_multiple_disconnects_keeps_retrying(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """多次 Redis 断连时应持续重试直到恢复"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        call_count = 0

        async def mock_consume(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                # 前 3 次调用模拟 Redis 断连
                raise ConnectionError("Redis connection lost")
            else:
                # 第 4 次恢复
                worker._running = False
                return []

        with patch.object(task_queue, "claim_pending", new_callable=AsyncMock, return_value=[]):
            with patch.object(task_queue, "consume", side_effect=mock_consume):
                with patch("app.pipeline.worker.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await asyncio.wait_for(worker.start(), timeout=3.0)

                    # 验证每次断连都等待了 5s
                    assert mock_sleep.call_count == 3
                    for call in mock_sleep.call_args_list:
                        assert call[0][0] == 5

        # 验证 consume 被调用了 4 次
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_cancelled_error_breaks_loop(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """CancelledError 应中断 Worker 循环（优雅停止）"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        async def mock_consume(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(task_queue, "claim_pending", new_callable=AsyncMock, return_value=[]):
            with patch.object(task_queue, "consume", side_effect=mock_consume):
                await asyncio.wait_for(worker.start(), timeout=3.0)

        # Worker 应正常退出，不会无限循环
        # 如果没有正确处理 CancelledError，wait_for 会超时


class TestConcurrencyControl:
    """测试并发控制"""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(
        self, task_queue, mock_db_session_factory
    ):
        """Semaphore 应限制同时处理的任务数"""
        max_concurrent = 2
        concurrent_count = 0
        max_observed = 0

        async def slow_process(file_path, doc_id, kb_id):
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            max_observed = max(max_observed, concurrent_count)
            await asyncio.sleep(0.1)
            concurrent_count -= 1

        mock_pipeline = AsyncMock()
        mock_pipeline.process = slow_process

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=max_concurrent,
        )

        # 入队 5 个任务
        for i in range(5):
            msg = TaskMessage(
                doc_id=f"doc-{i}", kb_id="kb-1", file_path=f"f{i}.pdf",
                created_at=1700000000.0, trace_id=f"trace-{i}",
            )
            await task_queue.enqueue(msg)

        # 消费并处理所有任务
        results = await task_queue.consume("test-worker", count=5, block_ms=100)
        tasks = []
        for msg_id, msg in results:
            tasks.append(asyncio.create_task(worker._process_task(msg_id, msg)))

        await asyncio.gather(*tasks)

        # 最大并发数不应超过 semaphore 限制
        assert max_observed <= max_concurrent
