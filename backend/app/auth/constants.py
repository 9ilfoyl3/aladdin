"""tenant-rbac-refactor 常量与枚举（禁止魔法值，集中定义）。

权限模型已从「自定义角色 + 扁平权限点」重构为「固定角色 + 归属轴」：
租户内人类成员仅有 ``admin`` / ``member`` 两个固定角色（见 ``TenantRoleEnum``），
租户内权限由「固定角色 + 是否资源所有者」判定，不再依赖任何权限点字典。

Key 类型、可见性、被授权主体类型、授权级别、审计动作等一律用字符串枚举表达。
"""

from __future__ import annotations

from enum import Enum


class TenantRoleEnum(str, Enum):
    """租户固定角色（取代自定义角色 + 权限点字典）。"""

    ADMIN = "admin"    # 租户管理员：管理本租户全部资源与用户，不受归属轴限制
    MEMBER = "member"  # 普通成员：仅管自有资源，他人只读


# v1 接受的租户角色取值（创建/修改用户时校验用）
TENANT_ROLES_ENABLED: frozenset[str] = frozenset(
    {TenantRoleEnum.ADMIN.value, TenantRoleEnum.MEMBER.value}
)


class ApiKeyTypeEnum(str, Enum):
    """API Key 三模型。"""

    TENANT_LEVEL = "tenant_level"      # 机器凭据，合成 Virtual_Identity
    USER_LEVEL = "user_level"          # 绑定用户，能力随该用户固定角色解析
    EXTERNAL_AGENT = "external_agent"  # 超管级代理，代表外部用户


class KbVisibilityEnum(str, Enum):
    """知识库可见性。"""

    PRIVATE = "private"            # 仅 owner 与被授权者
    ORGANIZATION = "organization"  # 租户内公共：本租户全员可读，写需 owner/admin


class GranteeTypeEnum(str, Enum):
    """被授权主体类型。

    点对点共享收敛后仅保留 ``user`` 一个取值（废弃自定义角色后按角色共享失去依附，
    跨租户 organization/tenant 预留枚举一并移除）。
    """

    USER = "user"  # 点对点共享给具体注册用户


# v1 实际接受的被授权主体类型（创建授权时校验用）
GRANTEE_TYPES_ENABLED: frozenset[str] = frozenset(
    {GranteeTypeEnum.USER.value}
)


class GrantPermissionEnum(str, Enum):
    """授权权限级别。"""

    READ = "read"
    WRITE = "write"


class OrgPermissionEnum(str, Enum):
    """组织公共库开放维度（仅 visibility=organization 时有效）。"""

    READ = "read"    # 组织成员仅可读内容
    WRITE = "write"  # 组织成员可读写内容（上传/删除文档、建文件夹）


# v1 接受的组织开放维度取值（设可见性时校验用）
ORG_PERMISSIONS_ENABLED: frozenset[str] = frozenset(
    {OrgPermissionEnum.READ.value, OrgPermissionEnum.WRITE.value}
)


class TenantTypeEnum(str, Enum):
    """租户类型。"""

    BUSINESS = "business"    # 普通业务租户
    EXTERNAL = "external"    # 外部用户内置租户（External_User_Tenant）


# 外部用户内置租户的固定标识（Bootstrap 创建时使用，保证幂等可查）
EXTERNAL_USER_TENANT_ID = "tenant-external-builtin"
EXTERNAL_USER_TENANT_NAME = "外部用户租户"

# 请求头：超管级代理 Key 携带的外部用户标识
HEADER_EXTERNAL_USER_ID = "X-External-User-Id"
# 请求头：目标租户入口（归属校验）
HEADER_TENANT_ID = "X-Tenant-ID"


# ============================================================
# 中文展示标签（前端角色界面用，单一真值来源）
# 角色 code 是稳定的英文契约；这里集中提供给前端展示的中文名，避免前端散落硬编码。
# ============================================================

# 角色 code -> 中文名
ROLE_LABELS: dict[str, str] = {
    TenantRoleEnum.ADMIN.value: "管理员",
    TenantRoleEnum.MEMBER.value: "普通成员",
}


def role_label(role: str) -> str:
    """角色中文名；未登记的角色回退为原值。"""
    return ROLE_LABELS.get(role, role)


class AuditActionEnum(str, Enum):
    """审计动作（仅元数据，绝不记录业务内容正文）。"""

    # —— 平台级（Super_Admin） ——
    TENANT_CREATE = "tenant.create"
    TENANT_SET_STATUS = "tenant.set_status"
    PROXY_KEY_CREATE = "apikey.proxy_create"
    PROXY_KEY_REVOKE = "apikey.proxy_revoke"
    # —— 租户级（Tenant_Admin） ——
    USER_CREATE = "user.create"
    USER_SET_STATUS = "user.set_status"
    USER_RESET_PASSWORD = "user.reset_password"
    USER_TRANSFER_KB = "user.transfer_kb"
    USER_UPDATE_PROFILE = "user.update_profile"
    TENANT_UPDATE_PROFILE = "tenant.update_profile"
    APIKEY_CREATE = "apikey.create"
    APIKEY_REVOKE = "apikey.revoke"
    APIKEY_UPDATE_SCOPE = "apikey.update_scope"
    KB_SET_VISIBILITY = "kb.set_visibility"
    KB_SHARE = "kb.share"
    KB_REVOKE_SHARE = "kb.revoke_share"
    INVITATION_CREATE = "invitation.create"
    INVITATION_REVOKE = "invitation.revoke"
    INVITATION_ACCEPT = "invitation.accept"
    # —— 系统/平台配置（kb-retrieval-optimization） ——
    SYSTEM_CONFIG_UPDATE = "system.config_update"      # 检索/分块（及 LLM/OCR）配置更新
    SYSTEM_CONFIG_RESET = "system.config_reset"        # 检索/分块配置恢复默认
    PLATFORM_CONFIG_UPDATE = "platform.config_update"  # 平台级配置（Load_Cache_TTL）更新
    # —— 认证 ——
    LOGIN_SUCCESS = "auth.login_success"
    LOGIN_FAIL = "auth.login_fail"
    CHANGE_PASSWORD = "auth.change_password"


class AuditResultEnum(str, Enum):
    """审计结果。"""

    SUCCESS = "success"
    FAIL = "fail"


class InvitationScopeEnum(str, Enum):
    """邀请链接用途。"""

    CREATE_TENANT = "create_tenant"  # 仅 Super_Admin 可发：被邀请人建租户 + 自身为租管
    CREATE_USER = "create_user"      # 租户管理员可发：在签发者租户内建普通用户
