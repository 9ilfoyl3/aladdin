"""管理路由（tenant-auth）：平台级（Super_Admin）与租户级（Tenant_Admin）。

平台级（op_level=platform，allow_api_key=False，仅 Super_Admin/JWT）：
  - 租户 CRUD / 启停
  - 为新租户创建初始 Tenant_Admin
  - 跨租户兜底：重置任意管理员口令、启停任意用户

租户级（需相应权限点，allow_api_key=False）：
  - 本租户用户 CRUD / 启停 / 口令重置（user:manage）
  - 自定义角色 CRUD、角色权限点分配、用户角色分配（role:manage）

越租户操作一律 404（存在性非泄露）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import CrossTenantError, PermissionDeniedError
from app.auth.bootstrap import ensure_tenant_builtin_roles
from app.auth.constants import (
    BuiltinRoleEnum,
    PermissionEnum,
    TenantTypeEnum,
)
from app.auth.identity import IdentityContext, OperationLevelEnum
from app.auth.password import hash_password
from app.auth.permission_resolver import resolve_role_ids
from app.schema.db import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)

router = APIRouter(prefix="/api/admin", tags=["Admin"])

# 临时口令生成（重置/建号下发一次）；强制改密保证用户首次登录必须改。
def _temp_password() -> str:
    return "Tmp-" + uuid.uuid4().hex[:12]


# ============================================================
# 平台级：租户管理（Super_Admin）
# ============================================================

_PLATFORM = dict(op_level=OperationLevelEnum.PLATFORM, allow_api_key=False)


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1)
    admin_username: str = Field(..., min_length=1, description="初始租户管理员用户名")
    admin_password: str | None = Field(default=None, description="不填则生成临时口令")


class TenantResponse(BaseModel):
    id: str
    name: str
    tenant_type: str
    is_active: bool


class TenantCreateResponse(TenantResponse):
    admin_username: str
    admin_temp_password: str | None = None


@router.post("/tenants", response_model=TenantCreateResponse, status_code=201)
async def create_tenant(
    body: TenantCreate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """创建租户 + 预置 admin/user 角色 + 初始 Tenant_Admin（分配 admin 角色）。"""
    tenant_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=body.name, tenant_type=TenantTypeEnum.BUSINESS.value, is_active=True))
    await db.flush()

    # 预置该租户内置角色
    code_to_id = {
        code: pid
        for pid, code in (await db.execute(select(Permission.id, Permission.code))).all()
    }
    roles = await ensure_tenant_builtin_roles(db, tenant_id, code_to_id)
    await db.flush()

    # 初始 Tenant_Admin
    temp_pwd = body.admin_password or _temp_password()
    admin_id = str(uuid.uuid4())
    admin_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=admin_id, tenant_id=tenant_id, username=body.admin_username,
        password_hash=admin_pwd_hash, is_active=True,
        must_change_password=True,  # 强制首次改密
    ))
    db.add(UserRole(user_id=admin_id, role_id=roles[BuiltinRoleEnum.ADMIN.value]))
    await db.commit()

    return TenantCreateResponse(
        id=tenant_id, name=body.name, tenant_type=TenantTypeEnum.BUSINESS.value,
        is_active=True, admin_username=body.admin_username,
        admin_temp_password=None if body.admin_password else temp_pwd,
    )


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    rows = (await db.execute(select(Tenant).order_by(Tenant.name))).scalars().all()
    return [TenantResponse(id=t.id, name=t.name, tenant_type=t.tenant_type, is_active=t.is_active) for t in rows]


class TenantToggle(BaseModel):
    is_active: bool


@router.put("/tenants/{tenant_id}/status", response_model=TenantResponse)
async def set_tenant_status(
    tenant_id: str,
    body: TenantToggle,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """启停租户（停用 != 删除，保留全部数据）。"""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise CrossTenantError()
    tenant.is_active = body.is_active
    await db.commit()
    return TenantResponse(id=tenant.id, name=tenant.name, tenant_type=tenant.tenant_type, is_active=tenant.is_active)


# ============================================================
# 租户级：用户管理（user:manage）+ 跨租户兜底（Super_Admin）
# ============================================================


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str | None = Field(default=None, description="不填则生成临时口令并强制改密")
    role_names: list[str] = Field(default_factory=lambda: [BuiltinRoleEnum.USER.value])


class UserResponse(BaseModel):
    id: str
    tenant_id: str | None
    username: str
    is_active: bool
    must_change_password: bool


class UserCreateResponse(UserResponse):
    temp_password: str | None = None


def _require_same_tenant(identity: IdentityContext, target_tenant_id: str | None) -> None:
    """非 Super_Admin 只能操作本租户对象；越租户 404（不泄露存在性）。"""
    if identity.is_super_admin:
        return
    if target_tenant_id != identity.tenant_id:
        raise CrossTenantError()


@router.post("/users", response_model=UserCreateResponse, status_code=201)
async def create_user(
    body: UserCreate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """在管理员本租户内创建用户并分配角色（R12.5：管理员建号，不受注册模式限制）。"""
    tenant_id = identity.tenant_id
    if tenant_id is None:
        # Super_Admin 不属于业务租户，建号须经平台流程指定租户，这里拒绝歧义路径
        raise PermissionDeniedError("请在具体租户上下文内创建用户")

    exists = await db.scalar(
        select(func.count(User.id)).where(User.tenant_id == tenant_id, User.username == body.username)
    )
    if exists:
        raise PermissionDeniedError("用户名已存在")

    temp_pwd = body.password or _temp_password()
    user_id = str(uuid.uuid4())
    user_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=body.username,
        password_hash=user_pwd_hash, is_active=True,
        must_change_password=body.password is None,  # 生成临时口令则强制改密
    ))
    await _assign_roles(db, tenant_id, user_id, body.role_names)
    await db.commit()
    return UserCreateResponse(
        id=user_id, tenant_id=tenant_id, username=body.username, is_active=True,
        must_change_password=body.password is None,
        temp_password=None if body.password else temp_pwd,
    )


class UserStatusToggle(BaseModel):
    is_active: bool


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def set_user_status(
    user_id: str,
    body: UserStatusToggle,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """启停用户（停用 != 删除）。停用时自增 token_version 使其已签发 JWT 失效。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    user.is_active = body.is_active
    if not body.is_active:
        user.token_version = user.token_version + 1  # 已签发 JWT 失效
    await db.commit()
    return _user_resp(user)


