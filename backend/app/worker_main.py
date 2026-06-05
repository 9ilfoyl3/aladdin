"""Pipeline Worker 独立进程入口

独立于 API 服务运行，消费 Redis Stream 中的文档处理任务。
与 API 不共享事件循环，避免大文件 Embedding 阻塞 API 响应。

启动方式：
    python -m app.worker_main
"""

import asyncio
import logging
import signal
import sys

from app.logging_config import setup_logging

# 在所有业务模块 import 之前配置日志
setup_logging(service_name="worker")

from app.config import get_settings
from app.pipeline.factory import create_pipeline
from app.pipeline.queue import TaskQueue
from app.pipeline.worker import PipelineWorker
from app.startup import configure_thread_pool, load_embed_configs
from app.storage.database import async_session, init_db

logger = logging.getLogger("worker_main")

# 优雅关闭超时（秒），超时后强制退出
_SHUTDOWN_TIMEOUT = 60


async def main():
    """Worker 主函数"""
    settings = get_settings()

    # 设置线程池上限（在任何 asyncio.to_thread 调用之前）
    configure_thread_pool()

    print("=" * 50)
    print("[Worker] Pipeline Worker 独立进程启动")
    print(f"[Worker] max_concurrent={settings.pipeline_max_concurrent}, "
          f"max_retries={settings.pipeline_max_retries}")
    print(f"[Worker] embed_batch_size={settings.pipeline_embed_batch_size}, "
          f"embed_concurrency={settings.pipeline_embed_concurrency}")
    print(f"[Worker] task_timeout={settings.pipeline_task_timeout_minutes}min, "
          f"circuit_breaker={settings.pipeline_circuit_breaker_threshold}")
    print(f"[Worker] slow_lane_min_mb={settings.pipeline_slow_lane_min_mb}, "
          f"slow_max_concurrent={settings.pipeline_slow_max_concurrent}")
    print(f"[Worker] db_pool={settings.db_pool_size}+{settings.db_max_overflow}, "
          f"thread_pool_max_workers={settings.thread_pool_max_workers or 'default'}")
    print("=" * 50)

    # 初始化数据库（确保表存在 + migration）
    await init_db()

    # 加载 Embedding/Rerank 配置（与 API 共用逻辑）
    await load_embed_configs()

    # 连接 Redis（快道）
    task_queue = await TaskQueue.create(settings.redis_url)
    if task_queue is None:
        print("[Worker] ❌ Redis 不可用，Worker 无法启动")
        sys.exit(1)

    # 慢道队列（大文件）：独立 stream + consumer group，与快道物理隔离，
    # 由同一个 Worker 进程消费，但受 slow_max_concurrent 限制在途数。
    slow_queue = await TaskQueue.create(
        settings.redis_url,
        stream_key="pipeline:tasks:slow",
        dlq_key="pipeline:dlq",
        group_name="pipeline-workers",
    )

    # 创建 Pipeline（通过工厂函数统一组装依赖）
    pipeline = await create_pipeline()

    # 创建 Worker
    worker = PipelineWorker(
        queue=task_queue,
        pipeline=pipeline,
        db_session_factory=async_session,
        max_concurrent=settings.pipeline_max_concurrent,
        max_retries=settings.pipeline_max_retries,
        slow_queue=slow_queue,
        slow_max_concurrent=settings.pipeline_slow_max_concurrent,
    )

    # 启动跨进程失效广播（InvalidationBus）—— M1/M2/M7 多进程热生效
    from app.startup import start_invalidation_bus
    from app.retrieval.cache import get_retrieval_cache
    from app.storage.milvus import get_milvus_client, MilvusClient
    from sqlalchemy import select

    async def _handle_kb_data(kb_id: str):
        """收到 kb_data 失效信号：清除对应知识库的 Milvus 加载缓存 + 检索结果缓存"""
        milvus = get_milvus_client()
        collection_name = MilvusClient._collection_name(kb_id)
        milvus._loaded_at.pop(collection_name, None)
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
        logger.info("InvalidationBus: kb_data 失效完成 kb_id=%s", kb_id)

    async def _handle_tenant_config(tenant_id: str):
        """收到 tenant_config 失效信号（M1 + M7）：
        1. 失效该租户的检索配置缓存（M1 多进程热生效）
        2. 失效该租户名下所有 KB 的检索结果缓存（M7 配置变更失效结果缓存）
        """
        from app.retrieval.config import get_retrieval_config_store
        store = get_retrieval_config_store()
        store.invalidate(tenant_id)

        # M7: 额外失效该租户名下 KB 的检索结果缓存
        cache = await get_retrieval_cache()
        kb_ids: list[str] = []
        if cache:
            kb_ids = await _get_tenant_kb_ids(tenant_id)
            for kb_id in kb_ids:
                await cache.invalidate_kb(kb_id)
        logger.info(
            "InvalidationBus: tenant_config 失效完成 tenant_id=%s, 失效 %d 个 KB 缓存",
            tenant_id, len(kb_ids),
        )

    async def _get_tenant_kb_ids(tenant_id: str) -> list[str]:
        """查询数据库获取该租户名下的所有 kb_id（轻量 select 仅取 id 列）"""
        try:
            from app.schema.db import KnowledgeBase
            async with async_session() as session:
                result = await session.execute(
                    select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id)
                )
                return [row[0] for row in result.all()]
        except Exception as e:
            logger.warning("查询租户 %s KB 列表失败（跳过结果缓存失效）: %s", tenant_id, e)
            return []

    await start_invalidation_bus({
        "kb_data": _handle_kb_data,
        "tenant_config": _handle_tenant_config,
    })

    # 优雅关闭（仅 Unix 支持 signal handler）
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()

        def _signal_handler():
            print("\n[Worker] 收到停止信号，正在优雅关闭...")

            async def _graceful_shutdown():
                try:
                    await asyncio.wait_for(worker.stop(), timeout=_SHUTDOWN_TIMEOUT)
                except asyncio.TimeoutError:
                    print(f"[Worker] ⚠️ 优雅关闭超时（{_SHUTDOWN_TIMEOUT}s），强制退出")
                    logger.warning("Graceful shutdown timed out after %ds", _SHUTDOWN_TIMEOUT)

            asyncio.create_task(_graceful_shutdown())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # 启动 Worker
    await worker.start()

    print("[Worker] Worker 已停止")


if __name__ == "__main__":
    asyncio.run(main())
