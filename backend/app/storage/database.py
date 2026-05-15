"""SQLite 数据库初始化与会话管理"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.schema.db import Base

# 将 sqlite:/// 转换为 sqlite+aiosqlite:/// 以支持异步
_settings = get_settings()
_database_url = _settings.database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

# 异步引擎
engine = create_async_engine(_database_url, echo=False)

# 异步会话工厂
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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
    """执行简单的增量迁移（为已有表添加新列）"""
    async with engine.begin() as conn:
        # llm_configs 表添加 stream_enabled 列
        try:
            await conn.execute(
                text("ALTER TABLE llm_configs ADD COLUMN stream_enabled BOOLEAN DEFAULT 1")
            )
        except Exception:
            pass  # 列已存在，忽略
        # llm_configs 表添加 max_context_tokens 列
        try:
            await conn.execute(
                text("ALTER TABLE llm_configs ADD COLUMN max_context_tokens INTEGER")
            )
        except Exception:
            pass  # 列已存在，忽略


async def init_db() -> None:
    """初始化数据库，创建所有表并执行迁移"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_db()
