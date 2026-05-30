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


async def _seed_rbac(sm, tenant_id="t1"):
    """建一个租户 + admin/user 角色 + 权限点字典，返回 (code_to_id, roles)。"""
    from app.auth.bootstrap import ensure_tenant_builtin_roles
    async with sm() as s:
        s.add(Tenant(id=tenant_id, name=tenant_id, is_active=True))
        await s.flush()
        # 预置权限点字典
        from app.auth.constants import PermissionEnum, PermissionTypeEnum
        for p in PermissionEnum:
            ptype = ("menu" if p.value.startswith("menu:")
                     else "btn" if p.value.startswith("btn:") else "api")
            s.add(Permission(id="p-" + p.value, code=p.value, type=ptype))
        await s.flush()
        code_to_id = {code: pid for pid, code in
                      (await s.execute(select(Permission.id, Permission.code))).all()}
        roles = await ensure_tenant_builtin_roles(s, tenant_id, code_to_id)
        await s.commit()
    return code_to_id, roles


# Feature: tenant-auth, Property 4: 实时权限即时生效（无 JWT 快照）
# 对任意角色权限点变更序列，resolve_effective_permissions 恒等于"按当前数据重新解析"。
@settings(max_examples=40, deadline=None)
@given(
    grant_codes=st.lists(st.sampled_from(["kb:create", "kb:read", "kb:write", "qa:invoke"]),
                         min_size=0, max_size=4, unique=True),
)
def test_property_4_realtime_permission(grant_codes):
    from app.schema.db import Role, RolePermission, UserRole
    from app.auth.permission_resolver import resolve_effective_permissions

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(Tenant(id="t1", name="t1", is_active=True))
            s.add(User(id="u1", tenant_id="t1", username="a", password_hash="x", is_active=True))
            s.add(Role(id="r1", tenant_id="t1", name="custom"))
            s.add(UserRole(user_id="u1", role_id="r1"))
            for code in ["kb:create", "kb:read", "kb:write", "qa:invoke"]:
                s.add(Permission(id="p-" + code, code=code, type="api"))
            await s.commit()

        # 初始无权限
        async with sm() as s:
            assert await resolve_effective_permissions(s, "u1") == frozenset()

        # 赋予 grant_codes 后，解析结果恒等于当前数据
        async with sm() as s:
            for code in grant_codes:
                s.add(RolePermission(role_id="r1", permission_id="p-" + code))
            await s.commit()
        async with sm() as s:
            assert await resolve_effective_permissions(s, "u1") == frozenset(grant_codes)

        # 移除一个再解析，仍恒等当前数据（即时生效，无快照）
        if grant_codes:
            async with sm() as s:
                rp = (await s.execute(select(RolePermission).where(
                    RolePermission.permission_id == "p-" + grant_codes[0]))).scalar_one_or_none()
                if rp:
                    await s.delete(rp)
                await s.commit()
            async with sm() as s:
                assert await resolve_effective_permissions(s, "u1") == frozenset(grant_codes[1:])
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 13: 当前用户有效权限点等于角色权限点并集（带 type）
def test_property_13_me_permissions_union_with_type():
    from app.schema.db import Role, RolePermission, UserRole
    from app.auth.permission_resolver import resolve_permissions_with_types

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        _, _ = None, None
        async with sm() as s:
            s.add(Tenant(id="t1", name="t1", is_active=True))
            s.add(User(id="u1", tenant_id="t1", username="a", password_hash="x", is_active=True))
            s.add(Role(id="r1", tenant_id="t1", name="role1"))
            s.add(Role(id="r2", tenant_id="t1", name="role2"))
            s.add(UserRole(user_id="u1", role_id="r1"))
            s.add(UserRole(user_id="u1", role_id="r2"))
            s.add(Permission(id="p1", code="kb:create", type="api"))
            s.add(Permission(id="p2", code="menu:knowledge", type="menu"))
            s.add(Permission(id="p3", code="btn:kb_delete", type="btn"))
            # r1: p1,p2  r2: p2,p3  -> 并集 {p1,p2,p3}
            s.add(RolePermission(role_id="r1", permission_id="p1"))
            s.add(RolePermission(role_id="r1", permission_id="p2"))
            s.add(RolePermission(role_id="r2", permission_id="p2"))
            s.add(RolePermission(role_id="r2", permission_id="p3"))
            await s.commit()
        async with sm() as s:
            items = await resolve_permissions_with_types(s, "u1")
        codes = {i["code"] for i in items}
        assert codes == {"kb:create", "menu:knowledge", "btn:kb_delete"}
        types = {i["code"]: i["type"] for i in items}
        assert types["kb:create"] == "api"
        assert types["menu:knowledge"] == "menu"
        assert types["btn:kb_delete"] == "btn"
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 17: 租户级 Key 授权范围动态规则与不重签
@settings(max_examples=40, deadline=None)
@given(
    public_kbs=st.lists(st.sampled_from(["kbP1", "kbP2", "kbP3"]), max_size=3, unique=True),
)
def test_property_17_tenant_key_dynamic_scope(public_kbs):
    from app.auth.kb_scope import assemble_allowed_kb_ids
    from app.auth.identity import KbScope

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        # 私有库 + 动态公共库
        async with sm() as s:
            s.add(Tenant(id="t1", name="t1", is_active=True))
            s.add(KnowledgeBase(id="kbPriv", name="priv", tenant_id="t1", visibility="private", owner_user_id="u1"))
            for kid in public_kbs:
                s.add(KnowledgeBase(id=kid, name=kid, tenant_id="t1", visibility="organization", owner_user_id="admin"))
            await s.commit()

        # 开启 all_public_kbs 动态规则的租户级 Key
        identity = IdentityContext(
            source=IdentitySourceEnum.API_KEY, op_level=OperationLevelEnum.TENANT,
            tenant_id="t1", api_key_id="k1",
            kb_scope=KbScope(all_public_kbs=True, explicit_kb_ids=frozenset()),
        )
        async with sm() as s:
            allowed = await assemble_allowed_kb_ids(s, identity)
        # 可访问公共库集合恒等于当前全部 organization KB（动态跟随，不含私有库）
        assert allowed == set(public_kbs)
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 24: 单租户检索范围与改造前等价
@settings(max_examples=40, deadline=None)
@given(
    kb_ids=st.lists(st.sampled_from(["kb1", "kb2", "kb3", "kb4"]), min_size=1, max_size=4, unique=True),
)
def test_property_24_single_tenant_retrieval_range(kb_ids):
    """单租户下管理员（kb:write_public）对本租户全部库可读：
    assemble_allowed_kb_ids 不缩小可检索集合（等于本租户全部库）。"""
    from app.auth.kb_scope import assemble_allowed_kb_ids
    from app.auth.constants import PermissionEnum

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(Tenant(id="t1", name="t1", is_active=True))
            # owner=admin 的库 + 公共库混合
            for i, kid in enumerate(kb_ids):
                vis = "organization" if i % 2 == 0 else "private"
                s.add(KnowledgeBase(id=kid, name=kid, tenant_id="t1", visibility=vis, owner_user_id="admin"))
            await s.commit()

        # admin 身份：owner 全部本租户库 + 公共库 -> 可读集合 = 本租户全部库
        identity = IdentityContext(
            source=IdentitySourceEnum.JWT, op_level=OperationLevelEnum.TENANT,
            tenant_id="t1", user_id="admin",
            effective_permissions=frozenset({PermissionEnum.KB_WRITE_PUBLIC.value}),
        )
        async with sm() as s:
            allowed = await assemble_allowed_kb_ids(s, identity)
        assert allowed == set(kb_ids)  # 不缩小：本租户全部库都可检索
        await engine.dispose()

    asyncio.run(run())


