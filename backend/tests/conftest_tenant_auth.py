"""tenant-auth 属性测试公共基座：hypothesis strategies + 内存伪实现。

鉴权判定（kb_authorization_decision）、口令哈希、JWT 被设计为纯函数/可注入，
故大多数属性可不触达真实 PG/Milvus，以内存数据高频验证。
"""

from __future__ import annotations

from hypothesis import settings, HealthCheck
from hypothesis import strategies as st

from app.auth.constants import (
    GranteeTypeEnum,
    GrantPermissionEnum,
    KbVisibilityEnum,
    PermissionEnum,
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
role_id_sets = st.sets(st.sampled_from(["r1", "r2", "r3"]), max_size=3)
visibilities = st.sampled_from([KbVisibilityEnum.PRIVATE.value, KbVisibilityEnum.ORGANIZATION.value])
permissions = st.sampled_from([GrantPermissionEnum.READ.value, GrantPermissionEnum.WRITE.value])

# 含合法与预留/非法的 grantee_type
grantee_types_any = st.sampled_from([
    GranteeTypeEnum.USER.value, GranteeTypeEnum.ROLE.value,
    GranteeTypeEnum.ORGANIZATION.value, GranteeTypeEnum.TENANT.value,
    "bogus",
])


@st.composite
def jwt_identities(draw, super_admin: bool | None = None):
    """随机 JWT 用户身份（普通用户或超管）。"""
    is_super = draw(st.booleans()) if super_admin is None else super_admin
    perms = draw(st.sets(st.sampled_from([
        PermissionEnum.KB_READ.value, PermissionEnum.KB_WRITE.value,
        PermissionEnum.KB_WRITE_PUBLIC.value, PermissionEnum.KB_CREATE.value,
        PermissionEnum.QA_INVOKE.value,
    ]), max_size=5))
    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.PLATFORM if is_super else OperationLevelEnum.TENANT,
        tenant_id=None if is_super else draw(tenant_ids),
        user_id=draw(user_ids),
        is_super_admin=is_super,
        effective_permissions=frozenset(perms),
        role_ids=frozenset(draw(role_id_sets)),
    )


@st.composite
def tenant_key_identities(draw):
    """随机租户级 Key 身份（Virtual_Identity，受 scope 约束）。"""
    return IdentityContext(
        source=IdentitySourceEnum.API_KEY,
        op_level=OperationLevelEnum.TENANT,
        tenant_id=draw(tenant_ids),
        api_key_id="k-" + draw(st.text(min_size=1, max_size=4, alphabet="abc")),
        effective_permissions=frozenset({
            PermissionEnum.KB_READ.value, PermissionEnum.KB_WRITE.value,
            PermissionEnum.QA_INVOKE.value,
        }),
        kb_scope=KbScope(
            all_public_kbs=draw(st.booleans()),
            explicit_kb_ids=frozenset(draw(st.sets(st.sampled_from(["kb1", "kb2", "kb3"]), max_size=3))),
        ),
    )


@st.composite
def external_user_identities(draw):
    """随机外部用户身份（external_agent 通道）。"""
    return IdentityContext(
        source=IdentitySourceEnum.API_KEY,
        op_level=OperationLevelEnum.TENANT,
        tenant_id="tenant-external-builtin",
        external_user_id=draw(st.sampled_from(["e1", "e2", "e3"])),
        api_key_id="proxy-key",
        effective_permissions=frozenset({
            PermissionEnum.KB_READ.value, PermissionEnum.KB_WRITE.value,
            PermissionEnum.QA_INVOKE.value,
        }),
    )


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
    """随机知识库（id/tenant/owner/visibility）。"""
    return {
        "kb_id": draw(st.sampled_from(["kb1", "kb2", "kb3"])),
        "kb_tenant_id": tenant_id if tenant_id is not None else draw(tenant_ids),
        "kb_owner_user_id": draw(st.sampled_from(["u1", "u2", "u3", "e1", "e2", None])),
        "kb_visibility": draw(visibilities),
    }


@st.composite
def grant_views(draw):
    """随机授权记录（含合法 user/role）。"""
    return GrantView(
        grantee_type=draw(st.sampled_from([GranteeTypeEnum.USER.value, GranteeTypeEnum.ROLE.value])),
        grantee_id=draw(st.sampled_from(["u1", "u2", "u3", "r1", "r2", "r3"])),
        permission=draw(permissions),
    )
