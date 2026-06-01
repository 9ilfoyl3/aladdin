"""IdentityContext：单次请求经认证后的统一身份对象（只读）。

无论凭据是 JWT 还是三种 API Key，认证后都合成为同一个 IdentityContext，
供 Authorization_Guard 与各资源级判定（tenant_guard / kb_authorization_decision）使用。
该对象不持有口令、不嵌入 JWT，是冻结只读对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.auth.constants import TenantRoleEnum


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

    权限模型为 WeKnora 式「固定角色 + 归属轴」，不再有任何权限点字典：
    - ``role`` 表示租户内固定角色（admin / member），它来自 JWT 中的 ``User.role``，
      或由 API Key 模型在认证时设定（见下表）。不再存在 effective_permissions /
      role_ids 这类权限点快照。
    - ``kb_scope`` 由 Guard 在每次请求**实时**构造，不来自 JWT 快照。
    - platform 级 Super_Admin 的 tenant_id 与 role 均为 None。
    - external_agent 通道的 tenant_id 硬锁为 External_User_Tenant，且带 external_user_id。

    role 取值矩阵：

    | 身份                | source  | op_level | role          |
    | ------------------- | ------- | -------- | ------------- |
    | Super_Admin         | jwt     | platform | None          |
    | 租户 admin          | jwt     | tenant   | admin         |
    | 租户 member         | jwt     | tenant   | member        |
    | user_level Key      | api_key | tenant   | 绑定用户的 role |
    | external_agent Key  | api_key | tenant   | member        |
    | tenant_level Key    | api_key | tenant   | None          |
    """

    source: IdentitySourceEnum
    op_level: OperationLevelEnum
    tenant_id: str | None = None          # platform 级 Super_Admin 为 None
    user_id: str | None = None            # JWT / 用户级 Key 时存在
    username: str | None = None           # 操作者用户名（JWT 用户/用户级 Key），供审计记录
    external_user_id: str | None = None   # external_agent Key 时存在（= external_users.id）
    api_key_id: str | None = None         # 任意 api_key 通道时存在
    is_super_admin: bool = False
    # 租户固定角色（admin / member）。Super_Admin 与 tenant_level Key（机器身份）为 None。
    role: "TenantRoleEnum | None" = None
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
    def is_tenant_admin(self) -> bool:
        """是否为租户管理员（租户内固定角色 == admin）。

        注意：Super_Admin 没有租户（tenant_id=None、role=None），**不是**“租户管理员”；
        其平台权限由 ``is_super_admin`` 单独承载，不混入此属性。需要“admin 或 super_admin”
        语义的代码必须显式写 ``identity.is_tenant_admin or identity.is_super_admin``，
        切勿把 super_admin 揉进本属性。
        """
        return self.role == TenantRoleEnum.ADMIN

    @property
    def acting_subject_id(self) -> str | None:
        """行事主体 id：注册用户为 user_id，外部用户为 external_user_id。

        用于 KB owner 比对（owner_user_id 对两类主体统一以此为准）。
        """
        return self.external_user_id if self.is_external_user else self.user_id

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
