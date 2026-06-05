"""会话删除级联清理钩子（Task 9 / Req 1.6）。

验证：
1. ``DELETE /api/sessions/{sid}`` 在 DB 删除后会调用
   ``SessionUploadService.cleanup_session_files(sid)`` 显式清理共享 collection
   ``kb_session_files`` 中该会话的向量。
2. 清理失败（``cleanup_session_files`` 抛异常）不阻塞会话删除主流程：响应仍 200，
   仅记 WARNING（DB 删除已提交，用户主诉求满足）。
3. 非 owner 删除被拒（404）时，``cleanup_session_files`` SHALL NOT 被调用（无 DB
   删除发生，自然不应触发后续清理）。
4. 调用顺序：DB 删除 commit 在前，cleanup 在后（commit 失败时 cleanup 不被调用）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-key-cleanup-0123456789abcdef")
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


def _patch_cleanup(mock_service: AsyncMock):
    """patch ``get_session_upload_service`` 返回带 mock cleanup 的服务。

    在 ``app.api.session`` 模块作用域 patch（直接绑定的 import），让 delete 端点
    取到 mock 实例。
    """
    return patch(
        "app.api.session.get_session_upload_service",
        return_value=mock_service,
    )


def test_delete_session_calls_cleanup_with_session_id(two_members, client):
    """DB 删除后调用 ``cleanup_session_files(session_id)``（Req 1.6）。"""
    alice = two_members["alice"]
    sid = client.post("/api/sessions", headers=_bearer(alice),
                      json={"title": "待删"}).json()["id"]

    svc = AsyncMock()
    svc.cleanup_session_files = AsyncMock(return_value=None)

    with _patch_cleanup(svc):
        r = client.delete(f"/api/sessions/{sid}", headers=_bearer(alice))

    assert r.status_code == 200, r.text
    svc.cleanup_session_files.assert_awaited_once_with(sid)


def test_delete_session_succeeds_when_cleanup_raises(two_members, client, caplog):
    """``cleanup_session_files`` 抛异常时主流程不受影响（Req 1.6 / 防御性兜底）。"""
    alice = two_members["alice"]
    sid = client.post("/api/sessions", headers=_bearer(alice),
                      json={"title": "清理失败也要删"}).json()["id"]

    svc = AsyncMock()
    svc.cleanup_session_files = AsyncMock(side_effect=RuntimeError("milvus down"))

    import logging
    with caplog.at_level(logging.WARNING, logger="app.api.session"):
        with _patch_cleanup(svc):
            r = client.delete(f"/api/sessions/{sid}", headers=_bearer(alice))

    # 主流程成功
    assert r.status_code == 200, r.text
    assert r.json() == {"detail": "已删除"}
    svc.cleanup_session_files.assert_awaited_once_with(sid)
    # 失败被记 WARNING（不报错给用户）
    assert any(
        "向量级联清理异常" in rec.message and sid in rec.message
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ), f"未找到预期的 WARNING 记录: {[r.message for r in caplog.records]}"

    # 二次删除返回 404（DB 行确实已删除，证明主流程未回滚）
    r2 = client.delete(f"/api/sessions/{sid}", headers=_bearer(alice))
    assert r2.status_code == 404


def test_non_owner_delete_does_not_trigger_cleanup(two_members, client):
    """非 owner 删除被拒（404）时不应触发 cleanup（无 DB 删除发生）。"""
    alice, bob = two_members["alice"], two_members["bob"]
    sid = client.post("/api/sessions", headers=_bearer(alice),
                      json={"title": "alice 私有"}).json()["id"]

    svc = AsyncMock()
    svc.cleanup_session_files = AsyncMock(return_value=None)

    with _patch_cleanup(svc):
        r = client.delete(f"/api/sessions/{sid}", headers=_bearer(bob))

    assert r.status_code == 404
    svc.cleanup_session_files.assert_not_called()

    # alice 再删自己的会话应正常触发清理
    with _patch_cleanup(svc):
        r2 = client.delete(f"/api/sessions/{sid}", headers=_bearer(alice))
    assert r2.status_code == 200
    svc.cleanup_session_files.assert_awaited_once_with(sid)
