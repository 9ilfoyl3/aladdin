"""模型 Provider 抽象基类

定义 LLM、Embedding、Rerank 三类模型的统一接口，
具体实现由各子模块提供（如 ollama、vllm、bge-m3 等）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


# ============================================================
# Function Calling 数据结构
# ============================================================


@dataclass
class LLMToolCall:
    """LLM 返回的工具调用"""

    id: str
    function_name: str
    arguments: str  # JSON string


@dataclass
class TokenUsage:
    """Token 用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResponse:
    """Function Calling 完整响应"""

    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop" | "tool_calls"
    usage: TokenUsage | None = None
    # 答案正文是否已在流式阶段逐 token 作为 answer 发射（如 vLLM 增量解析 final_answer）。
    # 引擎据此决定终止时是否需要补发完整答案：
    #   True  → 仅发 done 标记（避免重复）
    #   False → 若答案来自 final_answer 工具但未流式（如 Ollama 工具调用非增量返回），补发正文
    answer_streamed: bool = False
    # 模型把 final_answer 调用「写成纯文本 JSON」时（千问等弱 function-calling 模型），
    # 流式阶段路由器从普通 content 中提取出的答案文本。非空表示这是内联 final_answer，
    # 引擎应将其作为最终答案（而非把原始 JSON 当答案或思考）。
    inline_answer: str = ""


@dataclass
class StreamChunk:
    """Function Calling 流式响应片段"""

    content: str = ""
    tool_calls: list[LLMToolCall] | None = None
    finish_reason: str = ""
    response_type: str = "content"  # "content" | "thinking" | "tool_call"


# ============================================================
# Provider 抽象基类
# ============================================================


class LLMProvider(ABC):
    """大语言模型 Provider 抽象基类"""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成完整回复"""
        ...

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成回复，逐 token 返回"""
        ...

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> ChatResponse:
        """Function Calling: 发送消息和工具定义，获取可能包含 tool_calls 的响应"""
        raise NotImplementedError("This provider does not support function calling")

    async def stream_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Streaming Function Calling: 流式返回包含工具调用的响应片段"""
        raise NotImplementedError(
            "This provider does not support streaming function calling"
        )


class EmbedProvider(ABC):
    """向量嵌入模型 Provider 抽象基类"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成稠密向量"""
        ...

    @abstractmethod
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（用于 BM25 等稀疏检索）"""
        ...


class RerankProvider(ABC):
    """重排序模型 Provider 抽象基类"""

    @abstractmethod
    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """对候选文档重排序，返回 (原始索引, 分数) 列表"""
        ...
