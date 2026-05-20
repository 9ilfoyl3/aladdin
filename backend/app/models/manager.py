"""模型统一管理器

根据配置初始化 LLM、Embedding、Rerank 三类模型 Provider，
提供单例访问和资源清理。

优先级：数据库中 is_active=True 的配置 > 环境变量配置
"""

import logging
from typing import Optional

from app.config import Settings
from app.models.provider import LLMProvider, EmbedProvider, RerankProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM

logger = logging.getLogger(__name__)


class ModelManager:
    """统一管理所有模型实例，按配置初始化"""

    def __init__(self, config: Settings):
        """根据配置初始化各 Provider

        Args:
            config: Settings 实例，包含模型相关配置
        """
        self._config = config
        self.embedder: EmbedProvider = self._init_embedder_from_env(config)
        self.reranker: RerankProvider = self._init_reranker_from_env(config)

    def _init_embedder_from_env(self, config: Settings) -> EmbedProvider:
        """根据环境变量配置初始化嵌入模型（默认行为）"""
        if config.embed_provider == "remote":
            from app.models.embedding.remote import RemoteEmbedder
            return RemoteEmbedder(
                base_url=config.embed_base_url,
                model=config.embed_model,
                api_key=config.embed_api_key,
            )
        elif config.embed_provider == "flag-embedding":
            from app.models.embedding.bge_m3 import BgeM3Embedder
            return BgeM3Embedder(model_name=config.embed_model, device=config.embed_device)
        else:
            from app.models.embedding.sentence_transformer import SentenceTransformerEmbedder
            return SentenceTransformerEmbedder(model_name=config.embed_model, device=config.embed_device)

    def _init_reranker_from_env(self, config: Settings) -> RerankProvider:
        """根据环境变量配置初始化重排序模型（默认行为）"""
        if config.rerank_provider == "remote":
            from app.models.rerank.remote import RemoteReranker
            return RemoteReranker(
                base_url=config.rerank_base_url,
                model=config.rerank_model,
                api_key=config.rerank_api_key,
            )
        elif config.rerank_provider == "flag-embedding":
            from app.models.rerank.bge_reranker import BgeReranker
            return BgeReranker(model_name=config.rerank_model, device=config.rerank_device)
        else:
            from app.models.rerank.cross_encoder_reranker import CrossEncoderReranker
            return CrossEncoderReranker(model_name=config.rerank_model, device=config.rerank_device)

    def reload_embedder(self, provider: str, **kwargs) -> None:
        """动态重新加载 Embedding Provider

        Args:
            provider: local | remote
            **kwargs: 根据 provider 类型传入不同参数
        """
        if provider == "remote":
            from app.models.embedding.remote import RemoteEmbedder
            self.embedder = RemoteEmbedder(
                base_url=kwargs.get("base_url", ""),
                model=kwargs.get("model_name", "BAAI/bge-m3"),
                api_key=kwargs.get("api_key", ""),
                timeout=kwargs.get("timeout", 60.0),
            )
        elif provider == "local":
            local_provider = kwargs.get("local_provider", "sentence-transformers")
            model_name = kwargs.get("model_name", "BAAI/bge-m3")
            device = kwargs.get("device", "cpu")
            if local_provider == "flag-embedding":
                from app.models.embedding.bge_m3 import BgeM3Embedder
                self.embedder = BgeM3Embedder(model_name=model_name, device=device)
            else:
                from app.models.embedding.sentence_transformer import SentenceTransformerEmbedder
                self.embedder = SentenceTransformerEmbedder(model_name=model_name, device=device)
        logger.info("Embedding Provider 已重新加载: provider=%s", provider)

    def reload_reranker(self, provider: str, **kwargs) -> None:
        """动态重新加载 Rerank Provider

        Args:
            provider: local | remote
            **kwargs: 根据 provider 类型传入不同参数
        """
        if provider == "remote":
            from app.models.rerank.remote import RemoteReranker
            self.reranker = RemoteReranker(
                base_url=kwargs.get("base_url", ""),
                model=kwargs.get("model_name", "BAAI/bge-reranker-v2-m3"),
                api_key=kwargs.get("api_key", ""),
                timeout=kwargs.get("timeout", 60.0),
            )
        elif provider == "local":
            local_provider = kwargs.get("local_provider", "sentence-transformers")
            model_name = kwargs.get("model_name", "BAAI/bge-reranker-v2-m3")
            device = kwargs.get("device", "cpu")
            if local_provider == "flag-embedding":
                from app.models.rerank.bge_reranker import BgeReranker
                self.reranker = BgeReranker(model_name=model_name, device=device)
            else:
                from app.models.rerank.cross_encoder_reranker import CrossEncoderReranker
                self.reranker = CrossEncoderReranker(model_name=model_name, device=device)
        logger.info("Rerank Provider 已重新加载: provider=%s", provider)

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
