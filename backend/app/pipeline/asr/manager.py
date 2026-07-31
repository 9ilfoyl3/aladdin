"""ASR Manager - ASR Provider 管理器，负责注册、选择和调度 Provider"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .provider import ASRProvider, ASRResult

if TYPE_CHECKING:
    from app.schema.db import ASRConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProviderState:
    """Provider 集合与选路信息的不可变快照

    热重载时整体替换本对象（单次引用赋值），避免出现"providers 已换、
    default_name 还是旧的"这类半更新中间态。
    """

    providers: dict[str, ASRProvider] = field(default_factory=dict)
    default_name: str = ""
    fallback_name: str = ""


class ASRManager:
    """ASR Provider 管理器

    从数据库加载 ASR 配置并管理 Provider，
    支持 Provider 选择和失败自动 fallback。

    支持配置热重载：:meth:`reload_from_configs` 原子替换内部 Provider 集合，
    Manager 实例对象本身不变，因此所有持有该实例引用的位置
    （如 ``DocumentPipeline.asr_manager``）自动同步生效，无需重建 pipeline。
    """

    def __init__(self, configs: list[ASRConfig]) -> None:
        """初始化 ASR Manager

        Args:
            configs: 数据库中的 ASR 配置列表（可为空列表，此时 Manager 视为未配置）
        """
        self._state = self._build_state(configs)

    @classmethod
    def _build_state(cls, configs: list[ASRConfig]) -> _ProviderState:
        """按数据库配置构建 Provider 集合快照（不触碰实例状态）

        Args:
            configs: 数据库中的 ASR 配置列表

        Returns:
            构建好的 :class:`_ProviderState` 快照
        """
        logger.info("[ASR] 开始构建 ASR Provider 集合, 配置数量: %d", len(configs))

        providers: dict[str, ASRProvider] = {}
        default_name = ""
        fallback_name = ""

        for config in configs:
            provider = cls._create_provider(config)
            if provider and provider.is_available():
                providers[config.id] = provider
                logger.info("[ASR] 已注册 Provider: %s (id=%s)", provider.name, config.id)

                if config.is_default:
                    default_name = config.id
                if config.is_fallback:
                    fallback_name = config.id
            else:
                logger.warning(
                    "[ASR] Provider '%s' (id=%s) 不可用，跳过注册",
                    config.provider_type, config.id,
                )

        logger.info(
            "[ASR] 构建完成，已注册 %d 个 Provider, 默认: %s, Fallback: %s",
            len(providers),
            default_name or "(无)",
            fallback_name or "(无)",
        )
        return _ProviderState(
            providers=providers, default_name=default_name, fallback_name=fallback_name
        )

    def reload_from_configs(self, configs: list[ASRConfig]) -> None:
        """按新配置热重载 Provider 集合（原子替换，无需重启进程）

        先完整构建新快照，成功后再单次赋值替换；构建期抛异常则保留原有配置继续
        服务。正在执行中的转写调用已解析到旧 Provider 对象，继续跑完不受影响。

        Args:
            configs: 数据库中的最新 ASR 配置列表
        """
        try:
            new_state = self._build_state(configs)
        except Exception as e:  # noqa: BLE001 — 重载失败不能打断既有服务
            logger.warning("[ASR] 配置热重载失败，保留原有配置: %s", e)
            return

        self._state = new_state
        logger.info(
            "[ASR] 配置热重载完成，当前 %d 个 Provider 可用", len(new_state.providers)
        )

    def has_provider(self) -> bool:
        """是否存在可用 Provider（即该能力是否真正可用）"""
        return bool(self._state.providers)

    def __bool__(self) -> bool:
        """无可用 Provider 的 Manager 语义上等价于"未配置 ASR"。

        使调用方既有的 ``if self.asr_manager`` 真值判断保持原语义：启动时数据库无
        配置也会创建空 Manager（为"首次配置后热生效"留下可重载的对象），此时真值
        为 False，pipeline 行为与过去 ``asr_manager is None`` 完全一致。
        """
        return self.has_provider()

    @staticmethod
    def _create_provider(config: ASRConfig) -> ASRProvider | None:
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
        state = self._state
        target_name = name if name is not None else state.default_name

        if target_name not in state.providers:
            available = list(state.providers.keys())
            raise ValueError(
                f"ASR Provider '{target_name}' 未注册或不可用，"
                f"当前可用: {available}"
            )

        return state.providers[target_name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider ID"""
        return list(self._state.providers.keys())

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
        # 整个转写（含 fallback 决策）基于同一份状态快照，避免中途热重载导致
        # primary 与 fallback 来自不同代配置。
        state = self._state
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
                state.fallback_name
                and state.fallback_name in state.providers
                and state.fallback_name != (provider_name or state.default_name)
            ):
                fallback_provider = state.providers[state.fallback_name]
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
