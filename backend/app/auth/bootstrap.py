"""启动引导（tenant-rbac-refactor）：全新初始化，幂等。不做历史数据迁移。

职责：
- TenantBootstrap：创建内置 External_User_Tenant 及其内置管理员
  (External_User_Builtin_Admin，直接写 ``User.role="admin"``) 与内置公共库
  (visibility=organization)。不再预置权限点字典与自定义角色（固定角色模型）。
- SuperAdminBootstrap：首次启动且无 Super_Admin 时按环境变量创建
  (``is_super_admin=True``/``role=None``/``must_change_password=True``)，强制改密；
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
    EXTERNAL_USER_TENANT_ID,
    EXTERNAL_USER_TENANT_NAME,
    KbVisibilityEnum,
    TenantTypeEnum,
)
from app.auth.password import hash_password
from app.config import get_settings
from app.schema.db import (
    KnowledgeBase,
    Tenant,
    User,
)

logger = logging.getLogger(__name__)

# 内置外部用户公共库的固定标识（幂等可查）
_EXTERNAL_PUBLIC_KB_ID = "kb-external-public"
_EXTERNAL_PUBLIC_KB_NAME = "外部用户公共库"


async def _tenant_bootstrap(session: AsyncSession) -> None:
    """内置 External_User_Tenant（含内置公共库，不再预置默认管理员）。

    固定角色模型下不预置权限点 / 自定义角色 / 角色关联行。外部用户在认证时合成为
    member；该租户的治理由平台 Super_Admin 经管理端点完成（按需补充管理员），
    故引导阶段**不再创建默认管理员**。内置公共库为无主（owner_user_id=None）的组织库，
    供全体外部用户读取，其内容维护由超管或超管补建的管理员负责。
    """
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

    # 内置公共库（无主组织库，供全体外部用户读取；治理走平台超管/超管补建的管理员）
    pub_kb = await session.get(KnowledgeBase, _EXTERNAL_PUBLIC_KB_ID)
    if pub_kb is None:
        session.add(
            KnowledgeBase(
                id=_EXTERNAL_PUBLIC_KB_ID,
                name=_EXTERNAL_PUBLIC_KB_NAME,
                tenant_id=EXTERNAL_USER_TENANT_ID,
                owner_user_id=None,  # 无主：内置公共库不归属任何用户
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
            tenant_id=None,        # Super_Admin 不归属任何业务租户
            username=username,
            password_hash=await hash_password(password),
            role=None,             # Super_Admin 不参与租户固定角色
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

    清理 E 后鉴权始终强制：不再有 auth_enabled 分支。缺 Super_Admin 环境变量一律
    fail-fast（禁止默认口令兜底），由调用方在启动阶段失败退出。
    """
    async with session_factory() as session:
        await _tenant_bootstrap(session)

    async with session_factory() as session:
        await _super_admin_bootstrap(session)
