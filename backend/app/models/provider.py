"""模型 Provider 抽象基类

定义 LLM、Embedding、Rerank 三类模型的统一接口，
具体实现由各子模块提供（如 ollama、vllm、bge-m3 等）。
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator


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
