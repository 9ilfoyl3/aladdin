"""检索范围装配（tenant-auth）。

把"当前身份能检索哪些知识库"统一收口，供 chat / retrieval / mcp 在触达 Milvus 前裁剪。
- assemble_allowed_kb_ids：未显式指定范围时的默认可检索集合。
- authorize_requested_kbs：显式指定 kb 时逐个走 kb_authorization_decision(READ)，越界抛 404。

注意：本模块会查库（按 owner/visibility/grant 组装），非纯函数；但底层裁决仍复用
唯一的 kb_authorization_decision，不在此另起一套可见性判定。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import CrossTenantError
from app.auth.constants import GranteeTypeEnum, KbVisibilityEnum
from app.auth.identity import IdentityContext
from app.auth.kb_authz import (
    GrantView,
    KbAccessEnum,
    kb_authorization_decision,
)
from app.schema.db import KnowledgeBase, KnowledgeBaseGrant


async def _load_grants_for_kb(session: AsyncSession, kb_id: str) -> list[GrantView]:
    """加载某 KB 适用的授权记录（仅 user-grant）。"""
    rows = await session.execute(
        select(
            KnowledgeBaseGrant.grantee_type,
            KnowledgeBaseGrant.grantee_id,
            KnowledgeBaseGrant.permission,
        ).where(KnowledgeBaseGrant.kb_id == kb_id)
    )
    return [
        GrantView(grantee_type=gt, grantee_id=gid, permission=perm)
        for gt, gid, perm in rows.all()
        if gt == GranteeTypeEnum.USER.value
    ]


async def authorize_requested_kbs(
    session: AsyncSession,
    identity: IdentityContext,
    requested_kb_ids: list[str],
    access: KbAccessEnum = KbAccessEnum.READ,
) -> None:
    """逐个校验显式指定的 KB 是否在身份的相应访问范围内；任一不通过抛 CrossTenantError(404)。

    跨租户、不在可读范围、超出租户级 Key scope 都统一表现为 404（存在性非泄露）。
    可读但无写权（写场景）由 kb_authorization_decision 返回 403。
    """
    for kb_id in requested_kb_ids:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise CrossTenantError()
        grants = await _load_grants_for_kb(session, kb_id)
        decision = kb_authorization_decision(
            identity,
            kb_id=kb.id,
            kb_tenant_id=kb.tenant_id,
            kb_owner_user_id=kb.owner_user_id,
            kb_visibility=kb.visibility,
            kb_org_permission=kb.org_permission,
            access=access,
            grants=grants,
        )
        if not decision.allow:
            # 403（可读无写）原样抛出；其余（404）也走统一异常
            from app.api.errors import PermissionDeniedError

            if decision.http_status == 403:
                raise PermissionDeniedError()
            raise CrossTenantError()


async def assemble_allowed_kb_ids(
    session: AsyncSession, identity: IdentityContext
) -> set[str]:
    """未显式指定范围时，组装当前身份可检索（read）的 KB id 集合。

    - 租户级 Key（Virtual_Identity，无 subject）：按 ApiKey_Authorized_Scope——
      all_public_kbs ? 本租户全部 organization KB : ∅，并入显式 explicit_kb_ids（限同租户）。
    - 外部用户：自有 Private_KB ∪ External_User_Tenant 全部 Public_KB。
    - 租户管理员（admin）：本租户全部 KB（含他人私有库，只读监管）；写/改/删另由
      kb_authorization_decision 与 owner 闸门裁决（admin 不写他人库内容、不动他人库实体）。
    - 注册用户 / 用户级 Key：自有 Private_KB ∪ 同租户全部 Public_KB ∪ 被授予 read/write 的 Shared_KB。
    - Super_Admin（platform，tenant_id=None）：经顶部早返回得空集（内容检索不属其职权，
      受内容边界约束）。
    """
    tenant_id = identity.tenant_id
    if tenant_id is None:
        # platform 身份无内容检索范围
        return set()

    # 本租户全部 KB（同租户范围内组装；跨租户天然排除）
    rows = await session.execute(
        select(
            KnowledgeBase.id,
            KnowledgeBase.owner_user_id,
            KnowledgeBase.visibility,
        ).where(KnowledgeBase.tenant_id == tenant_id)
    )
    kbs = rows.all()

    public_ids = {
        kid for kid, _owner, vis in kbs if vis == KbVisibilityEnum.ORGANIZATION.value
    }

    # 租户级 Key：完全由 scope 决定
    scope = identity.kb_scope
    if scope is not None and identity.acting_subject_id is None:
        allowed = set(scope.explicit_kb_ids) & {kid for kid, _o, _v in kbs}
        if scope.all_public_kbs:
            allowed |= public_ids
        return allowed

    subject_id = identity.acting_subject_id
    own_ids = {kid for kid, owner, _vis in kbs if owner is not None and owner == subject_id}

    # 外部用户：自有私有库 ∪ 本（外部）租户公共库
    if identity.is_external_user:
        return own_ids | public_ids

    # 租户管理员 / 超管：监管可读本租户全部库（含他人私有库）。
    # 与 owner-only 实体操作解耦——这里只决定「列表/检索可见（read）」范围；
    # 写/改/删仍由 kb_authorization_decision 与 owner 闸门各自裁决（admin 不写他人库内容）。
    if identity.is_tenant_admin or identity.is_super_admin:
        return {kid for kid, _o, _v in kbs}

    # 注册用户 / 用户级 Key：自有 ∪ 公共 ∪ 被共享（read/write）
    shared_ids = await _shared_kb_ids(session, identity)
    # 仅保留同租户的被共享库（跨租户 grant 在 v1 不存在，这里再过滤一道）
    shared_ids &= {kid for kid, _o, _v in kbs}
    return own_ids | public_ids | shared_ids


async def _shared_kb_ids(session: AsyncSession, identity: IdentityContext) -> set[str]:
    """经 KnowledgeBaseGrant 直接被授予（user）read/write 的 KB id。"""
    subject_id = identity.acting_subject_id
    if subject_id is None:
        return set()

    rows = await session.execute(
        select(KnowledgeBaseGrant.kb_id).where(
            (KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value)
            & (KnowledgeBaseGrant.grantee_id == subject_id)
        )
    )
    return {r[0] for r in rows.all()}
