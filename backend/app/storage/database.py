"""PostgreSQL 数据库初始化与会话管理"""

from collections.abc import AsyncGenerator

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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话（用于 FastAPI 依赖注入）"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """初始化数据库，创建所有表"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
