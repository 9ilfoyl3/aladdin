"""测试数据库 ORM 模型与初始化"""

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.schema.db import Base, KnowledgeBase, Document, Chunk, ApiKey


@pytest_asyncio.fixture
async def db_session():
    """创建内存数据库会话用于测试"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_tables_created(db_session: AsyncSession):
    """验证所有表正确创建"""
    result = await db_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )
    tables = [r[0] for r in result.fetchall()]
    assert "knowledge_bases" in tables
    assert "documents" in tables
    assert "chunks" in tables
    assert "api_keys" in tables


@pytest.mark.asyncio
async def test_knowledge_base_crud(db_session: AsyncSession):
    """测试知识库 CRUD"""
    kb = KnowledgeBase(id="kb_001", name="测试知识库", description="用于测试")
    db_session.add(kb)
    await db_session.commit()

    stmt = select(KnowledgeBase).where(KnowledgeBase.id == "kb_001")
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.name == "测试知识库"
    assert fetched.doc_count == 0


@pytest.mark.asyncio
async def test_document_foreign_key(db_session: AsyncSession):
    """测试文档外键关联"""
    kb = KnowledgeBase(id="kb_002", name="KB2")
    db_session.add(kb)
    await db_session.flush()

    doc = Document(id="doc_001", kb_id="kb_002", filename="test.pdf", file_type="pdf", file_size=1024)
    db_session.add(doc)
    await db_session.commit()

    stmt = select(Document).where(Document.kb_id == "kb_002")
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.filename == "test.pdf"
    assert fetched.status == "pending"
    assert fetched.chunk_count == 0


@pytest.mark.asyncio
async def test_chunk_relationships(db_session: AsyncSession):
    """测试 Chunk 关联关系"""
    kb = KnowledgeBase(id="kb_003", name="KB3")
    doc = Document(id="doc_002", kb_id="kb_003", filename="doc.md", file_type="md")
    chunk = Chunk(
        id="chk_001", doc_id="doc_002", kb_id="kb_003",
        content="这是一段测试内容", chunk_index=0
    )
    db_session.add_all([kb, doc, chunk])
    await db_session.commit()

    stmt = select(Chunk).where(Chunk.doc_id == "doc_002")
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.content == "这是一段测试内容"
    assert fetched.chunk_index == 0
    assert fetched.parent_id is None


@pytest.mark.asyncio
async def test_api_key_model(db_session: AsyncSession):
    """测试 API Key 模型"""
    key = ApiKey(
        id="key_001", key_hash="abc123hash", prefix="sk-abcd",
        name="测试密钥", is_active=True
    )
    db_session.add(key)
    await db_session.commit()

    stmt = select(ApiKey).where(ApiKey.prefix == "sk-abcd")
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.is_active is True
    assert fetched.call_count == 0
    assert fetched.last_used_at is None