@router.post("/users/{user_id}/reset-password", response_model=UserCreateResponse)
async def reset_password(
    user_id: str,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """管理员重置用户口令：生成临时口令 + 强制改密 + 失效旧 JWT。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    temp_pwd = _temp_password()
    user.password_hash = await hash_password(temp_pwd)
    user.must_change_password = True
    user.token_version = user.token_version + 1
    await db.commit()
    return UserCreateResponse(
        id=user.id, tenant_id=user.tenant_id, username=user.username,
        is_active=user.is_active, must_change_password=True, temp_password=temp_pwd,
    )


# ============================================================
# 租户级：角色与权限点（role:manage）
# ============================================================


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    permission_codes: list[str] = Field(default_factory=list)


class RoleResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    is_builtin: bool
    permission_codes: list[str] = Field(default_factory=list)


@router.post("/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    body: RoleCreate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """在本租户内创建自定义角色并分配权限点（仅本租户内存在与生效）。"""
    tenant_id = identity.tenant_id
    if tenant_id is None:
        raise PermissionDeniedError("请在具体租户上下文内创建角色")
    exists = await db.scalar(
        select(func.count(Role.id)).where(Role.tenant_id == tenant_id, Role.name == body.name)
    )
    if exists:
        raise PermissionDeniedError("角色名已存在")
    role_id = str(uuid.uuid4())
    db.add(Role(id=role_id, tenant_id=tenant_id, name=body.name, is_builtin=False, description=body.description))
    await _set_role_permissions(db, role_id, body.permission_codes)
    await db.commit()
    return RoleResponse(id=role_id, tenant_id=tenant_id, name=body.name, is_builtin=False,
                        permission_codes=body.permission_codes)


@router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """列出本租户角色（含其权限点）。"""
    tenant_id = identity.tenant_id
    roles = (await db.execute(select(Role).where(Role.tenant_id == tenant_id))).scalars().all()
    out: list[RoleResponse] = []
    for r in roles:
        codes = (await db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == r.id)
        )).scalars().all()
        out.append(RoleResponse(id=r.id, tenant_id=r.tenant_id, name=r.name,
                                is_builtin=r.is_builtin, permission_codes=list(codes)))
    return out


class RolePermissionsUpdate(BaseModel):
    permission_codes: list[str]


@router.put("/roles/{role_id}/permissions", response_model=RoleResponse)
async def set_role_permissions(
    role_id: str,
    body: RolePermissionsUpdate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """为角色重设权限点集合（变更经实时解析在下次请求即时生效）。"""
    role = await db.get(Role, role_id)
    if role is None:
        raise CrossTenantError()
    _require_same_tenant(identity, role.tenant_id)
    await _set_role_permissions(db, role_id, body.permission_codes, replace=True)
    await db.commit()
    return RoleResponse(id=role.id, tenant_id=role.tenant_id, name=role.name,
                        is_builtin=role.is_builtin, permission_codes=body.permission_codes)


class UserRolesUpdate(BaseModel):
    role_ids: list[str]


@router.put("/users/{user_id}/roles")
async def set_user_roles(
    user_id: str,
    body: UserRolesUpdate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """重设用户的角色集合（即时生效）。所列角色必须属于同一租户。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    # 校验角色同租户
    for rid in body.role_ids:
        role = await db.get(Role, rid)
        if role is None or role.tenant_id != user.tenant_id:
            raise CrossTenantError()
    # 清空重设
    existing = (await db.execute(select(UserRole).where(UserRole.user_id == user_id))).scalars().all()
    for ur in existing:
        await db.delete(ur)
    for rid in body.role_ids:
        db.add(UserRole(user_id=user_id, role_id=rid))
    await db.commit()
    return {"detail": "已更新用户角色", "role_ids": body.role_ids}


# ============================================================
# 辅助
# ============================================================


def _user_resp(user: User) -> UserResponse:
    return UserResponse(
        id=user.id, tenant_id=user.tenant_id, username=user.username,
        is_active=user.is_active, must_change_password=user.must_change_password,
    )


async def _assign_roles(db: AsyncSession, tenant_id: str, user_id: str, role_names: list[str]) -> None:
    for name in role_names:
        role = (await db.execute(
            select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
        )).scalar_one_or_none()
        if role is not None:
            db.add(UserRole(user_id=user_id, role_id=role.id))


async def _set_role_permissions(
    db: AsyncSession, role_id: str, permission_codes: list[str], replace: bool = False
) -> None:
    if replace:
        existing = (await db.execute(
            select(RolePermission).where(RolePermission.role_id == role_id)
        )).scalars().all()
        for rp in existing:
            await db.delete(rp)
    # code -> id
    code_to_id = {
        code: pid
        for pid, code in (await db.execute(
            select(Permission.id, Permission.code).where(Permission.code.in_(permission_codes))
        )).all()
    }
    for code in permission_codes:
        pid = code_to_id.get(code)
        if pid:
            db.add(RolePermission(role_id=role_id, permission_id=pid))
