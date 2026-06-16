"""共享启动逻辑

API 服务和 Worker 进程共用的初始化函数，避免代码重复。
"""

import asyncio
import logging
import uuid

from sqlalchemy import select, func

from app.config import get_settings
from app.models.manager import get_model_manager
from app.pipeline.ocr.manager import OCRManager
from app.schema.db import ASRConfig, EmbedConfig, OCRConfig
from app.storage.database import async_session

logger = logging.getLogger(__name__)


def configure_thread_pool() -> None:
    """设置当前事件循环的默认线程池（asyncio.to_thread 使用它）。

    文档解析/切片、pymilvus 同步检索、bcrypt 等阻塞调用都经 asyncio.to_thread
    卸载到此线程池。默认 executor 上限是 min(32, CPU+4)，CPU 核多的机器够用；
    但在受限容器里希望显式控量，故由 THREAD_POOL_MAX_WORKERS 配置：
      0  -> 沿用 Python 默认（不显式设置 executor）
      >0 -> 固定为该上限

    API 与 Worker 进程各自的事件循环都需调用一次（启动早期、首个 to_thread 之前）。
    """
    settings = get_settings()
    max_workers = settings.thread_pool_max_workers
    if max_workers and max_workers > 0:
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="artoo-worker")
        )
        logger.info("线程池默认 executor 已设上限: max_workers=%d", max_workers)


async def load_embed_configs() -> None:
    """从数据库加载 active 的 Embedding/Rerank 配置覆盖环境变量默认值

    如果数据库中没有任何配置，根据环境变量自动创建默认配置（仅当配置了服务地址时）。
    API 和 Worker 启动时都需要调用此函数。
    """
    try:
        settings = get_settings()

        async with async_session() as session:
            # 清理历史遗留的 local 类型配置
            from sqlalchemy import delete
            await session.execute(
                delete(EmbedConfig).where(EmbedConfig.provider == "local")
            )
            await session.commit()

            # 检查是否有 embedding 配置，没有且环境变量配置了地址则创建默认
            embed_count = await session.scalar(
                select(func.count()).select_from(EmbedConfig).where(
                    EmbedConfig.config_type == "embedding"
                )
            )
            if embed_count == 0 and settings.embed_base_url:
                default_embed = EmbedConfig(
                    id=str(uuid.uuid4()),
                    name="远程 Embedding",
                    config_type="embedding",
                    provider="remote",
                    model_name=settings.embed_model,
                    base_url=settings.embed_base_url or None,
                    api_key=settings.embed_api_key or None,
                    timeout=60.0,
                    sparse_enabled=settings.embed_sparse_enabled,
                    is_active=True,
                )
                session.add(default_embed)

            # 检查是否有 rerank 配置，没有且环境变量配置了地址则创建默认
            rerank_count = await session.scalar(
                select(func.count()).select_from(EmbedConfig).where(
                    EmbedConfig.config_type == "rerank"
                )
            )
            if rerank_count == 0 and settings.rerank_base_url:
                default_rerank = EmbedConfig(
                    id=str(uuid.uuid4()),
                    name="远程 Rerank",
                    config_type="rerank",
                    provider="remote",
                    model_name=settings.rerank_model,
                    base_url=settings.rerank_base_url or None,
                    api_key=settings.rerank_api_key or None,
                    timeout=60.0,
                    is_active=True,
                )
                session.add(default_rerank)

            await session.commit()

            # 加载 active 的 embedding 配置
            result = await session.execute(
                select(EmbedConfig).where(
                    EmbedConfig.config_type == "embedding",
                    EmbedConfig.is_active == True,
                )
            )
            embed_config = result.scalar_one_or_none()

            # 加载 active 的 rerank 配置
            result = await session.execute(
                select(EmbedConfig).where(
                    EmbedConfig.config_type == "rerank",
                    EmbedConfig.is_active == True,
                )
            )
            rerank_config = result.scalar_one_or_none()

        manager = get_model_manager()

        if embed_config and embed_config.base_url:
            manager.reload_embedder(
                model_name=embed_config.model_name,
                base_url=embed_config.base_url or "",
                api_key=embed_config.api_key or "",
                timeout=embed_config.timeout,
                sparse_enabled=embed_config.sparse_enabled,
                max_connections=settings.pipeline_embed_max_connections,
            )
            logger.info(
                "Embedding 配置已加载: %s (%s, sparse=%s)",
                embed_config.name, embed_config.base_url, embed_config.sparse_enabled,
            )

        if rerank_config and rerank_config.base_url:
            manager.reload_reranker(
                model_name=rerank_config.model_name,
                base_url=rerank_config.base_url or "",
                api_key=rerank_config.api_key or "",
                timeout=rerank_config.timeout,
            )
            logger.info(
                "Rerank 配置已加载: %s (%s)", rerank_config.name, rerank_config.base_url
            )

    except Exception as e:
        logger.warning("加载数据库 Embed/Rerank 配置失败，使用环境变量默认值: %s", e)


async def load_ocr_manager() -> OCRManager | None:
    """从数据库加载 OCR 配置并创建 OCRManager

    Returns:
        OCRManager 实例，如果没有配置则返回 None
    """
    try:
        async with async_session() as session:
            result = await session.execute(select(OCRConfig))
            configs = result.scalars().all()
        if configs:
            logger.info("load_ocr_manager: 找到 %d 条 OCR 配置", len(configs))
            return OCRManager(configs)
        logger.info("load_ocr_manager: 数据库中无 OCR 配置")
        return None
    except Exception as e:
        logger.warning("加载 OCR 配置失败: %s", e)
        return None


async def load_asr_manager():
    """从数据库加载 ASR 配置并创建 ASRManager

    Returns:
        ASRManager 实例，如果没有配置则返回 None
    """
    from app.pipeline.asr.manager import ASRManager

    try:
        async with async_session() as session:
            result = await session.execute(select(ASRConfig))
            configs = result.scalars().all()
        if configs:
            logger.info("load_asr_manager: 找到 %d 条 ASR 配置", len(configs))
            return ASRManager(configs)
        logger.info("load_asr_manager: 数据库中无 ASR 配置")
        return None
    except Exception as e:
        logger.warning("加载 ASR 配置失败: %s", e)
        return None


async def start_invalidation_bus(handlers: dict[str, callable]) -> None:
    """初始化并启动 InvalidationBus 后台订阅（subOnce 防重）。

    API 和 Worker 进程启动时调用，传入各自的 handler 映射。
    """
    from app.storage.invalidation import init_invalidation_bus

    bus = await init_invalidation_bus()
    if bus and bus._redis is not None:
        # subOnce 防重：bus 单例 + subscribe_loop 内部 _loop_started 守卫，
        # 即使本函数被重复调用也只会有一个订阅循环（避免多份订阅交替重连刷屏）。
        if getattr(bus, "_loop_started", False):
            logger.info("InvalidationBus 后台订阅已在运行，跳过重复启动")
            return
        # 后台协程，不阻塞启动
        asyncio.create_task(bus.subscribe_loop(handlers))
        logger.info("InvalidationBus 后台订阅已启动")
