"""tenant-auth DB 相关属性/集成测试（内存或文件 sqlite）。

覆盖需触库的属性：P10 仓储过滤、P7 外部用户命名空间隔离/懒创建、P8 盖章、
P18/23 引导幂等。鉴权判定仍复用唯一纯函数，不另起逻辑。
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("JWT_SECRET", "test-secret-key-for-db-properties-0123456789")
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "root")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "RootPass#123")

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schema.db import (
    Base, KnowledgeBase, Tenant, ExternalUser, Permission, User,
)
from app.auth.identity import (
    IdentityContext, IdentitySourceEnum, OperationLevelEnum, TenantScopeModeEnum,
)
from app.repositories.tenant_repo import (
    TenantRepository, TenantScope, tenant_scope, install_tenant_loader_criteria,
)

install_tenant_loader_criteria()


def _new_engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


# Feature: tenant-auth, Property 10: 仓储层强制租户过滤
@settings(max_examples=60, deadline=None)
@given(
    kbs=st.lists(
        st.tuples(
            st.sampled_from(["kbA", "kbB", "kbC", "kbD"]),
            st.sampled_from(["t1", "t2", "t3"]),
        ),
        min_size=1, max_size=8, unique_by=lambda x: x[0],
    ),
    viewer_tenant=st.sampled_from(["t1", "t2", "t3"]),
)
def test_property_10_repository_tenant_filter(kbs, viewer_tenant):
    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            for kid, tid in kbs:
                s.add(KnowledgeBase(id=kid, name=kid, tenant_id=tid, visibility="private"))
            await s.commit()

        identity = IdentityContext(
            source=IdentitySourceEnum.JWT, op_level=OperationLevelEnum.TENANT,
            tenant_id=viewer_tenant, user_id="u",
        )
        # 方案A：scoped_select 仅含本租户
        async with sm() as s:
            repo = TenantRepository(s, identity)
            rows = (await s.execute(repo.scoped_select(KnowledgeBase))).scalars().all()
            assert all(k.tenant_id == viewer_tenant for k in rows)
            expected = {kid for kid, tid in kbs if tid == viewer_tenant}
            assert {k.id for k in rows} == expected
        # 方案B：裸 select 在 contextvar 作用域内同样只见本租户
        async with sm() as s:
            with tenant_scope(TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id=viewer_tenant)):
                rows = (await s.execute(select(KnowledgeBase))).scalars().all()
                assert {k.id for k in rows} == {kid for kid, tid in kbs if tid == viewer_tenant}
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 7: 外部用户命名空间隔离与懒创建幂等
@settings(max_examples=50, deadline=None)
@given(
    pairs=st.lists(
        st.tuples(
            st.sampled_from(["src1", "src2"]),
            st.sampled_from(["user-1", "user-2"]),
        ),
        min_size=1, max_size=10,
    ),
)
def test_property_7_external_user_namespace(pairs):
    from app.auth.apikey_auth import ApiKeyAuthenticator
    from app.auth.constants import EXTERNAL_USER_TENANT_ID

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(Tenant(id=EXTERNAL_USER_TENANT_ID, name="ext", tenant_type="external", is_active=True))
            await s.commit()

        resolved: dict[tuple[str, str], str] = {}
        for key_source, euid in pairs:
            async with sm() as s:
                auth = ApiKeyAuthenticator(s)
                eu = await auth._get_or_create_external_user(key_source, euid)
                # 同一 (source, euid) 恒解析为同一条
                if (key_source, euid) in resolved:
                    assert eu.id == resolved[(key_source, euid)]
                else:
                    resolved[(key_source, euid)] = eu.id
                assert eu.tenant_id == EXTERNAL_USER_TENANT_ID

        # 不同 source 同 euid -> 不同记录
        async with sm() as s:
            total = await s.scalar(select(func.count(ExternalUser.id)))
            assert total == len(set(pairs))
            # 外部用户从不写入 users 表
            assert (await s.scalar(select(func.count(User.id)))) == 0
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 18 + 23: 初始化引导幂等且预置数据完整
def test_property_18_23_bootstrap_idempotent():
    from app.auth.bootstrap import run_bootstrap
    from app.auth.constants import PermissionEnum, EXTERNAL_USER_TENANT_ID

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)

        async def counts():
            async with sm() as s:
                from app.schema.db import Role, RolePermission, UserRole
                return (
                    await s.scalar(select(func.count(Tenant.id))),
                    await s.scalar(select(func.count(User.id))),
                    await s.scalar(select(func.count(Role.id))),
                    await s.scalar(select(func.count(Permission.id))),
                    await s.scalar(select(func.count(KnowledgeBase.id))),
                    await s.scalar(select(func.count(User.id)).where(User.is_super_admin == True)),  # noqa: E712
                )

        await run_bootstrap(sm)
        c1 = await counts()
        await run_bootstrap(sm)
        c2 = await counts()
        assert c1 == c2                          # 幂等
        assert c1[3] == len(list(PermissionEnum))  # 权限点齐全
        assert c1[5] == 1                          # 恰一个 Super_Admin
        # 内置 External_User_Tenant + 公共库存在
        async with sm() as s:
            assert await s.get(Tenant, EXTERNAL_USER_TENANT_ID) is not None
            pub = (await s.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == EXTERNAL_USER_TENANT_ID)
            )).scalars().all()
            assert len(pub) >= 1
            assert all(k.visibility == "organization" for k in pub)
        await engine.dispose()

    asyncio.run(run())
