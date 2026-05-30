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

from app.config import get_settings
from app.pipeline.factory import create_pipeline
from app.pipeline.queue import TaskQueue
from app.pipeline.worker import PipelineWorker
from app.startup import load_embed_configs
from app.storage.database import async_session, init_db

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker_main")

# 优雅关闭超时（秒），超时后强制退出
_SHUTDOWN_TIMEOUT = 60


async def main():
    """Worker 主函数"""
    settings = get_settings()

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
