"""tenant-rbac-refactor 库重建 + bootstrap 登记验证（任务 20.1）。

经 ``create_all`` 重建（内存 sqlite）后断言 ORM schema 已完成破坏性重构：
- 四张角色/权限关联表（roles/role_permissions/user_roles/permissions）已删除；
- knowledge_base_grants 不含 source_tenant_id（清理 C）；
- embed_configs 不含 local_provider / device（清理 A）；
- users 含固定角色列 role 及 is_super_admin / must_change_password。

并驱动 run_bootstrap 断言初始化登记完整且幂等：
- 恰一个 Super_Admin（role=None / must_change_password / tenant_id=None / 用户名取自环境变量）；
- 内置 External_User_Tenant（tenant_type=external）+ 内置管理员（role=admin）+ 内置公共库（organization）；
- 重复执行 run_bootstrap 不重复创建（幂等）；
- 缺 Super_Admin 环境变量时 run_bootstrap fail-fast（RuntimeError）。

沿用既有 tenant-auth DB 测试风格：内存 sqlite + asyncio.run 包裹同步测试。
环境变量在导入 app 模块前以 setdefault 置好（JWT fail-fast 与 Super_Admin 引导依赖）。

Requirements: 6.1, 7.3, 9.3, 10.1, 11.3, 12.1, 15.3
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("JWT_SECRET", "schema-test-0123456789abcdef")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "root")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "RootPass#123")

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.bootstrap import run_bootstrap
from app.auth.constants import EXTERNAL_USER_TENANT_ID
from app.schema.db import Base, KnowledgeBase, Tenant, User

# bootstrap 内置外部管理员的固定标识（与 bootstrap._EXTERNAL_ADMIN_ID 对齐）
_EXTERNAL_ADMIN_ID = "user-external-builtin-admin"


def _new_engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


async def _create_all(engine) -> None:
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)


# ============================================================
# Schema 断言（破坏性重构后的表/列形态）
# ============================================================


def test_schema_dropped_role_permission_tables():
    """四张角色/权限关联表已从 ORM 元数据移除（R6.1 / R12.1）。"""

    async def run():
        engine = _new_engine()
        await _create_all(engine)
        tables = Base.metadata.tables
        for name in ("roles", "role_permissions", "user_roles", "permissions"):
            assert name not in tables, f"表 {name} 应已删除，但仍存在于 metadata"
        await engine.dispose()

    asyncio.run(run())


def test_schema_grant_has_no_source_tenant_id():
    """knowledge_base_grants 存在且不含 source_tenant_id（清理 C / R15.3）。"""

    async def run():
        engine = _new_engine()
        await _create_all(engine)
        tables = Base.metadata.tables
        assert "knowledge_base_grants" in tables
        cols = set(tables["knowledge_base_grants"].columns.keys())
        assert "source_tenant_id" not in cols
        # 收敛后仍保留的核心列
        assert {"kb_id", "grantee_type", "grantee_id", "permission"} <= cols
        await engine.dispose()

    asyncio.run(run())


def test_schema_embed_config_dropped_dead_columns():
    """embed_configs 存在且不含 local_provider / device 死列（清理 A / R9.3）。"""

    async def run():
        engine = _new_engine()
        await _create_all(engine)
        tables = Base.metadata.tables
        assert "embed_configs" in tables
        cols = set(tables["embed_configs"].columns.keys())
        assert "local_provider" not in cols
        assert "device" not in cols
        await engine.dispose()

    asyncio.run(run())


def test_schema_users_has_role_and_flags():
    """users 含固定角色列 role 及 is_super_admin / must_change_password。"""

    async def run():
        engine = _new_engine()
        await _create_all(engine)
        tables = Base.metadata.tables
        assert "users" in tables
        cols = set(tables["users"].columns.keys())
        assert "role" in cols
        assert "is_super_admin" in cols
        assert "must_change_password" in cols
        await engine.dispose()

    asyncio.run(run())


def test_schema_kb_has_org_permission_and_audit_has_actor_role():
    """kb-sharing-refinement 新增列：knowledge_bases.org_permission、audit_logs.actor_role。

    Requirements: R3.1, R7.1
    """

    async def run():
        engine = _new_engine()
        await _create_all(engine)
        tables = Base.metadata.tables
        # 组织公共库开放维度列
        assert "knowledge_bases" in tables
        kb_cols = set(tables["knowledge_bases"].columns.keys())
        assert "org_permission" in kb_cols
        # 审计角色快照列
        assert "audit_logs" in tables
        audit_cols = set(tables["audit_logs"].columns.keys())
        assert "actor_role" in audit_cols
        await engine.dispose()

    asyncio.run(run())


def test_schema_inspect_live_connection():
    """以 sqlalchemy.inspect 在实连接上交叉验证表存在性（与 metadata 一致）。"""

    async def run():
        from sqlalchemy import inspect as sa_inspect

        engine = _new_engine()
        await _create_all(engine)
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: sa_inspect(c).get_table_names())
        names = set(names)
        # 破坏性重构后这些表不应被 create_all 建出
        for dropped in ("roles", "role_permissions", "user_roles", "permissions"):
            assert dropped not in names
        # 仍应存在的核心表
        for kept in ("users", "knowledge_base_grants", "embed_configs", "tenants"):
            assert kept in names
        await engine.dispose()

    asyncio.run(run())


# ============================================================
# Bootstrap 登记断言
# ============================================================


def test_bootstrap_registers_super_admin_and_external_tenant():
    """run_bootstrap 登记完整：Super_Admin + External_User_Tenant + 内置管理员 + 公共库。

    Requirements: 7.3, 11.3, 12.1
    """

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        await _create_all(engine)
        await run_bootstrap(sm)

        async with sm() as s:
            # 恰一个 Super_Admin
            super_count = await s.scalar(
                select(func.count(User.id)).where(User.is_super_admin == True)  # noqa: E712
            )
            assert super_count == 1
            super_admin = (
                await s.execute(select(User).where(User.is_super_admin == True))  # noqa: E712
            ).scalar_one()
            assert super_admin.role is None
            assert super_admin.must_change_password is True
            assert super_admin.tenant_id is None
            assert super_admin.username == os.environ["SUPER_ADMIN_USERNAME"]

            # External_User_Tenant 存在且类型为 external
            ext_tenant = await s.get(Tenant, EXTERNAL_USER_TENANT_ID)
            assert ext_tenant is not None
            assert ext_tenant.tenant_type == "external"

            # 引导不再创建默认外部管理员（治理走平台超管/超管补建管理员）
            ext_admin = await s.get(User, _EXTERNAL_ADMIN_ID)
            assert ext_admin is None

            # 至少一个组织可见性公共库（External_User_Tenant 内），且为无主库
            pub = (
                await s.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.tenant_id == EXTERNAL_USER_TENANT_ID
                    )
                )
            ).scalars().all()
            assert len(pub) >= 1
            assert any(k.visibility == "organization" for k in pub)
            assert all(k.owner_user_id is None for k in pub)

        await engine.dispose()

    asyncio.run(run())


def test_bootstrap_is_idempotent():
    """重复执行 run_bootstrap 不重复创建（User/Tenant/KnowledgeBase 计数不变）。

    Requirements: 12.1
    """

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        await _create_all(engine)

        async def counts():
            async with sm() as s:
                return (
                    await s.scalar(select(func.count(User.id))),
                    await s.scalar(select(func.count(Tenant.id))),
                    await s.scalar(select(func.count(KnowledgeBase.id))),
                )

        await run_bootstrap(sm)
        first = await counts()
        await run_bootstrap(sm)
        second = await counts()
        assert first == second

        await engine.dispose()

    asyncio.run(run())


def test_bootstrap_fail_fast_missing_super_admin(monkeypatch):
    """缺 Super_Admin 环境变量时 run_bootstrap fail-fast（RuntimeError）。

    通过 monkeypatch app.auth.bootstrap.get_settings 返回 super_admin 字段为空的设置，
    规避 get_settings lru_cache 与 jwt_secret fail-fast 的相互干扰，保持稳健。

    Requirements: 11.3
    """
    from app.auth import bootstrap as bootstrap_mod

    class _EmptySuperAdminSettings:
        super_admin_username = ""
        super_admin_password = ""

    monkeypatch.setattr(bootstrap_mod, "get_settings", lambda: _EmptySuperAdminSettings())

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        await _create_all(engine)
        with pytest.raises(RuntimeError):
            await run_bootstrap(sm)
        await engine.dispose()

    asyncio.run(run())
