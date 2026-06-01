"""知识库访问授权统一判定（tenant-rbac-refactor）。

三处收敛点之二：系统中唯一裁决知识库读/写的纯函数。
设计为「纯函数 + 注入数据」——所有依赖（KB 关键字段、适用的 user-grants）由调用方
查好后注入，本函数不触库、不触 Milvus，因而可被属性测试高频驱动。

权限模型为 WeKnora 式「固定角色（admin/member）+ 归属轴（owner）」，不再有任何权限点字典。

判定顺序（**跨租户前置永远第一**，owner 先于一切角色判定）：
  1. 跨租户硬隔离前置：kb.tenant_id != identity.tenant_id -> (deny, 404)，先于一切。
  2. tenant_level Key（Virtual_Identity，机器身份）：role 为 None 且无 subject 且持 kb_scope，
     完全由 ApiKey_Authorized_Scope 裁决——落在 scope 内读写均放行，否则 404。
     （外部用户不会进入此分支：其 role=member 且 subject=external_user_id。）
  3. owner 放行：kb.owner_user_id == 行事主体（acting_subject_id）-> 读写均放行
     （注册用户 / 用户级 Key 绑定用户 / 外部用户统一以 acting_subject_id 比对）。
  4. organization 组织公共库：同租户可读；写当且仅当 org_permission=write（对全体同租户成员一致，
     admin 不因身份获得额外写权——要写也得走 org write 档或自身为 owner）。
  5. admin/super_admin：对 private 库**只读**放行；写一律 403（admin 不写他人库内容）。
  6. private 非 owner 非 admin：依据 user-grant 裁决（无可读 grant -> 404，可读无写 -> 403）。

注意（kb-sharing-refinement）：库**实体操作**（改名/删库/改可见性/共享）不走本函数，由
KB 路由的 owner-only 闸门（_ensure_kb_owner）裁决；本函数只裁决内容读/写。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.auth.constants import (
    GranteeTypeEnum,
    GrantPermissionEnum,
    KbVisibilityEnum,
    OrgPermissionEnum,
)
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
    """注入给判定函数的授权记录视图（已按目标 KB 过滤）。

    点对点共享收敛后 ``grantee_type`` 恒为 ``"user"``（自定义角色 / 组织 / 租户级被授权主体
    已废弃），判定仅识别 user-grant；其余取值不会被匹配。
    """

    grantee_type: str  # 恒为 "user"（GranteeTypeEnum.USER.value）
    grantee_id: str    # 被授权用户 id（对外部用户即 external_user_id）
    permission: str    # read | write


def _kb_in_scope(identity: IdentityContext, kb_id: str, kb_visibility: str) -> bool:
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
    kb_org_permission: str = OrgPermissionEnum.READ.value,
    access: KbAccessEnum,
    grants: list[GrantView],
) -> KbDecision:
    """对一次知识库读/写**内容**访问做出允许或拒绝判定（系统唯一入口）。

    Args:
        identity: 当前身份。
        kb_id / kb_tenant_id / kb_owner_user_id / kb_visibility: 目标 KB 的关键字段（已加载）。
        kb_org_permission: 组织公共库开放维度（read|write）；仅 organization 有效，private 忽略。
        access: 读或写。
        grants: 适用于该 KB 的授权记录（grantee_type 恒为 user；调用方已按 kb 过滤）。
    """
    is_write = access == KbAccessEnum.WRITE
    subject = identity.acting_subject_id

    # 1) 跨租户硬隔离前置（红线，先于一切 visibility/grant/owner）
    if kb_tenant_id != identity.tenant_id:
        return KbDecision.denied(404)

    # 2) tenant_level Key（Virtual_Identity，机器身份）：role 为 None 且无自然人主体，
    #    访问完全由 ApiKey_Authorized_Scope 裁决，不走 owner/admin/grant。
    #    外部用户不会进入此分支（其 role=member 且 subject=external_user_id）。
    if identity.role is None and subject is None and identity.kb_scope is not None:
        if not _kb_in_scope(identity, kb_id, kb_visibility):
            return KbDecision.denied(404)  # 不在 scope -> 不泄露存在性
        return KbDecision.allowed()         # scope 内读写均放行（机器身份）

    # 3) owner 放行（注册用户 / 用户级 Key 绑定用户 / 外部用户，统一以 acting_subject_id 比对）
    if kb_owner_user_id is not None and subject is not None and kb_owner_user_id == subject:
        return KbDecision.allowed()

    # 4) organization 组织公共库：同租户可读；写当且仅当开放维度为 write。
    #    放在 admin 判定之前，使「组织读写档」对 admin 与普通成员一致按档生效；
    #    admin 不因身份在组织只读库获得额外写权。
    if kb_visibility == KbVisibilityEnum.ORGANIZATION.value:
        if not is_write:
            return KbDecision.allowed()
        return (
            KbDecision.allowed()
            if kb_org_permission == OrgPermissionEnum.WRITE.value
            else KbDecision.denied(403)
        )

    # 5) admin/super_admin：对 private 库只读放行（监管），写一律 403（不写他人库内容）。
    if identity.is_tenant_admin or identity.is_super_admin:
        return KbDecision.denied(403) if is_write else KbDecision.allowed()

    # 6) private 非 owner 非 admin：依据 user-grant 裁决
    return _decide_by_user_grants(identity, grants, is_write)


def _decide_by_user_grants(
    identity: IdentityContext, grants: list[GrantView], is_write: bool
) -> KbDecision:
    """私有库非 owner / 非 admin：按 user-grant 裁决。

    仅匹配 ``grantee_type == user`` 且 ``grantee_id == 行事主体（acting_subject_id）``
    （以 acting_subject_id 比对，使外部用户的 subject=external_user_id 也能一致收到授权）。
    - 命中 write grant -> 读写均放行。
    - 命中 read grant -> 读放行；写 403（可读但无写）。
    - 无任何匹配 grant -> 不可读 -> 404（不泄露他人私有库存在性）。
    """
    subject = identity.acting_subject_id

    best_read = False
    best_write = False
    for g in grants:
        # 仅识别点对点 user-grant（自定义角色已废弃，无 role-grant 分支）
        if not (g.grantee_type == GranteeTypeEnum.USER.value and g.grantee_id == subject):
            continue
        if g.permission == GrantPermissionEnum.WRITE.value:
            best_write = True  # 写授权蕴含读
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
