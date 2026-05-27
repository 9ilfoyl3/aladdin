"""数据库 ORM 模型定义"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Float, Integer, String, Text, JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


class KnowledgeBase(Base):
    """知识库表"""
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retrieval_mode: Mapped[str] = mapped_column(String, default="hybrid")
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 关联
    folders: Mapped[list["Folder"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Folder(Base):
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


class Document(Base):
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


class Chunk(Base):
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


class ApiKey(Base):
    """API Key 表"""
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
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
    provider_type: Mapped[str] = mapped_column(String, nullable=False)  # paddleocr | external_api
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


class AgentNodeConfig(Base):
    """Agent 节点模型配置表"""
    __tablename__ = "agent_node_config"

    node_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    model_config_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("llm_configs.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatSession(Base):
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


class ChatMessageRecord(Base):
    """对话消息记录表"""
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    references: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 检索引用来源
    agent_steps: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # Agent 思考步骤
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    session: Mapped["ChatSession"] = relationship(back_populates="messages")

