"""知识库访问授权统一判定（tenant-auth）。

三处收敛点之二：系统中唯一裁决知识库读/写的纯函数。
设计为「纯函数 + 注入数据」——所有依赖（KB、适用的 grants、身份持有的角色）由调用方
查好后注入，本函数不触库、不触 Milvus，因而可被属性测试高频驱动。

判定顺序（**前置校验永远第一**）：
  1. tenant_guard：kb.tenant_id != identity.tenant_id -> (deny, 404)，先于一切 visibility/grant。
  2. 外部用户二级隔离：行事主体为 External_User 时——
       私有库：仅 owner==external_user_id 放行，否则 404；
       公共库：读放行、写 403（仅内置管理员维护）。
  3. owner：kb.owner_user_id == 行事主体 -> 读写均放行。
  4. organization：同租户可读；写需 kb:write_public 权限点。
  5. private：非 owner 时依据 grants（user/role）的 read/write 放行；否则不可读 -> 404，
       可读但无写 -> 403。
  6. 租户级 Key 叠加 kb_scope 裁剪：目标 KB 不在 ApiKey_Authorized_Scope 内 -> 404。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.auth.constants import KbVisibilityEnum, PermissionEnum, GranteeTypeEnum, GrantPermissionEnum
from app.auth.identity import IdentityContext


class KbAccessEnum(str, Enum):
    """知识库访问类型。"""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class KbDecision:
    """授权判定结果。allow=False 时 http_status 指明对外状态码（404 不泄露存在性 / 403 无权）。"""

    allow: bool
    http_status: int | None = None

    @classmethod
    def allowed(cls) -> "KbDecision":
        return cls(allow=True, http_status=None)

    @classmethod
    def denied(cls, http_status: int) -> "KbDecision":
        return cls(allow=False, http_status=http_status)


@dataclass(frozen=True)
class GrantView:
    """注入给判定函数的授权记录视图（已按目标 KB 过滤）。"""

    grantee_type: str
    grantee_id: str
    permission: str  # read | write


def _kb_in_scope(identity: IdentityContext, kb_id: str, kb_visibility: str, kb_tenant_id: str) -> bool:
    """租户级 Key 的 ApiKey_Authorized_Scope 裁剪判定。

    kb_scope 为 None 表示不额外裁剪（JWT 用户 / 用户级 Key / 外部用户走各自范围）。
    动态规则 all_public_kbs：本租户全部 organization KB（此处 KB 已确认同租户）。
    """
    scope = identity.kb_scope
    if scope is None:
        return True
    if scope.all_public_kbs and kb_visibility == KbVisibilityEnum.ORGANIZATION.value:
        return True
    return kb_id in scope.explicit_kb_ids


def kb_authorization_decision(
    identity: IdentityContext,
    *,
    kb_id: str,
    kb_tenant_id: str,
    kb_owner_user_id: str | None,
    kb_visibility: str,
    access: KbAccessEnum,
    grants: list[GrantView],
) -> KbDecision:
    """对一次知识库读/写访问做出允许或拒绝判定（系统唯一入口）。

    Args:
        identity: 当前身份。
        kb_id / kb_tenant_id / kb_owner_user_id / kb_visibility: 目标 KB 的关键字段（已加载）。
        access: 读或写。
        grants: 适用于该 KB 的授权记录（grantee_type 为 user/role；调用方已按 kb 过滤）。
    """
    # 1) 跨租户硬隔离前置校验（红线，先于一切 visibility/grant）
    if kb_tenant_id != identity.tenant_id:
        return KbDecision.denied(404)

    is_write = access == KbAccessEnum.WRITE
    subject_id = identity.acting_subject_id

    # 2) 外部用户二级隔离（External_User_Tenant 内逐外部用户隔离）
    if identity.is_external_user:
        if kb_visibility == KbVisibilityEnum.ORGANIZATION.value:
            # 公共库：读放行；写一律 403（仅内置管理员维护）
            return KbDecision.denied(403) if is_write else KbDecision.allowed()
        # 私有库：仅自有放行，否则 404（不泄露他人私有库存在性）
        if kb_owner_user_id is not None and kb_owner_user_id == subject_id:
            return KbDecision.allowed()
        return KbDecision.denied(404)

    # 3) 租户级 Key（Virtual_Identity）：无自然人主体，访问完全由 ApiKey_Authorized_Scope
    #    裁决，不走 owner/grant。kb_scope is not None 且无 subject 即标识受 scope 约束的机器身份。
    if identity.kb_scope is not None and subject_id is None:
        if not _kb_in_scope(identity, kb_id, kb_visibility, kb_tenant_id):
            return KbDecision.denied(404)  # 不在 scope -> 不泄露存在性
        if is_write and kb_visibility == KbVisibilityEnum.ORGANIZATION.value:
            # 写公共库需 kb:write_public；租户级 Key 固定不含该权限点 -> 403
            return (
                KbDecision.allowed()
                if identity.has_permission(PermissionEnum.KB_WRITE_PUBLIC.value)
                else KbDecision.denied(403)
            )
        return KbDecision.allowed()

    # 4) owner 读写均放行（注册用户 / 用户级 Key 绑定用户）
    if kb_owner_user_id is not None and subject_id is not None and kb_owner_user_id == subject_id:
        decision = KbDecision.allowed()
    elif kb_visibility == KbVisibilityEnum.ORGANIZATION.value:
        # 5) 组织公共库：同租户可读；写需 kb:write_public
        if is_write:
            if identity.has_permission(PermissionEnum.KB_WRITE_PUBLIC.value):
                decision = KbDecision.allowed()
            else:
                decision = KbDecision.denied(403)
        else:
            decision = KbDecision.allowed()
    else:
        # 6) 私有库且非 owner：依据 grants（user/role）
        decision = _decide_by_grants(identity, grants, is_write)

    return decision


def _decide_by_grants(
    identity: IdentityContext, grants: list[GrantView], is_write: bool
) -> KbDecision:
    """私有库非 owner：按 user/role grant 裁决。

    - 命中 write grant -> 读写均放行。
    - 命中 read grant -> 读放行；写 403（可读但无写）。
    - 无任何匹配 grant -> 不可读 -> 404（不泄露他人私有库存在性）。
    """
    user_id = identity.user_id
    role_ids = identity.role_ids

    best_read = False
    best_write = False
    for g in grants:
        matched = (
            (g.grantee_type == GranteeTypeEnum.USER.value and g.grantee_id == user_id)
            or (g.grantee_type == GranteeTypeEnum.ROLE.value and g.grantee_id in role_ids)
        )
        if not matched:
            continue
        if g.permission == GrantPermissionEnum.WRITE.value:
            best_write = True
            best_read = True
        elif g.permission == GrantPermissionEnum.READ.value:
            best_read = True

    if not best_read:
        # 完全不可读：与"不存在"不可区分
        return KbDecision.denied(404)
    if is_write and not best_write:
        # 可读但无写权
        return KbDecision.denied(403)
    return KbDecision.allowed()
