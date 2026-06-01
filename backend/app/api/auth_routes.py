"""认证路由（tenant-rbac-refactor）：登录 / 改密 / 当前用户 / 注册。

- POST /api/auth/login：校验凭据，签发 JWT（载荷不含权限点/角色）。
- POST /api/auth/change-password：自助改密（旧口令错误 401，成功清除 must_change_password）。
- GET  /api/auth/me：返回当前身份摘要 {user_id, tenant_id, is_super_admin, role}。
- GET/PUT /api/auth/me/profile：本人资料（简介/头像）+ 身份展示。
- POST /api/auth/register：仅当 registration_mode=self_serve 时开放；注册即开一个独立租户，
  注册人成为该租户管理员（固定角色 role=admin）。invite_only 一律 403。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_authenticated
from app.api.errors import (
    PermissionDeniedError,
    UnauthenticatedError,
    UserDisabledError,
)
from app.auth.audit import add_audit
from app.auth.constants import (
    AuditActionEnum,
    AuditResultEnum,
    ROLE_LABELS,
    TenantRoleEnum,
    TenantTypeEnum,
)
from app.auth.identity import IdentityContext, IdentitySourceEnum, OperationLevelEnum
from app.auth.jwt_auth import issue_token
from app.auth.password import hash_password, verify_password
from app.auth.validators import (
    validate_avatar,
    validate_description,
    validate_password,
    validate_tenant_name,
    validate_username,
)
from app.config import get_settings
from app.schema.db import Tenant, User

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


class MeResponse(BaseModel):
    """当前身份摘要：固定角色模型下前端据此驱动菜单/按钮。"""
    user_id: str
    tenant_id: str | None
    is_super_admin: bool
    # 固定角色：admin / member；Super_Admin 与机器身份(tenant_level Key)为 None
    role: str | None = None


class MeProfileResponse(BaseModel):
    """当前登录者的个人资料 + 身份展示信息（供左下角与个人页）。"""
    user_id: str
    username: str
    tenant_id: str | None
    tenant_name: str | None
    is_super_admin: bool
    # 固定角色：admin / member；Super_Admin 为 None
    role: str | None
    # 身份展示名（中文）：超管=超级管理员；否则按角色取 ROLE_LABELS
    role_label: str
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
    # 构造登录者身份用于审计（落角色快照 / 超管标记）。登录成功此刻身份已确定，
    # 否则 add_audit 取不到 actor_role/actor_is_super_admin，列表会显示为「—」。
    actor = IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.PLATFORM if matched.is_super_admin else OperationLevelEnum.TENANT,
        tenant_id=matched.tenant_id,
        user_id=matched.id,
        username=matched.username,
        is_super_admin=matched.is_super_admin,
        role=TenantRoleEnum(matched.role) if matched.role is not None else None,
    )
    add_audit(
        db, actor=actor, actor_username=matched.username,
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
        require_authenticated(allow_must_change_password=True)
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


@router.get("/me", response_model=MeResponse)
async def me(
    identity: IdentityContext = Depends(require_authenticated()),
):
    """返回当前身份摘要（固定角色），供前端驱动菜单/按钮显隐。

    role 由认证阶段合成：Super_Admin / tenant_level Key 为 None，
    其余取固定角色（admin/member）。
    """
    return MeResponse(
        user_id=identity.user_id or "",
        tenant_id=identity.tenant_id,
        is_super_admin=identity.is_super_admin,
        role=identity.role.value if identity.role is not None else None,
    )


class SelectableUser(BaseModel):
    """同租户可选用户（供知识库共享/转移等多选场景）。"""
    id: str
    username: str
    avatar: str | None = None


@router.get("/users/selectable", response_model=list[SelectableUser])
async def list_selectable_users(
    q: str | None = None,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """列出同租户、启用、非自己的用户（用户名模糊搜索，上限 50）。

    任意登录成员可用：用于共享对话框等「从本租户用户里挑人」的多选场景。
    仅返回展示所需的最小字段（id/username/avatar），不含口令等敏感信息。
    """
    if identity.tenant_id is None:
        # 平台超管无业务租户上下文，无可选同租户用户
        return []
    stmt = select(User).where(
        User.tenant_id == identity.tenant_id,
        User.is_active == True,  # noqa: E712
    )
    if identity.user_id:
        stmt = stmt.where(User.id != identity.user_id)
    if q and q.strip():
        stmt = stmt.where(User.username.ilike(f"%{q.strip()}%"))
    rows = (await db.execute(stmt.order_by(User.username).limit(50))).scalars().all()
    return [SelectableUser(id=u.id, username=u.username, avatar=u.avatar) for u in rows]


@router.get("/me/profile", response_model=MeProfileResponse)
async def get_my_profile(
    identity: IdentityContext = Depends(require_authenticated(allow_must_change_password=True)),
    db: AsyncSession = Depends(get_db_session),
):
    """当前登录者的资料与身份（供左下角展示与个人资料页）。"""
    return await _build_my_profile(db, identity)


@router.put("/me/profile", response_model=MeProfileResponse)
async def update_my_profile(
    body: UpdateProfileRequest,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
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
    """组装当前登录者资料：超管无业务租户、身份展示为"超级管理员"；
    其余按固定角色（admin/member）取中文展示名。"""
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
            tenant_name=None, is_super_admin=True, role=None,
            role_label="超级管理员", description=desc, avatar=avatar,
        )

    user = await db.get(User, identity.user_id) if identity.user_id else None
    if user is None:
        raise UnauthenticatedError()
    tenant_name = None
    if user.tenant_id:
        tenant = await db.get(Tenant, user.tenant_id)
        tenant_name = tenant.name if tenant else None
    role_value = user.role
    role_label = ROLE_LABELS.get(role_value, role_value) if role_value else "普通成员"
    return MeProfileResponse(
        user_id=user.id, username=user.username, tenant_id=user.tenant_id,
        tenant_name=tenant_name, is_super_admin=False, role=role_value,
        role_label=role_label, description=user.description, avatar=user.avatar,
    )


@router.get("/registration-mode")
async def registration_mode():
    """公开端点：前端据此决定是否显示"注册"入口。不泄露任何敏感信息。"""
    return {"self_serve": get_settings().registration_mode == "self_serve"}


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db_session)):
    """租户自助注册（registration_mode=self_serve 时开放；invite_only 一律 403）。

    语义：注册不是"加入某个已有租户"，而是**自助开通一个独立租户**——注册人即成为
    该租户的管理员（固定角色 role=admin），拥有本租户全部功能，并可邀请他人成为本租户用户。
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

    # 2) 注册人 = 该租户管理员（固定角色 role=admin，自助设置口令，无需强制改密）
    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=await hash_password(body.password),
        role=TenantRoleEnum.ADMIN.value,
        is_active=True, must_change_password=False,
    ))

    add_audit(
        db, actor=None, actor_username=username,
        action=AuditActionEnum.TENANT_CREATE,
        target_type="tenant", target_id=tenant_id, target_name=tenant_name,
        detail={"self_register": True, "admin_user_id": user_id}, request=request,
    )
    await db.commit()

    token = issue_token(user_id, tenant_id, 0)
    return RegisterResponse(access_token=token, must_change_password=False, tenant_id=tenant_id)
