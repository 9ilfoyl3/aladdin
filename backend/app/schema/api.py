"""API 请求/响应 Pydantic 模型

定义 Chat API 的 OpenAI 兼容请求和响应结构。
"""

from typing import Optional

from pydantic import BaseModel, Field


# ============================================================
# Chat API 请求模型
# ============================================================


class ChatMessage(BaseModel):
    """对话消息"""

    role: str = Field(..., description="角色: system / user / assistant")
    content: str = Field(..., description="消息内容")


class ChatCompletionRequest(BaseModel):
    """Chat Completion 请求体（OpenAI 兼容 + RAG 扩展字段）"""

    model: str = Field(default="rag", description="模型名称")
    messages: list[ChatMessage] = Field(..., description="对话消息列表")
    stream: bool = Field(default=False, description="是否流式返回")
    knowledge_base_id: Optional[str] = Field(default=None, description="知识库 ID，为空时使用全部知识库")
    retrieval_mode: Optional[str] = Field(
        default=None, description="检索模式: direct / hybrid / agent，为空时使用知识库默认配置"
    )
    model_config_id: Optional[str] = Field(
        default=None, description="LLM 模型配置 ID，为空时使用系统默认模型"
    )
    filter_doc_ids: Optional[list[str]] = Field(
        default=None, description="限定文档范围过滤，仅在指定文档中检索"
    )
    kb_ids: Optional[list[str]] = Field(
        default=None, description="多知识库联合检索，指定多个知识库 ID 列表"
    )
    session_id: Optional[str] = Field(
        default=None, description="会话 ID，传入后自动加载历史上下文并保存消息"
    )
    temperature: Optional[float] = Field(default=None, description="生成温度")
    max_tokens: Optional[int] = Field(default=None, description="最大生成 token 数")


# ============================================================
# Chat API 响应模型
# ============================================================


class UsageInfo(BaseModel):
    """Token 使用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ReferenceItem(BaseModel):
    """引用来源"""

    doc_id: str
    chunk_id: str
    filename: str = ""
    content: str
    child_content: str = ""
    score: float


class ResponseMessage(BaseModel):
    """响应消息"""

    role: str = "assistant"
    content: str = ""


class ChatChoice(BaseModel):
    """响应选项"""

    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    """Chat Completion 非流式响应（OpenAI 兼容 + RAG 扩展）"""

    id: str
    object: str = "chat.completion"
    choices: list[ChatChoice]
    usage: UsageInfo
    references: list[ReferenceItem] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


# ============================================================
# SSE 流式响应中的 delta 结构
# ============================================================


class DeltaContent(BaseModel):
    """流式 delta 内容"""

    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    """流式响应选项"""

    index: int = 0
    delta: DeltaContent
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """流式响应单个 chunk"""

    id: str
    object: str = "chat.completion.chunk"
    choices: list[StreamChoice]
