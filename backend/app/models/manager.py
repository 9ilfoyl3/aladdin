"""模型统一管理器

根据配置初始化 LLM、Embedding、Rerank 三类模型 Provider，
提供单例访问和资源清理。

Embedding 和 Rerank 统一使用远程服务，通过前端配置管理页面设置服务地址。
优先级：数据库中 is_active=True 的配置 > 环境变量配置
"""

import logging
from typing import Optional

from app.config import Settings
from app.models.provider import LLMProvider, EmbedProvider, RerankProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM

logger = logging.getLogger(__name__)


class _PlaceholderEmbedder(EmbedProvider):
    """占位 Embedding Provider，未配置远程服务时使用

    所有调用都会抛出明确的错误提示，引导用户去前端配置。
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "Embedding 服务未配置。请在前端「Embedding & Rerank 配置」页面添加远程服务地址。"
        )

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        raise RuntimeError(
            "Embedding 服务未配置。请在前端「Embedding & Rerank 配置」页面添加远程服务地址。"
        )


class _PlaceholderReranker(RerankProvider):
    """占位 Rerank Provider，未配置远程服务时使用"""

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        raise RuntimeError(
            "Rerank 服务未配置。请在前端「Embedding & Rerank 配置」页面添加远程服务地址。"
        )


class ModelManager:
    """统一管理所有模型实例，按配置初始化"""

    def __init__(self, config: Settings):
        """根据配置初始化各 Provider

        Args:
            config: Settings 实例，包含模型相关配置
        """
        self._config = config
        self.embedder: EmbedProvider = self._init_embedder(config)
        self.reranker: RerankProvider = self._init_reranker(config)

    def _init_embedder(self, config: Settings) -> EmbedProvider:
        """根据环境变量配置初始化嵌入模型

        如果配置了远程服务地址则使用远程服务，否则返回占位 Provider。
        """
        if config.embed_base_url:
            from app.models.embedding.remote import RemoteEmbedder
            return RemoteEmbedder(
                base_url=config.embed_base_url,
                model=config.embed_model,
                api_key=config.embed_api_key,
                sparse_enabled=config.embed_sparse_enabled,
            )
        # 未配置远程服务地址，返回占位 Provider（允许启动，后续通过前端配置）
        logger.warning("Embedding 远程服务未配置（EMBED_BASE_URL 为空），请通过前端配置页面设置")
        return _PlaceholderEmbedder()

    def _init_reranker(self, config: Settings) -> RerankProvider:
        """根据环境变量配置初始化重排序模型

        如果配置了远程服务地址则使用远程服务，否则返回占位 Provider。
        """
        if config.rerank_base_url:
            from app.models.rerank.remote import RemoteReranker
            return RemoteReranker(
                base_url=config.rerank_base_url,
                model=config.rerank_model,
                api_key=config.rerank_api_key,
            )
        # 未配置远程服务地址，返回占位 Provider
        logger.warning("Rerank 远程服务未配置（RERANK_BASE_URL 为空），请通过前端配置页面设置")
        return _PlaceholderReranker()

    def reload_embedder(self, **kwargs) -> None:
        """动态重新加载 Embedding Provider

        Args:
            **kwargs: 远程服务参数
                - base_url: 服务地址
                - model_name: 模型名称
                - api_key: API 密钥
                - timeout: 超时时间
                - sparse_enabled: 是否启用 sparse 向量
        """
        from app.models.embedding.remote import RemoteEmbedder
        self.embedder = RemoteEmbedder(
            base_url=kwargs.get("base_url", ""),
            model=kwargs.get("model_name", "BAAI/bge-m3"),
            api_key=kwargs.get("api_key", ""),
            timeout=kwargs.get("timeout", 60.0),
            sparse_enabled=kwargs.get("sparse_enabled", True),
            max_connections=kwargs.get("max_connections", 20),
        )
        logger.info("Embedding Provider 已重新加载: base_url=%s", kwargs.get("base_url"))

    def reload_reranker(self, **kwargs) -> None:
        """动态重新加载 Rerank Provider

        Args:
            **kwargs: 远程服务参数
                - base_url: 服务地址
                - model_name: 模型名称
                - api_key: API 密钥
                - timeout: 超时时间
        """
        from app.models.rerank.remote import RemoteReranker
        self.reranker = RemoteReranker(
            base_url=kwargs.get("base_url", ""),
            model=kwargs.get("model_name", "BAAI/bge-reranker-v2-m3"),
            api_key=kwargs.get("api_key", ""),
            timeout=kwargs.get("timeout", 60.0),
        )
        logger.info("Rerank Provider 已重新加载: base_url=%s", kwargs.get("base_url"))

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
