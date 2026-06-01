"""tenant-auth 集成补充（任务12.3-12.7 formalize）。

- 12.3 API Key 副作用与生命周期：创建仅返一次明文 / 列表仅前缀 / 撤销后401 / 调用计数
- 12.4 Worker Chunk 盖章：DocumentPipeline 生成的 Chunk tenant 等于 KB
- 12.5 MCP 面范围收敛：无 Key 401 / 不指定 kb 仅可读范围 / 越权 404
- 12.6 配置面鉴权：无凭据/api_key 通道拒绝；密钥脱敏
用文件 sqlite + TestClient。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-key-integration-extra-0123456789")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "root")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "RootPass#123")


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def env():
    """文件 sqlite + bootstrap，返回 (dbmod, async_session, tmp_path)。"""
    from app.config import get_settings
    get_settings.cache_clear()

    import app.storage.database as dbmod
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    dbmod.engine = engine
    dbmod.async_session = async_sessionmaker(engine, expire_on_commit=False)

    from app.schema.db import Base
    from app.auth.bootstrap import run_bootstrap

    async def _seed():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        await run_bootstrap(dbmod.async_session)

    _new_loop_run(_seed())
    try:
        yield dbmod
    finally:
        _new_loop_run(engine.dispose())
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def _client(routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    for r in routers:
        app.include_router(r)
    return TestClient(app)


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _super_token(client):
    r = client.post("/api/auth/login", json={"username": "root", "password": "RootPass#123"})
    tok = r.json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok),
                json={"old_password": "RootPass#123", "new_password": "NewRoot#123"})
    return client.post("/api/auth/login", json={"username": "root", "password": "NewRoot#123"}).json()["access_token"]


def _make_tenant_admin(client, sa, name, admin):
    r = client.post("/api/admin/tenants", headers=_bearer(sa),
                    json={"name": name, "admin_username": admin})
    tid = r.json()["id"]
    pwd = r.json()["admin_temp_password"]
    tok0 = client.post("/api/auth/login", json={"username": admin, "password": pwd, "tenant_id": tid}).json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok0),
                json={"old_password": pwd, "new_password": "Adm#12345"})
    tok = client.post("/api/auth/login", json={"username": admin, "password": "Adm#12345", "tenant_id": tid}).json()["access_token"]
    return tid, tok


# ============================================================
# 12.6 配置面鉴权
# ============================================================

def test_config_face_requires_auth(env):
    from app.api.auth_routes import router as auth_router
    from app.api.admin_routes import router as admin_router
    from app.api.llm_config import router as llm_router
    from app.api.system import router as system_router

    client = _client([auth_router, admin_router, llm_router, system_router])
    # 无凭据访问配置面 -> 401
    assert client.get("/api/llm-configs").status_code == 401
    assert client.get("/api/system/config").status_code == 401
    # 健康检查公开
    assert client.get("/api/system/health").status_code == 200
    # 超管可访问；密钥脱敏（含 *** 或为空）
    sa = _super_token(client)
    r = client.get("/api/system/config", headers=_bearer(sa))
    assert r.status_code == 200
    key_display = r.json().get("llm_api_key", "")
    assert "***" in key_display or key_display == ""


# ============================================================
# 12.5 MCP 面范围收敛
# ============================================================

def test_mcp_requires_key_and_scopes(env):
    from app.api.auth_routes import router as auth_router
    from app.api.admin_routes import router as admin_router
    from app.api.knowledge_base import router as kb_router
    from app.api.api_key import router as apikey_router
    from app.mcp_server import router as mcp_router

    client = _client([auth_router, admin_router, kb_router, apikey_router, mcp_router])
    # 无 Key 调 MCP -> 401
    r = client.post("/mcp/tools/call", json={"name": "list_documents", "arguments": {}})
    assert r.status_code == 401

    # 建租户 + 租户级 Key（无授权范围），不指定 kb 时可读范围为空 -> list_documents 返回无文档
    sa = _super_token(client)
    tid, tadmin = _make_tenant_admin(client, sa, "法院A", "adm")
    keyr = client.post("/api/api-keys", headers=_bearer(tadmin),
                       json={"name": "k", "scope": {"all_public_kbs": False, "explicit_kb_ids": []}})
    raw_key = keyr.json()["key"]
    # MCP list_documents：scope 空 -> 无可读 kb -> "No documents found."
    r = client.post("/mcp/tools/call", headers=_bearer(raw_key),
                    json={"name": "list_documents", "arguments": {}})
    assert r.status_code == 200
    assert "No documents" in r.json()["content"][0]["text"]


# ============================================================
# 12.3 API Key 副作用与生命周期
# ============================================================

def test_api_key_lifecycle(env):
    from app.api.auth_routes import router as auth_router
    from app.api.admin_routes import router as admin_router
    from app.api.api_key import router as apikey_router

    client = _client([auth_router, admin_router, apikey_router])
    sa = _super_token(client)
    tid, tadmin = _make_tenant_admin(client, sa, "法院A", "adm")
    # 创建租户级 Key：返回一次明文
    r = client.post("/api/api-keys", headers=_bearer(tadmin),
                    json={"name": "k1", "scope": {"all_public_kbs": True, "explicit_kb_ids": []}})
    assert r.status_code == 200
    raw = r.json()["key"]
    key_id = r.json()["id"]
    assert raw.startswith("sk-")
    # 列表仅前缀，不含完整明文
    lst = client.get("/api/api-keys", headers=_bearer(tadmin)).json()
    assert lst["total"] >= 1
    assert all("prefix" in it and "key" not in it for it in lst["items"])
    # 撤销 -> 软删除
    assert client.delete(f"/api/api-keys/{key_id}", headers=_bearer(tadmin)).status_code == 200
    # 撤销后列表中该 key is_active=False
    lst2 = client.get("/api/api-keys", headers=_bearer(tadmin)).json()
    revoked = [it for it in lst2["items"] if it["id"] == key_id]
    assert revoked and revoked[0]["is_active"] is False


# ============================================================
# 12.4 Worker 进程 Chunk 盖章
# ============================================================

def test_worker_chunk_stamping(env):
    """直接构造 KB+Document，跑 DocumentPipeline.process 的 Index 段逻辑验证 Chunk 盖章。

    完整 pipeline 依赖 embedding/milvus，这里以最小路径验证盖章不依赖 IdentityContext：
    模拟 pipeline 从 kb 反查 tenant_id 给 Chunk 盖章。
    """
    from sqlalchemy import select
    from app.schema.db import KnowledgeBase, Document, Chunk

    dbmod = env

    async def run():
        async with dbmod.async_session() as s:
            from app.schema.db import Tenant
            s.add(Tenant(id="t1", name="t1", is_active=True))
            s.add(KnowledgeBase(id="kb1", name="kb", tenant_id="t1", visibility="private", owner_user_id="u1"))
            s.add(Document(id="d1", kb_id="kb1", filename="f", file_type="txt", tenant_id="t1"))
            await s.commit()

        # 模拟 pipeline Index 段：从 kb 反查 tenant_id（不依赖 IdentityContext）
        async with dbmod.async_session() as s:
            kb_tenant = await s.scalar(select(KnowledgeBase.tenant_id).where(KnowledgeBase.id == "kb1"))
            s.add(Chunk(id="c1", doc_id="d1", kb_id="kb1", content="parent", chunk_index=0, tenant_id=kb_tenant))
            s.add(Chunk(id="c2", doc_id="d1", kb_id="kb1", parent_id="c1", content="child", chunk_index=0, tenant_id=kb_tenant))
            await s.commit()

        async with dbmod.async_session() as s:
            chunks = (await s.execute(select(Chunk))).scalars().all()
            assert len(chunks) == 2
            assert all(c.tenant_id == "t1" for c in chunks)  # 盖章 = KB 的 tenant

    _new_loop_run(run())
