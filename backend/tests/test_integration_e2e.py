"""端到端集成测试 - 验证完整文档处理流程

测试场景：
1. 完整流程：enqueue → Worker 消费 → pipeline.process 调用 → ACK
2. Redis 降级模式：queue 为 None 时 fallback 到 asyncio.create_task
3. 并发控制：同时提交 N 个任务，验证 max_concurrent 限制

Requirements: 1.1, 1.6, 4.1
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.pipeline.queue import TaskMessage, TaskQueue
from app.pipeline.worker import PipelineWorker


# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture
async def redis_client():
    """创建 fakeredis 异步客户端"""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def task_queue(redis_client):
    """创建 TaskQueue 实例并初始化 consumer group"""
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
    """创建 mock DocumentPipeline，process 方法为异步 mock"""
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


# ============================================================
# 测试 1：完整流程 enqueue → Worker 消费 → process → ACK
# ============================================================


class TestFullFlowE2E:
    """端到端完整流程测试 (Requirement 1.1)"""

    @pytest.mark.asyncio
    async def test_enqueue_consume_process_ack(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """完整流程：入队 → Worker 消费 → pipeline.process 被调用 → 消息 ACK"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=3,
        )

        # 1. 入队任务
        msg = TaskMessage(
            doc_id="e2e-doc-001",
            kb_id="e2e-kb-001",
            file_path="data/uploads/e2e-test.pdf",
        )
        msg_id = await task_queue.enqueue(msg)
        assert msg_id is not None

        # 验证 stream 中有 1 条消息
        stats = await task_queue.get_stats()
        assert stats.stream_length == 1

        # 2. Worker 消费并处理
        consume_count = 0

        async def controlled_consume(*args, **kwargs):
            nonlocal consume_count
            consume_count += 1
            if consume_count == 1:
                # 第一次消费返回真实结果
                return await TaskQueue.consume(task_queue, *args, **kwargs)
            else:
                # 后续停止 worker
                worker._running = False
                return []

        with patch.object(task_queue, "consume", side_effect=controlled_consume):
            with patch.object(
                task_queue, "claim_pending",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch.object(
                    worker, "_ping_embedding",
                    new_callable=AsyncMock, return_value=True,
                ):
                    await asyncio.wait_for(worker.start(), timeout=5.0)

        # 等待内部 task 完成
        await asyncio.sleep(0.1)

        # 3. 验证 pipeline.process 被正确调用
        mock_pipeline.process.assert_called_once_with(
            file_path="data/uploads/e2e-test.pdf",
            doc_id="e2e-doc-001",
            kb_id="e2e-kb-001",
        )

    @pytest.mark.asyncio
    async def test_multiple_messages_processed_sequentially(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """多条消息按顺序被消费和处理"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=3,
        )

        # 入队 3 个任务
        for i in range(3):
            msg = TaskMessage(
                doc_id=f"doc-{i}",
                kb_id="kb-1",
                file_path=f"data/uploads/file-{i}.pdf",
            )
            await task_queue.enqueue(msg)

        stats = await task_queue.get_stats()
        assert stats.stream_length == 3

        # Worker 消费所有任务
        consume_count = 0

        async def controlled_consume(*args, **kwargs):
            nonlocal consume_count
            consume_count += 1
            if consume_count <= 3:
                return await TaskQueue.consume(task_queue, *args, **kwargs)
            else:
                worker._running = False
                return []

        with patch.object(task_queue, "consume", side_effect=controlled_consume):
            with patch.object(
                task_queue, "claim_pending",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch.object(
                    worker, "_ping_embedding",
                    new_callable=AsyncMock, return_value=True,
                ):
                    await asyncio.wait_for(worker.start(), timeout=5.0)

        await asyncio.sleep(0.2)

        # 验证 pipeline.process 被调用 3 次
        assert mock_pipeline.process.call_count == 3

    @pytest.mark.asyncio
    async def test_task_metadata_preserved_through_flow(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """任务元数据在整个流程中保持完整"""
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
        )

        # 入队带完整元数据的任务
        msg = TaskMessage(
            doc_id="meta-doc-001",
            kb_id="meta-kb-001",
            file_path="data/uploads/metadata-test.docx",
        )
        await task_queue.enqueue(msg)

        # 消费并验证
        results = await task_queue.consume("test-worker", count=1, block_ms=100)
        assert len(results) == 1
        consumed_id, consumed_msg = results[0]

        # 验证元数据完整
        assert consumed_msg.doc_id == "meta-doc-001"
        assert consumed_msg.kb_id == "meta-kb-001"
        assert consumed_msg.file_path == "data/uploads/metadata-test.docx"
        assert consumed_msg.retry_count == 0
        assert consumed_msg.trace_id != ""  # 自动生成
        assert consumed_msg.created_at > 0  # 自动填充

        # 处理任务
        await worker._process_task(consumed_id, consumed_msg)

        # 验证 pipeline.process 使用正确参数
        mock_pipeline.process.assert_called_once_with(
            file_path="data/uploads/metadata-test.docx",
            doc_id="meta-doc-001",
            kb_id="meta-kb-001",
        )


# ============================================================
# 测试 2：Redis 降级模式 (Requirement 1.6)
# ============================================================


class TestRedisFallbackE2E:
    """Redis 降级模式测试 - queue 为 None 时 fallback 到 asyncio.create_task"""

    @pytest.mark.asyncio
    async def test_fallback_when_queue_is_none(self):
        """当 queue 为 None 时，使用 asyncio.create_task 执行处理"""
        # 模拟 _enqueue_or_fallback 的逻辑
        from app.api.document import _enqueue_or_fallback

        mock_request = MagicMock()
        mock_request.app.state = MagicMock(spec=[])  # 没有 task_queue 属性

        with patch(
            "app.api.document.asyncio.create_task"
        ) as mock_create_task:
            with patch(
                "app.api.document._run_pipeline_safe",
                new_callable=AsyncMock,
            ) as mock_run:
                # 让 create_task 返回一个 mock task
                mock_task = MagicMock()
                mock_create_task.return_value = mock_task

                await _enqueue_or_fallback(
                    mock_request,
                    "data/uploads/fallback.pdf",
                    "fallback-doc-001",
                    "fallback-kb-001",
                )

                # 验证 asyncio.create_task 被调用
                mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_when_queue_enqueue_fails(self):
        """当 queue.enqueue 抛出异常时，降级为 asyncio.create_task"""
        from app.api.document import _enqueue_or_fallback

        mock_queue = AsyncMock()
        mock_queue.enqueue = AsyncMock(
            side_effect=ConnectionError("Redis connection lost")
        )

        mock_request = MagicMock()
        mock_request.app.state.task_queue = mock_queue

        with patch(
            "app.api.document.asyncio.create_task"
        ) as mock_create_task:
            with patch(
                "app.api.document._run_pipeline_safe",
                new_callable=AsyncMock,
            ):
                mock_task = MagicMock()
                mock_create_task.return_value = mock_task

                await _enqueue_or_fallback(
                    mock_request,
                    "data/uploads/fallback2.pdf",
                    "fallback-doc-002",
                    "fallback-kb-002",
                )

                # 验证降级到 asyncio.create_task
                mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_success_does_not_fallback(self, task_queue):
        """当 queue.enqueue 成功时，不应触发 fallback"""
        from app.api.document import _enqueue_or_fallback

        mock_request = MagicMock()
        mock_request.app.state.task_queue = task_queue

        with patch(
            "app.api.document.asyncio.create_task"
        ) as mock_create_task:
            await _enqueue_or_fallback(
                mock_request,
                "data/uploads/normal.pdf",
                "normal-doc-001",
                "normal-kb-001",
            )

            # asyncio.create_task 不应被调用
            mock_create_task.assert_not_called()

        # 验证消息已入队
        stats = await task_queue.get_stats()
        assert stats.stream_length == 1

    @pytest.mark.asyncio
    async def test_fallback_logs_warning(self, caplog):
        """降级时应输出 WARNING 日志"""
        import logging
        from app.api.document import _enqueue_or_fallback

        mock_request = MagicMock()
        mock_request.app.state = MagicMock(spec=[])  # 没有 task_queue 属性

        with caplog.at_level(logging.WARNING):
            with patch(
                "app.api.document.asyncio.create_task"
            ) as mock_create_task:
                with patch("app.api.document._run_pipeline_safe", new_callable=AsyncMock):
                    mock_create_task.return_value = MagicMock()
                    await _enqueue_or_fallback(
                        mock_request,
                        "data/uploads/warn.pdf",
                        "warn-doc-001",
                        "warn-kb-001",
                    )

        assert "Redis unavailable, falling back to in-process task" in caplog.text


# ============================================================
# 测试 3：并发控制 (Requirement 4.1)
# ============================================================


class TestConcurrencyE2E:
    """并发控制端到端测试 - 验证 max_concurrent 限制"""

    @pytest.mark.asyncio
    async def test_max_concurrent_respected(
        self, task_queue, mock_db_session_factory
    ):
        """同时提交 N 个任务，验证同时执行数不超过 max_concurrent"""
        max_concurrent = 2
        concurrent_count = 0
        max_observed_concurrent = 0
        processed_docs: list[str] = []

        async def slow_process(file_path, doc_id, kb_id):
            nonlocal concurrent_count, max_observed_concurrent
            concurrent_count += 1
            max_observed_concurrent = max(max_observed_concurrent, concurrent_count)
            await asyncio.sleep(0.05)  # 模拟处理耗时
            processed_docs.append(doc_id)
            concurrent_count -= 1

        mock_pipeline = AsyncMock()
        mock_pipeline.process = slow_process

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=max_concurrent,
        )

        # 入队 6 个任务
        num_tasks = 6
        for i in range(num_tasks):
            msg = TaskMessage(
                doc_id=f"concurrent-doc-{i}",
                kb_id="kb-concurrent",
                file_path=f"data/uploads/concurrent-{i}.pdf",
            )
            await task_queue.enqueue(msg)

        # 消费所有任务并并发处理
        results = await task_queue.consume(
            "test-worker", count=num_tasks, block_ms=100
        )
        assert len(results) == num_tasks

        # 并发执行所有任务
        tasks = []
        for msg_id, msg in results:
            tasks.append(asyncio.create_task(worker._process_task(msg_id, msg)))

        await asyncio.gather(*tasks)

        # 验证：最大并发数不超过 max_concurrent
        assert max_observed_concurrent <= max_concurrent
        # 验证：所有任务都被处理
        assert len(processed_docs) == num_tasks

    @pytest.mark.asyncio
    async def test_semaphore_released_on_success(
        self, task_queue, mock_pipeline, mock_db_session_factory
    ):
        """任务成功完成后 semaphore 被释放"""
        max_concurrent = 1
        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=max_concurrent,
        )

        # 处理第一个任务
        msg1 = TaskMessage(
            doc_id="release-doc-1", kb_id="kb-1", file_path="f1.pdf"
        )
        msg_id1 = await task_queue.enqueue(msg1)
        results1 = await task_queue.consume("test-worker", count=1, block_ms=100)
        await worker._process_task(results1[0][0], results1[0][1])

        # semaphore 应该已释放，可以处理第二个任务
        msg2 = TaskMessage(
            doc_id="release-doc-2", kb_id="kb-1", file_path="f2.pdf"
        )
        await task_queue.enqueue(msg2)
        results2 = await task_queue.consume("test-worker", count=1, block_ms=100)
        await worker._process_task(results2[0][0], results2[0][1])

        # 两个任务都应被处理
        assert mock_pipeline.process.call_count == 2

    @pytest.mark.asyncio
    async def test_semaphore_released_on_failure(
        self, task_queue, mock_db_session_factory
    ):
        """任务失败后 semaphore 也被释放"""
        max_concurrent = 1
        call_count = 0

        async def failing_then_success(file_path, doc_id, kb_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")
            # 第二次成功

        mock_pipeline = AsyncMock()
        mock_pipeline.process = failing_then_success

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=max_concurrent,
            max_retries=0,  # 不重试，直接进 DLQ
        )

        # 第一个任务会失败
        msg1 = TaskMessage(
            doc_id="fail-doc-1", kb_id="kb-1", file_path="f1.pdf"
        )
        await task_queue.enqueue(msg1)
        results1 = await task_queue.consume("test-worker", count=1, block_ms=100)
        await worker._process_task(results1[0][0], results1[0][1])

        # semaphore 应该已释放，第二个任务可以执行
        msg2 = TaskMessage(
            doc_id="fail-doc-2", kb_id="kb-1", file_path="f2.pdf"
        )
        await task_queue.enqueue(msg2)
        results2 = await task_queue.consume("test-worker", count=1, block_ms=100)
        await worker._process_task(results2[0][0], results2[0][1])

        # 两个任务都应被尝试处理
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_tasks_all_complete(
        self, task_queue, mock_db_session_factory
    ):
        """所有并发任务最终都能完成，不会因 semaphore 死锁"""
        max_concurrent = 3
        num_tasks = 10
        completed: list[str] = []

        async def track_process(file_path, doc_id, kb_id):
            await asyncio.sleep(0.02)
            completed.append(doc_id)

        mock_pipeline = AsyncMock()
        mock_pipeline.process = track_process

        worker = PipelineWorker(
            queue=task_queue,
            pipeline=mock_pipeline,
            db_session_factory=mock_db_session_factory,
            max_concurrent=max_concurrent,
        )

        # 入队所有任务
        for i in range(num_tasks):
            msg = TaskMessage(
                doc_id=f"all-doc-{i}", kb_id="kb-1", file_path=f"f{i}.pdf"
            )
            await task_queue.enqueue(msg)

        # 消费并并发处理
        results = await task_queue.consume(
            "test-worker", count=num_tasks, block_ms=100
        )

        tasks = []
        for msg_id, msg in results:
            tasks.append(asyncio.create_task(worker._process_task(msg_id, msg)))

        # 设置超时，防止死锁
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

        # 所有任务都应完成
        assert len(completed) == num_tasks
