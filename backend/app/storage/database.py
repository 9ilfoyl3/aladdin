"""PostgreSQL 数据库初始化与会话管理"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.schema.db import Base

_settings = get_settings()
_database_url = _settings.database_url

# 兼容处理：如果用户配置了不带驱动的 URL，自动补上异步驱动
if _database_url.startswith("sqlite:///"):
    _database_url = _database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
elif _database_url.startswith("postgresql://"):
    _database_url = _database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# 异步引擎（PostgreSQL 使用连接池，pool_size 可按需调整）
engine = create_async_engine(
    _database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

# 异步会话工厂
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 安装租户隔离兜底（方案 B）：对所有 TenantScopedMixin 模型按 contextvar 三态自动
# 注入 tenant 过滤。幂等，API 与 Worker 各 import 一次本模块即生效。
from app.repositories.tenant_repo import install_tenant_loader_criteria  # noqa: E402

install_tenant_loader_criteria()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话（用于 FastAPI 依赖注入）"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _migrate_db() -> None:
    """执行增量迁移（为已有表添加新列，兼容已运行的数据库）"""
    migrations = [
        "ALTER TABLE llm_configs ADD COLUMN stream_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_configs ADD COLUMN max_context_tokens INTEGER",
        "ALTER TABLE llm_configs ADD COLUMN chat_visible BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE llm_configs ADD COLUMN thinking_enabled BOOLEAN DEFAULT FALSE",
        # 文档表新增字段
        "ALTER TABLE documents ADD COLUMN folder_id VARCHAR REFERENCES folders(id)",
        # 文档表新增进度追踪字段
        "ALTER TABLE documents ADD COLUMN progress INTEGER DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN progress_message VARCHAR",
        "ALTER TABLE documents ADD COLUMN file_hash VARCHAR",
        # Embedding 配置表新增 sparse 支持字段
        "ALTER TABLE embed_configs ADD COLUMN sparse_enabled BOOLEAN DEFAULT TRUE",
        # 对话消息表新增知识库追踪字段
        "ALTER TABLE chat_messages ADD COLUMN kb_id VARCHAR",
        "ALTER TABLE chat_messages ADD COLUMN kb_ids JSON",
        # 会话表新增归属用户列：会话/消息为个人对话历史，须按 owner 收敛（per-user 隔离）。
        # 已存在的历史会话 owner_user_id 留空（NULL），将不再出现在任何用户的列表中
        # （无主会话对所有人不可见），避免修复前的跨用户泄露在旧数据上残留。
        "ALTER TABLE chat_sessions ADD COLUMN owner_user_id VARCHAR",
        # 清理历史遗留列：旧版本 knowledge_bases 表带 retrieval_mode NOT NULL 列，
        # 当前模型已移除该字段，插入时不再赋值，会触发 NOT NULL 约束错误。
        # 解除其 NOT NULL 约束以兼容旧库（列保留，值留空，无数据丢失）。
        "ALTER TABLE knowledge_bases ALTER COLUMN retrieval_mode DROP NOT NULL",
        # ===== tenant-rbac-refactor：为旧库补齐租户隔离 / 归属 / 可见性列 =====
        # create_all 只新建缺失的表，不会为已存在的表补列；下列 ALTER 让升级前建立的
        # 旧库（已有业务数据）平滑获得新列。带 NOT NULL 的列给 DEFAULT，已有行自动回填。
        # 受租户隔离的资源表统一补 tenant_id（旧数据归属未知，留 NULL，由后续治理回填）。
        "ALTER TABLE knowledge_bases ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE knowledge_bases ADD COLUMN owner_user_id VARCHAR",
        "ALTER TABLE knowledge_bases ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'private'",
        "ALTER TABLE knowledge_bases ADD COLUMN org_permission VARCHAR NOT NULL DEFAULT 'read'",
        "ALTER TABLE folders ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE documents ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chunks ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chat_sessions ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chat_messages ADD COLUMN tenant_id VARCHAR",
        # API Key 三模型字段（tenant_level / user_level / external_agent）
        "ALTER TABLE api_keys ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE api_keys ADD COLUMN key_type VARCHAR NOT NULL DEFAULT 'tenant_level'",
        "ALTER TABLE api_keys ADD COLUMN bound_user_id VARCHAR",
        "ALTER TABLE api_keys ADD COLUMN authorized_scope JSON",
        "ALTER TABLE api_keys ADD COLUMN key_source VARCHAR",
        # 索引（与模型 index=True 对齐；租户过滤在每次查询都会用到，缺索引影响性能）
        "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_owner_user_id ON knowledge_bases (owner_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_folders_tenant_id ON folders (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_documents_tenant_id ON documents (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chunks_tenant_id ON chunks (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_tenant_id ON chat_sessions (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_tenant_id ON chat_messages (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_tenant_id ON api_keys (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_bound_user_id ON api_keys (bound_user_id)",
    ]
    for sql in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception as e:
            # 列已存在 / 列不存在 等均属正常（幂等迁移），其他错误需要关注
            msg = str(e)
            if any(
                kw in msg
                for kw in ("already exists", "DuplicateColumn", "does not exist", "UndefinedColumn")
            ):
                pass
            else:
                import logging
                logging.getLogger(__name__).warning("Migration 跳过: %s | 原因: %s", sql.strip()[:60], e)


async def init_db() -> None:
    """初始化数据库，创建所有表并执行迁移与引导（API 与 Worker 共用入口）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_db()
    # tenant-auth 全新初始化引导（幂等）：内置 External_User_Tenant/管理员/公共库、
    # 预置权限点与 admin/user 角色、Super_Admin。API 进程与 Worker 进程都会经此，
    # 引导内部幂等并容忍并发首启。不做历史数据迁移/回填。
    from app.auth.bootstrap import run_bootstrap
    await run_bootstrap(async_session)
    # 重置被中断的任务（上次服务重启时正在处理的文档）
    await _reset_interrupted_tasks()


async def _reset_interrupted_tasks() -> None:
    """重置被中断的任务：将 processing 状态的文档改为 failed

    服务重启时，processing 状态意味着上次处理被中断，不可能自动恢复。
    """
    import logging
    _logger = logging.getLogger(__name__)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE documents SET status='failed', error_message='服务重启，处理中断' "
                    "WHERE status='processing'"
                )
            )
            if result.rowcount > 0:
                _logger.info("重置 %d 个中断的文档为 failed 状态", result.rowcount)
                print(f"[Init] 重置 {result.rowcount} 个中断的文档为 failed 状态")
    except Exception as e:
        _logger.warning("重置中断任务失败: %s", e)
