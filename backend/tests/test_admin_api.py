"""Admin API 接口测试

测试知识库 CRUD、文档管理、检索测试、系统配置接口。
使用 httpx AsyncClient + 内存 SQLite 数据库。
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Mock 重型依赖模块，避免导入 FlagEmbedding / torch 等
sys.modules.setdefault("pymilvus", MagicMock())

# get_settings() 启动期 fail-fast 需要 JWT_SECRET（清理 E 后鉴权始终强制）
os.environ.setdefault("JWT_SECRET", "admin-api-test-secret-0123456789abcdef")

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
    import app.api.deps as deps
    import app.storage.database as dbmod
    from app.auth.identity import IdentityContext, IdentitySourceEnum, OperationLevelEnum
    from app.auth.constants import TenantRoleEnum

    # 把全局 async_session 指向测试用内存 sqlite：KB/文档路由用 get_db_session、守卫与
    # kb_scope 直接用 app.storage.database.async_session，均须落到测试库（否则连真实 PG 失败）。
    _orig_async_session = dbmod.async_session
    dbmod.async_session = _test_session_factory

    # 进程隔离地注入一个固定的租户管理员身份：本测试聚焦知识库/文档/系统配置的**功能正确性**，
    # 与鉴权无关（清理 E 后已无 auth_enabled 旁路），故 monkeypatch _resolve_identity 让所有
    # 守卫放行；身份带 tenant_id 以便建库盖章。鉴权本身正确性由 tenant-auth 套件覆盖。
    _orig_resolve = deps._resolve_identity

    async def _fake_resolve(request, session):
        return (
            IdentityContext(
                source=IdentitySourceEnum.JWT,
                op_level=OperationLevelEnum.TENANT,
                tenant_id="t-test",
                user_id="u-test",
                username="tester",
                is_super_admin=False,
                role=TenantRoleEnum.ADMIN,
            ),
            False,
        )

    deps._resolve_identity = _fake_resolve
    app.dependency_overrides[get_db] = _override_get_db
    deps.get_db_session = _override_get_db
    app.dependency_overrides[deps.get_db_session] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # 清理
    deps._resolve_identity = _orig_resolve
    dbmod.async_session = _orig_async_session
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
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "测试知识库"
        assert data["description"] == "用于测试"
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
        assert len(data["items"]) == 2

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
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新名称"

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
        assert len(resp.json()["items"]) == 2

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
        """更新系统配置（更新 LLM 模型与分块参数，返回脱敏配置）"""
        resp = await client.put("/api/system/config", json={
            "llm_model": "qwen2.5:14b",
            "parent_chunk_size": 3000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_model"] == "qwen2.5:14b"
        assert data["parent_chunk_size"] == 3000

    @pytest.mark.asyncio
    async def test_queue_stats_no_redis(self, client):
        """队列统计 - Redis 不可用时返回全零"""
        from app.main import app
        # 确保 task_queue 为 None（模拟 Redis 不可用）
        app.state.task_queue = None

        resp = await client.get("/api/system/queue-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_length"] == 0
        assert data["pending_count"] == 0
        assert data["active_workers"] == 0
        assert data["dlq_length"] == 0

    @pytest.mark.asyncio
    async def test_queue_stats_with_redis(self, client):
        """队列统计 - Redis 可用时返回实际统计"""
        from app.main import app
        from app.pipeline.queue import QueueStats

        # Mock task_queue
        mock_queue = AsyncMock()
        mock_queue.get_stats.return_value = QueueStats(
            stream_length=5,
            pending_count=2,
            active_workers=1,
            dlq_length=3,
        )
        app.state.task_queue = mock_queue

        resp = await client.get("/api/system/queue-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_length"] == 5
        assert data["pending_count"] == 2
        assert data["active_workers"] == 1
        assert data["dlq_length"] == 3

        mock_queue.get_stats.assert_called_once()
