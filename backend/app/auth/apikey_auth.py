"""ApiKeyAuthenticator：API Key 校验 + 判型 + 身份合成（tenant-auth）。

三处收敛点之一（认证侧）。替换原 verify_key 的"仅校验"，扩展为按 key_type 分支
合成统一的 IdentityContext（固定角色，不再有权限点字典）：
  - tenant_level   -> Virtual_Identity，role=None（机器身份，靠 kb_scope 裁剪）
  - user_level     -> 绑定用户的固定角色（admin/member）；通道操作级别钉死 tenant
  - external_agent -> 校验 X-External-User-Id；按 (key_source, external_user_id) 懒创建
                      External_User；tenant_id 硬锁 External_User_Tenant；固定 member

错误语义：Key 不存在/撤销 -> 401；租户停用 -> 403；绑定用户停用 -> 403；
代理 Key 缺 X-External-User-Id -> 400。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_key
from app.api.errors import (
    MissingExternalUserIdError,
    TenantDisabledError,
    UnauthenticatedError,
    UserDisabledError,
)
from app.auth.constants import (
    EXTERNAL_USER_TENANT_ID,
    HEADER_EXTERNAL_USER_ID,
    ApiKeyTypeEnum,
    TenantRoleEnum,
)
from app.auth.apikey_usage import record_api_key_usage
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    KbScope,
    OperationLevelEnum,
)
from app.schema.db import ApiKey, ExternalUser, Tenant, User


def _scope_from_json(authorized_scope: dict | None) -> KbScope:
    """把 api_keys.authorized_scope JSON 解析为 KbScope。

    结构：{ "all_public_kbs": bool, "explicit_kb_ids": [..] }
    缺省（None/空）= 不开放任何范围（all_public_kbs=False, explicit=∅）——
    fail-closed：未显式授权即不可访问任何 KB，不做"宽松默认"。
    """
    data = authorized_scope or {}
    return KbScope(
        all_public_kbs=bool(data.get("all_public_kbs", False)),
        explicit_kb_ids=frozenset(data.get("explicit_kb_ids", []) or []),
    )


async def _tenant_active(session: AsyncSession, tenant_id: str | None) -> bool:
    if tenant_id is None:
        return False
    tenant = await session.get(Tenant, tenant_id)
    return tenant is not None and tenant.is_active


async def _bump_usage(session: AsyncSession, api_key_id: str) -> None:
    """记录一次调用（内存合并，由后台周期批量落库；不在鉴权关键路径上写库）。

    见 app/auth/apikey_usage.py：根因修复——把每请求一次写+commit 移出关键路径，
    消除同 Key 高频调用的行锁竞争与逐请求 I/O。
    """
    await record_api_key_usage(api_key_id)


class ApiKeyAuthenticator:
    """API Key 认证器。authenticate 返回合成的 IdentityContext，失败抛 AppError。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def authenticate(self, raw_key: str, headers: Mapping[str, str]) -> IdentityContext:
        # 1) SHA256 比对 + 启用校验（Bearer sk-... 明文通道）
        key_hash = hash_key(raw_key)
        from sqlalchemy import select

        result = await self.session.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)  # noqa: E712
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            raise UnauthenticatedError("API Key 无效或已被撤销")
        return await self._finalize(api_key, headers)

    async def authenticate_by_id(
        self, api_key_id: str, headers: Mapping[str, str]
    ) -> IdentityContext:
        """按 AK（api_key.id）合成身份，供 AK/SK 签名通道使用（签名已在上层校验）。

        与 ``authenticate`` 的差异仅在定位方式：签名通道不上行明文密钥，故以 AK 定位；
        密钥是否存在/被撤销在此确认（查不到 / 已停用 -> 401）。后续判型与副作用完全一致。
        """
        api_key = await self.session.get(ApiKey, api_key_id)
        if api_key is None or not api_key.is_active:
            raise UnauthenticatedError("API Key 无效或已被撤销")
        return await self._finalize(api_key, headers)

    async def _finalize(
        self, api_key: ApiKey, headers: Mapping[str, str]
    ) -> IdentityContext:
        """已定位到启用的 ApiKey 后的统一收尾：租户校验 + 判型合成 + 计数。"""
        # 租户启用校验（停用 403）
        if not await _tenant_active(self.session, api_key.tenant_id):
            raise TenantDisabledError()

        # 按 key_type 分支
        key_type = api_key.key_type or ApiKeyTypeEnum.TENANT_LEVEL.value
        if key_type == ApiKeyTypeEnum.EXTERNAL_AGENT.value:
            identity = await self._auth_external_agent(api_key, headers)
        elif key_type == ApiKeyTypeEnum.USER_LEVEL.value:
            identity = await self._auth_user_level(api_key)
        else:
            identity = self._auth_tenant_level(api_key)

        # 副作用：计数 + last_used
        await _bump_usage(self.session, api_key.id)
        return identity

    def _auth_tenant_level(self, api_key: ApiKey) -> IdentityContext:
        """租户级 Key -> Virtual_Identity（机器身份 role=None，靠 Key 的 scope 裁剪）。"""
        return IdentityContext(
            source=IdentitySourceEnum.API_KEY,
            op_level=OperationLevelEnum.TENANT,
            tenant_id=api_key.tenant_id,
            api_key_id=api_key.id,
            role=None,  # 机器身份无角色，访问范围完全由 kb_scope 裁剪
            kb_scope=_scope_from_json(api_key.authorized_scope),
        )

    async def _auth_user_level(self, api_key: ApiKey) -> IdentityContext:
        """用户级 Key -> 绑定 user 的固定角色；通道操作级别钉死 tenant。"""
        bound_user_id = api_key.bound_user_id
        if not bound_user_id:
            # 数据不一致：用户级 Key 却无绑定用户。fail-closed。
            raise UnauthenticatedError("用户级 Key 未绑定用户")
        user = await self.session.get(User, bound_user_id)
        if user is None:
            raise UnauthenticatedError("用户级 Key 绑定的用户不存在")
        if not user.is_active:
            raise UserDisabledError()

        # 角色随绑定用户。super_admin 不应绑定到用户级 Key，但若出现则置 None 兜底；
        # 否则取该用户的固定角色，缺失时回退 member（fail-safe，仅予最小角色）。
        role = (
            None
            if user.is_super_admin
            else (TenantRoleEnum(user.role) if user.role else TenantRoleEnum.MEMBER)
        )
        # 通道边界：Guard 还会剥离管理/平台操作；此处携带固定角色供内容级判定。
        return IdentityContext(
            source=IdentitySourceEnum.API_KEY,
            op_level=OperationLevelEnum.TENANT,
            tenant_id=api_key.tenant_id,
            user_id=bound_user_id,
            username=user.username,
            api_key_id=api_key.id,
            role=role,
        )

    async def _auth_external_agent(
        self, api_key: ApiKey, headers: Mapping[str, str]
    ) -> IdentityContext:
        """超管级代理 Key -> 校验 external_user_id；按命名空间懒创建外部用户。"""
        external_user_id_raw = _header(headers, HEADER_EXTERNAL_USER_ID)
        if not external_user_id_raw or not external_user_id_raw.strip():
            raise MissingExternalUserIdError()
        external_user_id_raw = external_user_id_raw.strip()

        # 命名空间隔离键：key_source = 该代理 Key 自身标识（api_key.id），
        # 与调用方传入的 external_user_id 组成复合键。
        key_source = api_key.id
        external_user = await self._get_or_create_external_user(
            key_source, external_user_id_raw
        )

        return IdentityContext(
            source=IdentitySourceEnum.API_KEY,
            op_level=OperationLevelEnum.TENANT,
            tenant_id=EXTERNAL_USER_TENANT_ID,  # 硬锁定，忽略任何目标租户入口
            external_user_id=external_user.id,
            api_key_id=api_key.id,
            role=TenantRoleEnum.MEMBER,  # 外部用户固定为 member
        )

    async def _get_or_create_external_user(
        self, key_source: str, external_user_id: str
    ) -> ExternalUser:
        """按 (key_source, external_user_id) 复合键查找；不存在则懒创建并归属 External_User_Tenant。"""
        from sqlalchemy import select

        result = await self.session.execute(
            select(ExternalUser).where(
                ExternalUser.key_source == key_source,
                ExternalUser.external_user_id == external_user_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        new_eu = ExternalUser(
            id=str(uuid.uuid4()),
            tenant_id=EXTERNAL_USER_TENANT_ID,
            key_source=key_source,
            external_user_id=external_user_id,
        )
        self.session.add(new_eu)
        try:
            await self.session.commit()
            await self.session.refresh(new_eu)
            return new_eu
        except Exception:
            # 并发首访竞态：另一个请求已插入同一复合键 -> 回滚后改查既有记录。
            # 这是对唯一约束竞态的显式处理（见 design 显式兼容清单之外的并发正确性），
            # 不是掩盖缺陷的"瞎兼容"。
            await self.session.rollback()
            result = await self.session.execute(
                select(ExternalUser).where(
                    ExternalUser.key_source == key_source,
                    ExternalUser.external_user_id == external_user_id,
                )
            )
            again = result.scalar_one_or_none()
            if again is None:
                raise
            return again


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """大小写不敏感地取请求头（Starlette Headers 本就不敏感，dict 兜底）。"""
    try:
        return headers.get(name)
    except Exception:
        return None
