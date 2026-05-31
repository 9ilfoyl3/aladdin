"""tenant-auth 常量与枚举（禁止魔法值，集中定义）。

权限点、Key 类型、可见性、被授权主体类型一律用字符串枚举；
并定义两组固定的内容级权限集合（不含任何管理/平台权限点）。
"""

from __future__ import annotations

from enum import Enum


class PermissionTypeEnum(str, Enum):
    """权限点类型：驱动后端能力 / 前端菜单 / 前端按钮。"""

    API = "api"      # 功能/操作权限（后端能否执行某操作）
    MENU = "menu"    # 前端菜单项可见性
    BTN = "btn"      # 前端按钮/动作可见性


class PermissionEnum(str, Enum):
    """权限点字典（code）。鉴权依据权限点而非硬编码角色名。

    新增权限点 = 在此追加并由 Bootstrap 写入 permissions 表，无需改鉴权代码或表结构。
    """

    # —— 知识库内容能力（api） ——
    KB_CREATE = "kb:create"
    KB_READ = "kb:read"
    KB_WRITE = "kb:write"
    KB_WRITE_PUBLIC = "kb:write_public"          # 写入组织公共库（默认仅 admin）
    KB_MANAGE_VISIBILITY = "kb:manage_visibility"  # 提升/收编可见性
    KB_SHARE = "kb:share"                         # 点对点共享自己的库
    QA_INVOKE = "qa:invoke"                       # 发起问答
    RECALL_INVOKE = "recall:invoke"               # 发起召回

    # —— 管理能力（api，Administrative_Operation） ——
    APIKEY_MANAGE = "apikey:manage"        # 管理租户级 Key（管理员）
    APIKEY_SELF = "apikey:self"            # 为自己创建/管理用户级 Key（普通用户即可，非管理级）
    USER_MANAGE = "user:manage"
    ROLE_MANAGE = "role:manage"
    CONFIG_MANAGE = "config:manage"               # LLM/Embed/OCR/Agent/系统配置

    # —— 平台能力（api，Platform_Operation，仅 Super_Admin/JWT） ——
    TENANT_MANAGE = "tenant:manage"

    # —— 前端菜单（menu） ——
    MENU_KNOWLEDGE = "menu:knowledge"
    MENU_CHAT = "menu:chat"
    MENU_RETRIEVAL = "menu:retrieval"
    MENU_SETTINGS = "menu:settings"
    MENU_ADMIN = "menu:admin"
    MENU_AUDIT = "menu:audit"          # 审计日志菜单（租管/超管）

    # —— 前端按钮（btn） ——
    BTN_KB_DELETE = "btn:kb_delete"
    BTN_KB_SHARE = "btn:kb_share"
    BTN_DOC_UPLOAD = "btn:doc_upload"
    BTN_APIKEY_CREATE = "btn:apikey_create"


class ApiKeyTypeEnum(str, Enum):
    """API Key 三模型。"""

    TENANT_LEVEL = "tenant_level"      # 机器凭据，合成 Virtual_Identity
    USER_LEVEL = "user_level"          # 绑定用户，继承实时权限
    EXTERNAL_AGENT = "external_agent"  # 超管级代理，代表外部用户


class KbVisibilityEnum(str, Enum):
    """知识库可见性。"""

    PRIVATE = "private"            # 仅 owner 与被授权者
    ORGANIZATION = "organization"  # 租户内公共：本租户全员可读，写需权限


class GranteeTypeEnum(str, Enum):
    """被授权主体类型。v1 仅 user/role 生效；organization/tenant 结构预留、行为关闭。"""

    USER = "user"
    ROLE = "role"
    # —— 预留（v1 创建端点拒绝、判定函数从不放行） ——
    ORGANIZATION = "organization"
    TENANT = "tenant"


# v1 实际接受的被授权主体类型（创建授权时校验用）
GRANTEE_TYPES_ENABLED: frozenset[str] = frozenset(
    {GranteeTypeEnum.USER.value, GranteeTypeEnum.ROLE.value}
)


class GrantPermissionEnum(str, Enum):
    """授权权限级别。"""

    READ = "read"
    WRITE = "write"


class TenantTypeEnum(str, Enum):
    """租户类型。"""

    BUSINESS = "business"    # 普通业务租户
    EXTERNAL = "external"    # 外部用户内置租户（External_User_Tenant）
    DEFAULT = "default"      # 保留（本特性不做历史迁移，不创建承接历史数据的默认租户）


class BuiltinRoleEnum(str, Enum):
    """内置角色名。"""

    ADMIN = "admin"
    USER = "user"


