"""FastAPI 依赖装配：AuthorizationGuard（单一鉴权扼流点）与 get_repo。

三处收敛点之一（鉴权侧）。所有受保护端点通过 Depends(authorization_guard(...)) 接入，
默认拒绝。Guard 内部顺序（任一不满足即拒绝）：
  1. 解析凭据 -> IdentityContext（无有效凭据 401）
  2. must_change_password 闸门（除改自身口令外一律 403）
  3. 通道级别校验（allow_api_key=False 遇 api_key -> 403；op_level=platform 要求
     is_super_admin 且 source=jwt；api_key 一律不得行使管理/平台权限点）
  4. X-Tenant-ID 目标租户归属校验（api_key 忽略该头恒用自身 tenant；JWT 普通用户
     不一致 -> 403；Super_Admin 容器/账号范围可指定）
  5. 所需权限点校验（缺失 -> 403）
  6. set contextvar 三态（请求结束 reset），返回 IdentityContext

灰度开关 auth_enabled=False 时旁路鉴权（见 design 显式兼容清单 C1）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    PermissionDeniedError,
    TenantDisabledError,
    UnauthenticatedError,
)
from app.auth.apikey_auth import ApiKeyAuthenticator
from app.auth.constants import (
    ADMINISTRATIVE_PERMISSIONS,
    HEADER_TENANT_ID,
    PLATFORM_PERMISSIONS,
)
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    OperationLevelEnum,
)
from app.auth.jwt_auth import JwtError, decode_token
from app.auth.permission_resolver import (
    resolve_effective_permissions,
    resolve_role_ids,
)
from app.config import get_settings
from app.repositories.tenant_repo import (
    TenantRepository,
    reset_tenant_scope,
    scope_from_identity,
    set_tenant_scope,
)
from app.schema.db import Tenant, User

# Authorization 头的 Bearer 前缀（大小写不敏感匹配）。
_BEARER_PREFIX = "bearer "


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if not auth:
        return None
    if auth[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    return auth[len(_BEARER_PREFIX):].strip()


def _is_api_key_token(token: str) -> bool:
    """API Key 以 sk- 开头；其余按 JWT 解析。"""
    return token.startswith("sk-")


async def _resolve_identity(
    request: Request, session: AsyncSession
) -> tuple[IdentityContext, bool]:
    """从请求凭据解析 IdentityContext。无有效凭据 -> 401。

    返回 (identity, must_change_password)。must_change_password 由本函数已取到的
    User 直接读出，避免 Guard 再单独查一次 users（同请求内消除冗余查询）。
    API Key 通道无"当前用户改密"概念，must_change_password 恒为 False。
    """
    token = _extract_bearer(request)
    if not token:
        raise UnauthenticatedError("缺少 Authorization 凭据")

    if _is_api_key_token(token):
        # API Key 通道（含三模型）：无强制改密闸门
        identity = await ApiKeyAuthenticator(session).authenticate(token, request.headers)
        return identity, False

    # JWT 通道
    try:
        claims = decode_token(token)
    except JwtError as e:
        raise UnauthenticatedError(str(e)) from e

    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        # 用户不存在或已停用：旧 token 失效
        raise UnauthenticatedError("账号不存在或已停用")
    if user.token_version != claims.token_version:
        # 停用/重置口令使旧 JWT 失效
        raise UnauthenticatedError("登录态已失效，请重新登录")

    # 租户启用校验（Super_Admin 无租户，跳过）
    if user.tenant_id is not None:
        tenant = await session.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise TenantDisabledError()

    # role_ids 解析一次，复用给权限点解析（消除同请求内对 user_roles 的二次查询）
    role_ids = await resolve_role_ids(session, user.id)
    perms = await resolve_effective_permissions(session, user.id, role_ids=role_ids)
    op_level = (
        OperationLevelEnum.PLATFORM if user.is_super_admin else OperationLevelEnum.TENANT
    )
    identity = IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=op_level,
        tenant_id=user.tenant_id,
        user_id=user.id,
        is_super_admin=user.is_super_admin,
        effective_permissions=perms,
        role_ids=role_ids,
    )
    return identity, bool(user.must_change_password)


def authorization_guard(
    *,
    required_permissions: set[str] | None = None,
    op_level: OperationLevelEnum = OperationLevelEnum.TENANT,
    allow_api_key: bool = True,
    allow_must_change_password: bool = False,
) -> Callable[..., AsyncGenerator[IdentityContext, None]]:
    """鉴权依赖工厂（默认拒绝）。

    实现为 yield 依赖：yield 出 IdentityContext 供端点使用，请求结束后在 finally
    中 reset 仓储兜底 contextvar，避免租户范围跨请求泄漏（连接/任务复用场景）。

    Args:
        required_permissions: 端点所需权限点；缺任一 -> 403。
        op_level: TENANT（默认）或 PLATFORM（仅 Super_Admin/JWT）。
        allow_api_key: 管理/平台端点设 False，api_key 通道访问 -> 403。
        allow_must_change_password: 仅"改自身口令"端点设 True，绕过强制改密闸门。
    """
    required = required_permissions or set()

    async def _guard(request: Request) -> AsyncGenerator[IdentityContext, None]:
        from app.storage.database import async_session

        settings = get_settings()
        scope_token = None
        try:
            # —— 灰度旁路（C1）：仅当显式关闭鉴权 ——
            if not settings.auth_enabled:
                # 旁路时给一个 platform 级匿名身份，便于联调；正式环境 auth_enabled=True。
                yield IdentityContext(
                    source=IdentitySourceEnum.JWT,
                    op_level=OperationLevelEnum.PLATFORM,
                    tenant_id=None,
                    is_super_admin=True,
                )
                return

            async with async_session() as session:
                identity, must_change_pwd = await _resolve_identity(request, session)

                # 2) must_change_password 闸门（复用解析阶段已读出的标记，不再二次查库）
                if not allow_must_change_password and must_change_pwd:
                    raise PermissionDeniedError("请先修改初始口令后再操作")

                # 3) 通道级别校验
                if not allow_api_key and identity.source == IdentitySourceEnum.API_KEY:
                    raise PermissionDeniedError("该操作不允许通过 API Key 调用")

                if op_level == OperationLevelEnum.PLATFORM:
                    if not (identity.is_super_admin and identity.source == IdentitySourceEnum.JWT):
                        raise PermissionDeniedError("仅平台超级管理员可执行该操作")

                # api_key 通道一律不得行使管理/平台权限点（即便绑定用户持有）
                if identity.source == IdentitySourceEnum.API_KEY:
                    forbidden = (ADMINISTRATIVE_PERMISSIONS | PLATFORM_PERMISSIONS) & required
                    if forbidden:
                        raise PermissionDeniedError("API Key 通道不可执行管理或平台操作")

                # 4) 目标租户入口归属校验
                _enforce_target_tenant(request, identity)

                # 5) 所需权限点校验（Super_Admin 平台身份隐含拥有全部权限点，
                #    其职权由 is_super_admin 而非 RBAC 角色承载，故跳过权限点缺失校验；
                #    但内容正文仍受 Content_View_Boundary 约束，由各内容端点单独拦截）
                if not identity.is_super_admin:
                    missing = required - identity.effective_permissions
                    if missing:
                        raise PermissionDeniedError("权限不足")

            # 6) 设置 contextvar 三态（仓储兜底用），yield 给端点，结束后 reset。
            scope_token = set_tenant_scope(scope_from_identity(identity))
            yield identity
        finally:
            if scope_token is not None:
                reset_tenant_scope(scope_token)

    return _guard


def _enforce_target_tenant(request: Request, identity: IdentityContext) -> None:
    """X-Tenant-ID 归属校验（R11）。"""
    requested = request.headers.get(HEADER_TENANT_ID)
    if not requested:
        return
    # api_key 通道（含代理 Key）：忽略该头，恒用自身 tenant
    if identity.source == IdentitySourceEnum.API_KEY:
        return
    # Super_Admin：容器/账号范围可指定目标租户（内容边界另行约束）
    if identity.is_super_admin:
        return
    # JWT 普通用户：与自身 Home_Tenant 不一致 -> 403
    if requested != identity.tenant_id:
        raise PermissionDeniedError("无权访问指定租户")


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """提供一个请求级会话（带 tenant_scope reset 收尾）。"""
    from app.storage.database import async_session

    async with async_session() as session:
        yield session


def get_repo(
    identity: IdentityContext,
    session: AsyncSession,
) -> TenantRepository:
    """装配 TenantRepository（在端点内显式调用，传入 Guard 返回的 identity 与会话）。"""
    return TenantRepository(session, identity)
