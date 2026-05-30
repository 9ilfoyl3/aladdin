"""启动引导（tenant-auth）：全新初始化，幂等。不做历史数据迁移。

职责：
- TenantBootstrap：预置权限点字典 + admin/user 内置角色；创建内置 External_User_Tenant
  及其内置管理员(External_User_Builtin_Admin)与内置公共库。
- SuperAdminBootstrap：首次启动且无 Super_Admin 时按环境变量创建，强制改密；
  缺必需环境变量时 fail-fast（禁止默认口令兜底）。

所有创建均幂等（已存在则跳过、不重复创建、不删除已有记录）。API 进程与 Worker
进程都会经 init_db -> bootstrap，故须容忍并发首启。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import (
    ADMINISTRATIVE_PERMISSIONS,
    EXTERNAL_USER_TENANT_ID,
    EXTERNAL_USER_TENANT_NAME,
    PLATFORM_PERMISSIONS,
    USER_ROLE_DEFAULT_PERMISSIONS,
    BuiltinRoleEnum,
    KbVisibilityEnum,
    PermissionEnum,
    PermissionTypeEnum,
    TenantTypeEnum,
)
from app.auth.password import hash_password
from app.config import get_settings
from app.schema.db import (
    KnowledgeBase,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)

logger = logging.getLogger(__name__)

# 内置外部用户管理员与公共库的固定标识（幂等可查）
_EXTERNAL_ADMIN_ID = "user-external-builtin-admin"
_EXTERNAL_ADMIN_USERNAME = "external_admin"
_EXTERNAL_PUBLIC_KB_ID = "kb-external-public"
_EXTERNAL_PUBLIC_KB_NAME = "外部用户公共库"

# 权限点类型推断：按 code 前缀归类（menu:/btn:/其余=api）
def _ptype(code: str) -> str:
    if code.startswith("menu:"):
        return PermissionTypeEnum.MENU.value
    if code.startswith("btn:"):
        return PermissionTypeEnum.BTN.value
    return PermissionTypeEnum.API.value


async def _ensure_permissions(session: AsyncSession) -> dict[str, str]:
    """幂等写入全部权限点字典，返回 code -> permission_id 映射。"""
    existing = {
        code: pid
        for pid, code in (
            await session.execute(select(Permission.id, Permission.code))
        ).all()
    }
    code_to_id: dict[str, str] = dict(existing)
    for perm in PermissionEnum:
        if perm.value in code_to_id:
            continue
        pid = str(uuid.uuid4())
        session.add(Permission(id=pid, code=perm.value, type=_ptype(perm.value)))
        code_to_id[perm.value] = pid
    return code_to_id


async def _ensure_role(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    permission_codes: set[str],
    code_to_id: dict[str, str],
) -> str:
    """幂等创建租户内置角色并赋予权限点；返回 role_id。已存在则补齐缺失的权限点关联。"""
    role = (
        await session.execute(
            select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
        )
    ).scalar_one_or_none()
    if role is None:
        role_id = str(uuid.uuid4())
        session.add(Role(id=role_id, tenant_id=tenant_id, name=name, is_builtin=True))
    else:
        role_id = role.id

    # 已有的关联
    existing_pids = {
        pid
        for (pid,) in (
            await session.execute(
                select(RolePermission.permission_id).where(RolePermission.role_id == role_id)
            )
        ).all()
    }
    for code in permission_codes:
        pid = code_to_id.get(code)
        if pid and pid not in existing_pids:
            session.add(RolePermission(role_id=role_id, permission_id=pid))
    return role_id


async def _admin_permission_codes() -> set[str]:
    """admin 角色权限点 = 全部权限点（含管理/平台/内容/菜单/按钮）。

    注意：admin 持有管理权限点用于 JWT 管理面；API Key 通道由 Guard 另行剥离，
    不影响此处角色定义（角色是数据，通道边界是 Guard 逻辑）。
    """
    return {p.value for p in PermissionEnum}


async def ensure_tenant_builtin_roles(
    session: AsyncSession, tenant_id: str, code_to_id: dict[str, str]
) -> dict[str, str]:
    """为某租户幂等创建 admin/user 两个内置角色，返回 {name: role_id}。"""
    admin_id = await _ensure_role(
        session, tenant_id, BuiltinRoleEnum.ADMIN.value,
        await _admin_permission_codes(), code_to_id,
    )
    user_id = await _ensure_role(
        session, tenant_id, BuiltinRoleEnum.USER.value,
        set(USER_ROLE_DEFAULT_PERMISSIONS), code_to_id,
    )
    return {BuiltinRoleEnum.ADMIN.value: admin_id, BuiltinRoleEnum.USER.value: user_id}


async def _tenant_bootstrap(session: AsyncSession) -> None:
    """权限点字典 + 内置 External_User_Tenant（含内置管理员与公共库）。"""
    code_to_id = await _ensure_permissions(session)
    await session.flush()

    # 内置 External_User_Tenant
    ext_tenant = await session.get(Tenant, EXTERNAL_USER_TENANT_ID)
    if ext_tenant is None:
        session.add(
            Tenant(
                id=EXTERNAL_USER_TENANT_ID,
                name=EXTERNAL_USER_TENANT_NAME,
                tenant_type=TenantTypeEnum.EXTERNAL.value,
                is_active=True,
            )
        )
        await session.flush()

    # 该租户的内置 admin/user 角色
    roles = await ensure_tenant_builtin_roles(session, EXTERNAL_USER_TENANT_ID, code_to_id)
    await session.flush()

    # 内置管理员（External_User_Builtin_Admin）
    ext_admin = await session.get(User, _EXTERNAL_ADMIN_ID)
    if ext_admin is None:
        # 内置管理员仅维护公共库，不对外登录；口令置随机不可用值（不下发）。
        session.add(
            User(
                id=_EXTERNAL_ADMIN_ID,
                tenant_id=EXTERNAL_USER_TENANT_ID,
                username=_EXTERNAL_ADMIN_USERNAME,
                password_hash=hash_password(uuid.uuid4().hex),
                is_active=True,
                is_super_admin=False,
                must_change_password=False,
            )
        )
        await session.flush()
    # 绑定 admin 角色（幂等）
    link = (
        await session.execute(
            select(UserRole).where(
                UserRole.user_id == _EXTERNAL_ADMIN_ID,
                UserRole.role_id == roles[BuiltinRoleEnum.ADMIN.value],
            )
        )
    ).scalar_one_or_none()
    if link is None:
        session.add(
            UserRole(user_id=_EXTERNAL_ADMIN_ID, role_id=roles[BuiltinRoleEnum.ADMIN.value])
        )

    # 内置公共库（供全体外部用户读取，仅内置管理员维护）
    pub_kb = await session.get(KnowledgeBase, _EXTERNAL_PUBLIC_KB_ID)
    if pub_kb is None:
        session.add(
            KnowledgeBase(
                id=_EXTERNAL_PUBLIC_KB_ID,
                name=_EXTERNAL_PUBLIC_KB_NAME,
                tenant_id=EXTERNAL_USER_TENANT_ID,
                owner_user_id=_EXTERNAL_ADMIN_ID,
                visibility=KbVisibilityEnum.ORGANIZATION.value,
                doc_count=0,
            )
        )

    await session.commit()


async def _super_admin_bootstrap(session: AsyncSession) -> None:
    """首次启动且无 Super_Admin 时按环境变量创建（强制改密）。"""
    count = await session.scalar(
        select(func.count(User.id)).where(User.is_super_admin == True)  # noqa: E712
    )
    if count and count > 0:
        return  # 已存在，幂等跳过

    settings = get_settings()
    username = (settings.super_admin_username or "").strip()
    password = settings.super_admin_password or ""
    if not username or not password:
        raise RuntimeError(
            "首次启动需创建 Super_Admin：请配置 SUPER_ADMIN_USERNAME / SUPER_ADMIN_PASSWORD"
            "（禁止默认口令兜底）"
        )

    session.add(
        User(
            id=str(uuid.uuid4()),
            tenant_id=None,  # Super_Admin 不归属任何业务租户
            username=username,
            password_hash=hash_password(password),
            is_active=True,
            is_super_admin=True,
            must_change_password=True,  # 强制首次登录改密
            token_version=0,
        )
    )
    await session.commit()
    logger.info("已创建初始 Super_Admin（强制首次改密）")


async def run_bootstrap(session_factory) -> None:
    """统一引导入口：由 init_db 之后调用（API 与 Worker 共用）。

    auth_enabled=False（灰度联调）时仍执行引导（建表/建内置数据是无害且必要的），
    但 Super_Admin 引导在缺少环境变量时只警告不致命，避免联调期被启动期硬依赖卡住。
    """
    settings = get_settings()
    async with session_factory() as session:
        await _tenant_bootstrap(session)

    async with session_factory() as session:
        try:
            await _super_admin_bootstrap(session)
        except RuntimeError:
            if settings.auth_enabled:
                raise  # 正式启用鉴权：缺 Super_Admin 配置必须 fail-fast
            logger.warning(
                "auth_enabled=false 且未配置 Super_Admin 环境变量，跳过 Super_Admin 引导（联调态）"
            )
