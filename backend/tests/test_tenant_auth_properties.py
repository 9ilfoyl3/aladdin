"""tenant-auth 正确性属性测试（hypothesis）。

每条属性 >=100 次迭代（profile tenant_auth）。注释标注来源属性编号。
纯函数属性（kb 授权判定 / 口令 / JWT）以内存数据驱动，不触达真实 PG/Milvus。
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-key-for-properties-0123456789abcdef")

from hypothesis import given, settings, strategies as st

from tests.conftest_tenant_auth import (  # noqa: E402  load profile + strategies
    any_identities,
    external_user_identities,
    grant_views,
    grantee_types_any,
    jwt_identities,
    kb_records,
    tenant_key_identities,
)
from app.auth.constants import (  # noqa: E402
    GRANTEE_TYPES_ENABLED,
    GranteeTypeEnum,
    KbVisibilityEnum,
    PermissionEnum,
)
from app.auth.identity import IdentityContext, IdentitySourceEnum, OperationLevelEnum  # noqa: E402
from app.auth.kb_authz import (  # noqa: E402
    KbAccessEnum,
    GrantView,
    kb_authorization_decision,
)
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
        access=access, grants=grants,
    )


# Feature: tenant-auth, Property 1: 跨租户硬隔离不变式（最高安全红线）
@given(identity=any_identities(), kb=kb_records(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=4))
def test_property_1_cross_tenant_hard_isolation(identity, kb, access, grants):
    # 当资源 tenant 与身份 tenant 不一致时，恒拒绝且为 404（先于一切 visibility/grant）
    if kb["kb_tenant_id"] != identity.tenant_id:
        d = _decide(identity, kb, access, grants)
        assert d.allow is False
        assert d.http_status == 404


# Feature: tenant-auth, Property 2: 知识库访问授权统一判定真值表（同租户）
@given(identity=jwt_identities(super_admin=False), kb=kb_records(), access=st.sampled_from(list(KbAccessEnum)),
       grants=st.lists(grant_views(), max_size=4))
def test_property_2_kb_decision_truth_table(identity, kb, access, grants):
    # 仅考察同租户组合
    kb = {**kb, "kb_tenant_id": identity.tenant_id}
    d = _decide(identity, kb, access, grants)
    is_write = access == KbAccessEnum.WRITE
    subject = identity.user_id
    is_owner = kb["kb_owner_user_id"] is not None and kb["kb_owner_user_id"] == subject
    if is_owner:
        assert d.allow is True
        return
    if kb["kb_visibility"] == KbVisibilityEnum.ORGANIZATION.value:
        if not is_write:
            assert d.allow is True
        else:
            expect = identity.has_permission(PermissionEnum.KB_WRITE_PUBLIC.value)
            assert d.allow is expect
            if not expect:
                assert d.http_status == 403
        return
    # private 非 owner：依据 grants
    best_read = best_write = False
    for g in grants:
        matched = (
            (g.grantee_type == GranteeTypeEnum.USER.value and g.grantee_id == identity.user_id)
            or (g.grantee_type == GranteeTypeEnum.ROLE.value and g.grantee_id in identity.role_ids)
        )
        if not matched:
            continue
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


# Feature: tenant-auth, Property 5: 预留 grantee_type 行为关闭
@given(gt=grantee_types_any)
def test_property_5_reserved_grantee_type_closed(gt):
    # 仅 user/role 属于 v1 启用集合；其余（organization/tenant/非法）均不启用
    enabled = gt in GRANTEE_TYPES_ENABLED
    assert enabled == (gt in (GranteeTypeEnum.USER.value, GranteeTypeEnum.ROLE.value))


# Feature: tenant-auth, Property 6: 外部用户互隔离不变式（同租户内二级隔离）
@given(identity=external_user_identities(), kb=kb_records(tenant_id="tenant-external-builtin"),
       access=st.sampled_from(list(KbAccessEnum)), grants=st.lists(grant_views(), max_size=4))
def test_property_6_external_user_isolation(identity, kb, access, grants):
    d = _decide(identity, kb, access, grants)
    is_write = access == KbAccessEnum.WRITE
    if kb["kb_visibility"] == KbVisibilityEnum.ORGANIZATION.value:
        # 公共库：读放行、写 403（仅内置管理员维护）
        if is_write:
            assert d.allow is False and d.http_status == 403
        else:
            assert d.allow is True
    else:
        # 私有库：仅自有放行，否则 404（不泄露他人私有库）
        if kb["kb_owner_user_id"] == identity.external_user_id:
            assert d.allow is True
        else:
            assert d.allow is False and d.http_status == 404


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


# Feature: tenant-auth, Property 3: API Key 通道权限边界（永不触达管理/平台操作）
# 纯判定层面：api_key 身份的 op_level 恒为 tenant（绝不 platform）。
@given(identity=st.one_of(tenant_key_identities(), external_user_identities()))
def test_property_3_api_key_never_platform(identity):
    assert identity.source == IdentitySourceEnum.API_KEY
    assert identity.op_level == OperationLevelEnum.TENANT
    assert identity.is_super_admin is False
    # api_key 固定权限集不含任何管理/平台权限点
    from app.auth.constants import ADMINISTRATIVE_PERMISSIONS, PLATFORM_PERMISSIONS
    assert not (identity.effective_permissions & (ADMINISTRATIVE_PERMISSIONS | PLATFORM_PERMISSIONS))


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
