"""IdentityContext：单次请求经认证后的统一身份对象（只读）。

无论凭据是 JWT 还是三种 API Key，认证后都合成为同一个 IdentityContext，
供 Authorization_Guard 与各资源级判定（tenant_guard / kb_authorization_decision）使用。
该对象不持有口令、不嵌入 JWT，是冻结只读对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IdentitySourceEnum(str, Enum):
    """身份来源。"""

    JWT = "jwt"          # 人类用户 / Super_Admin
    API_KEY = "api_key"  # 三种 API Key 通道


class OperationLevelEnum(str, Enum):
    """操作级别（通道权限边界）。"""

    PLATFORM = "platform"  # 仅 Super_Admin（经 JWT）
    TENANT = "tenant"      # 租户内（JWT 用户 / 所有 API Key 通道）


class TenantScopeModeEnum(str, Enum):
    """仓储兜底（方案 B）的 contextvar 三态。"""

    TENANT = "tenant"      # 注入 tenant_id == 指定租户
    PLATFORM = "platform"  # 不注入租户过滤（Super_Admin 跨租户读元数据）
    EXTERNAL = "external"  # 注入 tenant_id == External_User_Tenant（外部用户）


@dataclass(frozen=True)
class IdentityContext:
    """单次请求的统一身份。所有字段在认证/Guard 阶段一次性确定后只读。

    约束：
    - effective_permissions 与 kb_scope 由 Guard 在每次请求**实时**构造，不来自 JWT 快照。
    - platform 级 Super_Admin 的 tenant_id 为 None。
    - external_agent 通道的 tenant_id 硬锁为 External_User_Tenant，且带 external_user_id。
    """

    source: IdentitySourceEnum
    op_level: OperationLevelEnum
    tenant_id: str | None = None          # platform 级 Super_Admin 为 None
    user_id: str | None = None            # JWT / 用户级 Key 时存在
    external_user_id: str | None = None   # external_agent Key 时存在（= external_users.id）
    api_key_id: str | None = None         # 任意 api_key 通道时存在
    is_super_admin: bool = False
    # 实时解析得到的有效权限点集合（含 api/menu/btn 三类）
    effective_permissions: frozenset[str] = field(default_factory=frozenset)
    # 持有的角色 id 集合（用于 grantee_type=role 的授权判定）
    role_ids: frozenset[str] = field(default_factory=frozenset)
    # 可访问知识库范围裁剪（租户级 Key 的 ApiKey_Authorized_Scope；None 表示不额外裁剪）
    kb_scope: "KbScope | None" = None

    # —— 便捷判定 ——

    @property
    def is_api_key(self) -> bool:
        return self.source == IdentitySourceEnum.API_KEY

    @property
    def is_external_user(self) -> bool:
        return self.external_user_id is not None

    @property
    def acting_subject_id(self) -> str | None:
        """行事主体 id：注册用户为 user_id，外部用户为 external_user_id。

        用于 KB owner 比对（owner_user_id 对两类主体统一以此为准）。
        """
        return self.external_user_id if self.is_external_user else self.user_id

    def has_permission(self, code: str) -> bool:
        return code in self.effective_permissions

    def tenant_scope_mode(self) -> TenantScopeModeEnum:
        """映射到仓储兜底 contextvar 的三态。"""
        if self.op_level == OperationLevelEnum.PLATFORM and self.is_super_admin:
            return TenantScopeModeEnum.PLATFORM
        if self.is_external_user:
            return TenantScopeModeEnum.EXTERNAL
        return TenantScopeModeEnum.TENANT


@dataclass(frozen=True)
class KbScope:
    """租户级 API Key 的可访问知识库范围（ApiKey_Authorized_Scope）。

    实际可访问集合 = (all_public_kbs ? 本租户全部 organization KB : ∅) ∪ explicit_kb_ids。
    动态规则部分在判定时实时取本租户 organization KB，故此处只持有静态意图。
    None（IdentityContext.kb_scope 为 None）表示不额外裁剪（JWT 用户/用户级 Key/外部用户走各自范围）。
    """

    all_public_kbs: bool = False
    explicit_kb_ids: frozenset[str] = field(default_factory=frozenset)
