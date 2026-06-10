"""FastAPI 应用入口"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.logging_config import setup_logging

# 在所有业务模块 import 之前配置日志
setup_logging(service_name="backend")

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
from app.api.session_upload import router as session_upload_router
from app.api.skills import router as skills_router
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
    # 最早设置线程池上限（在任何 asyncio.to_thread 调用之前）
    from app.startup import configure_thread_pool
    configure_thread_pool()

    await init_db()

    # 自动迁移：添加可能缺失的新列
    await _auto_migrate_columns()

    # 初始化对象存储 bucket（知识库源文件权威存储）
    await _init_object_store()

    # 从数据库加载 active 的 Embed/Rerank 配置覆盖环境变量默认值
    await load_embed_configs()

    # 启动时补偿清理 + 对账：清本地遗留文件 + 删 MinIO 无 DB 记录的孤儿对象
    asyncio.create_task(_cleanup_orphan_files())

    # 初始化 TaskQueue（仅用于入队，Worker 在独立进程中运行）
    await _init_task_queue(app)

    # 启动 API Key 用量追踪器（内存合并 + 周期批量落库，使鉴权关键路径零写库）
    from app.auth.apikey_usage import init_usage_tracker, shutdown_usage_tracker
    init_usage_tracker(async_session)

    # 启动跨进程失效广播（InvalidationBus）
    from app.startup import start_invalidation_bus
    from app.storage.milvus import get_milvus_client, MilvusClient
    from app.retrieval.cache import get_retrieval_cache

    async def _handle_kb_data(kb_id: str):
        """收到 kb_data 失效信号：清除对应知识库的 Milvus 加载缓存 + 检索结果缓存"""
        # 失效 Milvus 加载缓存（使下次搜索强制重新 load）
        milvus = get_milvus_client()
        collection_name = MilvusClient._collection_name(kb_id)
        milvus._loaded_at.pop(collection_name, None)
        # 失效检索结果缓存
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


async def _init_object_store() -> None:
    """初始化对象存储 bucket（知识库源文件权威存储）。不可用时记 WARNING 不阻塞启动。"""
    from app.storage.object_store import get_object_store

    store = get_object_store()
    if store is None:
        print("[API] ⚠️ MinIO 未配置或不可用，文件上传将不可用")
        logger.warning("Object store unavailable; file upload disabled")
        return
    try:
        await store.ensure_bucket()
        print(f"[API] 对象存储就绪 (bucket={store.bucket})")
        logger.info("Object store ready (bucket=%s)", store.bucket)
    except Exception as e:  # noqa: BLE001
        print(f"[API] ⚠️ 初始化 MinIO bucket 失败: {e}")
        logger.warning("Failed to init MinIO bucket: %s", e)


async def _cleanup_orphan_files() -> None:
    """启动时补偿清理 + 对账：

    1. 本地遗留文件清理：删 DB 无记录但磁盘残留的旧上传文件（历史 / 降级路径产物）。
    2. MinIO ↔ DB 对账：删 MinIO 中无对应 DB 记录的孤儿对象（上传补偿没删干净、
       或硬崩溃留下的残留）。仅删 last_modified 早于宽限期的对象，避免误删正在
       上传/建索引中（DB 行尚未提交）的新对象。

    两者相辅相成：上传路径已做即时失败补偿（删刚上传的对象），本对账是兜底。
    """
    await _cleanup_local_orphan_files()
    await _reconcile_minio_orphans()


async def _cleanup_local_orphan_files() -> None:
    """删 DB 无记录但本地磁盘残留的旧上传文件（历史 / 降级路径产物）。
    不删 pending/processing 状态文档的文件（Worker 可能正在处理）。
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
            print(f"[Cleanup] 启动清理：删除了 {orphan_count} 个本地孤儿上传文件")
            logger.info("Startup cleanup: removed %d local orphan upload files", orphan_count)
    except Exception as e:
        logger.warning("Startup local orphan cleanup failed (non-critical): %s", e)


