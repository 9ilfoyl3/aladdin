"""邀请链接路由（tenant-auth 管理扩展）。

两类邀请（带有效期 + 可选次数，token 只存哈希）：
- create_tenant：仅 Super_Admin 签发。被邀请人接受后创建一个新租户 + 自身成为该租户管理员。
- create_user：具备 user:manage 的租户管理员签发，scope 锁定签发者租户，被邀请人接受后
  在该租户内创建一个普通用户（按签发时预设角色）。

安全：
- 管理端点（签发/列表/吊销）经 Authorization_Guard，allow_api_key=False。
- 接受端点免登录（被邀请人尚无账号），但严格受 token 有效性 + scope 约束，
  且建号仍走用户名/口令校验。
- 有效期由 expires_at 强制；max_uses 可选（null=有效期内不限次，1=一次性）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import CrossTenantError, PermissionDeniedError, ValidationInputError
from app.auth.audit import add_audit
from app.auth.bootstrap import ensure_tenant_builtin_roles
from app.auth.constants import (
    AuditActionEnum,
    BuiltinRoleEnum,
    InvitationScopeEnum,
    PermissionEnum,
    TenantTypeEnum,
)
from app.auth.identity import IdentityContext, IdentitySourceEnum, OperationLevelEnum
from app.auth.jwt_auth import issue_token
from app.auth.password import hash_password
from app.auth.validators import validate_password, validate_tenant_name, validate_username
from app.schema.api import PageResult
from app.schema.db import Invitation, Permission, Role, Tenant, User, UserRole

router = APIRouter(prefix="/api", tags=["Invitation"])

# 邀请 token：sk 之外的独立前缀，避免与 API Key 混淆
_INVITE_PREFIX = "inv-"
_TOKEN_BYTES = 32


def _generate_token() -> str:
    return f"{_INVITE_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    # 与 DB 列 TIMESTAMP WITHOUT TIME ZONE 一致：用 naive UTC（去掉 tzinfo），
    # 避免 asyncpg "can't subtract offset-naive and offset-aware datetimes"。
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================
# 请求/响应模型
# ============================================================


class CreateInvitationRequest(BaseModel):
    scope: str = Field(..., description="create_tenant | create_user")
    expires_in_hours: int = Field(..., ge=1, le=24 * 30, description="有效期（小时），1h–30d")
    max_uses: int | None = Field(default=None, ge=1, description="可用次数；留空=有效期内不限次")
    role_names: list[str] = Field(default_factory=lambda: [BuiltinRoleEnum.USER.value],
                                  description="create_user 时新用户预设角色")


class InvitationCreateResponse(BaseModel):
    id: str
    token: str = Field(description="完整邀请 token，仅创建时返回一次")
    scope: str
    tenant_id: str | None
    expires_at: str
    max_uses: int | None


class InvitationItem(BaseModel):
    id: str
    token: str | None = None  # 明文 token，供列表随时复制（仅本端点返回）
    scope: str
    tenant_id: str | None
    role_names: list[str] | None
    max_uses: int | None
    used_count: int
    expires_at: str
    is_active: bool
    created_by_username: str | None
    created_at: str


class InvitationInfo(BaseModel):
    """接受页展示用（免登录）。不暴露敏感信息。"""
    scope: str
    tenant_name: str | None
    valid: bool


class AcceptInvitationRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    # create_tenant 时必填：新租户名
    tenant_name: str | None = Field(default=None)


# ============================================================
# 辅助
# ============================================================


def _invite_status_active(inv: Invitation) -> bool:
    """有效性：is_active + 未过期 + 未用满。"""
    if not inv.is_active:
        return False
    exp = inv.expires_at
    # 统一按 naive UTC 比较（DB 存 naive；若历史数据带 tz 则剥离）
    if exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    if exp < _now():
        return False
    if inv.max_uses is not None and inv.used_count >= inv.max_uses:
        return False
    return True


# ============================================================
# 签发（管理端，allow_api_key=False）
# ============================================================


@router.post("/admin/invitations", response_model=InvitationCreateResponse, status_code=201)
async def create_invitation(
    body: CreateInvitationRequest,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """签发邀请链接。

    create_tenant 仅 Super_Admin；create_user 由 user:manage 签发并锁本租户。
    """
    if body.scope not in (InvitationScopeEnum.CREATE_TENANT.value, InvitationScopeEnum.CREATE_USER.value):
        raise ValidationInputError("scope 仅支持 create_tenant | create_user")

    tenant_id: str | None = None
    role_names: list[str] | None = None

    if body.scope == InvitationScopeEnum.CREATE_TENANT.value:
        # 建租户邀请属平台操作，仅 Super_Admin
        if not (identity.is_super_admin and identity.source == IdentitySourceEnum.JWT):
            raise PermissionDeniedError("仅平台超级管理员可签发建租户邀请")
    else:
        # 建用户邀请：锁签发者租户（Super_Admin 无业务租户，不能签发建用户邀请）
        if identity.tenant_id is None:
            raise PermissionDeniedError("请在具体租户上下文内签发建用户邀请")
        tenant_id = identity.tenant_id
        # 停用租户冻结：不可再签发建用户邀请
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None or not tenant.is_active:
            raise PermissionDeniedError("该租户已停用，数据已冻结，无法签发邀请")
        role_names = body.role_names or [BuiltinRoleEnum.USER.value]
        # 租户管理员不得经邀请分配 admin（管理员）角色
        if not identity.is_super_admin and BuiltinRoleEnum.ADMIN.value in role_names:
            raise PermissionDeniedError("无权经邀请分配管理员(admin)角色")

    raw_token = _generate_token()
    inv_id = str(uuid.uuid4())
    expires_at = _now() + timedelta(hours=body.expires_in_hours)
    db.add(Invitation(
        id=inv_id,
        token_hash=_hash_token(raw_token),
        token_plain=raw_token,  # 保留明文供列表随时复制/重复使用
        scope=body.scope,
        tenant_id=tenant_id,
        role_names=role_names,
        max_uses=body.max_uses,
        used_count=0,
        expires_at=expires_at,
        is_active=True,
        created_by=identity.user_id or "",
        created_by_username=None,
    ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.INVITATION_CREATE,
        target_type="invitation", target_id=inv_id,
        detail={"scope": body.scope, "tenant_id": tenant_id,
                "max_uses": body.max_uses, "expires_at": expires_at.isoformat()},
        request=request,
    )
    await db.commit()
    return InvitationCreateResponse(
        id=inv_id, token=raw_token, scope=body.scope, tenant_id=tenant_id,
        expires_at=expires_at.isoformat(), max_uses=body.max_uses,
    )


@router.get("/admin/invitations", response_model=PageResult[InvitationItem])
async def list_invitations(
    page: int = 1,
    page_size: int = 20,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """列出邀请（超管看全局；租管仅看本租户）。返回明文 token 供随时复制。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    base = select(Invitation)
    count_base = select(func.count(Invitation.id))
    if not identity.is_super_admin:
        base = base.where(Invitation.tenant_id == identity.tenant_id)
        count_base = count_base.where(Invitation.tenant_id == identity.tenant_id)
    total = await db.scalar(count_base) or 0
    offset = (page - 1) * page_size
    rows = (await db.execute(
        base.order_by(Invitation.created_at.desc()).offset(offset).limit(page_size)
    )).scalars().all()
    items = [
        InvitationItem(
            id=r.id, token=r.token_plain, scope=r.scope, tenant_id=r.tenant_id, role_names=r.role_names,
            max_uses=r.max_uses, used_count=r.used_count,
            expires_at=r.expires_at.isoformat() if r.expires_at else "",
            is_active=_invite_status_active(r),
            created_by_username=r.created_by_username,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]
    return PageResult[InvitationItem](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.delete("/admin/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: str,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """吊销邀请（软删除：is_active=False）。租管仅能吊销本租户邀请。"""
    inv = await db.get(Invitation, invitation_id)
    if inv is None:
        raise CrossTenantError()
    if not identity.is_super_admin and inv.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    inv.is_active = False
    add_audit(
        db, actor=identity, action=AuditActionEnum.INVITATION_REVOKE,
        target_type="invitation", target_id=inv.id,
        detail={"scope": inv.scope}, request=request,
    )
    await db.commit()


# ============================================================
# 接受（免登录）
# ============================================================


async def _load_valid_invitation(db: AsyncSession, token: str) -> Invitation:
    inv = (await db.execute(
        select(Invitation).where(Invitation.token_hash == _hash_token(token))
    )).scalar_one_or_none()
    if inv is None or not _invite_status_active(inv):
        # 不区分不存在/已失效，避免枚举
        raise CrossTenantError("邀请无效或已失效")
    return inv


@router.get("/invitations/{token}", response_model=InvitationInfo)
async def get_invitation_info(token: str, db: AsyncSession = Depends(get_db_session)):
    """免登录校验邀请有效性，返回用途与（建用户时）目标租户名供页面展示。"""
    inv = await _load_valid_invitation(db, token)
    tenant_name = None
    if inv.tenant_id:
        tenant = await db.get(Tenant, inv.tenant_id)
        tenant_name = tenant.name if tenant else None
    return InvitationInfo(scope=inv.scope, tenant_name=tenant_name, valid=True)


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str,
    body: AcceptInvitationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    """接受邀请：按 scope 建租户(+租管) 或 建普通用户。受 token scope 严格约束。"""
    inv = await _load_valid_invitation(db, token)
    username = validate_username(body.username)
    validate_password(body.password)

    if inv.scope == InvitationScopeEnum.CREATE_TENANT.value:
        result = await _accept_create_tenant(db, inv, username, body)
    else:
        result = await _accept_create_user(db, inv, username, body)

    # 计数并在用满时失效
    inv.used_count = inv.used_count + 1
    if inv.max_uses is not None and inv.used_count >= inv.max_uses:
        inv.is_active = False
    add_audit(
        db, actor=None, actor_username=username,
        action=AuditActionEnum.INVITATION_ACCEPT,
        target_type="invitation", target_id=inv.id,
        detail={"scope": inv.scope, "created_user": result.get("user_id"),
                "created_tenant": result.get("tenant_id")},
        request=request,
    )
    await db.commit()
    return result


async def _accept_create_tenant(
    db: AsyncSession, inv: Invitation, username: str, body: AcceptInvitationRequest
) -> dict:
    """建租户 + 预置角色 + 该用户成为租户管理员（admin 角色）。"""
    if not body.tenant_name:
        raise ValidationInputError("建租户邀请需提供 tenant_name")
    tenant_name = validate_tenant_name(body.tenant_name)

    # 用户名全局唯一：先校验避免落库触发 500
    dup = await db.scalar(select(func.count(User.id)).where(User.username == username))
    if dup:
        raise PermissionDeniedError("用户名已存在")

    tenant_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=tenant_name, tenant_type=TenantTypeEnum.BUSINESS.value, is_active=True))
    await db.flush()

    code_to_id = {
        code: pid for pid, code in (await db.execute(select(Permission.id, Permission.code))).all()
    }
    roles = await ensure_tenant_builtin_roles(db, tenant_id, code_to_id)
    await db.flush()

    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=await hash_password(body.password),
        is_active=True, must_change_password=False,  # 自助设置的口令，无需再强制改
    ))
    db.add(UserRole(user_id=user_id, role_id=roles[BuiltinRoleEnum.ADMIN.value]))
    return {"detail": "租户与管理员已创建", "tenant_id": tenant_id, "user_id": user_id}


async def _accept_create_user(
    db: AsyncSession, inv: Invitation, username: str, body: AcceptInvitationRequest
) -> dict:
    """在邀请绑定的租户内建普通用户（按预设角色）。"""
    tenant = await db.get(Tenant, inv.tenant_id)
    if tenant is None or not tenant.is_active:
        raise PermissionDeniedError("目标租户不可用")

    exists = await db.scalar(
        select(func.count(User.id)).where(User.username == username)
    )
    if exists:
        raise PermissionDeniedError("用户名已存在")

    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=inv.tenant_id, username=username,
        password_hash=await hash_password(body.password),
        is_active=True, must_change_password=False,
    ))
    # 预设角色（限本租户内存在的角色名）
    for name in (inv.role_names or [BuiltinRoleEnum.USER.value]):
        role = (await db.execute(
            select(Role).where(Role.tenant_id == inv.tenant_id, Role.name == name)
        )).scalar_one_or_none()
        if role is not None:
            db.add(UserRole(user_id=user_id, role_id=role.id))
    return {"detail": "用户已创建", "tenant_id": inv.tenant_id, "user_id": user_id}
