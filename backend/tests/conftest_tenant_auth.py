"""tenant-rbac-refactor 属性测试公共基座：hypothesis strategies + 内存伪实现。

鉴权判定（kb_authorization_decision）、口令哈希、JWT 被设计为纯函数/可注入，
故大多数属性可不触达真实 PG/Milvus，以内存数据高频验证。

权限模型为「固定角色（admin/member）+ 归属轴」，
身份生成器一律产出 ``role``（admin / member / None），不再有 effective_permissions / role_ids。
"""

from __future__ import annotations

from hypothesis import settings, HealthCheck
from hypothesis import strategies as st

from app.auth.constants import (
    GranteeTypeEnum,
    GrantPermissionEnum,
    KbVisibilityEnum,
    OrgPermissionEnum,
    TenantRoleEnum,
)
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    KbScope,
    OperationLevelEnum,
)
from app.auth.kb_authz import GrantView

# 统一 profile：每条属性 >=100 次迭代
settings.register_profile(
    "tenant_auth",
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
settings.load_profile("tenant_auth")


# ============================================================
# 基础生成器
# ============================================================

tenant_ids = st.sampled_from(["t1", "t2", "t3", "tenant-external-builtin"])
user_ids = st.sampled_from(["u1", "u2", "u3"])
visibilities = st.sampled_from([KbVisibilityEnum.PRIVATE.value, KbVisibilityEnum.ORGANIZATION.value])
permissions = st.sampled_from([GrantPermissionEnum.READ.value, GrantPermissionEnum.WRITE.value])
# 组织公共库开放维度（仅 visibility=organization 有效；private 忽略）
org_permissions = st.sampled_from([OrgPermissionEnum.READ.value, OrgPermissionEnum.WRITE.value])

# 含合法（user）与「遗留/非法」取值的 grantee_type。
# 自定义角色 / 组织 / 租户级被授权主体枚举已删除，这里用字符串字面量保留它们，
# 以便属性测试断言这些遗留/非法值不在 GRANTEE_TYPES_ENABLED 内。
grantee_types_any = st.sampled_from([
    GranteeTypeEnum.USER.value, "role", "organization", "tenant", "bogus",
])


@st.composite
def jwt_identities(draw, super_admin: bool | None = None, role=None):
    """随机 JWT 用户身份（普通用户或超管）。

    身份的能力由固定角色 ``role`` 承载：
    - 超管（Super_Admin）：role=None、op_level=platform、tenant_id=None。
    - 租户用户：role∈{admin, member}、op_level=tenant、带 tenant_id。

    Args:
        super_admin: 固定是否为超管；None 时随机。
        role: 固定租户角色（仅非超管时生效）；None 时随机 admin/member。
    """
    is_super = draw(st.booleans()) if super_admin is None else super_admin
    if is_super:
        chosen_role = None
        op_level = OperationLevelEnum.PLATFORM
        tenant_id = None
    else:
        chosen_role = (
            role
            if role is not None
            else draw(st.sampled_from([TenantRoleEnum.ADMIN, TenantRoleEnum.MEMBER]))
        )
        op_level = OperationLevelEnum.TENANT
        tenant_id = draw(tenant_ids)
    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=op_level,
        tenant_id=tenant_id,
        user_id=draw(user_ids),
        is_super_admin=is_super,
        role=chosen_role,
    )


@st.composite
def tenant_key_identities(draw):
    """随机租户级 Key 身份（Virtual_Identity，机器身份，受 scope 约束）。

    机器身份没有固定角色（role=None），访问完全由 kb_scope 裁剪。
    """
    return IdentityContext(
        source=IdentitySourceEnum.API_KEY,
        op_level=OperationLevelEnum.TENANT,
        tenant_id=draw(tenant_ids),
        api_key_id="k-" + draw(st.text(min_size=1, max_size=4, alphabet="abc")),
        role=None,
        kb_scope=KbScope(
            all_public_kbs=draw(st.booleans()),
            explicit_kb_ids=frozenset(draw(st.sets(st.sampled_from(["kb1", "kb2", "kb3"]), max_size=3))),
        ),
    )


@st.composite
def external_user_identities(draw, external_user_id=None):
    """随机外部用户身份（external_agent 通道）。

    外部用户固定角色为 member（role=member），tenant_id 硬锁为 External_User_Tenant。

    Args:
        external_user_id: 固定外部用户 id（供需要指定外部主体的属性使用）；None 时随机。
    """
    ext_id = external_user_id if external_user_id is not None else draw(st.sampled_from(["e1", "e2", "e3"]))
    return IdentityContext(
        source=IdentitySourceEnum.API_KEY,
        op_level=OperationLevelEnum.TENANT,
        tenant_id="tenant-external-builtin",
        external_user_id=ext_id,
        api_key_id="proxy-key",
        role=TenantRoleEnum.MEMBER,
    )


@st.composite
def two_external_user_identities(draw):
    """两个 external_user_id 不同的外部用户身份（Property 5 专用）。

    两者均 role=member、同处 External_User_Tenant，但 external_user_id 必不相同，
    用于断言一方私有库对另一方读/写恒 404（外部用户之间私有库互不可见）。

    Returns:
        (ident_a, ident_b) 元组，两者 external_user_id 保证不同。
    """
    pool = ["e1", "e2", "e3"]
    a_id = draw(st.sampled_from(pool))
    # 从剩余取值里挑第二个，保证与 a_id 必然不同（不依赖 assume，稳健）
    remaining = [x for x in pool if x != a_id]
    b_id = draw(st.sampled_from(remaining))
    ident_a = draw(external_user_identities(external_user_id=a_id))
    ident_b = draw(external_user_identities(external_user_id=b_id))
    return ident_a, ident_b


@st.composite
def any_identities(draw):
    """任意身份（JWT 普通/超管、租户级 Key、外部用户）。"""
    kind = draw(st.sampled_from(["jwt", "super", "tenant_key", "external"]))
    if kind == "jwt":
        return draw(jwt_identities(super_admin=False))
    if kind == "super":
        return draw(jwt_identities(super_admin=True))
    if kind == "tenant_key":
        return draw(tenant_key_identities())
    return draw(external_user_identities())


@st.composite
def kb_records(draw, tenant_id=None):
    """随机知识库（id/tenant/owner/visibility/org_permission）。

    org_permission 仅在 visibility=organization 时参与判定（private 忽略），
    但此处一律采样，确保判定函数对 private 库忽略该维度也被覆盖。
    """
    return {
        "kb_id": draw(st.sampled_from(["kb1", "kb2", "kb3"])),
        "kb_tenant_id": tenant_id if tenant_id is not None else draw(tenant_ids),
        "kb_owner_user_id": draw(st.sampled_from(["u1", "u2", "u3", "e1", "e2", None])),
        "kb_visibility": draw(visibilities),
        "kb_org_permission": draw(org_permissions),
    }


@st.composite
def grant_views(draw):
    """随机授权记录（点对点共享收敛后仅 grantee_type=user）。

    被授权者可为注册用户（u1/u2/u3）或外部用户（e1/e2，外部用户现在也可接收共享）。
    """
    return GrantView(
        grantee_type=GranteeTypeEnum.USER.value,
        grantee_id=draw(st.sampled_from(["u1", "u2", "u3", "e1", "e2"])),
        permission=draw(permissions),
    )
