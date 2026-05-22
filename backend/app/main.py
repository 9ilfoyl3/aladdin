"""FastAPI 应用入口"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent_node_config import router as agent_node_config_router
from app.api.api_key import router as api_key_router
from app.api.chat import router as chat_router
from app.api.document import router as document_router
from app.api.embed_config import router as embed_config_router
from app.api.folder import router as folder_router
from app.api.knowledge_base import router as kb_router
from app.api.llm_config import router as llm_config_router
from app.api.ocr_config import router as ocr_config_router
from app.api.middleware import ApiKeyAuthMiddleware
from app.api.retrieval import router as retrieval_router
from app.api.system import router as system_router
from app.config import get_settings
from app.storage.database import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库，加载模型配置，启动 Pipeline Worker"""
    await init_db()
    # 从数据库加载 active 的 Embed/Rerank 配置覆盖环境变量默认值
    await _load_active_embed_configs()

    # 启动时补偿清理：删除孤儿上传文件（DB 记录已删除但文件残留的情况）
    asyncio.create_task(_cleanup_orphan_files())

    # 初始化 TaskQueue 和 PipelineWorker
    await _start_pipeline_worker(app)

    yield

    # 关闭 Pipeline Worker
    await _stop_pipeline_worker(app)


async def _start_pipeline_worker(app: FastAPI) -> None:
    """启动 Pipeline Worker

    尝试连接 Redis 并创建 TaskQueue。如果 Redis 不可用，
    跳过 Worker 启动，仅使用降级模式（asyncio.create_task）。
    """
    from app.models.manager import get_model_manager
    from app.pipeline.pipeline import DocumentPipeline
    from app.pipeline.queue import TaskQueue
    from app.pipeline.worker import PipelineWorker
    from app.storage.database import async_session
    from app.storage.milvus import MilvusClient

    settings = get_settings()

    # 尝试创建 TaskQueue（Redis 不可用时返回 None）
    task_queue = await TaskQueue.create(settings.redis_url)
    app.state.task_queue = task_queue

    if task_queue is not None:
        try:
            # 创建 DocumentPipeline 实例
            model_manager = get_model_manager()
            milvus_client = MilvusClient(
                host=settings.milvus_host, port=settings.milvus_port
            )

            # 从数据库加载 OCR 配置
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

            # 创建并启动 PipelineWorker
            worker = PipelineWorker(
                queue=task_queue,
                pipeline=pipeline,
                db_session_factory=async_session,
                max_concurrent=settings.pipeline_max_concurrent,
                max_retries=settings.pipeline_max_retries,
            )
            app.state.pipeline_worker = worker

            # 以后台任务方式启动 Worker
            worker_task = asyncio.create_task(worker.start())
            app.state.pipeline_worker_task = worker_task
            print(f"[Worker] Pipeline worker initialized, max_concurrent={settings.pipeline_max_concurrent}")
            logger.info("Pipeline worker initialized and started")
        except Exception as e:
            logger.error("Failed to start pipeline worker: %s", e, exc_info=True)
            print(f"[Worker] ❌ Failed to start pipeline worker: {e}")
            app.state.task_queue = task_queue  # 保留 queue 用于入队
            app.state.pipeline_worker = None
            app.state.pipeline_worker_task = None
    else:
        print("[Worker] ⚠️ Redis unavailable, using fallback mode (asyncio.create_task)")
        logger.warning(
            "Redis unavailable, pipeline worker not started, using fallback mode"
        )
        app.state.task_queue = None
        app.state.pipeline_worker = None
        app.state.pipeline_worker_task = None


async def _stop_pipeline_worker(app: FastAPI) -> None:
    """停止 Pipeline Worker"""
    worker = getattr(app.state, "pipeline_worker", None)
    if worker is not None:
        await worker.stop()
        logger.info("Pipeline worker stopped")

    # 取消 worker task（如果仍在运行）
    worker_task = getattr(app.state, "pipeline_worker_task", None)
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


async def _cleanup_orphan_files() -> None:
    """启动时补偿清理：删除 DB 中已无记录但磁盘上残留的上传文件

    场景：批量删除时 DB 记录已删除，但后台异步清理本地文件时服务重启了。
    此函数扫描 uploads 目录，对比 DB 中的文档记录，删除孤儿文件。
    """
    import os
    from pathlib import Path
    from sqlalchemy import select
    from app.schema.db import Document
    from app.storage.database import async_session

    upload_dir = Path("data/uploads")
    if not upload_dir.exists():
        return

    try:
        # 获取磁盘上所有文件的 doc_id（文件名格式: {doc_id}.{ext}）
        disk_files = {}
        for f in upload_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                doc_id = f.stem  # 去掉扩展名就是 doc_id
                disk_files[doc_id] = f

        if not disk_files:
            return

        # 批量查询 DB 中存在的 doc_id
        async with async_session() as session:
            result = await session.execute(
                select(Document.id).where(Document.id.in_(list(disk_files.keys())))
            )
            existing_ids = {row[0] for row in result.all()}

        # 删除孤儿文件
        orphan_count = 0
        for doc_id, file_path in disk_files.items():
            if doc_id not in existing_ids:
                try:
                    os.remove(file_path)
                    orphan_count += 1
                except OSError:
                    pass

        if orphan_count > 0:
            print(f"[Cleanup] 启动清理：删除了 {orphan_count} 个孤儿上传文件")
            logger.info("Startup cleanup: removed %d orphan upload files", orphan_count)
    except Exception as e:
        logger.warning("Startup orphan file cleanup failed (non-critical): %s", e)


async def _load_active_embed_configs():
    """启动时从数据库加载 active 的 Embedding/Rerank 配置
    
    如果数据库中没有任何配置，根据环境变量自动创建默认配置。
    """
    import uuid
    from sqlalchemy import select, func
    from app.schema.db import EmbedConfig
    from app.storage.database import async_session
    from app.models.manager import get_model_manager
    from app.config import get_settings

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

            # 检查是否有 rerank 配置，没有则创建默认
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
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("加载数据库 Embed/Rerank 配置失败，使用环境变量默认值: %s", e)


app = FastAPI(
    title="Agentic RAG System",
    description="基于 Agent 编排的 RAG 知识库系统",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key 认证中间件（仅拦截 /v1/ 路径）
app.add_middleware(ApiKeyAuthMiddleware)

# 注册路由
app.include_router(chat_router, tags=["Chat"])
app.include_router(kb_router)
app.include_router(folder_router)
app.include_router(document_router)
app.include_router(retrieval_router)
app.include_router(system_router)
app.include_router(api_key_router)
app.include_router(llm_config_router)
app.include_router(embed_config_router)
app.include_router(ocr_config_router)
app.include_router(agent_node_config_router)


@app.get("/")
async def root():
    """根路径健康检查"""
    return {"message": "Agentic RAG System is running"}
