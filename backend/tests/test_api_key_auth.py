"""API Key 认证系统测试

覆盖 auth 工具模块、CRUD 接口和中间件。
"""

import sys
from unittest.mock import MagicMock

# Mock 重型依赖模块，避免导入 FlagEmbedding / torch 等
sys.modules.setdefault("pymilvus", MagicMock())

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.auth import generate_api_key, get_key_prefix, hash_key, verify_key
from app.main import app
from app.schema.db import ApiKey, Base
from app.storage.database import get_db


# ============================================================
# 测试用数据库配置
# ============================================================

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """每个测试前重建数据库"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    """测试用 HTTP 客户端"""
    app.dependency_overrides[get_db] = override_get_db
    # Patch 中间件使用的 async_session，使其指向测试数据库
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.api.middleware.async_session", test_session_factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()


# ============================================================
# Task 10.1: auth 工具模块测试
# ============================================================


class TestAuthUtils:
    """API Key 生成/哈希/验证逻辑测试"""

    def test_generate_api_key_format(self):
        """生成的 Key 应以 sk- 开头，总长度 51 字符"""
        key = generate_api_key()
        assert key.startswith("sk-")
        # sk- (3) + 48 hex chars = 51
        assert len(key) == 51

    def test_generate_api_key_uniqueness(self):
        """每次生成的 Key 应不同"""
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100

    def test_hash_key_deterministic(self):
        """相同 Key 的哈希值应一致"""
        key = "sk-abc123"
        assert hash_key(key) == hash_key(key)

    def test_hash_key_different_for_different_keys(self):
        """不同 Key 的哈希值应不同"""
        assert hash_key("sk-aaa") != hash_key("sk-bbb")

    def test_hash_key_is_sha256(self):
        """哈希值应为 64 位十六进制字符串（SHA256）"""
        h = hash_key("sk-test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_get_key_prefix(self):
        """前缀应为前 11 个字符 + ..."""
        key = "sk-abcdef1234567890abcdef1234567890abcdef12345678"
        prefix = get_key_prefix(key)
        assert prefix == "sk-abcdef12..."
        assert len(prefix) == 14

    @pytest.mark.asyncio
    async def test_verify_key_valid(self):
        """有效 Key 应验证通过"""
        raw_key = generate_api_key()
        # 插入测试记录
        async with test_session_factory() as session:
            api_key = ApiKey(
                id="test-id",
                key_hash=hash_key(raw_key),
                prefix=get_key_prefix(raw_key),
                name="test",
                is_active=True,
                call_count=0,
            )
            session.add(api_key)
            await session.commit()

        # 验证
        async with test_session_factory() as session:
            result = await verify_key(raw_key, session)
            assert result is not None
            assert result.id == "test-id"

    @pytest.mark.asyncio
    async def test_verify_key_invalid(self):
        """无效 Key 应返回 None"""
        async with test_session_factory() as session:
            result = await verify_key("sk-nonexistent", session)
            assert result is None

    @pytest.mark.asyncio
    async def test_verify_key_inactive(self):
        """已撤销的 Key 应返回 None"""
        raw_key = generate_api_key()
        async with test_session_factory() as session:
            api_key = ApiKey(
                id="inactive-id",
                key_hash=hash_key(raw_key),
                prefix=get_key_prefix(raw_key),
                name="inactive",
                is_active=False,
                call_count=0,
            )
            session.add(api_key)
            await session.commit()

        async with test_session_factory() as session:
            result = await verify_key(raw_key, session)
            assert result is None

    @pytest.mark.asyncio
    async def test_verify_key_increments_call_count(self):
        """验证通过后应递增 call_count"""
        raw_key = generate_api_key()
        async with test_session_factory() as session:
            api_key = ApiKey(
                id="count-id",
                key_hash=hash_key(raw_key),
                prefix=get_key_prefix(raw_key),
                name="counter",
                is_active=True,
                call_count=5,
            )
            session.add(api_key)
            await session.commit()

        # 验证一次
        async with test_session_factory() as session:
            await verify_key(raw_key, session)

        # 检查计数
        async with test_session_factory() as session:
            from sqlalchemy import select
            stmt = select(ApiKey).where(ApiKey.id == "count-id")
            result = await session.execute(stmt)
            key_record = result.scalar_one()
            assert key_record.call_count == 6
            assert key_record.last_used_at is not None


# ============================================================
# Task 10.2: CRUD 接口测试
# ============================================================


class TestApiKeyCRUD:
    """API Key CRUD 接口测试"""

    @pytest.mark.asyncio
    async def test_create_api_key(self, client: AsyncClient):
        """创建 Key 应返回完整明文"""
        resp = await client.post("/api/api-keys", json={"name": "my-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"].startswith("sk-")
        assert len(data["key"]) == 51
        assert data["name"] == "my-key"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_api_key_no_name(self, client: AsyncClient):
        """不提供名称也能创建"""
        resp = await client.post("/api/api-keys", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"].startswith("sk-")
        assert data["name"] is None

    @pytest.mark.asyncio
    async def test_list_api_keys(self, client: AsyncClient):
        """列表应返回前缀而非完整 Key"""
        # 先创建两个
        await client.post("/api/api-keys", json={"name": "key-1"})
        await client.post("/api/api-keys", json={"name": "key-2"})

        resp = await client.get("/api/api-keys")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # 列表中不应包含完整 Key
        for item in data["items"]:
            assert "key" not in item
            assert item["prefix"].endswith("...")
            assert item["is_active"] is True

    @pytest.mark.asyncio
    async def test_revoke_api_key(self, client: AsyncClient):
        """撤销 Key 应设置 is_active=False"""
        # 创建
        resp = await client.post("/api/api-keys", json={"name": "to-revoke"})
        key_id = resp.json()["id"]

        # 撤销
        resp = await client.delete(f"/api/api-keys/{key_id}")
        assert resp.status_code == 200

        # 验证状态
        resp = await client.get("/api/api-keys")
        items = resp.json()["items"]
        revoked = [i for i in items if i["id"] == key_id][0]
        assert revoked["is_active"] is False

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_key(self, client: AsyncClient):
        """撤销不存在的 Key 应返回 404"""
        resp = await client.delete("/api/api-keys/nonexistent-id")
        assert resp.status_code == 404


# ============================================================
# Task 10.3: 中间件测试
# ============================================================


class TestApiKeyMiddleware:
    """API Key 认证中间件测试"""

    @pytest.mark.asyncio
    async def test_root_no_auth_required(self, client: AsyncClient):
        """根路径不需要认证"""
        resp = await client.get("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_api_no_auth_required(self, client: AsyncClient):
        """/api/ 管理接口不需要认证"""
        resp = await client.get("/api/api-keys")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_v1_missing_auth_header(self, client: AsyncClient):
        """/v1/ 路径缺少 Authorization 头应返回 401"""
        resp = await client.post("/v1/chat/completions", json={})
        assert resp.status_code == 401
        assert "Authorization" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_v1_invalid_auth_format(self, client: AsyncClient):
        """/v1/ 路径 Authorization 格式错误应返回 401"""
        resp = await client.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_v1_invalid_key(self, client: AsyncClient):
        """/v1/ 路径无效 Key 应返回 401"""
        resp = await client.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": "Bearer sk-invalidkey000000000000000000000000000000000000000"},
        )
        assert resp.status_code == 401
        assert "无效" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_v1_valid_key_passes_auth(self, client: AsyncClient):
        """/v1/ 路径有效 Key 应通过认证（后续可能因请求体无效返回其他错误）"""
        # 先创建一个 Key
        create_resp = await client.post("/api/api-keys", json={"name": "auth-test"})
        raw_key = create_resp.json()["key"]

        # 用有效 Key 访问 /v1/ 端点
        # 请求体不完整会返回 422，但不是 401，说明认证通过了
        resp = await client.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        # 认证通过后，因为请求体缺少必填字段会返回 422
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_v1_revoked_key_rejected(self, client: AsyncClient):
        """已撤销的 Key 应被拒绝"""
        # 创建并撤销
        create_resp = await client.post("/api/api-keys", json={"name": "revoke-test"})
        data = create_resp.json()
        raw_key = data["key"]
        key_id = data["id"]

        await client.delete(f"/api/api-keys/{key_id}")

        # 用已撤销的 Key 访问
        resp = await client.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 401
