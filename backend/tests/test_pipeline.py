"""DocumentPipeline 单元测试

使用 mock 替代 Milvus 和 Embedding 模型，验证管道编排逻辑。
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Mock 掉需要 GPU 的模块，避免导入失败
sys.modules.setdefault("FlagEmbedding", MagicMock())
sys.modules.setdefault("pymilvus", MagicMock())
sys.modules.setdefault("pymilvus.connections", MagicMock())
sys.modules.setdefault("pymilvus.utility", MagicMock())

from app.pipeline.embedder import EmbedResult
from app.pipeline.pipeline import DocumentPipeline
from app.schema.db import Base, Chunk, Document, KnowledgeBase


@pytest_asyncio.fixture
async def db_engine():
    """内存 SQLite 引擎"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    """会话工厂"""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def seed_data(db_session_factory):
    """预置知识库和文档记录"""
    kb_id = "test-kb-001"
    doc_id = "test-doc-001"
    async with db_session_factory() as session:
        kb = KnowledgeBase(id=kb_id, name="测试知识库")
        session.add(kb)
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            filename="test.txt",
            file_type="txt",
            status="pending",
        )
        session.add(doc)
        await session.commit()
    return kb_id, doc_id


@pytest.fixture
def mock_model_manager():
    """Mock ModelManager"""
    manager = MagicMock()
    manager.llm = MagicMock()
    # embedder 需要是 EmbedProvider 接口
    manager.embedder = AsyncMock()
    return manager


@pytest.fixture
def mock_milvus():
    """Mock MilvusClient"""
    client = AsyncMock()
    client.has_collection = AsyncMock(return_value=True)
    client.insert = AsyncMock(return_value=1)
    return client


@pytest.mark.asyncio
async def test_pipeline_process_success(
    db_session_factory, seed_data, mock_model_manager, mock_milvus, tmp_path
):
    """测试正常文档处理流程"""
    kb_id, doc_id = seed_data

    # 创建临时测试文件
    test_file = tmp_path / "test.txt"
    test_file.write_text("这是一段测试文本。用于验证管道处理流程。" * 20, encoding="utf-8")

    # Mock embed 返回（动态匹配输入数量）
    mock_model_manager.embedder.embed = AsyncMock(
        side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
    )
    mock_model_manager.embedder.embed_sparse = AsyncMock(
        side_effect=lambda texts: [{1: 0.5, 2: 0.3} for _ in texts]
    )

    pipeline = DocumentPipeline(mock_model_manager, mock_milvus, db_session_factory)

    await pipeline.process(str(test_file), doc_id, kb_id)

    # 验证文档状态更新为 completed
    async with db_session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one()
        assert doc.status == "completed"
        assert doc.chunk_count > 0

        # 验证 chunk 已写入
        chunk_result = await session.execute(select(Chunk).where(Chunk.doc_id == doc_id))
        chunks = chunk_result.scalars().all()
        assert len(chunks) > 0

    # 验证 Milvus insert 被调用
    mock_milvus.insert.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_process_failure(
    db_session_factory, seed_data, mock_model_manager, mock_milvus, tmp_path
):
    """测试文档处理失败时状态更新为 failed"""
    kb_id, doc_id = seed_data

    # 使用不存在的文件触发异常
    pipeline = DocumentPipeline(mock_model_manager, mock_milvus, db_session_factory)

    with pytest.raises(Exception):
        await pipeline.process("/nonexistent/file.txt", doc_id, kb_id)

    # 验证文档状态更新为 failed
    async with db_session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one()
        assert doc.status == "failed"
        assert doc.error_message is not None


@pytest.mark.asyncio
async def test_pipeline_creates_parent_and_child_chunks(
    db_session_factory, seed_data, mock_model_manager, mock_milvus, tmp_path
):
    """测试管道正确创建父子 chunk 关系"""
    kb_id, doc_id = seed_data

    # 创建足够长的文本以产生多个 chunk
    test_file = tmp_path / "long.txt"
    content = "这是第一段内容。\n\n" * 50 + "这是第二段内容。\n\n" * 50
    test_file.write_text(content, encoding="utf-8")

    # Mock embed 返回（动态匹配输入数量）
    mock_model_manager.embedder.embed = AsyncMock(
        side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
    )
    mock_model_manager.embedder.embed_sparse = AsyncMock(
        side_effect=lambda texts: [{1: 0.5} for _ in texts]
    )

    pipeline = DocumentPipeline(mock_model_manager, mock_milvus, db_session_factory)
    await pipeline.process(str(test_file), doc_id, kb_id)

    # 验证父子关系
    async with db_session_factory() as session:
        from sqlalchemy import select

        # 父 chunk（parent_id 为 None）
        parent_result = await session.execute(
            select(Chunk).where(Chunk.doc_id == doc_id, Chunk.parent_id.is_(None))
        )
        parents = parent_result.scalars().all()
        assert len(parents) > 0

        # 子 chunk（parent_id 不为 None）
        child_result = await session.execute(
            select(Chunk).where(Chunk.doc_id == doc_id, Chunk.parent_id.isnot(None))
        )
        children = child_result.scalars().all()
        assert len(children) > 0

        # 子 chunk 的 parent_id 应指向已有的父 chunk
        parent_ids = {p.id for p in parents}
        for child in children:
            assert child.parent_id in parent_ids