async def _reconcile_minio_orphans() -> None:
    """对账 MinIO 与 DB，删除无对应 DB 记录的孤儿对象（KB 源文件 / 缩略图 / 会话附件）。

    对象布局：
    - KB 源文件：根级 ``{doc_id}.{ext}`` —— 比对 ``Document.id``
    - 缩略图：``thumbnails/{doc_id}.png`` —— 比对 ``Document.id``
    - 会话附件：``sessions/{session_id}/{file_id}.{ext}`` —— 比对 ``SessionFile.id``

    仅删 last_modified 早于 now - grace 的对象，避免误删正在上传/建索引的新对象。
    """
    import time

    from app.storage.object_store import get_object_store

    store = get_object_store()
    if store is None:
        return

    settings = get_settings()
    cutoff = time.time() - settings.minio_orphan_grace_seconds

    try:
        objects = await store.list_objects_with_mtime("")
    except Exception as e:  # noqa: BLE001
        logger.warning("MinIO 对账列举对象失败（跳过）: %s", e)
        return

    if not objects:
        return

    # 收集候选 id（只看超过宽限期的对象）
    doc_ids: set[str] = set()           # KB 源文件 / 缩略图的 doc_id
    session_file_ids: set[str] = set()  # 会话附件 file_id
    # key -> 归属类型，便于后续判定删除
    candidates: list[tuple[str, str, str]] = []  # (key, kind, id)

    for key, mtime in objects:
        if mtime > cutoff:
            continue  # 宽限期内的新对象，跳过
        if key.startswith("thumbnails/"):
            stem = key[len("thumbnails/"):].rsplit(".", 1)[0]
            doc_ids.add(stem)
            candidates.append((key, "doc", stem))
        elif key.startswith("sessions/"):
            # sessions/{session_id}/{file_id}.{ext}
            rest = key[len("sessions/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                file_id = parts[1].rsplit(".", 1)[0]
                session_file_ids.add(file_id)
                candidates.append((key, "session", file_id))
        else:
            stem = key.rsplit(".", 1)[0]
            if "/" in stem:
                continue  # 未知层级，保守跳过
            doc_ids.add(stem)
            candidates.append((key, "doc", stem))

    if not candidates:
        return

    # 批量查 DB 中实际存在的 id
    existing_doc_ids: set[str] = set()
    existing_session_file_ids: set[str] = set()
    try:
        async with async_session() as session:
            if doc_ids:
                result = await session.execute(
                    select(Document.id).where(Document.id.in_(list(doc_ids)))
                )
                existing_doc_ids = {row[0] for row in result.all()}
            if session_file_ids:
                from app.schema.db import SessionFile

                result = await session.execute(
                    select(SessionFile.id).where(
                        SessionFile.id.in_(list(session_file_ids))
                    )
                )
                existing_session_file_ids = {row[0] for row in result.all()}
    except Exception as e:  # noqa: BLE001
        logger.warning("MinIO 对账查询 DB 失败（跳过删除）: %s", e)
        return

    # 找出孤儿对象 key
    orphan_keys: list[str] = []
    for key, kind, ident in candidates:
        if kind == "doc" and ident not in existing_doc_ids:
            orphan_keys.append(key)
        elif kind == "session" and ident not in existing_session_file_ids:
            orphan_keys.append(key)

    if not orphan_keys:
        return

    try:
        await store.remove_many(orphan_keys)
        print(f"[Reconcile] MinIO 对账：删除了 {len(orphan_keys)} 个孤儿对象")
        logger.info("MinIO reconcile: removed %d orphan objects", len(orphan_keys))
    except Exception as e:  # noqa: BLE001
        logger.warning("MinIO 对账删除孤儿对象失败（非致命）: %s", e)



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
app.include_router(session_upload_router)
app.include_router(skills_router)
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