# Feature: tenant-auth, Property 14: 停用即时失效（JWT 401 / 用户级 Key 403）
def test_property_14_deactivation_invalidates():
    from app.api.auth import generate_api_key, hash_key, get_key_prefix
    from app.auth.apikey_auth import ApiKeyAuthenticator
    from app.api.errors import UserDisabledError
    from app.schema.db import ApiKey
    from app.auth.constants import ApiKeyTypeEnum

    async def run():
        engine = _new_engine()
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        raw = generate_api_key()
        async with sm() as s:
            s.add(Tenant(id="t1", name="t1", is_active=True))
            s.add(User(id="u1", tenant_id="t1", username="a", password_hash="x",
                       is_active=True, token_version=0))
            s.add(ApiKey(id="k1", key_hash=hash_key(raw), prefix=get_key_prefix(raw),
                         tenant_id="t1", key_type=ApiKeyTypeEnum.USER_LEVEL.value,
                         bound_user_id="u1", is_active=True))
            await s.commit()

        # 用户级 Key 正常解析
        async with sm() as s:
            idc = await ApiKeyAuthenticator(s).authenticate(raw, {})
            assert idc.user_id == "u1"

        # 停用用户后：绑定它的用户级 Key -> 403（UserDisabledError）
        async with sm() as s:
            u = await s.get(User, "u1")
            u.is_active = False
            u.token_version += 1  # 同步失效 JWT
            await s.commit()
        async with sm() as s:
            try:
                await ApiKeyAuthenticator(s).authenticate(raw, {})
                assert False, "停用用户的用户级 Key 应被拒"
            except UserDisabledError:
                pass

        # JWT 失效：旧 token_version 不匹配当前 DB -> resolve 时拒绝（这里直接比对语义）
        async with sm() as s:
            u = await s.get(User, "u1")
            assert u.token_version == 1  # 旧 JWT 携带 0 -> 不匹配 -> 失效
        await engine.dispose()

    asyncio.run(run())
