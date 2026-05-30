"""数据库 ORM 模型定义"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Float, Integer, String, Text, JSON, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


class TenantScopedMixin:
    """受租户隔离的模型标记 Mixin（tenant-auth）。

    继承本 Mixin 的模型都带有 `tenant_id` 列，并被仓储层方案 B
    （`with_loader_criteria` + contextvar 三态）识别为需自动注入 tenant 过滤的目标。
    Mixin 只声明列，不承载行为；真正的过滤注入在 `app/repositories/tenant_repo.py`。
    """

    # 受隔离资源的归属租户。建表即存在（新分支、create_all 一步到位，不做历史回填）。
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)


class KnowledgeBase(Base, TenantScopedMixin):
    """知识库表"""
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    # tenant-auth：归属与可见性
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)  # KB_Owner
    visibility: Mapped[str] = mapped_column(String, default="private", nullable=False)  # private | organization
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 关联
    folders: Mapped[list["Folder"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Folder(Base, TenantScopedMixin):
    """文件夹表（支持嵌套目录）"""
    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("folders.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 关联
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="folders")
    children: Mapped[list["Folder"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    parent: Mapped[Optional["Folder"]] = relationship(back_populates="children", remote_side=[id])
    documents: Mapped[list["Document"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class Document(Base, TenantScopedMixin):
    """文档表"""
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    folder_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("folders.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # SHA256 文件内容哈希
    status: Mapped[str] = mapped_column(String, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100 处理进度
    progress_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 阶段描述
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    folder: Mapped[Optional["Folder"]] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base, TenantScopedMixin):
    """Chunk 元数据表（向量存 Milvus，元数据存 SQLite）"""
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.id"), nullable=False)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    document: Mapped["Document"] = relationship(back_populates="chunks")
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="chunks")


class ApiKey(Base, TenantScopedMixin):
    """API Key 表"""
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # tenant-auth：三模型 Key
    # tenant_level（默认，机器凭据）| user_level（绑定用户）| external_agent（超管级代理）
    key_type: Mapped[str] = mapped_column(String, default="tenant_level", nullable=False)
    bound_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)  # 用户级 Key 绑定的 user
    authorized_scope: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 租户级 Key 授权范围
    key_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 代理 Key 的来源标识（命名空间前缀）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LLMConfig(Base):
    """LLM 模型配置表"""
    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # ollama | vllm
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stream_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    max_context_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OCRConfig(Base):
    """OCR 服务配置表"""
    __tablename__ = "ocr_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False)  # external_api | textin
    api_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeout: Mapped[float] = mapped_column(Float, default=30.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class EmbedConfig(Base):
    """Embedding/Rerank 远程服务配置表"""
    __tablename__ = "embed_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    config_type: Mapped[str] = mapped_column(String(20), nullable=False)  # embedding | rerank
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="remote")  # 统一为 remote
    # 保留字段（兼容旧数据库，不再使用）
    local_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    device: Mapped[str] = mapped_column(String(10), default="cpu")
    # 远程服务字段
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)  # BAAI/bge-m3 等
    base_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeout: Mapped[float] = mapped_column(Float, default=60.0)
    # sparse 向量支持（仅 embedding 类型有效）
    sparse_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)  # 当前是否启用
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentPreset(Base):
    """Agent 预设配置表"""
    __tablename__ = "agent_presets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatSession(Base, TenantScopedMixin):
    """对话会话表"""
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    kb_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model_config_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 关联
    messages: Mapped[list["ChatMessageRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessageRecord.created_at"
    )


class ChatMessageRecord(Base, TenantScopedMixin):
    """对话消息记录表"""
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    references: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 检索引用来源
    agent_steps: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # Agent 思考步骤
    kb_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 本条消息使用的主知识库 ID
    kb_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # 多知识库联合检索时的知识库 ID 列表
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    session: Mapped["ChatSession"] = relationship(back_populates="messages")


# ============================================================
# tenant-auth：租户认证与隔离体系新增模型
# 新分支、库可重建：直接由 create_all 建表，不做历史数据迁移/回填。
# ============================================================


class Tenant(Base):
    """租户表：绝对隔离边界（一个组织 / 一个业务系统）"""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # default(保留) | external(外部用户内置租户) | business(普通业务租户)
    tenant_type: Mapped[str] = mapped_column(String, default="business", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """注册用户表（经 JWT 登录的真实人类账号）"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Super_Admin 不归属任何业务租户 -> 可空
    tenant_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("tenants.id"), index=True, nullable=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 停用/重置口令时自增，使旧 JWT 失效（token 内 token_version 不匹配即拒绝）
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),
    )


class ExternalUser(Base):
    """外部用户关联表：独立于 users，避免两类人群混入（R29.5）

    命名空间隔离核心：(key_source, external_user_id) 复合唯一——
    同 external_user_id 但不同代理 Key 来源解析为两个彼此独立的外部用户。
    """
    __tablename__ = "external_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), index=True, nullable=False)  # = External_User_Tenant
    # 命名空间前缀：签发该请求所用 External_Agent_ApiKey 的标识（api_keys.id）
    key_source: Mapped[str] = mapped_column(String, nullable=False)
    # 调用方通过 X-External-User-Id 传入的原始值
    external_user_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("key_source", "external_user_id", name="uq_external_identity"),
    )


class Role(Base):
    """角色表：一组权限点的命名集合，租户内对象（绝不跨租户）"""
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)  # admin | user | 自定义
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_role_tenant_name"),
    )


class Permission(Base):
    """权限点字典（全局共享定义，不归属租户）。新增权限点=插入数据，无需改表。"""
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # kb:create / menu:knowledge / btn:kb_delete
    type: Mapped[str] = mapped_column(String, nullable=False)  # api | menu | btn
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class RolePermission(Base):
    """角色-权限点关联表"""
    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(String, ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(String, ForeignKey("permissions.id"), primary_key=True)


class UserRole(Base):
    """用户-角色关联表"""
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[str] = mapped_column(String, ForeignKey("roles.id"), primary_key=True)


class KnowledgeBaseGrant(Base):
    """统一知识库授权/共享表：把 私有/组织公共/点对点 统一到一套授权数据（R16）"""
    __tablename__ = "knowledge_base_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), index=True, nullable=False)
    # v1 仅接受 user|role；organization|tenant 为预留枚举（结构预留、行为关闭，R16.2/16.3）
    grantee_type: Mapped[str] = mapped_column(String, nullable=False)
    grantee_id: Mapped[str] = mapped_column(String, nullable=False)  # user_id 或 role_id
    permission: Mapped[str] = mapped_column(String, nullable=False)  # read | write
    granted_by: Mapped[str] = mapped_column(String, nullable=False)  # 发起共享的 user_id
    # 面向未来跨租户预留，v1 恒为 NULL、鉴权代码从不读取（R16.7）
    source_tenant_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "grantee_type", "grantee_id", name="uq_grant_target"),
    )
