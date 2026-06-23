"""OCR Manager - OCR Provider 管理器，负责注册、选择和调度 Provider"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .provider import OCRProvider, OCRResult

if TYPE_CHECKING:
    from app.schema.db import OCRConfig

logger = logging.getLogger(__name__)


class OCRManager:
    """OCR Provider 管理器

    从数据库加载 OCR 配置并管理 Provider，
    支持 Provider 选择和失败自动 fallback。
    """

    def __init__(self, configs: list[OCRConfig]) -> None:
        """初始化 OCR Manager

        Args:
            configs: 数据库中的 OCR 配置列表
        """
        self._providers: dict[str, OCRProvider] = {}
        self._default_name: str = ""
        self._fallback_name: str = ""
        self._init_from_db(configs)


    def _init_from_db(self, configs: list[OCRConfig]) -> None:
        """根据数据库配置初始化所有可用 Provider

        遍历配置列表，对每条记录创建 Provider 并注册。
        仅注册 is_available() 返回 True 的 Provider。

        Args:
            configs: 数据库中的 OCR 配置列表
        """
        logger.info("[OCR] 开始从数据库初始化 OCR Manager, 配置数量: %d", len(configs))

        for config in configs:
            provider = self._create_provider(config)
            if provider and provider.is_available():
                self._providers[config.id] = provider
                logger.info("[OCR] 已注册 Provider: %s (id=%s)", provider.name, config.id)

                if config.is_default:
                    self._default_name = config.id
                if config.is_fallback:
                    self._fallback_name = config.id
            else:
                logger.warning(
                    "[OCR] Provider '%s' (id=%s) 不可用，跳过注册",
                    config.provider_type, config.id,
                )

        logger.info(
            "[OCR] 初始化完成，已注册 %d 个 Provider, 默认: %s, Fallback: %s",
            len(self._providers),
            self._default_name or "(无)",
            self._fallback_name or "(无)",
        )
    def _create_provider(self, config: OCRConfig) -> OCRProvider | None:
        """根据配置创建对应 Provider 实例

        Args:
            config: 单条 OCR 配置记录

        Returns:
            OCRProvider 实例，或 None（类型不支持时）
        """
        from .external_api_provider import ExternalAPIProvider, PaddleOCRProvider, VLOCRProvider
        from .textin_provider import TextInProvider

        if config.provider_type == "textin":
            return TextInProvider(
                api_url=config.api_url,
                api_key=config.api_key or "",
                timeout=config.timeout,
            )
        elif config.provider_type == "external_api":
            return ExternalAPIProvider(
                api_url=config.api_url,
                api_key=config.api_key or "",
                timeout=config.timeout,
            )
        elif config.provider_type == "external_api_paddle":
            return PaddleOCRProvider(
                api_url=config.api_url,
                api_key=config.api_key or "",
                timeout=config.timeout,
            )
        elif config.provider_type == "external_api_vl":
            return VLOCRProvider(
                api_url=config.api_url,
                api_key=config.api_key or "",
                timeout=config.timeout,
            )

        logger.warning("[OCR] 不支持的 provider_type: %s", config.provider_type)
        return None

    def get_provider(self, name: Optional[str] = None) -> OCRProvider:
        """获取指定 Provider，默认返回配置中的默认 Provider

        Args:
            name: Provider ID（config.id），为 None 时使用默认 Provider

        Returns:
            OCRProvider: 对应的 Provider 实例

        Raises:
            ValueError: 指定的 Provider 未注册或不可用
        """
        target_name = name if name is not None else self._default_name

        if target_name not in self._providers:
            available = list(self._providers.keys())
            raise ValueError(
                f"OCR Provider '{target_name}' 未注册或不可用，"
                f"当前可用: {available}"
            )

        return self._providers[target_name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider ID

        Returns:
            已注册 Provider ID 列表
        """
        return list(self._providers.keys())

    async def recognize(
        self, file_path: str, provider_name: Optional[str] = None
    ) -> OCRResult:
        """执行 OCR 识别，支持自动 fallback

        Args:
            file_path: 文件路径（PDF/图片）
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider

        Returns:
            OCRResult: 统一格式的识别结果

        Raises:
            ValueError: 无可用 Provider
            Exception: OCR 识别失败且无 fallback
        """
        primary_provider = self.get_provider(provider_name)
        logger.info("[OCR] 开始识别文件: %s, 使用 Provider: %s", file_path, primary_provider.name)

        try:
            result = await primary_provider.recognize(file_path)
            logger.info(
                "[OCR] 识别成功, Provider: %s, 文本长度: %d, 置信度: %.3f",
                primary_provider.name, len(result.full_text), result.avg_confidence,
            )
            return result
        except Exception as primary_error:
            logger.warning("[OCR] Provider '%s' 识别失败: %s", primary_provider.name, primary_error)

            # 检查是否有可用的 fallback Provider
            if (
                self._fallback_name
                and self._fallback_name in self._providers
                and self._fallback_name != (provider_name or self._default_name)
            ):
                fallback_provider = self._providers[self._fallback_name]
                logger.info("[OCR] 切换到 fallback Provider: %s", fallback_provider.name)

                try:
                    result = await fallback_provider.recognize(file_path)
                    logger.info(
                        "[OCR] Fallback 识别成功, Provider: %s, 文本长度: %d",
                        fallback_provider.name, len(result.full_text),
                    )
                    return result
                except Exception as fallback_error:
                    logger.error(
                        "[OCR] Fallback Provider '%s' 也失败: %s",
                        fallback_provider.name, fallback_error,
                    )
                    raise primary_error from fallback_error

            # 没有可用的 fallback，直接抛出原始异常
            raise primary_error
