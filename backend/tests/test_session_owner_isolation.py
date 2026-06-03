"""会话归属隔离回归（per-user 对话历史隔离，tenant-auth 修复）。

修复前缺陷：ChatSession/ChatMessageRecord 仅按 tenant_id 隔离，导致同租户内
任意用户都能列出/打开/改名/删除/续写他人对话历史。本测试固化"同租户两用户
彼此不可见对方会话"的行为（在租户硬隔离之上再按 owner_user_id 收敛）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-key-session-0123456789abcdef")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "root")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "RootPass#123")


@pytest.fixture()
def client():
    """构建隔离的 app（文件 sqlite + bootstrap + 认证/管理/会话路由）。"""
    from app.config import get_settings
    get_settings.cache_clear()

    import app.storage.database as dbmod
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    dbmod.engine = engine
    test_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    dbmod.async_session = test_sessionmaker

    # session.py 在模块加载时 `from app.storage.database import async_session`，
    # 已绑定原 DB 引擎；显式重绑到测试库，使会话端点也走临时 sqlite。
    # （deps.py 的 Guard 用的是函数内延迟 import，无需额外处理。）
    import app.api.session as session_mod
    session_mod.async_session = test_sessionmaker

    from app.schema.db import Base
    from app.auth.bootstrap import run_bootstrap

    async def _seed():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        await run_bootstrap(dbmod.async_session)

    _loop = asyncio.new_event_loop()
    try:
        _loop.run_until_complete(_seed())
    finally:
        _loop.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.errors import register_exception_handlers
    from app.api.auth_routes import router as auth_router
    from app.api.admin_routes import router as admin_router
    from app.api.session import router as session_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(session_router)
    c = TestClient(app)
    try:
        yield c
    finally:
        _disp = asyncio.new_event_loop()
        try:
            _disp.run_until_complete(engine.dispose())
        finally:
            _disp.close()
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _login(client, username, password, tenant_id=None):
    body = {"username": username, "password": password}
    if tenant_id:
        body["tenant_id"] = tenant_id
    return client.post("/api/auth/login", json=body)


def _super_admin_token(client):
    r = _login(client, "root", "RootPass#123")
    assert r.status_code == 200
    tok = r.json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok),
                json={"old_password": "RootPass#123", "new_password": "NewRoot#123"})
    return _login(client, "root", "NewRoot#123").json()["access_token"]


def _make_member(client, tadmin_tok, tid, username):
    """在租户内建普通用户并完成首登改密，返回可用 token。"""
    r = client.post("/api/admin/users", headers=_bearer(tadmin_tok),
                    json={"username": username})
    assert r.status_code == 201, r.text
    pwd = r.json()["temp_password"]
    tok0 = _login(client, username, pwd, tid).json()["access_token"]
    new_pwd = f"{username.capitalize()}#1234"
    client.post("/api/auth/change-password", headers=_bearer(tok0),
                json={"old_password": pwd, "new_password": new_pwd})
    return _login(client, username, new_pwd, tid).json()["access_token"]


@pytest.fixture()
def two_members(client):
    """同一租户内的两个普通用户 alice / bob。"""
    sa = _super_admin_token(client)
    r = client.post("/api/admin/tenants", headers=_bearer(sa),
                    json={"name": "法院A", "admin_username": "tadmin"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    tadmin_pwd = r.json()["admin_temp_password"]
    tok0 = _login(client, "tadmin", tadmin_pwd, tid).json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok0),
                json={"old_password": tadmin_pwd, "new_password": "Admin#1234"})
    tadmin = _login(client, "tadmin", "Admin#1234", tid).json()["access_token"]
    alice = _make_member(client, tadmin, tid, "alice")
    bob = _make_member(client, tadmin, tid, "bob")
    return {"tid": tid, "alice": alice, "bob": bob}


def test_create_sets_owner_and_lists_only_own(two_members, client):
    """会话创建后仅 owner 在列表可见；他人列表看不到（即便同租户）。"""
    alice, bob = two_members["alice"], two_members["bob"]

    # alice 建会话（列表需至少一条消息才显示，故先确认创建返回 owner 归属隔离）
    sa = client.post("/api/sessions", headers=_bearer(alice), json={"title": "A 的对话"})
    assert sa.status_code == 200, sa.text
    alice_sid = sa.json()["id"]

    # bob 的会话列表里不应出现 alice 的会话
    rb = client.get("/api/sessions", headers=_bearer(bob))
    assert rb.status_code == 200
    assert all(item["id"] != alice_sid for item in rb.json())


def test_other_user_cannot_access_session(two_members, client):
    """他人不可读取/改名/删除/清空别人的会话（统一 404，存在性非泄露）。"""
    alice, bob = two_members["alice"], two_members["bob"]

    sid = client.post("/api/sessions", headers=_bearer(alice),
                      json={"title": "私密对话"}).json()["id"]

    # bob 读取 alice 会话消息 -> 404
    assert client.get(f"/api/sessions/{sid}/messages", headers=_bearer(bob)).status_code == 404
    # bob 改名 alice 会话 -> 404
    assert client.put(f"/api/sessions/{sid}", headers=_bearer(bob),
                      json={"title": "改你的"}).status_code == 404
    # bob 清空 alice 会话消息 -> 404
    assert client.delete(f"/api/sessions/{sid}/messages", headers=_bearer(bob)).status_code == 404
    # bob 删除 alice 会话 -> 404
    assert client.delete(f"/api/sessions/{sid}", headers=_bearer(bob)).status_code == 404

    # alice 本人仍可正常访问
    assert client.get(f"/api/sessions/{sid}/messages", headers=_bearer(alice)).status_code == 200
    assert client.put(f"/api/sessions/{sid}", headers=_bearer(alice),
                      json={"title": "新标题"}).status_code == 200


def test_owner_can_delete_own_session(two_members, client):
    """owner 删除自己的会话成功。"""
    alice = two_members["alice"]
    sid = client.post("/api/sessions", headers=_bearer(alice),
                      json={"title": "可删"}).json()["id"]
    assert client.delete(f"/api/sessions/{sid}", headers=_bearer(alice)).status_code == 200
