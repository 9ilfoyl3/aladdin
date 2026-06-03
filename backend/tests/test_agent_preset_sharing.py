"""智能体预设归属与开放可见性测试（agent-preset-sharing）。

覆盖纯判定逻辑：
- _is_visible：内置 ∪ 本租户自有 ∪ 本租户已开放；管理员无特权看他人私有；跨租户不可见。
- _ensure_owner_or_404：仅创建者可改/删；内置 403；可见但非创建者 403；不可见 404。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.agent_config import _is_visible, _ensure_owner_or_404, _is_builtin
from app.auth.constants import TenantRoleEnum
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    OperationLevelEnum,
)
from app.schema.db import AgentPreset


def _identity(*, user_id: str, tenant_id: str, role: TenantRoleEnum = TenantRoleEnum.MEMBER,
              is_super_admin: bool = False) -> IdentityContext:
    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.PLATFORM if is_super_admin else OperationLevelEnum.TENANT,
        tenant_id=tenant_id,
        user_id=user_id,
        username=user_id,
        is_super_admin=is_super_admin,
        role=None if is_super_admin else role,
    )


def _preset(*, id="p1", tenant_id="T1", owner="u1", is_shared=False) -> AgentPreset:
    return AgentPreset(
        id=id, name="n", description=None, config_json={}, is_default=False,
        tenant_id=tenant_id, owner_user_id=owner, is_shared=is_shared,
    )


# ---------- 可见性 ----------

def test_owner_sees_own_private():
    """创建者可见自己的私有预设。"""
    me = _identity(user_id="u1", tenant_id="T1")
    assert _is_visible(_preset(owner="u1", is_shared=False), me) is True


def test_other_member_cannot_see_private():
    """同租户他人看不到未开放的私有预设。"""
    other = _identity(user_id="u2", tenant_id="T1")
    assert _is_visible(_preset(owner="u1", is_shared=False), other) is False


def test_admin_cannot_see_others_private():
    """租户管理员也看不到他人未开放的私有预设（管理员无特权）。"""
    admin = _identity(user_id="admin", tenant_id="T1", role=TenantRoleEnum.ADMIN)
    assert _is_visible(_preset(owner="u1", is_shared=False), admin) is False


def test_shared_visible_to_same_tenant():
    """已开放预设对本租户他人可见。"""
    other = _identity(user_id="u2", tenant_id="T1")
    assert _is_visible(_preset(owner="u1", is_shared=True), other) is True


def test_shared_not_visible_cross_tenant():
    """已开放预设对其他租户不可见（租户隔离）。"""
    outsider = _identity(user_id="u9", tenant_id="T2")
    assert _is_visible(_preset(owner="u1", tenant_id="T1", is_shared=True), outsider) is False


def test_builtin_visible_to_everyone():
    """内置预设（tenant_id=None）对任何租户成员可见。"""
    builtin = AgentPreset(id="preset-quick-qa", name="快速问答", description=None,
                          config_json={}, is_default=False, tenant_id=None,
                          owner_user_id=None, is_shared=True)
    assert _is_builtin(builtin) is True
    for tid in ("T1", "T2"):
        who = _identity(user_id="x", tenant_id=tid)
        assert _is_visible(builtin, who) is True


# ---------- 管理权（改/删）----------

def test_owner_can_manage():
    """创建者可改/删自己的预设。"""
    me = _identity(user_id="u1", tenant_id="T1")
    p = _preset(owner="u1")
    assert _ensure_owner_or_404(p, me) is p


def test_non_owner_shared_cannot_manage():
    """非创建者即便能看到已开放预设也不能改/删（403）。"""
    other = _identity(user_id="u2", tenant_id="T1")
    with pytest.raises(HTTPException) as ei:
        _ensure_owner_or_404(_preset(owner="u1", is_shared=True), other)
    assert ei.value.status_code == 403


def test_admin_cannot_manage_others():
    """管理员不能改/删他人私有预设：不可见 → 404（不泄露存在性）。"""
    admin = _identity(user_id="admin", tenant_id="T1", role=TenantRoleEnum.ADMIN)
    with pytest.raises(HTTPException) as ei:
        _ensure_owner_or_404(_preset(owner="u1", is_shared=False), admin)
    assert ei.value.status_code == 404


def test_builtin_cannot_be_managed():
    """内置预设任何人不可改/删（403）。"""
    builtin = AgentPreset(id="preset-smart-reasoning", name="智能推理", description=None,
                          config_json={}, is_default=True, tenant_id=None,
                          owner_user_id=None, is_shared=True)
    admin = _identity(user_id="admin", tenant_id="T1", role=TenantRoleEnum.ADMIN)
    with pytest.raises(HTTPException) as ei:
        _ensure_owner_or_404(builtin, admin)
    assert ei.value.status_code == 403


def test_missing_preset_404():
    """预设不存在 → 404。"""
    me = _identity(user_id="u1", tenant_id="T1")
    with pytest.raises(HTTPException) as ei:
        _ensure_owner_or_404(None, me)
    assert ei.value.status_code == 404
