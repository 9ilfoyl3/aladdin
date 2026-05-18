"""Admin API 接口测试

测试知识库 CRUD、文档管理、检索测试、系统配置接口。
使用 httpx AsyncClient + 内存 SQLite 数据库。
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Mock 重型依赖模块，避免导入 FlagEmbedding / torch 等
sys.modules.setdefault("FlagEmbedding", MagicMock())
sys.modules.setdefault("torch", MagicMock())
sys.modules.setdefault("sentence_transformers", MagicMock())
sys.modules.setdefault("pymilvus", MagicMock())

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.schema.db import Base


# 使用内存数据库进行测试
_test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_test_session_factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    """测试用数据库会话"""
    async with _test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture
async def client():
    """创建测试客户端"""
    # 创建表
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 覆盖依赖
    from app.main import app
    from app.storage.database import get_db
    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # 清理
    app.dependency_overrides.clear()
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ============================================================
# Task 9.1: 知识库 CRUD 测试
# ============================================================


class TestKnowledgeBaseCRUD:
    """知识库 CRUD 接口测试"""

    @pytest.mark.asyncio
    async def test_create_knowledge_base(self, client):
        """创建知识库"""
        resp = await client.post("/api/knowledge-bases", json={
            "name": "测试知识库",
            "description": "用于测试",
            "retrieval_mode": "hybrid",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "测试知识库"
        assert data["description"] == "用于测试"
        assert data["retrieval_mode"] == "hybrid"
        assert data["doc_count"] == 0
        assert "id" in data

    @pytest.mark.asyncio
    async def test_list_knowledge_bases(self, client):
        """获取知识库列表"""
        # 先创建两个
        await client.post("/api/knowledge-bases", json={"name": "KB1"})
        await client.post("/api/knowledge-bases", json={"name": "KB2"})

        resp = await client.get("/api/knowledge-bases")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_get_knowledge_base(self, client):
        """获取知识库详情"""
        create_resp = await client.post("/api/knowledge-bases", json={"name": "详情测试"})
        kb_id = create_resp.json()["id"]

        resp = await client.get(f"/api/knowledge-bases/{kb_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "详情测试"

    @pytest.mark.asyncio
    async def test_get_knowledge_base_not_found(self, client):
        """获取不存在的知识库"""
        resp = await client.get("/api/knowledge-bases/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_knowledge_base(self, client):
        """更新知识库"""
        create_resp = await client.post("/api/knowledge-bases", json={"name": "原名称"})
        kb_id = create_resp.json()["id"]

        resp = await client.put(f"/api/knowledge-bases/{kb_id}", json={
            "name": "新名称",
            "retrieval_mode": "agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新名称"
        assert data["retrieval_mode"] == "agent"

    @pytest.mark.asyncio
    async def test_delete_knowledge_base(self, client):
        """删除知识库"""
        create_resp = await client.post("/api/knowledge-bases", json={"name": "待删除"})
        kb_id = create_resp.json()["id"]

        # Mock Milvus drop_collection
        with patch("app.api.knowledge_base._get_milvus") as mock_milvus:
            mock_client = AsyncMock()
            mock_milvus.return_value = mock_client

            resp = await client.delete(f"/api/knowledge-bases/{kb_id}")
            assert resp.status_code == 204

        # 确认已删除
        resp = await client.get(f"/api/knowledge-bases/{kb_id}")
        assert resp.status_code == 404


# ============================================================
# Task 9.2: 文档管理测试
# ============================================================


class TestDocumentManagement:
    """文档管理接口测试"""

    @pytest_asyncio.fixture
    async def kb_id(self, client):
        """创建一个知识库用于文档测试"""
        resp = await client.post("/api/knowledge-bases", json={"name": "文档测试库"})
        return resp.json()["id"]

    @pytest.mark.asyncio
    async def test_upload_document(self, client, kb_id):
        """上传文档"""
        # 创建临时文件
        content = b"This is a test document content."

        with patch("app.api.document._run_pipeline", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/knowledge-bases/{kb_id}/documents/upload",
                files={"file": ("test.txt", content, "text/plain")},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["filename"] == "test.txt"
        assert data["file_type"] == "txt"
        assert data["status"] == "pending"
        assert data["kb_id"] == kb_id

    @pytest.mark.asyncio
    async def test_upload_unsupported_type(self, client, kb_id):
        """上传不支持的文件类型"""
        resp = await client.post(
            f"/api/knowledge-bases/{kb_id}/documents/upload",
            files={"file": ("test.exe", b"binary", "application/octet-stream")},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_documents(self, client, kb_id):
        """获取文档列表"""
        with patch("app.api.document._run_pipeline", new_callable=AsyncMock):
            await client.post(
                f"/api/knowledge-bases/{kb_id}/documents/upload",
                files={"file": ("doc1.txt", b"content1", "text/plain")},
            )
            await client.post(
                f"/api/knowledge-bases/{kb_id}/documents/upload",
                files={"file": ("doc2.txt", b"content2", "text/plain")},
            )

        resp = await client.get(f"/api/knowledge-bases/{kb_id}/documents")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_get_document(self, client, kb_id):
        """获取文档详情"""
        with patch("app.api.document._run_pipeline", new_callable=AsyncMock):
            upload_resp = await client.post(
                f"/api/knowledge-bases/{kb_id}/documents/upload",
                files={"file": ("detail.txt", b"content", "text/plain")},
            )
        doc_id = upload_resp.json()["id"]

        resp = await client.get(f"/api/documents/{doc_id}")
        assert resp.status_code == 200
        assert resp.json()["filename"] == "detail.txt"

    @pytest.mark.asyncio
    async def test_delete_document(self, client, kb_id):
        """删除文档"""
        with patch("app.api.document._run_pipeline", new_callable=AsyncMock):
            upload_resp = await client.post(
                f"/api/knowledge-bases/{kb_id}/documents/upload",
                files={"file": ("delete_me.txt", b"content", "text/plain")},
            )
        doc_id = upload_resp.json()["id"]

        with patch("app.api.document._get_milvus") as mock_milvus:
            mock_client = AsyncMock()
            mock_milvus.return_value = mock_client

            resp = await client.delete(f"/api/documents/{doc_id}")
            assert resp.status_code == 204

        # 确认已删除
        resp = await client.get(f"/api/documents/{doc_id}")
        assert resp.status_code == 404


# ============================================================
# Task 9.4: 系统配置测试
# ============================================================


class TestSystemAPI:
    """系统配置接口测试"""

    @pytest.mark.asyncio
    async def test_health_check(self, client):
        """健康检查"""
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "services" in data
        assert "milvus" in data["services"]
        assert "llm" in data["services"]

    @pytest.mark.asyncio
    async def test_get_config(self, client):
        """获取系统配置"""
        resp = await client.get("/api/system/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_provider" in data
        assert "llm_model" in data
        assert "embed_model" in data

    @pytest.mark.asyncio
    async def test_update_config(self, client):
        """更新系统配置"""
        resp = await client.put("/api/system/config", json={
            "agent_max_iterations": 5,
            "agent_timeout": 15.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_max_iterations"] == 5
        assert data["agent_timeout"] == 15.0
