"""FastAPI 依赖装配：AuthorizationGuard（单一鉴权扼流点）与 get_repo。

三处收敛点之一（鉴权侧）。所有受保护端点通过 Depends(authorization_guard(...)) 或其
便捷包装（require_platform / require_tenant_admin / require_member / require_authenticated）
接入，默认拒绝。守卫由「声明操作级别」驱动，权限判定全部下沉到四个纯函数 gate
（_platform_gate / _admin_gate / _member_floor / _must_change_gate），便于单元/属性测试。

Guard 内部顺序（任一不满足即拒绝）：
  1. 解析凭据 -> IdentityContext（无有效凭据 401）
  2. must_change_password 闸门（除显式 allow_must_change_password 的端点外一律 403）
  3. 通道级别校验（allow_api_key=False 遇 api_key -> 403）
  4. platform 闸门（op_level=platform 要求 super_admin 且 source=jwt）
  5. admin 闸门（require_admin 要求 admin 或 super_admin）
  6. member_floor 闸门（require_member_floor 要求 role∈{admin,member} 或 super_admin）
  7. X-Tenant-ID 目标租户归属校验（api_key 忽略该头恒用自身 tenant；JWT 普通用户
     不一致 -> 403；Super_Admin 容器/账号范围可指定）
  8. set contextvar 三态（请求结束 reset），返回 IdentityContext
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Mapping

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
    HEADER_TENANT_ID,
    TenantRoleEnum,
)
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    OperationLevelEnum,
)
from app.auth.jwt_auth import JwtError, decode_token
from app.repositories.tenant_repo import (
    TenantRepository,
    reset_tenant_scope,
    scope_from_identity,
    set_tenant_scope,
)
from app.schema.db import Tenant, User

# Authorization 头的 Bearer 前缀（大小写不敏感匹配）。
_BEARER_PREFIX = "bearer "

logger = logging.getLogger(__name__)


async def _resolve_cross_tenant_kb_ids(
    identity: IdentityContext, session: AsyncSession
) -> frozenset[str]:
    """cross-tenant-kb-share：算出当前身份经跨租户点对点分享被授予 read 的 KB id 集合。

    包一层异常兜底：任何查询异常都退化为空集（等价旧行为），绝不因本特性拖垮鉴权主流程。
    """
    try:
        from app.auth.kb_scope import cross_tenant_granted_kb_ids

        return await cross_tenant_granted_kb_ids(session, identity)
    except Exception:  # noqa: BLE001 — 跨租户放行是增量能力，失败应安全降级而非中断鉴权
        logger.warning("解析跨租户分享 KB 失败，降级为无跨租户放行", exc_info=True)
        return frozenset()


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


# ============================================================
# 纯判定 gate（无 I/O，全可单元/属性测试；Properties 6-9 驱动）
# ============================================================


def _platform_gate(identity: IdentityContext) -> bool:
    """平台级：当且仅当 super_admin 且来源为 JWT。"""
    return identity.is_super_admin and identity.source == IdentitySourceEnum.JWT


def _admin_gate(identity: IdentityContext) -> bool:
    """管理级：admin 或 super_admin（布尔判等，无等级比较）。注意通道(api_key)拦截在守卫里单独做。"""
    return identity.is_tenant_admin or identity.is_super_admin


def _member_floor(identity: IdentityContext) -> bool:
    """建归属资源最低档：role∈{admin,member}（外部用户=member 满足）或 super_admin。
    role 为 None 的 tenant_level 机器身份不满足。"""
    return identity.role is not None or identity.is_super_admin


def _must_change_gate(must_change: bool, allow: bool) -> bool:
    """强制改密闸门：返回 True 表示应拒绝（must_change 且端点未声明 allow）。"""
    return must_change and not allow


async def resolve_identity_from_credentials(
    token: str, headers: Mapping[str, str], session: AsyncSession
) -> tuple[IdentityContext, bool]:
    """从 token + headers 解析 IdentityContext（不绑定 Request，供 HTTP 与 WS 共用）。

    返回 (identity, must_change_password)。must_change_password 由本函数已取到的
    User 直接读出，避免 Guard 再单独查一次 users（同请求内消除冗余查询）。
    API Key 通道无"当前用户改密"概念，must_change_password 恒为 False。

    token 为 sk- 前缀走 API Key 认证（headers 提供 external_agent 的
    X-External-User-Id 等）；否则按 JWT 解析并做 User/租户校验。无有效凭据 -> 401。
    """
    if not token:
        raise UnauthenticatedError("缺少 Authorization 凭据")

    if _is_api_key_token(token):
        # API Key 通道（含三模型）：无强制改密闸门
        identity = await ApiKeyAuthenticator(session).authenticate(token, headers)
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

    # 固定角色：Super_Admin 不参与租户角色（role=None）；普通用户取 User.role，
    # 缺省回退为 member（与 DB 默认一致）。
    role = (
        None
        if user.is_super_admin
        else (TenantRoleEnum(user.role) if user.role else TenantRoleEnum.MEMBER)
    )
    op_level = (
        OperationLevelEnum.PLATFORM if user.is_super_admin else OperationLevelEnum.TENANT
    )
    identity = IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=op_level,
        tenant_id=user.tenant_id,
        user_id=user.id,
        username=user.username,
        is_super_admin=user.is_super_admin,
        role=role,
    )
    return identity, bool(user.must_change_password)


async def _resolve_identity(
    request: Request, session: AsyncSession
) -> tuple[IdentityContext, bool]:
    """从请求凭据解析 IdentityContext。无有效凭据 -> 401。

    从 Request 取 Bearer token 与 headers 后委托 resolve_identity_from_credentials，
    行为与既有实现完全一致。
    """
    token = _extract_bearer(request)
    if not token:
        raise UnauthenticatedError("缺少 Authorization 凭据")
    return await resolve_identity_from_credentials(token, request.headers, session)


def authorization_guard(
    *,
    op_level: OperationLevelEnum = OperationLevelEnum.TENANT,
    require_admin: bool = False,
    require_member_floor: bool = False,
    allow_api_key: bool = True,
    allow_must_change_password: bool = False,
) -> Callable[..., AsyncGenerator[IdentityContext, None]]:
    """鉴权依赖工厂（默认拒绝，声明式）。

    实现为 yield 依赖：yield 出 IdentityContext 供端点使用，请求结束后在 finally
    中 reset 仓储兜底 contextvar，避免租户范围跨请求泄漏（连接/任务复用场景）。

    Args:
        op_level: TENANT（默认）或 PLATFORM（仅 super_admin/JWT，见 _platform_gate）。
        require_admin: 要求管理级（admin 或 super_admin，见 _admin_gate）。
        require_member_floor: 要求成员及以上（role 非空 或 super_admin，见 _member_floor）。
        allow_api_key: 管理/平台端点设 False，api_key 通道访问 -> 403。
        allow_must_change_password: 仅"改自身口令"端点设 True，绕过强制改密闸门。
    """

    async def _guard(request: Request) -> AsyncGenerator[IdentityContext, None]:
        from app.storage.database import async_session

        scope_token = None
        try:
            async with async_session() as session:
                identity, must_change_pwd = await _resolve_identity(request, session)

                # 1) must_change_password 闸门（复用解析阶段已读出的标记，不再二次查库）
                if _must_change_gate(must_change_pwd, allow_must_change_password):
                    raise PermissionDeniedError("请先修改初始口令后再操作")

                # 2) 通道级别校验
                if not allow_api_key and identity.source == IdentitySourceEnum.API_KEY:
                    raise PermissionDeniedError("该操作不允许通过 API Key 调用")

                # 3) platform 闸门
                if op_level == OperationLevelEnum.PLATFORM and not _platform_gate(identity):
                    raise PermissionDeniedError("仅平台超级管理员可执行该操作")

                # 4) admin 闸门
                if require_admin and not _admin_gate(identity):
                    raise PermissionDeniedError("需要管理员权限")

                # 5) member_floor 闸门
                if require_member_floor and not _member_floor(identity):
                    raise PermissionDeniedError("需要成员及以上角色")

                # 6) 目标租户入口归属校验
                _enforce_target_tenant(request, identity)

                # 6.5) cross-tenant-kb-share：算出当前身份经跨租户分享被授予 read 的 KB id。
                #      须在设置 contextvar 之前查（此时无租户兜底过滤，方能查到他租户 KB）。
                #      空集时 scope 行为与改造前等价。
                cross_kb_ids = await _resolve_cross_tenant_kb_ids(identity, session)

            # 7) 设置 contextvar 三态（仓储兜底用），yield 给端点，结束后 reset。
            scope_token = set_tenant_scope(
                scope_from_identity(identity, cross_tenant_kb_ids=cross_kb_ids)
            )
            yield identity
        finally:
            if scope_token is not None:
                reset_tenant_scope(scope_token)

    return _guard


# ============================================================
# 便捷包装：按操作级别声明，收敛守卫调用面
# ============================================================


def require_platform(**kw):
    """平台级端点：仅 super_admin/JWT，禁 api_key 通道。"""
    return authorization_guard(op_level=OperationLevelEnum.PLATFORM, allow_api_key=False, **kw)


def require_tenant_admin(**kw):
    """租户管理端点：admin 或 super_admin，禁 api_key 通道。"""
    return authorization_guard(require_admin=True, allow_api_key=False, **kw)


def require_member(**kw):
    """需成员及以上：role∈{admin,member} 或 super_admin（外部用户=member 满足）。"""
    return authorization_guard(require_member_floor=True, **kw)


def require_authenticated(**kw):
    """仅要求通过认证（默认 TENANT 级，允许 api_key 通道）。"""
    return authorization_guard(**kw)


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