# ============================================================
# 固定内容级权限集合（不含任何管理/平台权限点）
# 用于：租户级 Key 的 Virtual_Identity、外部用户身份。
# 这两类身份永远不得触达 Administrative_Operation / Platform_Operation。
# ============================================================

# 租户级 API Key（Virtual_Identity）固定权限：内容读写 + 问答召回（受 scope 进一步约束）
CONTENT_LEVEL_PERMISSIONS: frozenset[str] = frozenset({
    PermissionEnum.KB_CREATE.value,
    PermissionEnum.KB_READ.value,
    PermissionEnum.KB_WRITE.value,
    PermissionEnum.QA_INVOKE.value,
    PermissionEnum.RECALL_INVOKE.value,
})

# 外部用户固定权限：管理自有私有库 + 读公共库 + 问答召回（不含写公共库、不含任何管理）
EXTERNAL_USER_PERMISSIONS: frozenset[str] = frozenset({
    PermissionEnum.KB_CREATE.value,
    PermissionEnum.KB_READ.value,
    PermissionEnum.KB_WRITE.value,
    PermissionEnum.QA_INVOKE.value,
    PermissionEnum.RECALL_INVOKE.value,
})

# 管理级权限点集合（API Key 通道一律不得拥有/行使）
ADMINISTRATIVE_PERMISSIONS: frozenset[str] = frozenset({
    PermissionEnum.APIKEY_MANAGE.value,
    PermissionEnum.USER_MANAGE.value,
    PermissionEnum.ROLE_MANAGE.value,
    PermissionEnum.CONFIG_MANAGE.value,
})

# 平台级权限点集合（仅 Super_Admin 经 JWT 行使）
PLATFORM_PERMISSIONS: frozenset[str] = frozenset({
    PermissionEnum.TENANT_MANAGE.value,
})

# user 内置角色的默认权限点（基础内容能力，不含 kb:write_public、不含任何管理/平台权限点）
USER_ROLE_DEFAULT_PERMISSIONS: frozenset[str] = frozenset({
    PermissionEnum.KB_CREATE.value,
    PermissionEnum.KB_READ.value,
    PermissionEnum.KB_WRITE.value,
    PermissionEnum.KB_SHARE.value,
    PermissionEnum.QA_INVOKE.value,
    PermissionEnum.RECALL_INVOKE.value,
    PermissionEnum.APIKEY_SELF.value,  # 仅为自己创建用户级 Key（非管理级，不等于 apikey:manage）
    PermissionEnum.MENU_KNOWLEDGE.value,
    PermissionEnum.MENU_CHAT.value,
    PermissionEnum.MENU_RETRIEVAL.value,
    PermissionEnum.BTN_DOC_UPLOAD.value,
    PermissionEnum.BTN_KB_SHARE.value,
    PermissionEnum.BTN_APIKEY_CREATE.value,
})


# 外部用户内置租户的固定标识（Bootstrap 创建时使用，保证幂等可查）
EXTERNAL_USER_TENANT_ID = "tenant-external-builtin"
EXTERNAL_USER_TENANT_NAME = "外部用户租户"

# 请求头：超管级代理 Key 携带的外部用户标识
HEADER_EXTERNAL_USER_ID = "X-External-User-Id"
# 请求头：目标租户入口（归属校验）
HEADER_TENANT_ID = "X-Tenant-ID"


# Tenant_Admin（内置 admin 角色）权限点 = 全部权限点 − 平台级权限点。
# 租户管理员"除超管职权外都有"：能管本租户用户/角色/Key/可见性/审计，
# 但绝不持有 tenant:manage（平台级）——故前端"租户管理"菜单对其自动隐藏，
# 后端平台端点也因 op_level=platform 将其拒绝。
TENANT_ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    {p.value for p in PermissionEnum} - PLATFORM_PERMISSIONS
)


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
    USER_SET_ROLES = "user.set_roles"
    USER_TRANSFER_KB = "user.transfer_kb"
    ROLE_CREATE = "role.create"
    ROLE_SET_PERMISSIONS = "role.set_permissions"
    ROLE_DELETE = "role.delete"
    APIKEY_CREATE = "apikey.create"
    APIKEY_REVOKE = "apikey.revoke"
    APIKEY_UPDATE_SCOPE = "apikey.update_scope"
    KB_SET_VISIBILITY = "kb.set_visibility"
    KB_SHARE = "kb.share"
    KB_REVOKE_SHARE = "kb.revoke_share"
    INVITATION_CREATE = "invitation.create"
    INVITATION_REVOKE = "invitation.revoke"
    INVITATION_ACCEPT = "invitation.accept"
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
    CREATE_USER = "create_user"      # user:manage 可发：在签发者租户内建普通用户
