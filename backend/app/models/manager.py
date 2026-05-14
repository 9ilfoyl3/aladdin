"""模型统一管理器

根据配置初始化 LLM、Embedding、Rerank 三类模型 Provider，
提供单例访问和资源清理。
"""

from typing import Optional

from app.config import Settings
from app.models.provider import LLMProvider, EmbedProvider, RerankProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM
from app.models.embedding.bge_m3 import BgeM3Embedder
from app.models.rerank.bge_reranker import BgeReranker


class ModelManager:
    """统一管理所有模型实例，按配置初始化"""

    def __init__(self, config: Settings):
        """根据配置初始化各 Provider

        Args:
            config: Settings 实例，包含模型相关配置
        """
        self.embedder: EmbedProvider = self._init_embedder(config)
        self.reranker: RerankProvider = self._init_reranker(config)

    def _init_embedder(self, config: Settings) -> EmbedProvider:
        """初始化 bge-m3 嵌入模型"""
        return BgeM3Embedder(model_name=config.embed_model, device=config.embed_device)

    def _init_reranker(self, config: Settings) -> RerankProvider:
        """初始化 bge-reranker 重排序模型"""
        return BgeReranker(model_name=config.rerank_model, device=config.rerank_device)

    async def close(self):
        """清理资源"""
        pass


# 模块级单例
_manager: Optional[ModelManager] = None


def get_model_manager(config: Optional[Settings] = None) -> ModelManager:
    """获取 ModelManager 单例

    首次调用需传入 config，后续调用可省略。

    Args:
        config: Settings 实例，仅首次初始化时需要

    Returns:
        ModelManager 单例实例
    """
    global _manager
    if _manager is None:
        if config is None:
            from app.config import get_settings
            config = get_settings()
        _manager = ModelManager(config)
    return _manager
