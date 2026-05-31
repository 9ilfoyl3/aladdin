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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import CrossTenantError, PermissionDeniedError
from app.auth.audit import add_audit
from app.auth.bootstrap import ensure_tenant_builtin_roles
from app.auth.constants import (
    AuditActionEnum,
    BuiltinRoleEnum,
    PermissionEnum,
    PERMISSION_TYPE_LABELS,
    PLATFORM_PERMISSIONS,
    TenantTypeEnum,
    permission_label,
)
from app.auth.identity import IdentityContext, OperationLevelEnum
from app.auth.password import hash_password
from app.auth.permission_resolver import resolve_role_ids
from app.auth.validators import (
    validate_password,
    validate_role_name,
    validate_tenant_name,
    validate_username,
)
from app.schema.api import PageResult
from app.schema.db import (
    AuditLog,
    KnowledgeBase,
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
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """创建租户 + 预置 admin/user 角色 + 初始 Tenant_Admin（分配 admin 角色）。"""
    name = validate_tenant_name(body.name)
    admin_username = validate_username(body.admin_username)
    if body.admin_password is not None:
        validate_password(body.admin_password)
    # 用户名全局唯一：建租户前先校验初始管理员用户名未被占用（避免落库触发 500）
    dup = await db.scalar(
        select(func.count(User.id)).where(User.username == admin_username)
    )
    if dup:
        raise PermissionDeniedError("用户名已存在")
    tenant_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=name, tenant_type=TenantTypeEnum.BUSINESS.value, is_active=True))
    await db.flush()

    # 预置该租户内置角色
    code_to_id = {
        code: pid
        for pid, code in (await db.execute(select(Permission.id, Permission.code))).all()
    }
    roles = await ensure_tenant_builtin_roles(db, tenant_id, code_to_id)
    await db.flush()

    # 初始 Tenant_Admin
    is_generated = body.admin_password is None
    temp_pwd = body.admin_password or _temp_password()
    admin_id = str(uuid.uuid4())
    admin_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=admin_id, tenant_id=tenant_id, username=admin_username,
        password_hash=admin_pwd_hash, is_active=True,
        must_change_password=True,  # 强制首次改密
        # 生成的临时口令保留明文，供超管在该管理员首登改密前再次查看/复制
        temp_password=temp_pwd if is_generated else None,
    ))
    db.add(UserRole(user_id=admin_id, role_id=roles[BuiltinRoleEnum.ADMIN.value]))
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
    request: Request,
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
    add_audit(
        db, actor=identity, action=AuditActionEnum.TENANT_SET_STATUS,
        target_type="tenant", target_id=tenant.id, target_name=tenant.name,
        detail={"is_active": body.is_active}, request=request,
    )
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
    # 角色名列表（供列表展示）
    role_names: list[str] = Field(default_factory=list)
    # 首登改密前可见的临时口令明文（改密后为 None）
    temp_password: str | None = None


class UserCreateResponse(UserResponse):
    pass


def _require_same_tenant(identity: IdentityContext, target_tenant_id: str | None) -> None:
    """非 Super_Admin 只能操作本租户对象；越租户 404（不泄露存在性）。"""
    if identity.is_super_admin:
        return
    if target_tenant_id != identity.tenant_id:
        raise CrossTenantError()


def _forbid_self(identity: IdentityContext, target_user_id: str) -> None:
    """管理员不得对自己执行危险管理操作（停用/重置/改角色/转移），避免自锁或自降权。

    Super_Admin 做跨租户兜底时操作的恒是他人（其自身无业务租户），故仅对租户管理员生效。
    自身改密走"修改口令"入口，不经这些端点。
    """
    if identity.user_id is not None and identity.user_id == target_user_id:
        raise PermissionDeniedError("不能对自己执行该操作")


