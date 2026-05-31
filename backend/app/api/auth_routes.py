"""认证路由（tenant-auth）：登录 / 改密 / 当前用户权限 / 注册。

- POST /api/auth/login：校验凭据，签发 JWT（载荷不含权限点）。
- POST /api/auth/change-password：自助改密（旧口令错误 401，成功清除 must_change_password）。
- GET  /api/auth/me/permissions：返回当前用户实时 Effective_Permission_Set（按 type 分组）。
- POST /api/auth/register：仅当 registration_mode=self_serve 时开放；invite_only 一律 403
  （v1 邀请制 = 仅管理员经 admin_routes 建号，不实现匿名自助注册/邀请令牌）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import (
    PermissionDeniedError,
    UnauthenticatedError,
    UserDisabledError,
)
from app.auth.constants import BuiltinRoleEnum, PermissionTypeEnum
from app.auth.identity import IdentityContext, OperationLevelEnum
from app.auth.jwt_auth import issue_token
from app.auth.password import hash_password, verify_password
from app.auth.permission_resolver import resolve_permissions_with_types
from app.config import get_settings
from app.schema.db import Permission, Role, RolePermission, Tenant, User, UserRole

router = APIRouter(prefix="/api/auth", tags=["Auth"])


# ============================================================
# 请求/响应模型
# ============================================================


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    tenant_id: str | None = Field(default=None, description="多租户同名用户时指定归属租户")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
    is_super_admin: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, description="新口令至少 8 位")


class PermissionItem(BaseModel):
    code: str
    type: str


class MePermissionsResponse(BaseModel):
    user_id: str
    tenant_id: str | None
    is_super_admin: bool
    permissions: list[PermissionItem]


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    tenant_id: str = Field(..., description="自助注册归属的租户")


# ============================================================
# 接口实现
# ============================================================


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db_session)):
    """登录：校验凭据，签发 JWT。凭据无效一律 401（不区分用户不存在/口令错，避免枚举）。"""
    stmt = select(User).where(User.username == body.username)
    if body.tenant_id is not None:
        stmt = stmt.where(User.tenant_id == body.tenant_id)
    candidates = (await db.execute(stmt)).scalars().all()

    # 同名用户可能跨租户存在多条；逐一校验口令
    matched: User | None = None
    for u in candidates:
        if await verify_password(body.password, u.password_hash):
            matched = u
            break
    if matched is None:
        raise UnauthenticatedError("用户名或口令错误")
    if not matched.is_active:
        raise UserDisabledError()
    # 租户启用校验（Super_Admin 无租户跳过）
    if matched.tenant_id is not None:
        tenant = await db.get(Tenant, matched.tenant_id)
        if tenant is None or not tenant.is_active:
            raise UserDisabledError("所属租户已停用")

    token = issue_token(matched.id, matched.tenant_id, matched.token_version)
    return LoginResponse(
        access_token=token,
        must_change_password=matched.must_change_password,
        is_super_admin=matched.is_super_admin,
    )


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    identity: IdentityContext = Depends(
        authorization_guard(allow_must_change_password=True)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """自助改密：校验旧口令（错误 401），更新哈希并清除 must_change_password。

    本端点是 must_change_password 闸门唯一放行的操作（allow_must_change_password=True）。
    改密后自增 token_version，使旧 JWT 失效（强制重新登录）。
    """
    user = await db.get(User, identity.user_id)
    if user is None:
        raise UnauthenticatedError()
    if not await verify_password(body.old_password, user.password_hash):
        raise UnauthenticatedError("旧口令不正确")
    user.password_hash = await hash_password(body.new_password)
    user.must_change_password = False
    user.token_version = user.token_version + 1  # 旧 token 失效
    await db.commit()
    return {"detail": "口令已修改，请用新口令重新登录"}


@router.get("/me/permissions", response_model=MePermissionsResponse)
async def me_permissions(
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    """返回当前用户实时有效权限点（按 type 标识，供前端驱动菜单/按钮）。"""
    if identity.user_id is None:
        # API Key 通道无"当前用户"概念
        return MePermissionsResponse(
            user_id="", tenant_id=identity.tenant_id,
            is_super_admin=identity.is_super_admin, permissions=[],
        )
    items = await resolve_permissions_with_types(db, identity.user_id)
    return MePermissionsResponse(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        is_super_admin=identity.is_super_admin,
        permissions=[PermissionItem(code=i["code"], type=i["type"]) for i in items],
    )


@router.post("/register", response_model=LoginResponse, status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db_session)):
    """自助注册：仅 registration_mode=self_serve 开放；invite_only 一律 403。"""
    settings = get_settings()
    if settings.registration_mode != "self_serve":
        raise PermissionDeniedError("平台未开放自助注册，请联系管理员创建账号")

    tenant = await db.get(Tenant, body.tenant_id)
    if tenant is None or not tenant.is_active:
        raise PermissionDeniedError("目标租户不可用")

    # 同租户用户名唯一
    exists = await db.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == body.tenant_id, User.username == body.username
        )
    )
    if exists:
        raise PermissionDeniedError("用户名已存在")

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        tenant_id=body.tenant_id,
        username=body.username,
        password_hash=await hash_password(body.password),
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    # 默认赋予该租户的 user 角色
    user_role = (
        await db.execute(
            select(Role).where(
                Role.tenant_id == body.tenant_id, Role.name == BuiltinRoleEnum.USER.value
            )
        )
    ).scalar_one_or_none()
    if user_role is not None:
        db.add(UserRole(user_id=user_id, role_id=user_role.id))
    await db.commit()

    token = issue_token(user_id, body.tenant_id, 0)
    return LoginResponse(access_token=token, must_change_password=False)
