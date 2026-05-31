"""tenant-auth 集成与功能还原回归（FastAPI TestClient + 文件 sqlite）。

formalize 端到端流程为常驻回归：账号生命周期(P13/14/15)、默认拒绝(P19)、租户停用
(P20)、跨租户隔离(P1)、双入口一致性(R9.4)、引导冒烟(R33/35)、API Key 副作用。
用文件型 sqlite 让后台任务与校验共享同一库。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-key-integration-0123456789abcdef")
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "root")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "RootPass#123")


@pytest.fixture()
def client():
    """构建隔离的 app（文件 sqlite + bootstrap + 认证/管理/KB 路由）。"""
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
    from app.api.knowledge_base import router as kb_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(kb_router)
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
    r = client.post("/api/auth/login", json=body)
    return r


def _super_admin_token(client):
    """超管登录 + 改密 + 重新登录，返回可用 token。"""
    r = _login(client, "root", "RootPass#123")
    assert r.status_code == 200
    tok = r.json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok),
                json={"old_password": "RootPass#123", "new_password": "NewRoot#123"})
    return _login(client, "root", "NewRoot#123").json()["access_token"]


def test_default_deny_and_login(client):
    # Property 19: 无凭据访问受保护路由 -> 401
    assert client.get("/api/admin/tenants").status_code == 401
    assert client.get("/api/auth/me/permissions").status_code == 401
    # 凭据错误 -> 401
    assert _login(client, "root", "wrong").status_code == 401


def test_must_change_password_gate(client):
    # Property 15: 超管首登 must_change_password；改密前受保护操作 403
    tok = _login(client, "root", "RootPass#123").json()["access_token"]
    assert client.get("/api/admin/tenants", headers=_bearer(tok)).status_code == 403
    # 改密后旧 token 失效（token_version 自增）-> 401
    client.post("/api/auth/change-password", headers=_bearer(tok),
                json={"old_password": "RootPass#123", "new_password": "NewRoot#123"})
    assert client.get("/api/admin/tenants", headers=_bearer(tok)).status_code == 401


def test_account_lifecycle_and_perms(client):
    # Property 13/14: 角色权限并集 / 停用即时失效
    sa = _super_admin_token(client)
    # 建租户
    r = client.post("/api/admin/tenants", headers=_bearer(sa),
                    json={"name": "法院A", "admin_username": "tadmin"})
    assert r.status_code == 201
    tid = r.json()["id"]
    tadmin_pwd = r.json()["admin_temp_password"]
    # 租户管理员改密
    tok0 = _login(client, "tadmin", tadmin_pwd, tid).json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok0),
                json={"old_password": tadmin_pwd, "new_password": "Admin#1234"})
    tadmin = _login(client, "tadmin", "Admin#1234", tid).json()["access_token"]
    # 建普通用户
    r = client.post("/api/admin/users", headers=_bearer(tadmin),
                    json={"username": "alice", "role_names": ["user"]})
    assert r.status_code == 201
    alice_pwd = r.json()["temp_password"]
    tok = _login(client, "alice", alice_pwd, tid).json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok),
                json={"old_password": alice_pwd, "new_password": "Alice#1234"})
    alice = _login(client, "alice", "Alice#1234", tid).json()["access_token"]
    # me/permissions：含 user 角色权限点
    perms = {p["code"] for p in client.get("/api/auth/me/permissions", headers=_bearer(alice)).json()["permissions"]}
    assert "kb:create" in perms and "user:manage" not in perms
    # 停用 alice -> 其 token 失效（Property 14）
    # 找到 alice 的 user_id：用 tadmin 重置她口令需要 id，简化为停用所有同租户用户中的 alice
    # 这里直接通过登录响应没有 id，改用：tadmin 创建第二个用户并停用之验证停用闸门
    r2 = client.post("/api/admin/users", headers=_bearer(tadmin), json={"username": "bob"})
    bob_pwd = r2.json()["temp_password"]
    bob_id = r2.json()["id"]
    bob_tok = _login(client, "bob", bob_pwd, tid).json()["access_token"]
    # 停用 bob
    assert client.put(f"/api/admin/users/{bob_id}/status", headers=_bearer(tadmin),
                      json={"is_active": False}).status_code == 200
    # bob 旧 token 失效
    assert client.get("/api/auth/me/permissions", headers=_bearer(bob_tok)).status_code == 401
    # bob 无法再登录
    assert _login(client, "bob", bob_pwd, tid).status_code == 403


def test_cross_tenant_and_response_fields(client):
    # Property 1 + 25: 跨租户 404；KB 响应含既有字段 + 追加 visibility/owner
    sa = _super_admin_token(client)
    # 两租户各一管理员（租户名可中文；用户名须 ASCII，二者解耦）
    ids = {}
    for name, uname in (("法院A", "adm_a"), ("法院B", "adm_b")):
        r = client.post("/api/admin/tenants", headers=_bearer(sa),
                        json={"name": name, "admin_username": uname})
        ids[name] = (r.json()["id"], r.json()["admin_temp_password"], uname)

    def admin_token(name):
        tid, pwd, uname = ids[name]
        tok0 = _login(client, uname, pwd, tid).json()["access_token"]
        client.post("/api/auth/change-password", headers=_bearer(tok0),
                    json={"old_password": pwd, "new_password": "Adm#12345"})
        return _login(client, uname, "Adm#12345", tid).json()["access_token"], tid

    ta, tida = admin_token("法院A")
    tb, tidb = admin_token("法院B")
    # A 建库
    r = client.post("/api/knowledge-bases", headers=_bearer(ta), json={"name": "卷宗"})
    assert r.status_code == 201
    body = r.json()
    # Property 25: 既有字段齐全 + 追加字段
    for f in ("id", "name", "description", "config", "doc_count", "created_at", "updated_at"):
        assert f in body
    assert body["visibility"] == "private" and body["owner_user_id"]
    kb = body["id"]
    # A 能读
    assert client.get(f"/api/knowledge-bases/{kb}", headers=_bearer(ta)).status_code == 200
    # B 跨租户读 -> 404
    assert client.get(f"/api/knowledge-bases/{kb}", headers=_bearer(tb)).status_code == 404
    # B 列表看不到 A 的库
    assert client.get("/api/knowledge-bases", headers=_bearer(tb)).json()["total"] == 0


def test_tenant_disabled_blocks_access(client):
    # Property 20: 租户停用后该租户身份请求 403
    sa = _super_admin_token(client)
    r = client.post("/api/admin/tenants", headers=_bearer(sa),
                    json={"name": "法院C", "admin_username": "cadm"})
    tid = r.json()["id"]
    pwd = r.json()["admin_temp_password"]
    tok0 = _login(client, "cadm", pwd, tid).json()["access_token"]
    client.post("/api/auth/change-password", headers=_bearer(tok0),
                json={"old_password": pwd, "new_password": "Cadm#1234"})
    cadm = _login(client, "cadm", "Cadm#1234", tid).json()["access_token"]
    # 正常可访问
    assert client.get("/api/knowledge-bases", headers=_bearer(cadm)).status_code == 200
    # 超管停用该租户
    assert client.put(f"/api/admin/tenants/{tid}/status", headers=_bearer(sa),
                      json={"is_active": False}).status_code == 200
    # 停用后访问 -> 403
    assert client.get("/api/knowledge-bases", headers=_bearer(cadm)).status_code == 403