async def _assert_tenant_active(db: AsyncSession, tenant_id: str | None) -> None:
    """停用租户 = 数据冻结、只读：禁止对其下对象做任何写操作（新增管理员/建用户/
    启停/重置口令/改角色/转移知识库等），仅允许查看。要恢复写入须先重新启用该租户。

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
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
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
    role_map = await _role_names_for_users(db, [u.id for u in rows])
    return [_user_resp(u, role_map.get(u.id, [])) for u in rows]


class TenantAdminCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str | None = Field(default=None, description="不填则生成临时口令并强制改密")


@router.post("/tenants/{tenant_id}/admins", response_model=UserCreateResponse, status_code=201)
async def create_tenant_admin(
    tenant_id: str,
    body: TenantAdminCreate,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.TENANT_MANAGE.value}, **_PLATFORM)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """平台级：在指定租户内**新增一个租户管理员**（admin 角色）。

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

    # 确保该租户的内置 admin/user 角色存在（历史租户兜底）
    code_to_id = {
        code: pid for pid, code in (await db.execute(select(Permission.id, Permission.code))).all()
    }
    roles = await ensure_tenant_builtin_roles(db, tenant_id, code_to_id)

    is_generated = body.password is None
    temp_pwd = body.password or _temp_password()
    user_id = str(uuid.uuid4())
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=await hash_password(temp_pwd), is_active=True,
        must_change_password=is_generated,
        temp_password=temp_pwd if is_generated else None,
    ))
    db.add(UserRole(user_id=user_id, role_id=roles[BuiltinRoleEnum.ADMIN.value]))
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_CREATE,
        target_type="user", target_id=user_id, target_name=username,
        detail={"tenant_id": tenant_id, "role_names": [BuiltinRoleEnum.ADMIN.value],
                "by_super_admin": True}, request=request,
    )
    await db.commit()
    return UserCreateResponse(
        id=user_id, tenant_id=tenant_id, username=username, is_active=True,
        must_change_password=is_generated, role_names=[BuiltinRoleEnum.ADMIN.value],
        temp_password=temp_pwd if is_generated else None,
    )


def _guard_assignable_permissions(identity: IdentityContext, codes: list[str]) -> None:
    """防越权提权：

    - 平台级权限点（tenant:manage 等）绝不允许写入任何租户角色（仅 Super_Admin 平台身份持有）。
    - 非 Super_Admin 只能把"自己当前已拥有"的权限点分配给角色，不能凭空授予自己没有的能力。
    Super_Admin 经此放行（其本就持平台全权，且通常不在具体租户内编辑角色）。
    """
    requested = set(codes)
    # 平台级权限点永不进租户角色
    platform_in_req = requested & set(PLATFORM_PERMISSIONS)
    if platform_in_req:
        raise PermissionDeniedError(
            f"不可将平台级权限点分配给租户角色：{sorted(platform_in_req)}"
        )
    if identity.is_super_admin:
        return
    owned = set(identity.effective_permissions or frozenset())
    escalated = requested - owned
    if escalated:
        raise PermissionDeniedError(
            f"不能分配你自身不具备的权限点：{sorted(escalated)}"
        )


