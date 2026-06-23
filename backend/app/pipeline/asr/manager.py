"""ASR Manager - ASR Provider 管理器，负责注册、选择和调度 Provider"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .provider import ASRProvider, ASRResult

if TYPE_CHECKING:
    from app.schema.db import ASRConfig

logger = logging.getLogger(__name__)


class ASRManager:
    """ASR Provider 管理器

    从数据库加载 ASR 配置并管理 Provider，
    支持 Provider 选择和失败自动 fallback。
    """

    def __init__(self, configs: list[ASRConfig]) -> None:
        """初始化 ASR Manager

        Args:
            configs: 数据库中的 ASR 配置列表
        """
        self._providers: dict[str, ASRProvider] = {}
        self._default_name: str = ""
        self._fallback_name: str = ""
        self._init_from_db(configs)

    def _init_from_db(self, configs: list[ASRConfig]) -> None:
        """根据数据库配置初始化所有可用 Provider

        Args:
            configs: 数据库中的 ASR 配置列表
        """
        logger.info("[ASR] 开始从数据库初始化 ASR Manager, 配置数量: %d", len(configs))

        for config in configs:
            provider = self._create_provider(config)
            if provider and provider.is_available():
                self._providers[config.id] = provider
                logger.info("[ASR] 已注册 Provider: %s (id=%s)", provider.name, config.id)

                if config.is_default:
                    self._default_name = config.id
                if config.is_fallback:
                    self._fallback_name = config.id
            else:
                logger.warning(
                    "[ASR] Provider '%s' (id=%s) 不可用，跳过注册",
                    config.provider_type, config.id,
                )

        logger.info(
            "[ASR] 初始化完成，已注册 %d 个 Provider, 默认: %s, Fallback: %s",
            len(self._providers),
            self._default_name or "(无)",
            self._fallback_name or "(无)",
        )

    def _create_provider(self, config: ASRConfig) -> ASRProvider | None:
        """根据配置创建对应 Provider 实例

        Args:
            config: 单条 ASR 配置记录

        Returns:
            ASRProvider 实例，或 None（类型不支持时）
        """
        from .openai_provider import OpenAIASRProvider

        # 当前所有厂商统一走 OpenAI 兼容接口
        if config.provider_type in ("openai", "external_api"):
            return OpenAIASRProvider(
                api_url=config.api_url,
                model_name=config.model_name,
                api_key=config.api_key or "",
                language=config.language or "",
                timeout=config.timeout,
            )

        logger.warning("[ASR] 不支持的 provider_type: %s", config.provider_type)
        return None

    def get_provider(self, name: Optional[str] = None) -> ASRProvider:
        """获取指定 Provider，默认返回配置中的默认 Provider

        Args:
            name: Provider ID（config.id），为 None 时使用默认 Provider

        Returns:
            ASRProvider: 对应的 Provider 实例

        Raises:
            ValueError: 指定的 Provider 未注册或不可用
        """
        target_name = name if name is not None else self._default_name

        if target_name not in self._providers:
            available = list(self._providers.keys())
            raise ValueError(
                f"ASR Provider '{target_name}' 未注册或不可用，"
                f"当前可用: {available}"
            )

        return self._providers[target_name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider ID"""
        return list(self._providers.keys())

    async def transcribe(
        self, file_path: str, provider_name: Optional[str] = None
    ) -> ASRResult:
        """执行语音转写，支持自动 fallback

        Args:
            file_path: 音频文件路径
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider

        Returns:
            ASRResult: 统一格式的转写结果

        Raises:
            ValueError: 无可用 Provider
            Exception: 转写失败且无 fallback
        """
        primary_provider = self.get_provider(provider_name)
        logger.info("[ASR] 开始转写音频: %s, 使用 Provider: %s", file_path, primary_provider.name)

        try:
            result = await primary_provider.transcribe(file_path)
            logger.info(
                "[ASR] 转写成功, Provider: %s, 文本长度: %d",
                primary_provider.name, len(result.full_text),
            )
            return result
        except Exception as primary_error:
            logger.warning("[ASR] Provider '%s' 转写失败: %s", primary_provider.name, primary_error)

            if (
                self._fallback_name
                and self._fallback_name in self._providers
                and self._fallback_name != (provider_name or self._default_name)
            ):
                fallback_provider = self._providers[self._fallback_name]
                logger.info("[ASR] 切换到 fallback Provider: %s", fallback_provider.name)

                try:
                    result = await fallback_provider.transcribe(file_path)
                    logger.info(
                        "[ASR] Fallback 转写成功, Provider: %s, 文本长度: %d",
                        fallback_provider.name, len(result.full_text),
                    )
                    return result
                except Exception as fallback_error:
                    logger.error(
                        "[ASR] Fallback Provider '%s' 也失败: %s",
                        fallback_provider.name, fallback_error,
                    )
                    raise primary_error from fallback_error

            raise primary_error
