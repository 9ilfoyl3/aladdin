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
from app.models.manager import get_model_manager
from app.pipeline.pipeline import DocumentPipeline
from app.pipeline.queue import TaskQueue
from app.pipeline.worker import PipelineWorker
from app.storage.database import init_db, async_session
from app.storage.milvus import MilvusClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker_main")


async def _load_embed_configs():
    """从数据库加载 active 的 Embedding/Rerank 配置覆盖环境变量默认值"""
    import uuid
    from sqlalchemy import select, func
    from app.schema.db import EmbedConfig

    try:
        settings = get_settings()
        async with async_session() as session:
            # 检查是否有 embedding 配置，没有则创建默认
            embed_count = await session.scalar(
                select(func.count()).select_from(EmbedConfig).where(EmbedConfig.config_type == "embedding")
            )
            if embed_count == 0:
                provider_type = "remote" if settings.embed_provider == "remote" else "local"
                local_prov = None if provider_type == "remote" else settings.embed_provider
                default_embed = EmbedConfig(
                    id=str(uuid.uuid4()),
                    name="默认 Embedding" if provider_type == "local" else "远程 Embedding",
                    config_type="embedding",
                    provider=provider_type,
                    local_provider=local_prov,
                    model_name=settings.embed_model,
                    device=settings.embed_device,
                    base_url=settings.embed_base_url or None,
                    api_key=settings.embed_api_key or None,
                    timeout=60.0,
                    is_active=True,
                )
                session.add(default_embed)

            # 检查是否有 rerank 配置
            rerank_count = await session.scalar(
                select(func.count()).select_from(EmbedConfig).where(EmbedConfig.config_type == "rerank")
            )
            if rerank_count == 0:
                provider_type = "remote" if settings.rerank_provider == "remote" else "local"
                local_prov = None if provider_type == "remote" else settings.rerank_provider
                default_rerank = EmbedConfig(
                    id=str(uuid.uuid4()),
                    name="默认 Rerank" if provider_type == "local" else "远程 Rerank",
                    config_type="rerank",
                    provider=provider_type,
                    local_provider=local_prov,
                    model_name=settings.rerank_model,
                    device=settings.rerank_device,
                    base_url=settings.rerank_base_url or None,
                    api_key=settings.rerank_api_key or None,
                    timeout=60.0,
                    is_active=True,
                )
                session.add(default_rerank)

            await session.commit()

            # 加载 active 配置
            result = await session.execute(
                select(EmbedConfig).where(EmbedConfig.config_type == "embedding", EmbedConfig.is_active == True)
            )
            embed_config = result.scalar_one_or_none()

            result = await session.execute(
                select(EmbedConfig).where(EmbedConfig.config_type == "rerank", EmbedConfig.is_active == True)
            )
            rerank_config = result.scalar_one_or_none()

        manager = get_model_manager()
        if embed_config:
            manager.reload_embedder(
                provider=embed_config.provider,
                local_provider=embed_config.local_provider,
                model_name=embed_config.model_name,
                device=embed_config.device,
                base_url=embed_config.base_url or "",
                api_key=embed_config.api_key or "",
                timeout=embed_config.timeout,
            )
            print(f"[Worker] Embedding 配置已加载: {embed_config.name} ({embed_config.base_url})")

        if rerank_config:
            manager.reload_reranker(
                provider=rerank_config.provider,
                local_provider=rerank_config.local_provider,
                model_name=rerank_config.model_name,
                device=rerank_config.device,
                base_url=rerank_config.base_url or "",
                api_key=rerank_config.api_key or "",
                timeout=rerank_config.timeout,
            )
            print(f"[Worker] Rerank 配置已加载: {rerank_config.name} ({rerank_config.base_url})")

    except Exception as e:
        logger.warning("加载数据库 Embed/Rerank 配置失败，使用环境变量默认值: %s", e)


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
    print("=" * 50)

    # 初始化数据库（确保表存在 + migration）
    await init_db()

    # 加载 Embedding/Rerank 配置
    await _load_embed_configs()

    # 连接 Redis
    task_queue = await TaskQueue.create(settings.redis_url)
    if task_queue is None:
        print("[Worker] ❌ Redis 不可用，Worker 无法启动")
        sys.exit(1)

    # 创建 Pipeline
    model_manager = get_model_manager()
    milvus_client = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)

    # 加载 OCR 配置
    from app.pipeline.ocr.manager import OCRManager
    from app.schema.db import OCRConfig
    from sqlalchemy import select

    ocr_manager = None
    async with async_session() as session:
        result = await session.execute(select(OCRConfig))
        configs = result.scalars().all()
    if configs:
        ocr_manager = OCRManager(configs)

    pipeline = DocumentPipeline(
        model_manager=model_manager,
        milvus_client=milvus_client,
        db_session_factory=async_session,
        ocr_manager=ocr_manager,
    )

    # 创建 Worker
    worker = PipelineWorker(
        queue=task_queue,
        pipeline=pipeline,
        db_session_factory=async_session,
        max_concurrent=settings.pipeline_max_concurrent,
        max_retries=settings.pipeline_max_retries,
    )

    # 优雅关闭（仅 Unix 支持 signal handler）
    import sys
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()

        def _signal_handler():
            print("\n[Worker] 收到停止信号，正在优雅关闭...")
            asyncio.create_task(worker.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # 启动 Worker
    await worker.start()

    print("[Worker] Worker 已停止")


if __name__ == "__main__":
    asyncio.run(main())