@router.get("/users", response_model=PageResult[UserResponse])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, description="按用户名模糊搜索"),
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
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
    role_map = await _role_names_for_users(db, [u.id for u in rows])
    items = [_user_resp(u, role_map.get(u.id, [])) for u in rows]
    return PageResult[UserResponse](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.get("/users/{user_id}/roles")
async def get_user_roles(
    user_id: str,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """查询某用户当前的角色 id 列表。"""
    user = await db.get(User, user_id)
    if user is None:
        raise CrossTenantError()
    _require_same_tenant(identity, user.tenant_id)
    role_ids = (await db.execute(
        select(UserRole.role_id).where(UserRole.user_id == user_id)
    )).scalars().all()
    return {"user_id": user_id, "role_ids": list(role_ids)}


@router.post("/users", response_model=UserCreateResponse, status_code=201)
async def create_user(
    body: UserCreate,
    request: Request,
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

    username = validate_username(body.username)
    if body.password is not None:
        validate_password(body.password)

    await _assert_tenant_active(db, tenant_id)

    exists = await db.scalar(
        select(func.count(User.id)).where(User.username == username)
    )
    if exists:
        raise PermissionDeniedError("用户名已存在")

    # 角色：不选则兜底为 user；租户管理员不得分配 admin（管理员）角色
    role_names = body.role_names or [BuiltinRoleEnum.USER.value]
    _assert_can_assign_roles(identity, role_names=role_names)

    is_generated = body.password is None
    temp_pwd = body.password or _temp_password()
    user_id = str(uuid.uuid4())
    user_pwd_hash = await hash_password(temp_pwd)
    db.add(User(
        id=user_id, tenant_id=tenant_id, username=username,
        password_hash=user_pwd_hash, is_active=True,
        must_change_password=is_generated,  # 生成临时口令则强制改密
        # 生成的临时口令保留明文供管理员在用户首登改密前再次查看；自带口令不保留
        temp_password=temp_pwd if is_generated else None,
    ))
    await _assign_roles(db, tenant_id, user_id, role_names)
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_CREATE,
        target_type="user", target_id=user_id, target_name=username,
        detail={"role_names": role_names}, request=request,
    )
    await db.commit()
    return UserCreateResponse(
        id=user_id, tenant_id=tenant_id, username=username, is_active=True,
        must_change_password=is_generated, role_names=role_names,
        temp_password=temp_pwd if is_generated else None,
    )


class UserStatusToggle(BaseModel):
    is_active: bool


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def set_user_status(
    user_id: str,
    body: UserStatusToggle,
    request: Request,
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
        is_active=user.is_active, must_change_password=True, temp_password=temp_pwd,
    )


class TransferKbRequest(BaseModel):
    target_user_id: str = Field(..., min_length=1, description="接收知识库的同租户用户")


@router.post("/users/{user_id}/transfer-knowledge-bases")
async def transfer_knowledge_bases(
    user_id: str,
    body: TransferKbRequest,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
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


class PermissionDictItem(BaseModel):
    code: str
    type: str
    # 中文展示名（对标具体页面/动作/能力）+ 类型中文名，供前端直观呈现
    label: str
    type_label: str


@router.get("/permissions", response_model=list[PermissionDictItem])
async def list_permission_dict(
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """权限点字典（全部 code + type + 中文名），供角色编辑界面挑选。租户无关、全局一致。"""
    rows = (await db.execute(
        select(Permission.code, Permission.type).order_by(Permission.type, Permission.code)
    )).all()
    return [
        PermissionDictItem(
            code=c, type=t,
            label=permission_label(c),
            type_label=PERMISSION_TYPE_LABELS.get(t, t),
        )
        for c, t in rows
    ]


@router.post("/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    body: RoleCreate,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """在本租户内创建自定义角色并分配权限点（仅本租户内存在与生效）。"""
    tenant_id = identity.tenant_id
    if tenant_id is None:
        raise PermissionDeniedError("请在具体租户上下文内创建角色")
    await _assert_tenant_active(db, tenant_id)
    name = validate_role_name(body.name)
    exists = await db.scalar(
        select(func.count(Role.id)).where(Role.tenant_id == tenant_id, Role.name == name)
    )
    if exists:
        raise PermissionDeniedError("角色名已存在")
    _guard_assignable_permissions(identity, body.permission_codes)
    role_id = str(uuid.uuid4())
    db.add(Role(id=role_id, tenant_id=tenant_id, name=name, is_builtin=False, description=body.description))
    await _set_role_permissions(db, role_id, body.permission_codes)
    add_audit(
        db, actor=identity, action=AuditActionEnum.ROLE_CREATE,
        target_type="role", target_id=role_id, target_name=name,
        detail={"permission_codes": body.permission_codes}, request=request,
    )
    await db.commit()
    return RoleResponse(id=role_id, tenant_id=tenant_id, name=name, is_builtin=False,
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
    request: Request,
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
    await _assert_tenant_active(db, role.tenant_id)
    _guard_assignable_permissions(identity, body.permission_codes)
    await _set_role_permissions(db, role_id, body.permission_codes, replace=True)
    add_audit(
        db, actor=identity, action=AuditActionEnum.ROLE_SET_PERMISSIONS,
        target_type="role", target_id=role.id, target_name=role.name,
        detail={"permission_codes": body.permission_codes}, request=request,
    )
    await db.commit()
    return RoleResponse(id=role.id, tenant_id=role.tenant_id, name=role.name,
                        is_builtin=role.is_builtin, permission_codes=body.permission_codes)


class UserRolesUpdate(BaseModel):
    role_ids: list[str]


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: str,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.ROLE_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """删除自定义角色（内置 admin/user 不可删）。同租户校验，越租户 404。"""
    from sqlalchemy import delete as sql_delete

    role = await db.get(Role, role_id)
    if role is None:
        raise CrossTenantError()
    _require_same_tenant(identity, role.tenant_id)
    await _assert_tenant_active(db, role.tenant_id)
    if role.is_builtin:
        raise PermissionDeniedError("内置角色不可删除")
    role_name = role.name
    await db.execute(sql_delete(RolePermission).where(RolePermission.role_id == role_id))
    await db.execute(sql_delete(UserRole).where(UserRole.role_id == role_id))
    await db.execute(sql_delete(Role).where(Role.id == role_id))
    add_audit(
        db, actor=identity, action=AuditActionEnum.ROLE_DELETE,
        target_type="role", target_id=role_id, target_name=role_name, request=request,
    )
    await db.commit()


@router.put("/users/{user_id}/roles")
async def set_user_roles(
    user_id: str,
    body: UserRolesUpdate,
    request: Request,
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
    _forbid_self(identity, user_id)
    await _assert_tenant_active(db, user.tenant_id)
    # 校验角色同租户，并收集角色对象用于 admin 角色分配守卫
    role_objs: list[Role] = []
    for rid in body.role_ids:
        role = await db.get(Role, rid)
        if role is None or role.tenant_id != user.tenant_id:
            raise CrossTenantError()
        role_objs.append(role)
    # 租户管理员不得分配/移交 admin 角色（仅 Super_Admin 可设租户管理员）
    _assert_can_assign_roles(identity, role_objs=role_objs)
    # 清空重设
    existing = (await db.execute(select(UserRole).where(UserRole.user_id == user_id))).scalars().all()
    for ur in existing:
        await db.delete(ur)
    for rid in body.role_ids:
        db.add(UserRole(user_id=user_id, role_id=rid))
    add_audit(
        db, actor=identity, action=AuditActionEnum.USER_SET_ROLES,
        target_type="user", target_id=user.id, target_name=user.username,
        detail={"role_ids": body.role_ids}, request=request,
    )
    await db.commit()
    return {"detail": "已更新用户角色", "role_ids": body.role_ids}


# ============================================================
# 审计日志查询（user:manage 或 role:manage 即可读；租管限本租户，超管全局）
# ============================================================


class AuditLogItem(BaseModel):
    id: str
    actor_user_id: str | None
    actor_username: str | None
    actor_tenant_id: str | None
    actor_is_super_admin: bool
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
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.USER_MANAGE.value}, allow_api_key=False)
    ),
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


def _user_resp(user: User, role_names: list[str] | None = None) -> UserResponse:
    return UserResponse(
        id=user.id, tenant_id=user.tenant_id, username=user.username,
        is_active=user.is_active, must_change_password=user.must_change_password,
        role_names=role_names or [],
        # 仅在用户尚未改密时返回临时口令明文（改密后该字段已被清空）
        temp_password=user.temp_password if user.must_change_password else None,
    )


async def _role_names_for_users(db: AsyncSession, user_ids: list[str]) -> dict[str, list[str]]:
    """批量解析 user_id -> 角色名列表（供列表展示，避免 N+1）。"""
    if not user_ids:
        return {}
    rows = await db.execute(
        select(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
    )
    out: dict[str, list[str]] = {}
    for uid, rname in rows.all():
        out.setdefault(uid, []).append(rname)
    return out


def _assert_can_assign_roles(
    identity: IdentityContext, role_names: list[str] | None = None,
    role_objs: list[Role] | None = None,
) -> None:
    """租户管理员不得分配/移交 admin（管理员）角色——只有 Super_Admin 可设租户管理员。

    role_names: 按名校验（建用户场景）；role_objs: 按角色对象校验（改用户角色场景）。
    """
    if identity.is_super_admin:
        return
    names = set(role_names or [])
    if role_objs:
        names |= {r.name for r in role_objs}
    if BuiltinRoleEnum.ADMIN.value in names:
        raise PermissionDeniedError("无权分配/移交管理员(admin)角色，请联系平台超级管理员")


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
