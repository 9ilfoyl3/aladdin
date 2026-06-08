"""管理路由（tenant-rbac-refactor）：平台级（Super_Admin）与租户级（Tenant_Admin）。

平台级（require_platform：op_level=platform、禁 api_key、仅 Super_Admin/JWT）：
  - 租户 CRUD / 启停 / 维护展示资料
  - 为新租户创建初始 Tenant_Admin、补充 Tenant_Admin（直接写 role=admin）
  - 跨租户兜底：列出指定租户用户、重置任意管理员口令、启停任意用户

租户级（require_tenant_admin：admin 或 super_admin、禁 api_key）：
  - 本租户用户 CRUD / 启停 / 口令重置 / 转移知识库
  - 审计日志查询（租管限本租户，超管全局）

本次重构已删除自定义角色 / 权限点端点（角色 CRUD、权限点字典、用户角色分配）：
租户内权限改由「固定角色（admin/member）+ 归属轴」判定。建用户固定写 role=member；
建初始/补充 Tenant_Admin 写 role=admin。越租户操作一律 404（存在性非泄露）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_platform, require_tenant_admin
from app.api.errors import CrossTenantError, PermissionDeniedError
from app.auth.audit import add_audit
from app.auth.constants import (
    AuditActionEnum,
    TenantRoleEnum,
    TenantTypeEnum,
)
from app.auth.identity import IdentityContext
from app.auth.password import hash_password
from app.auth.validators import (
    validate_avatar,
    validate_description,
    validate_password,
    validate_tenant_name,
    validate_username,
)
from app.schema.api import PageResult
from app.schema.db import (
    AuditLog,
    KnowledgeBase,
    Tenant,
    User,
)

router = APIRouter(prefix="/api/admin", tags=["Admin"])

# 临时口令生成（重置/建号下发一次）；强制改密保证用户首次登录必须改。
def _temp_password() -> str:
    return "Tmp-" + uuid.uuid4().hex[:12]


# ============================================================
# 平台级：租户管理（Super_Admin）
# ============================================================


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1)
    admin_username: str = Field(..., min_length=1, description="初始租户管理员用户名")
    admin_password: str | None = Field(default=None, description="不填则生成临时口令")
    description: str | None = Field(default=None, description="租户简介（企业组织介绍）")
    avatar: str | None = Field(default=None, description="租户头像 data URL（≤2MB）")


class TenantResponse(BaseModel):
    id: str
    name: str
    tenant_type: str
    is_active: bool
    description: str | None = None
    avatar: str | None = None


class TenantCreateResponse(TenantResponse):
    admin_username: str
    admin_temp_password: str | None = None


@router.post("/tenants", response_model=TenantCreateResponse, status_code=201)
async def create_tenant(
    body: TenantCreate,
    request: Request,
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """创建租户 + 初始 Tenant_Admin（固定角色 role=admin）。"""
    name = validate_tenant_name(body.name)
    admin_username = validate_username(body.admin_username)
    if body.admin_password is not None:
        validate_password(body.admin_password)
    description = validate_description(body.description)
    avatar = validate_avatar(body.avatar)
    # 用户名全局唯一：建租户前先校验初始管理员用户名未被占用（避免落库触发 500）
    dup = await db.scalar(
        select(func.count(User.id)).where(User.username == admin_username)
    )
    if dup:
        raise PermissionDeniedError("用户名已存在")
    tenant_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=name, tenant_type=TenantTypeEnum.BUSINESS.value,
                  is_active=True, description=description, avatar=avatar))
    await db.flush()

    # 初始 Tenant_Admin：固定角色直接写 role=admin（不再经角色关联表）
    is_generated = body.admin_password is None
    temp_pwd = body.admin_password or _temp_password()
    admin_id = str(uuid.uuid4())
    admin_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=admin_id, tenant_id=tenant_id, username=admin_username,
        password_hash=admin_pwd_hash, is_active=True,
        role=TenantRoleEnum.ADMIN.value,
        must_change_password=True,  # 强制首次改密
        # 生成的临时口令保留明文，供超管在该管理员首登改密前再次查看/复制
        temp_password=temp_pwd if is_generated else None,
    ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.TENANT_CREATE,
        target_type="tenant", target_id=tenant_id, target_name=name,
        detail={"admin_username": admin_username}, request=request,
    )
    await db.commit()

    return TenantCreateResponse(
        id=tenant_id, name=name, tenant_type=TenantTypeEnum.BUSINESS.value,
        is_active=True, admin_username=admin_username,
        admin_temp_password=None if body.admin_password else temp_pwd,
        description=description, avatar=avatar,
    )


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    rows = (await db.execute(select(Tenant).order_by(Tenant.name))).scalars().all()
    return [
        TenantResponse(id=t.id, name=t.name, tenant_type=t.tenant_type, is_active=t.is_active,
                       description=t.description, avatar=t.avatar)
        for t in rows
    ]


class TenantToggle(BaseModel):
    is_active: bool


class TenantProfileUpdate(BaseModel):
    """超管维护租户（企业组织）的展示资料：名称/简介/头像。均可单独提交（None=不改，""=清除简介/头像）。"""
    name: str | None = Field(default=None)
    description: str | None = Field(default=None)
    avatar: str | None = Field(default=None)


@router.put("/tenants/{tenant_id}/profile", response_model=TenantResponse)
async def update_tenant_profile(
    tenant_id: str,
    body: TenantProfileUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """平台级：超管维护租户（企业组织）的名称/简介/头像。租户内成员不可改这些。"""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise CrossTenantError()
    if body.name is not None:
        tenant.name = validate_tenant_name(body.name)
    if body.description is not None:
        tenant.description = validate_description(body.description)
    if body.avatar is not None:
        tenant.avatar = validate_avatar(body.avatar)
    add_audit(
        db, actor=identity, action=AuditActionEnum.TENANT_UPDATE_PROFILE,
        target_type="tenant", target_id=tenant.id, target_name=tenant.name,
        request=request,
    )
    await db.commit()
    return TenantResponse(id=tenant.id, name=tenant.name, tenant_type=tenant.tenant_type,
                          is_active=tenant.is_active, description=tenant.description, avatar=tenant.avatar)


@router.put("/tenants/{tenant_id}/status", response_model=TenantResponse)
async def set_tenant_status(
    tenant_id: str,
    body: TenantToggle,
    request: Request,
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """启停租户（停用 != 删除，保留全部数据）。"""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise CrossTenantError()
    tenant.is_active = body.is_active
    add_audit(
        db, actor=identity, action=AuditActionEnum.TENANT_SET_STATUS,
        target_type="tenant", target_id=tenant.id, target_name=tenant.name,
        detail={"is_active": body.is_active}, request=request,
    )
    await db.commit()
    return TenantResponse(id=tenant.id, name=tenant.name, tenant_type=tenant.tenant_type,
                          is_active=tenant.is_active, description=tenant.description, avatar=tenant.avatar)


# ============================================================
# 租户级：用户管理（require_tenant_admin）+ 跨租户兜底（Super_Admin）
# ============================================================


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str | None = Field(default=None, description="不填则生成临时口令并强制改密")
    description: str | None = Field(default=None, description="可选：用户简介")
    avatar: str | None = Field(default=None, description="可选：用户头像 data URL（≤2MB）")


class UserResponse(BaseModel):
    id: str
    tenant_id: str | None
    username: str
    is_active: bool
    must_change_password: bool
    # 固定角色：admin / member；Super_Admin 为 None
    role: str | None = None
    # 首登改密前可见的临时口令明文（改密后为 None）
    temp_password: str | None = None
    # 简介与头像（列表/详情展示）
    description: str | None = None
    avatar: str | None = None


class UserCreateResponse(UserResponse):
    pass


def _require_same_tenant(identity: IdentityContext, target_tenant_id: str | None) -> None:
    """非 Super_Admin 只能操作本租户对象；越租户 404（不泄露存在性）。"""
    if identity.is_super_admin:
        return
    if target_tenant_id != identity.tenant_id:
        raise CrossTenantError()


def _forbid_self(identity: IdentityContext, target_user_id: str) -> None:
    """管理员不得对自己执行危险管理操作（停用/重置/转移），避免自锁或自降权。

    Super_Admin 做跨租户兜底时操作的恒是他人（其自身无业务租户），故仅对租户管理员生效。
    自身改密走"修改口令"入口，不经这些端点。
    """
    if identity.user_id is not None and identity.user_id == target_user_id:
        raise PermissionDeniedError("不能对自己执行该操作")


async def _assert_tenant_active(db: AsyncSession, tenant_id: str | None) -> None:
    """停用租户 = 数据冻结、只读：禁止对其下对象做任何写操作（新增管理员/建用户/
    启停/重置口令/转移知识库等），仅允许查看。要恢复写入须先重新启用该租户。

    租户启停本身（set_tenant_status）不经此校验，否则无法再启用。
    """
    if tenant_id is None:
        return
    tenant = await db.get(Tenant, tenant_id)
    if tenant is not None and not tenant.is_active:
        raise PermissionDeniedError("该租户已停用，数据已冻结，仅可查看，无法修改")


@router.get("/tenants/{tenant_id}/users", response_model=list[UserResponse])
async def list_tenant_users(
    tenant_id: str,
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """平台级：列出指定租户下的全部用户（供超管下钻做兜底口令重置 / 启停）。

    仅 Super_Admin（platform）。返回元数据，不含口令哈希。
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise CrossTenantError()
    rows = (await db.execute(
        select(User).where(User.tenant_id == tenant_id).order_by(User.username)
    )).scalars().all()
    return [_user_resp(u) for u in rows]


class TenantAdminCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str | None = Field(default=None, description="不填则生成临时口令并强制改密")


@router.post("/tenants/{tenant_id}/admins", response_model=UserCreateResponse, status_code=201)
async def create_tenant_admin(
    tenant_id: str,
    body: TenantAdminCreate,
    request: Request,
    identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """平台级：在指定租户内**新增一个租户管理员**（固定角色 role=admin）。

    仅 Super_Admin。用于租户原管理员被停用/不可用时补充管理员，避免租户无人可管。
    用户名全局唯一；生成临时口令时强制首登改密、明文保留至改密前。
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise CrossTenantError()
    if not tenant.is_active:
        raise PermissionDeniedError("该租户已停用，数据已冻结，仅可查看，无法新增管理员")
    username = validate_username(body.username)
    if body.password is not None:
        validate_password(body.password)
    dup = await db.scalar(select(func.count(User.id)).where(User.username == username))
    if dup:
        raise PermissionDeniedError("用户名已存在")

    is_generated = body.password is None
    temp_pwd = body.password or _temp_password()
    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=await hash_password(temp_pwd), is_active=True,
        role=TenantRoleEnum.ADMIN.value,
        must_change_password=is_generated,
        temp_password=temp_pwd if is_generated else None,
    ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_CREATE,
        target_type="user", target_id=user_id, target_name=username,
        detail={"tenant_id": tenant_id, "role": TenantRoleEnum.ADMIN.value,
                "by_super_admin": True}, request=request,
    )
    await db.commit()
    return UserCreateResponse(
        id=user_id, tenant_id=tenant_id, username=username, is_active=True,
        must_change_password=is_generated, role=TenantRoleEnum.ADMIN.value,
        temp_password=temp_pwd if is_generated else None,
    )


@router.get("/users", response_model=PageResult[UserResponse])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, description="按用户名模糊搜索"),
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """分页列出本租户用户（Super_Admin 列全部），支持按用户名模糊搜索。仅元数据。"""
    base = select(User)
    count_base = select(func.count(User.id))
    if not identity.is_super_admin:
        base = base.where(User.tenant_id == identity.tenant_id)
        count_base = count_base.where(User.tenant_id == identity.tenant_id)
    if q:
        like = f"%{q.strip()}%"
        base = base.where(User.username.ilike(like))
        count_base = count_base.where(User.username.ilike(like))

    total = await db.scalar(count_base) or 0
    offset = (page - 1) * page_size
    rows = (await db.execute(
        base.order_by(User.username).offset(offset).limit(page_size)
    )).scalars().all()
    items = [_user_resp(u) for u in rows]
    return PageResult[UserResponse](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.post("/users", response_model=UserCreateResponse, status_code=201)
async def create_user(
    body: UserCreate,
    request: Request,
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """在管理员本租户内创建用户（固定角色 member）。

    租户管理员建号一律为 member（R2.1/R2.6）；UserCreate 不暴露角色字段，
    租管在本端点无法请求把用户设为 admin（设立 admin 仅经平台流程，R2.2/R14.5）。
    """
    tenant_id = identity.tenant_id
    if tenant_id is None:
        # Super_Admin 不属于业务租户，建号须经平台流程指定租户，这里拒绝歧义路径
        raise PermissionDeniedError("请在具体租户上下文内创建用户")

    username = validate_username(body.username)
    if body.password is not None:
        validate_password(body.password)
    description = validate_description(body.description)
    avatar = validate_avatar(body.avatar)

    await _assert_tenant_active(db, tenant_id)

    exists = await db.scalar(
        select(func.count(User.id)).where(User.username == username)
    )
    if exists:
        raise PermissionDeniedError("用户名已存在")

    is_generated = body.password is None
    temp_pwd = body.password or _temp_password()
    user_id = str(uuid.uuid4())
    user_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=user_pwd_hash, is_active=True,
        role=TenantRoleEnum.MEMBER.value,  # 租管建号固定 member
        must_change_password=is_generated,  # 生成临时口令则强制改密
        # 生成的临时口令保留明文供管理员在用户首登改密前再次查看；自带口令不保留
        temp_password=temp_pwd if is_generated else None,
        description=description, avatar=avatar,
    ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_CREATE,
        target_type="user", target_id=user_id, target_name=username,
        detail={"role": TenantRoleEnum.MEMBER.value}, request=request,
    )
    await db.commit()
    return UserCreateResponse(
        id=user_id, tenant_id=tenant_id, username=username, is_active=True,
        must_change_password=is_generated, role=TenantRoleEnum.MEMBER.value,
        temp_password=temp_pwd if is_generated else None,
        description=description, avatar=avatar,
    )


class UserStatusToggle(BaseModel):
    is_active: bool


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def set_user_status(
    user_id: str,
    body: UserStatusToggle,
    request: Request,
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """启停用户（停用 != 删除）。停用时自增 token_version 使其已签发 JWT 失效。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    _forbid_self(identity, user_id)
    await _assert_tenant_active(db, user.tenant_id)
    user.is_active = body.is_active
    if not body.is_active:
        user.token_version = user.token_version + 1  # 已签发 JWT 失效
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_SET_STATUS,
        target_type="user", target_id=user.id, target_name=user.username,
        detail={"is_active": body.is_active}, request=request,
    )
    await db.commit()
    return _user_resp(user)


