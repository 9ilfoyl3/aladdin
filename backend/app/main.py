"""FastAPI 应用入口"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.agent_config import router as agent_config_router
from app.api.api_key import router as api_key_router
from app.api.auth_routes import router as auth_router
from app.api.admin_routes import router as admin_router
from app.api.invitation_routes import router as invitation_router
from app.mcp_server import router as mcp_router
from app.api.chat import router as chat_router
from app.api.document import router as document_router
from app.api.embed_config import router as embed_config_router
from app.api.folder import router as folder_router
from app.api.knowledge_base import router as kb_router
from app.api.llm_config import router as llm_config_router
from app.api.ocr_config import router as ocr_config_router
from app.api.retrieval import router as retrieval_router
from app.api.session import router as session_router
from app.api.system import router as system_router
from app.config import get_settings
from app.pipeline.queue import TaskQueue
from app.schema.db import Document
from app.startup import load_embed_configs
from app.storage.database import async_session, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库，加载模型配置"""
    await init_db()

    # 自动迁移：添加可能缺失的新列
    await _auto_migrate_columns()

    # 从数据库加载 active 的 Embed/Rerank 配置覆盖环境变量默认值
    await load_embed_configs()

    # 启动时补偿清理：删除孤儿上传文件（DB 记录已删除但文件残留的情况）
    asyncio.create_task(_cleanup_orphan_files())

    # 初始化 TaskQueue（仅用于入队，Worker 在独立进程中运行）
    await _init_task_queue(app)

    # 启动 API Key 用量追踪器（内存合并 + 周期批量落库，使鉴权关键路径零写库）
    from app.auth.apikey_usage import init_usage_tracker, shutdown_usage_tracker
    init_usage_tracker(async_session)

    yield

    # 停止用量追踪器并落库剩余增量（优雅关闭，不丢最后一个区间）
    await shutdown_usage_tracker()

    # 关闭 TaskQueue 连接
    await _close_task_queue(app)


async def _auto_migrate_columns() -> None:
    """自动添加可能缺失的数据库列（轻量级迁移）"""
    from sqlalchemy import text

    migrations = [
        # chat_messages.agent_steps (JSON, nullable) - 存储 Agent 思考步骤
        ("chat_messages", "agent_steps", "ALTER TABLE chat_messages ADD COLUMN agent_steps JSON"),
        # agent_presets 表（如果不存在则由 init_db 的 create_all 创建）
    ]

    async with async_session() as session:
        for table, column, sql in migrations:
            try:
                # 检查列是否已存在
                check_sql = text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                )
                result = await session.execute(check_sql, {"table": table, "column": column})
                if result.scalar() is None:
                    await session.execute(text(sql))
                    await session.commit()
                    logger.info("自动迁移：添加列 %s.%s", table, column)
            except Exception as e:
                logger.debug("迁移检查跳过 %s.%s: %s", table, column, e)
                await session.rollback()


async def _init_task_queue(app: FastAPI) -> None:
    """初始化 TaskQueue（仅用于入队，Worker 在独立进程中运行）

    同时初始化快道（常规/小文件）和慢道（大文件）两个队列。大文件按
    PIPELINE_SLOW_LANE_MIN_MB 阈值路由到慢道，避免占满快道导致小文件排队。
    """
    settings = get_settings()
    task_queue = await TaskQueue.create(settings.redis_url)
    app.state.task_queue = task_queue

    slow_queue = None
    if task_queue is not None:
        slow_queue = await TaskQueue.create(
            settings.redis_url,
            stream_key="pipeline:tasks:slow",
            dlq_key="pipeline:dlq",
            group_name="pipeline-workers",
        )
    app.state.slow_task_queue = slow_queue

    if task_queue is not None:
        print("[API] TaskQueue 已连接 Redis（快道+慢道），文档任务将入队由独立 Worker 处理")
        logger.info("TaskQueue connected to Redis (fast + slow lanes)")
    else:
        print("[API] ⚠️ Redis 不可用，文档上传后将使用降级模式处理")
        logger.warning("Redis unavailable, using fallback mode")


async def _close_task_queue(app: FastAPI) -> None:
    """关闭 TaskQueue 连接（快道 + 慢道）"""
    for attr in ("task_queue", "slow_task_queue"):
        queue = getattr(app.state, attr, None)
        if queue is not None and hasattr(queue, '_redis'):
            try:
                await queue._redis.aclose()
            except Exception:
                pass


async def _cleanup_orphan_files() -> None:
    """启动时补偿清理：删除 DB 中已无记录但磁盘上残留的上传文件

    场景：批量删除时 DB 记录已删除，但后台异步清理本地文件时服务重启了。
    此函数扫描 uploads 目录，对比 DB 中的文档记录，删除孤儿文件。
    注意：不删除 pending/processing 状态文档的文件（Worker 可能正在处理）。
    """
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

        # 批量查询 DB 中存在的 doc_id 及其状态
        async with async_session() as session:
            result = await session.execute(
                select(Document.id, Document.status).where(
                    Document.id.in_(list(disk_files.keys()))
                )
            )
            existing_docs = {row[0]: row[1] for row in result.all()}

        # 删除孤儿文件（DB 中无记录的文件）
        # 不删除 pending/processing 状态的文件（Worker 可能正在处理）
        orphan_count = 0
        for doc_id, file_path in disk_files.items():
            if doc_id not in existing_docs:
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

# 注意：原全局 ApiKeyAuthMiddleware 已退役（tenant-auth）。
# 鉴权改由各路由 Depends(authorization_guard(...)) 统一施加（见任务 9.3）。

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
app.include_router(agent_config_router)
app.include_router(session_router)
app.include_router(mcp_router)
# tenant-auth：认证与管理路由
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(invitation_router)

# 统一异常处理（AppError -> {"detail": ...}，跨租户 404/权限 403 等语义一致）
from app.api.errors import register_exception_handlers  # noqa: E402

register_exception_handlers(app)


@app.get("/")
async def root():
    """根路径健康检查"""
    return {"message": "Agentic RAG System is running"}
