"""认证路由（tenant-auth）：登录 / 改密 / 当前用户权限 / 注册。

- POST /api/auth/login：校验凭据，签发 JWT（载荷不含权限点）。
- POST /api/auth/change-password：自助改密（旧口令错误 401，成功清除 must_change_password）。
- GET  /api/auth/me/permissions：返回当前用户实时 Effective_Permission_Set（按 type 分组）。
- POST /api/auth/register：仅当 registration_mode=self_serve 时开放；invite_only 一律 403
  （v1 邀请制 = 仅管理员经 admin_routes 建号，不实现匿名自助注册/邀请令牌）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import (
    PermissionDeniedError,
    UnauthenticatedError,
    UserDisabledError,
)
from app.auth.audit import add_audit
from app.auth.bootstrap import ensure_tenant_builtin_roles
from app.auth.constants import (
    AuditActionEnum,
    AuditResultEnum,
    BuiltinRoleEnum,
    PermissionTypeEnum,
    TenantTypeEnum,
)
from app.auth.identity import IdentityContext, OperationLevelEnum
from app.auth.jwt_auth import issue_token
from app.auth.password import hash_password, verify_password
from app.auth.permission_resolver import resolve_permissions_with_types
from app.auth.validators import (
    validate_avatar,
    validate_description,
    validate_password,
    validate_tenant_name,
    validate_username,
)
from app.config import get_settings
from app.schema.db import Permission, Role, RolePermission, Tenant, User, UserRole

router = APIRouter(prefix="/api/auth", tags=["Auth"])


# ============================================================
# 请求/响应模型
# ============================================================


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
    is_super_admin: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1, description="新口令规则见 validators.validate_password")


class PermissionItem(BaseModel):
    code: str
    type: str


class MePermissionsResponse(BaseModel):
    user_id: str
    tenant_id: str | None
    is_super_admin: bool
    permissions: list[PermissionItem]


class MeProfileResponse(BaseModel):
    """当前登录者的个人资料 + 身份展示信息（供左下角与个人页）。"""
    user_id: str
    username: str
    tenant_id: str | None
    tenant_name: str | None
    is_super_admin: bool
    # 身份展示名：超管=超级管理员；否则取其在本租户的角色（admin=管理员/user=普通用户/自定义名）
    role_names: list[str]
    description: str | None
    avatar: str | None


class UpdateProfileRequest(BaseModel):
    """本人自助维护：简介与头像。两者均可单独提交（None=不改，""=清除）。"""
    description: str | None = Field(default=None)
    avatar: str | None = Field(default=None)


class RegisterRequest(BaseModel):
    """租户自助注册：注册即开一个独立租户，注册人成为该租户管理员。"""
    username: str = Field(..., min_length=1, description="注册人用户名（全局唯一），将成为新租户管理员")
    password: str = Field(..., min_length=1)
    tenant_name: str = Field(..., min_length=1, description="新建租户的名称（如个人空间名/组织名）")


class RegisterResponse(LoginResponse):
    tenant_id: str


# ============================================================
# 接口实现
# ============================================================


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db_session)):
    """登录：校验凭据，签发 JWT。凭据无效一律 401（不区分用户不存在/口令错，避免枚举）。

    用户名全局唯一，故仅凭 用户名+口令 即可定位账号，无需指定租户。
    """
    matched = (await db.execute(
        select(User).where(User.username == body.username)
    )).scalar_one_or_none()

    if matched is None or not await verify_password(body.password, matched.password_hash):
        add_audit(
            db, actor=None, actor_username=body.username,
            action=AuditActionEnum.LOGIN_FAIL, result=AuditResultEnum.FAIL,
            detail={"reason": "bad_credentials"}, request=request,
        )
        await db.commit()
        raise UnauthenticatedError("用户名或口令错误")
    if not matched.is_active:
        raise UserDisabledError()
    # 租户启用校验（Super_Admin 无租户跳过）
    if matched.tenant_id is not None:
        tenant = await db.get(Tenant, matched.tenant_id)
        if tenant is None or not tenant.is_active:
            raise UserDisabledError("所属租户已停用")

    token = issue_token(matched.id, matched.tenant_id, matched.token_version)
    add_audit(
        db, actor=None, actor_username=matched.username,
        action=AuditActionEnum.LOGIN_SUCCESS,
        target_type="user", target_id=matched.id, target_name=matched.username,
        request=request,
    )
    await db.commit()
    return LoginResponse(
        access_token=token,
        must_change_password=matched.must_change_password,
        is_super_admin=matched.is_super_admin,
    )


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
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
    validate_password(body.new_password)
    user.password_hash = await hash_password(body.new_password)
    user.must_change_password = False
    user.temp_password = None  # 清除明文临时口令（首登改密后不再可见）
    user.token_version = user.token_version + 1  # 旧 token 失效
    add_audit(
        db, actor=identity, actor_username=user.username,
        action=AuditActionEnum.CHANGE_PASSWORD,
        target_type="user", target_id=user.id, target_name=user.username,
        request=request,
    )
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


@router.get("/me/profile", response_model=MeProfileResponse)
async def get_my_profile(
    identity: IdentityContext = Depends(authorization_guard(allow_must_change_password=True)),
    db: AsyncSession = Depends(get_db_session),
):
    """当前登录者的资料与身份（供左下角展示与个人资料页）。"""
    return await _build_my_profile(db, identity)


@router.put("/me/profile", response_model=MeProfileResponse)
async def update_my_profile(
    body: UpdateProfileRequest,
    request: Request,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    """本人自助维护简介与头像。description/avatar 传 None 表示不改，传空串表示清除。"""
    if identity.user_id is None:
        raise PermissionDeniedError("当前身份不支持维护个人资料")
    user = await db.get(User, identity.user_id)
    if user is None:
        raise UnauthenticatedError()
    if body.description is not None:
        user.description = validate_description(body.description)
    if body.avatar is not None:
        user.avatar = validate_avatar(body.avatar)
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_UPDATE_PROFILE,
        target_type="user", target_id=user.id, target_name=user.username,
        request=request,
    )
    await db.commit()
    return await _build_my_profile(db, identity)


async def _build_my_profile(db: AsyncSession, identity: IdentityContext) -> MeProfileResponse:
    """组装当前登录者资料：超管无业务租户、身份展示为"超级管理员"。"""
    if identity.is_super_admin:
        uname = identity.username or "superadmin"
        desc = None
        avatar = None
        if identity.user_id:
            u = await db.get(User, identity.user_id)
            if u is not None:
                uname = u.username
                desc = u.description
                avatar = u.avatar
        return MeProfileResponse(
            user_id=identity.user_id or "", username=uname, tenant_id=None,
            tenant_name=None, is_super_admin=True, role_names=["超级管理员"],
            description=desc, avatar=avatar,
        )

    user = await db.get(User, identity.user_id) if identity.user_id else None
    if user is None:
        raise UnauthenticatedError()
    tenant_name = None
    if user.tenant_id:
        tenant = await db.get(Tenant, user.tenant_id)
        tenant_name = tenant.name if tenant else None
    role_names = (await db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
    )).scalars().all()
    return MeProfileResponse(
        user_id=user.id, username=user.username, tenant_id=user.tenant_id,
        tenant_name=tenant_name, is_super_admin=False, role_names=list(role_names),
        description=user.description, avatar=user.avatar,
    )


@router.get("/registration-mode")
async def registration_mode():
    """公开端点：前端据此决定是否显示"注册"入口。不泄露任何敏感信息。"""
    return {"self_serve": get_settings().registration_mode == "self_serve"}


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db_session)):
    """租户自助注册（registration_mode=self_serve 时开放；invite_only 一律 403）。

    语义：注册不是"加入某个已有租户"，而是**自助开通一个独立租户**——注册人即成为
    该租户的管理员(admin 角色)，拥有本租户全部功能，并可邀请他人成为本租户用户。
    这样既满足"自由注册"，又不暴露/穿透他人租户，符合租户硬隔离模型。
    """
    settings = get_settings()
    if settings.registration_mode != "self_serve":
        raise PermissionDeniedError("平台未开放自助注册，请联系管理员")

    username = validate_username(body.username)
    validate_password(body.password)
    tenant_name = validate_tenant_name(body.tenant_name)

    # 用户名全局唯一
    exists = await db.scalar(select(func.count(User.id)).where(User.username == username))
    if exists:
        raise PermissionDeniedError("用户名已存在")

    # 1) 新建独立租户
    tenant_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=tenant_name, tenant_type=TenantTypeEnum.BUSINESS.value, is_active=True))
    await db.flush()

    # 2) 预置该租户内置 admin/user 角色
    code_to_id = {
        code: pid for pid, code in (await db.execute(select(Permission.id, Permission.code))).all()
    }
    roles = await ensure_tenant_builtin_roles(db, tenant_id, code_to_id)
    await db.flush()

    # 3) 注册人 = 该租户管理员（自助设置口令，无需强制改密）
    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=await hash_password(body.password),
        is_active=True, must_change_password=False,
    ))
    db.add(UserRole(user_id=user_id, role_id=roles[BuiltinRoleEnum.ADMIN.value]))

    add_audit(
        db, actor=None, actor_username=username,
        action=AuditActionEnum.TENANT_CREATE,
        target_type="tenant", target_id=tenant_id, target_name=tenant_name,
        detail={"self_register": True, "admin_user_id": user_id}, request=request,
    )
    await db.commit()

    token = issue_token(user_id, tenant_id, 0)
    return RegisterResponse(access_token=token, must_change_password=False, tenant_id=tenant_id)