@router.post("/users/{user_id}/reset-password", response_model=UserCreateResponse)
async def reset_password(
    user_id: str,
    request: Request,
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """管理员重置用户口令：生成临时口令 + 强制改密 + 失效旧 JWT。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    _forbid_self(identity, user_id)
    await _assert_tenant_active(db, user.tenant_id)
    temp_pwd = _temp_password()
    user.password_hash = await hash_password(temp_pwd)
    user.must_change_password = True
    user.temp_password = temp_pwd  # 保留明文供首登改密前再次查看
    user.token_version = user.token_version + 1
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_RESET_PASSWORD,
        target_type="user", target_id=user.id, target_name=user.username,
        request=request,
    )
    await db.commit()
    return UserCreateResponse(
        id=user.id, tenant_id=user.tenant_id, username=user.username,
        is_active=user.is_active, must_change_password=True, role=user.role,
        temp_password=temp_pwd,
    )


class TransferKbRequest(BaseModel):
    target_user_id: str = Field(..., min_length=1, description="接收知识库的同租户用户")


@router.post("/users/{user_id}/transfer-knowledge-bases")
async def transfer_knowledge_bases(
    user_id: str,
    body: TransferKbRequest,
    request: Request,
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """把源用户名下全部知识库的归属转移给同租户内另一启用用户。

    轻操作：仅改 owner_user_id（不搬文档/向量/chunks，kb_id 不变 → Milvus 不动），
    同步单事务即时完成。源/目标须同属操作者租户、目标须启用、源≠目标。
    """
    from sqlalchemy import update as sql_update

    if user_id == body.target_user_id:
        raise PermissionDeniedError("源用户与目标用户不能相同")

    source = await db.get(User, user_id)
    if source is None:
        raise CrossTenantError()
    _require_same_tenant(identity, source.tenant_id)
    await _assert_tenant_active(db, source.tenant_id)
    # 转移=资产交接，要求源用户先停用：避免在用账号被悄悄搬空，且明确交接语义
    if source.is_active:
        raise PermissionDeniedError("请先停用该用户，再转移其名下知识库")

    target = await db.get(User, body.target_user_id)
    if target is None:
        raise CrossTenantError()
    _require_same_tenant(identity, target.tenant_id)
    # 双方必须同租户（防跨租户转移破坏隔离）
    if source.tenant_id != target.tenant_id:
        raise CrossTenantError()
    if not target.is_active:
        raise PermissionDeniedError("目标用户已停用，不能作为接收人")

    # 统计并改归属（限定同租户，双保险）
    count = await db.scalar(
        select(func.count(KnowledgeBase.id)).where(
            KnowledgeBase.owner_user_id == user_id,
            KnowledgeBase.tenant_id == source.tenant_id,
        )
    ) or 0
    await db.execute(
        sql_update(KnowledgeBase)
        .where(
            KnowledgeBase.owner_user_id == user_id,
            KnowledgeBase.tenant_id == source.tenant_id,
        )
        .values(owner_user_id=body.target_user_id)
    )
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_TRANSFER_KB,
        target_type="user", target_id=user_id, target_name=source.username,
        detail={"target_user_id": body.target_user_id,
                "target_username": target.username, "transferred_count": count},
        request=request,
    )
    await db.commit()
    return {"detail": "知识库归属已转移", "transferred_count": count,
            "from_user_id": user_id, "to_user_id": body.target_user_id}


# ============================================================
# 审计日志查询（require_tenant_admin：租管限本租户，超管全局）
# ============================================================


class AuditLogItem(BaseModel):
    id: str
    actor_user_id: str | None
    actor_username: str | None
    actor_tenant_id: str | None
    actor_is_super_admin: bool
    # 操作者写入时刻的固定角色快照（admin/member/None）
    actor_role: str | None
    action: str
    target_type: str | None
    target_id: str | None
    target_name: str | None
    detail: dict | None
    result: str
    ip: str | None
    created_at: str


@router.get("/audit-logs", response_model=PageResult[AuditLogItem])
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: str | None = Query(None, description="按动作过滤"),
    actor: str | None = Query(None, description="按操作者用户名模糊过滤"),
    identity: IdentityContext = Depends(require_tenant_admin()),
    db: AsyncSession = Depends(get_db_session),
):
    """审计日志（只读）。超管看全局；租管仅看本租户操作者产生的记录。"""
    base = select(AuditLog)
    count_base = select(func.count(AuditLog.id))
    if not identity.is_super_admin:
        base = base.where(AuditLog.actor_tenant_id == identity.tenant_id)
        count_base = count_base.where(AuditLog.actor_tenant_id == identity.tenant_id)
    if action:
        base = base.where(AuditLog.action == action)
        count_base = count_base.where(AuditLog.action == action)
    if actor:
        like = f"%{actor.strip()}%"
        base = base.where(AuditLog.actor_username.ilike(like))
        count_base = count_base.where(AuditLog.actor_username.ilike(like))

    total = await db.scalar(count_base) or 0
    offset = (page - 1) * page_size
    rows = (await db.execute(
        base.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
    )).scalars().all()
    items = [
        AuditLogItem(
            id=r.id, actor_user_id=r.actor_user_id, actor_username=r.actor_username,
            actor_tenant_id=r.actor_tenant_id, actor_is_super_admin=r.actor_is_super_admin,
            actor_role=r.actor_role,
            action=r.action, target_type=r.target_type, target_id=r.target_id,
            target_name=r.target_name, detail=r.detail, result=r.result, ip=r.ip,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]
    return PageResult[AuditLogItem](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


# ============================================================
# 辅助
# ============================================================


def _user_resp(user: User) -> UserResponse:
    return UserResponse(
        id=user.id, tenant_id=user.tenant_id, username=user.username,
        is_active=user.is_active, must_change_password=user.must_change_password,
        role=user.role,
        # 仅在用户尚未改密时返回临时口令明文（改密后该字段已被清空）
        temp_password=user.temp_password if user.must_change_password else None,
        description=user.description, avatar=user.avatar,
    )
