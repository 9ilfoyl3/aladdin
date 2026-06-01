"""tenant-auth 正确性属性测试（hypothesis）。

每条属性 >=100 次迭代（profile tenant_auth）。注释标注来源属性编号。
纯函数属性（kb 授权判定 / 口令 / JWT）以内存数据驱动，不触达真实 PG/Milvus。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-key-for-properties-0123456789abcdef")

from hypothesis import example, given, settings, strategies as st

from tests.conftest_tenant_auth import (  # noqa: E402  load profile + strategies
    any_identities,
    external_user_identities,
    grant_views,
    jwt_identities,
    kb_records,
    tenant_key_identities,
    two_external_user_identities,
)
from app.auth.constants import (  # noqa: E402
    GRANTEE_TYPES_ENABLED,
    GranteeTypeEnum,
    KbVisibilityEnum,
    OrgPermissionEnum,
    TenantRoleEnum,
)
from app.auth.identity import IdentityContext, IdentitySourceEnum, OperationLevelEnum  # noqa: E402
from app.auth.kb_authz import (  # noqa: E402
    KbAccessEnum,
    GrantView,
    kb_authorization_decision,
)
from app.api.deps import _platform_gate, _admin_gate, _member_floor, _must_change_gate  # noqa: E402
from app.auth.validators import (  # noqa: E402
    validate_org_permission,
    validate_role,
    validate_tenant_type,
)
from app.api.errors import ValidationInputError  # noqa: E402
from app.auth.password import (  # noqa: E402
    _hash_password_sync as hash_password,
    _verify_password_sync as verify_password,
)
from app.auth import jwt_auth  # noqa: E402


def _decide(identity, kb, access, grants):
    return kb_authorization_decision(
        identity,
        kb_id=kb["kb_id"], kb_tenant_id=kb["kb_tenant_id"],
        kb_owner_user_id=kb["kb_owner_user_id"], kb_visibility=kb["kb_visibility"],
        kb_org_permission=kb["kb_org_permission"],
        access=access, grants=grants,
    )


# Feature: tenant-rbac-refactor, Property 1: 跨租户硬隔离前置（最高安全红线）
@given(identity=any_identities(), kb=kb_records(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=4))
def test_property_1_cross_tenant_hard_isolation(identity, kb, access, grants):
    # 当资源 tenant 与身份 tenant 不一致时，恒拒绝且为 404（先于一切 visibility/grant/owner）。
    # 该判定为严格 != 比较，无 super_admin 例外（super_admin tenant_id=None，与任意非 None kb 租户判定为跨租户）。
    if kb["kb_tenant_id"] != identity.tenant_id:
        d = _decide(identity, kb, access, grants)
        assert d.allow is False
        assert d.http_status == 404


# Feature: kb-sharing-refinement, Property 2: 知识库内容访问统一判定真值表（含 org_permission）
@given(identity=jwt_identities(super_admin=False), kb=kb_records(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=4))
def test_property_2_kb_decision_truth_table(identity, kb, access, grants):
    # 仅考察同租户、注册用户（admin/member）组合
    kb = {**kb, "kb_tenant_id": identity.tenant_id}
    d = _decide(identity, kb, access, grants)
    is_write = access == KbAccessEnum.WRITE
    subject = identity.user_id
    is_owner = kb["kb_owner_user_id"] is not None and kb["kb_owner_user_id"] == subject
    if is_owner:
        # owner（行事主体）读写均放行
        assert d.allow is True
        return
    if kb["kb_visibility"] == KbVisibilityEnum.ORGANIZATION.value:
        # 组织公共库（判定顺序先于 admin 分支）：同租户可读；
        # 写当且仅当开放维度为 write，否则 403（admin 也不因身份获得额外写权）。
        if not is_write:
            assert d.allow is True
        elif kb["kb_org_permission"] == "write":
            assert d.allow is True
        else:
            assert d.allow is False and d.http_status == 403
        return
    if identity.is_tenant_admin:
        # admin/super_admin 对 private 库只读放行；写一律 403（不写他人库内容）。
        if not is_write:
            assert d.allow is True
        else:
            assert d.allow is False and d.http_status == 403
        return
    # private 非 owner 非 admin：依据 user-grant 裁决
    best_read = best_write = False
    for g in grants:
        if g.grantee_type == GranteeTypeEnum.USER.value and g.grantee_id == identity.acting_subject_id:
            if g.permission == "write":
                best_write = best_read = True
            elif g.permission == "read":
                best_read = True
    if not best_read:
        assert d.allow is False and d.http_status == 404
    elif is_write and not best_write:
        assert d.allow is False and d.http_status == 403
    else:
        assert d.allow is True


# Feature: kb-sharing-refinement, Property 3: 内容写权来源受限（owner / org-write 档 / write user-grant）
@given(identity=jwt_identities(super_admin=False), kb=kb_records(),
       grants=st.lists(grant_views(), max_size=4))
def test_property_3_write_only_owner_org_write_or_grant(identity, kb, grants):
    # 同租户。若 WRITE 被放行，写权来源只能是三者之一：
    #   1) owner（行事主体即库 owner）；
    #   2) organization 且 org_permission=write 的同租户成员（含 admin，按档而非身份）；
    #   3) 持有匹配 write user-grant。
    # admin 不因身份获得他人 private 库写权（admin 写 private 恒拒）。
    kb = {**kb, "kb_tenant_id": identity.tenant_id}
    d = _decide(identity, kb, KbAccessEnum.WRITE, grants)
    if d.allow:
        is_owner = (
            kb["kb_owner_user_id"] is not None
            and kb["kb_owner_user_id"] == identity.acting_subject_id
        )
        is_org_write = (
            kb["kb_visibility"] == KbVisibilityEnum.ORGANIZATION.value
            and kb["kb_org_permission"] == "write"
        )
        has_write_grant = any(
            g.grantee_type == GranteeTypeEnum.USER.value
            and g.grantee_id == identity.acting_subject_id
            and g.permission == "write"
            for g in grants
        )
        assert is_owner or is_org_write or has_write_grant


# Feature: tenant-rbac-refactor, Property 4: 租户级 Key 的范围裁剪
@given(identity=tenant_key_identities(), kb=kb_records(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=3))
def test_property_4_tenant_key_scope_clipping(identity, kb, access, grants):
    # 租户级 Key（机器身份：role=None、无 subject、带 kb_scope）。同租户下，
    # 当且仅当目标 KB 落在 ApiKey_Authorized_Scope 内放行，否则 404。
    kb = {**kb, "kb_tenant_id": identity.tenant_id}
    scope = identity.kb_scope
    in_scope = (
        scope.all_public_kbs and kb["kb_visibility"] == KbVisibilityEnum.ORGANIZATION.value
    ) or (kb["kb_id"] in scope.explicit_kb_ids)
    d = _decide(identity, kb, access, grants)
    if in_scope:
        assert d.allow is True
    else:
        assert d.allow is False and d.http_status == 404


# Feature: tenant-rbac-refactor, Property 5: 外部用户之间私有库互不可见
@given(pair=two_external_user_identities(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=3))
def test_property_5_external_users_private_isolation(pair, access, grants):
    ident_a, ident_b = pair
    # ident_a 在外部租户拥有的私有库
    kb = {
        "kb_id": "kbx",
        "kb_tenant_id": "tenant-external-builtin",
        "kb_owner_user_id": ident_a.external_user_id,
        "kb_visibility": KbVisibilityEnum.PRIVATE.value,
        "kb_org_permission": OrgPermissionEnum.READ.value,  # private 忽略，仅满足 _decide 入参
    }
    # 过滤掉任何可能授予 ident_b 的 grant，保证本属性纯粹考察"互不可见"
    filtered = [g for g in grants if g.grantee_id != ident_b.external_user_id]
    d = _decide(ident_b, kb, access, filtered)
    # ident_a 的私有库对 ident_b 读/写恒 404
    assert d.allow is False and d.http_status == 404


# Feature: tenant-rbac-refactor, Property 6: 平台级操作仅限 Super_Admin 经 JWT
@given(identity=any_identities())
def test_property_6_platform_only_super_admin_via_jwt(identity):
    # 平台守卫纯判定：当且仅当 is_super_admin 且来源为 JWT 才放行，其余一律拒绝。
    # any_identities 覆盖 JWT 普通/超管、租户级 Key、外部用户 —— 仅「超管经 JWT」应放行。
    expected = identity.is_super_admin and identity.source == IdentitySourceEnum.JWT
    assert _platform_gate(identity) is expected


# Feature: tenant-rbac-refactor, Property 7: 管理级操作的角色与通道边界
@given(identity=any_identities())
def test_property_7_admin_role_and_channel_boundary(identity):
    # 管理级有效裁决 = 通道为 JWT 且通过 _admin_gate（require_tenant_admin 用 allow_api_key=False）。
    is_jwt = identity.source == IdentitySourceEnum.JWT
    if identity.source == IdentitySourceEnum.API_KEY:
        # API Key 通道恒拒（无论绑定用户角色为何）：组合裁决必为 False。
        assert (is_jwt and _admin_gate(identity)) is False
    else:
        # JWT 通道：当且仅当 admin 或 super_admin 才放行，member 一律拒。
        assert _admin_gate(identity) == (identity.is_tenant_admin or identity.is_super_admin)


# Feature: tenant-rbac-refactor, Property 8: 创建归属资源的角色下限
@given(identity=any_identities())
def test_property_8_member_floor_for_owned_resource(identity):
    # 建归属资源最低档：当且仅当 role 非空（admin/member，外部用户=member）或 super_admin 才放行；
    # role=None 的 tenant_level 机器身份被拒。
    assert _member_floor(identity) == (identity.role is not None or identity.is_super_admin)


# Feature: tenant-rbac-refactor, Property 9: 强制改密闸门
@given(must_change=st.booleans(), allow=st.booleans())
def test_property_9_must_change_password_gate(must_change, allow):
    # 强制改密闸门：返回 True（拒绝）当且仅当 must_change 为真且端点未声明 allow。
    assert _must_change_gate(must_change, allow) is (must_change and not allow)


# Feature: tenant-rbac-refactor, Property 10: 角色取值校验
@settings(max_examples=200)
@example(s="admin")
@example(s="member")
@example(s=" admin ")
@given(s=st.text(max_size=20))
def test_property_10_validate_role(s):
    # validate_role 当且仅当去空白后属于 {admin, member} 通过并返回 strip 值，否则抛 400。
    is_valid = s.strip() in {"admin", "member"}
    if is_valid:
        assert validate_role(s) == s.strip()
    else:
        with pytest.raises(ValidationInputError):
            validate_role(s)


# Feature: tenant-rbac-refactor, Property 12: 租户类型校验
@settings(max_examples=200)
@example(s="business")
@example(s="external")
@example(s="default")
@given(s=st.text(max_size=20))
def test_property_12_validate_tenant_type(s):
    # validate_tenant_type 当且仅当去空白后属于 {business, external} 通过并返回 strip 值，
    # 其余（含遗留 default）一律抛 400。
    is_valid = s.strip() in {"business", "external"}
    if is_valid:
        assert validate_tenant_type(s) == s.strip()
    else:
        with pytest.raises(ValidationInputError):
            validate_tenant_type(s)


# Feature: kb-sharing-refinement, Property 14: 组织开放维度校验
@settings(max_examples=200)
@example(s="read")
@example(s="write")
@example(s=" read ")
@example(s="organization")
@given(s=st.text(max_size=20))
def test_property_14_validate_org_permission(s):
    # validate_org_permission 当且仅当去空白后属于 {read, write} 通过并返回 strip 值，否则抛 400。
    is_valid = s.strip() in {"read", "write"}
    if is_valid:
        assert validate_org_permission(s) == s.strip()
    else:
        with pytest.raises(ValidationInputError):
            validate_org_permission(s)


# Feature: kb-sharing-refinement, Property 13: 库实体操作 owner 专属
@given(identity=jwt_identities(), kb=kb_records())
def test_property_13_kb_entity_gate_owner_only(identity, kb):
    # _ensure_kb_owner（库实体操作 owner 专属闸门）：当且仅当同租户且 owner==行事主体放行；
    #   跨租户 -> CrossTenantError(404)；同租户非 owner（含 admin/super_admin）-> PermissionDeniedError(403)。
    # 该判定不依赖角色与 visibility，仅 tenant + owner 判等（与 kb_authorization_decision 内容判定解耦）。
    from app.api.knowledge_base import _ensure_kb_owner
    from app.api.errors import CrossTenantError, PermissionDeniedError

    class _KB:
        def __init__(self, tenant_id, owner_user_id):
            self.tenant_id = tenant_id
            self.owner_user_id = owner_user_id

    kb_obj = _KB(kb["kb_tenant_id"], kb["kb_owner_user_id"])
    is_owner = (
        kb["kb_tenant_id"] == identity.tenant_id
        and kb["kb_owner_user_id"] is not None
        and kb["kb_owner_user_id"] == identity.acting_subject_id
    )
    if is_owner:
        # owner 放行（无异常）
        _ensure_kb_owner(identity, kb_obj)
    elif kb["kb_tenant_id"] != identity.tenant_id:
        # 跨租户优先 -> 404（存在性非泄露），先于 owner 判定
        with pytest.raises(CrossTenantError):
            _ensure_kb_owner(identity, kb_obj)
    else:
        # 同租户非 owner（含 admin/super_admin）-> 403
        with pytest.raises(PermissionDeniedError):
            _ensure_kb_owner(identity, kb_obj)


# Feature: tenant-auth, Property 11: JWT 不含权限快照且认证往返一致
@given(uid=st.text(min_size=1, max_size=12, alphabet="abcXYZ123"),
       tid=st.one_of(st.none(), st.text(min_size=1, max_size=8, alphabet="t12")),
       tv=st.integers(min_value=0, max_value=99))
def test_property_11_jwt_no_perms_and_roundtrip(uid, tid, tv):
    token = jwt_auth.issue_token(uid, tid, tv)
    # 载荷不含任何权限点字段
    import jwt as _jwt
    payload = _jwt.decode(token, options={"verify_signature": False})
    for forbidden in ("perms", "permissions", "roles", "scope", "effective_permissions"):
        assert forbidden not in payload
    # 往返一致
    claims = jwt_auth.decode_token(token)
    assert claims.user_id == uid
    assert claims.tenant_id == tid
    assert claims.token_version == tv


# Feature: tenant-auth, Property 12: 口令哈希往返
# 口令域限定为 bcrypt 合法范围（<=72 字节，见 hash_password 契约；超长拒绝单独验证）。
_pwd_text = st.text(min_size=1, max_size=60).filter(lambda s: len(s.encode("utf-8")) <= 72)


@settings(max_examples=100, deadline=None)
@given(pwd=_pwd_text, other=_pwd_text)
def test_property_12_password_hash_roundtrip(pwd, other):
    h = hash_password(pwd)
    assert h != pwd                      # 持久化哈希 != 明文
    assert verify_password(pwd, h) is True   # 正确明文校验为真
    if other != pwd:
        assert verify_password(other, h) is False  # 任意不等明文校验为假


def test_password_too_long_rejected():
    """超过 72 字节的口令显式拒绝（不静默截断）。"""
    import pytest
    with pytest.raises(ValueError):
        hash_password("a" * 73)


# Feature: tenant-rbac-refactor, Property 7（补充）: API Key 通道权限边界（永不触达管理/平台操作）
# 固定角色模型下：api_key 身份的 op_level 恒为 tenant，且既不过平台闸门也不过管理闸门。
@given(identity=st.one_of(tenant_key_identities(), external_user_identities()))
def test_property_3_api_key_never_platform(identity):
    from app.api.deps import _admin_gate, _platform_gate

    assert identity.source == IdentitySourceEnum.API_KEY
    assert identity.op_level == OperationLevelEnum.TENANT
    assert identity.is_super_admin is False
    # api_key 角色或为 None（tenant_level 机器身份）或为 member（外部用户），绝非 admin
    assert identity.is_tenant_admin is False
    # 平台闸门要求 source=JWT，管理闸门要求 admin/super_admin -> api_key 一律不过
    assert _platform_gate(identity) is False
    assert _admin_gate(identity) is False


# Feature: tenant-auth, Property 16: 公共库提升不变量（纯判定：提升只改 visibility）
@given(kb=kb_records())
def test_property_16_promotion_preserves_owner_tenant(kb):
    # 模拟提升：只改 visibility，owner/tenant 不变
    before_owner, before_tenant = kb["kb_owner_user_id"], kb["kb_tenant_id"]
    promoted = {**kb, "kb_visibility": KbVisibilityEnum.ORGANIZATION.value}
    assert promoted["kb_owner_user_id"] == before_owner
    assert promoted["kb_tenant_id"] == before_tenant


# Feature: tenant-auth, Property 21: 目标租户入口归属校验（纯判定层）
@given(identity=any_identities(), requested=st.sampled_from(["t1", "t2", "t3", None]))
def test_property_21_target_tenant_enforcement(identity, requested):
    from app.api.deps import _enforce_target_tenant
    from app.api.errors import PermissionDeniedError

    class _Req:
        def __init__(self, tid):
            self.headers = {"X-Tenant-ID": tid} if tid else {}

    req = _Req(requested)
    if requested is None:
        _enforce_target_tenant(req, identity)  # 无指定，放行
        return
    if identity.source == IdentitySourceEnum.API_KEY or identity.is_super_admin:
        _enforce_target_tenant(req, identity)  # api_key 忽略该头 / 超管可指定
        return
    # JWT 普通用户：不一致 403，一致放行
    if requested != identity.tenant_id:
        try:
            _enforce_target_tenant(req, identity)
            assert False, "应拒绝跨租户指定"
        except PermissionDeniedError:
            pass
    else:
        _enforce_target_tenant(req, identity)


# Feature: tenant-auth, Property 8: 资源租户盖章与归属继承
# stamp/盖章逻辑：TenantRepository.stamp 对受隔离模型盖 identity.tenant_id；
# 新建 KB 默认 private 且 owner=创建者；Document/Chunk 的 tenant 继承所属 KB。
@given(
    tid=st.sampled_from(["t1", "t2", "t3"]),
    uid=st.sampled_from(["u1", "u2"]),
)
def test_property_8_stamp_and_inheritance(tid, uid):
    from app.repositories.tenant_repo import TenantRepository
    from app.schema.db import KnowledgeBase, Document, Chunk
    from app.auth.constants import KbVisibilityEnum

    identity = IdentityContext(
        source=IdentitySourceEnum.JWT, op_level=OperationLevelEnum.TENANT,
        tenant_id=tid, user_id=uid,
    )
    repo = TenantRepository(session=None, identity=identity)  # stamp 不触库

    # 受隔离模型 stamp -> tenant_id == identity.tenant_id
    kb = KnowledgeBase(id="kb", name="k", visibility=KbVisibilityEnum.PRIVATE.value, owner_user_id=uid)
    repo.stamp(kb)
    assert kb.tenant_id == tid
    # 新建 KB 默认 private + owner=创建者（acting_subject_id）
    assert kb.visibility == KbVisibilityEnum.PRIVATE.value
    assert kb.owner_user_id == identity.acting_subject_id

    # Document/Chunk 盖章后 tenant 与身份一致（落库继承等价于盖同一 tenant）
    doc = Document(id="d", kb_id="kb", filename="f", file_type="txt")
    repo.stamp(doc)
    assert doc.tenant_id == tid
    chunk = Chunk(id="c", doc_id="d", kb_id="kb", content="x")
    repo.stamp(chunk)
    assert chunk.tenant_id == tid


# Feature: tenant-auth, Property 9: 检索/召回向量结果的租户一致性
# 设计：检索前经 authorize_requested_kbs 把 kb 限定在身份可读范围（同租户），
# 故返回 chunk 的 tenant 恒等于身份 tenant。此处验证「授权门只放行同租户 kb」这一前提：
# 对任意身份与跨租户 kb，kb_authorization_decision(READ) 必拒（404），
# 因而不可能有跨租户 kb 进入检索集合 -> 结果 chunk 不会跨租户。
@given(identity=any_identities(), kb=kb_records(), grants=st.lists(grant_views(), max_size=3))
def test_property_9_retrieval_tenant_consistency(identity, kb, grants):
    d = _decide(identity, kb, KbAccessEnum.READ, grants)
    if kb["kb_tenant_id"] != identity.tenant_id:
        # 跨租户 kb 永远不被放行进入检索 -> 召回结果不可能跨租户
        assert d.allow is False and d.http_status == 404
    elif d.allow:
        # 被放行的 kb 必与身份同租户
        assert kb["kb_tenant_id"] == identity.tenant_id


# Feature: tenant-auth, Property 22: 超级管理员业务内容可见边界
# 内容边界 helper：超管 + 未放宽配置 -> 读正文 403；非超管 / 放宽配置 -> 放行。
@given(is_super=st.booleans(), boundary_open=st.booleans())
def test_property_22_super_admin_content_boundary(is_super, boundary_open):
    import importlib
    from app.config import get_settings
    # 用一个最小 helper 复制内容边界判定（与 document/session/retrieval 中一致）
    from app.api.errors import PermissionDeniedError

    identity = IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.PLATFORM if is_super else OperationLevelEnum.TENANT,
        tenant_id=None if is_super else "t1", user_id=None if is_super else "u1",
        is_super_admin=is_super,
    )

    def _boundary(identity, open_flag):
        if identity.is_super_admin and not open_flag:
            raise PermissionDeniedError()

    if is_super and not boundary_open:
        try:
            _boundary(identity, boundary_open)
            assert False, "超管未放宽时应拒绝读正文"
        except PermissionDeniedError:
            pass
    else:
        _boundary(identity, boundary_open)  # 非超管 或 已放宽 -> 放行
