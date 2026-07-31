"""OCR Manager - Provider 注册、选择、能力编排与 fallback"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from .errors import OCRError
from .input_prep import PreparedInput, prepare_input
from .provider import OCRProvider, OCRResult, PageOCRResult
from .registry import get_provider_class

if TYPE_CHECKING:
    from app.schema.db import OCRConfig

logger = logging.getLogger(__name__)


class OCRManager:
    """OCR Provider 管理器

    职责：从数据库配置注册 Provider、按 default/fallback 选择、
    依据 Provider 能力编排输入（整文件直送 / 按页图片逐页识别）。
    不涉及任何响应格式判断——那是各 Provider 的契约。
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

        仅注册 provider_type 在注册表内、且 is_available() 为 True 的配置。

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
                    "[OCR] Provider '%s' (id=%s) 不可用或类型未注册，跳过",
                    config.provider_type, config.id,
                )

        logger.info(
            "[OCR] 初始化完成，已注册 %d 个 Provider, 默认: %s, Fallback: %s",
            len(self._providers),
            self._default_name or "(无)",
            self._fallback_name or "(无)",
        )

    @staticmethod
    def _create_provider(config: OCRConfig) -> OCRProvider | None:
        """按注册表创建 Provider 实例

        Args:
            config: 单条 OCR 配置记录

        Returns:
            OCRProvider 实例，或 None（provider_type 未注册）
        """
        # 导入 providers 包触发注册（放在此处避免模块级循环导入）
        import app.pipeline.ocr.providers  # noqa: F401

        provider_cls = get_provider_class(config.provider_type)
        if provider_cls is None:
            logger.warning(
                "[OCR] 不支持的 provider_type: %s（该配置需重建）", config.provider_type
            )
            return None

        return provider_cls(  # type: ignore[call-arg]
            api_url=config.api_url,
            api_key=config.api_key or "",
            timeout=config.timeout,
            extra_config=config.extra_config or {},
        )

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
                f"OCR Provider '{target_name}' 未注册或不可用，当前可用: {available}"
            )

        return self._providers[target_name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider ID"""
        return list(self._providers.keys())

    async def recognize(
        self, file_path: str, provider_name: Optional[str] = None
    ) -> OCRResult:
        """单文件直送识别（文件须为目标 Provider 可接受的类型）

        用于嵌入图片 OCR 与配置连通性测试。整篇文档识别请用
        :meth:`recognize_document`，它会按 Provider 能力准备输入。

        Args:
            file_path: 文件路径
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider

        Returns:
            OCRResult: 统一格式的识别结果
        """
        provider = self.get_provider(provider_name)
        from app.pipeline.concurrency import get_ocr_semaphore

        async with get_ocr_semaphore():
            return await provider.recognize(file_path)

    async def recognize_document(
        self, file_path: str, provider_name: Optional[str] = None
    ) -> OCRResult:
        """整篇文档识别：按 Provider 能力准备输入，失败时切换 fallback

        - Provider 接受该输入类型 → 整文件直送。
        - 只接受图片而输入是 PDF → 按页渲染整页图片并发逐页识别，按页合并。

        Args:
            file_path: 文件路径（PDF / 图片）
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider

        Returns:
            OCRResult: 统一格式的识别结果

        Raises:
            ValueError: 无可用 Provider
            OCRError: 识别失败且无可用 fallback（含契约不符、输入不支持）
        """
        primary_name = provider_name or self._default_name
        primary = self.get_provider(provider_name)
        logger.info(
            "[OCR] 开始识别文档: %s, Provider: %s (accepts=%s)",
            file_path, primary.name, sorted(primary.capability.accepts),
        )

        try:
            return await self._run_with_capability(primary, file_path)
        except Exception as primary_error:
            logger.warning(
                "[OCR] Provider '%s' 识别失败: %s: %s",
                primary.name, type(primary_error).__name__, primary_error,
            )

            fallback = self._pick_fallback(primary_name)
            if fallback is None:
                raise

            logger.info("[OCR] 切换到 fallback Provider: %s", fallback.name)
            try:
                # fallback 能力可能与 primary 不同，重新做输入准备
                return await self._run_with_capability(fallback, file_path)
            except Exception as fallback_error:
                logger.error(
                    "[OCR] Fallback Provider '%s' 也失败: %s",
                    fallback.name, fallback_error,
                )
                raise primary_error from fallback_error

    def _pick_fallback(self, primary_name: str) -> OCRProvider | None:
        """取可用的 fallback Provider（与 primary 不同才有意义）"""
        if (
            self._fallback_name
            and self._fallback_name in self._providers
            and self._fallback_name != primary_name
        ):
            return self._providers[self._fallback_name]
        return None

    async def _run_with_capability(
        self, provider: OCRProvider, file_path: str
    ) -> OCRResult:
        """按能力准备输入并执行识别，最后清理临时产物"""
        prepared = await asyncio.to_thread(
            prepare_input, file_path, provider.capability, provider.name
        )
        try:
            if prepared.kind == "whole":
                return await self._recognize_one(provider, prepared.paths[0])
            return await self._recognize_pages(provider, prepared)
        finally:
            prepared.cleanup()

    @staticmethod
    async def _recognize_one(provider: OCRProvider, path: str) -> OCRResult:
        """单次识别，受全局 OCR 信号量限流"""
        from app.pipeline.concurrency import get_ocr_semaphore

        async with get_ocr_semaphore():
            return await provider.recognize(path)

    async def _recognize_pages(
        self, provider: OCRProvider, prepared: PreparedInput
    ) -> OCRResult:
        """并发逐页识别并按页码合并为单个 OCRResult

        单页失败不放弃整篇：记 WARNING 后该页留空，其余页正常合并
        （扫描件常有个别页因图像质量导致服务报错）。全部页失败则抛出。
        """
        from app.pipeline.concurrency import get_ocr_semaphore

        semaphore = get_ocr_semaphore()
        total = len(prepared.paths)

        async def _one(idx: int, path: str) -> OCRResult | Exception:
            async with semaphore:
                try:
                    return await provider.recognize(path)
                except Exception as e:  # noqa: BLE001 — 单页失败降级为空页
                    logger.warning(
                        "[OCR] 第 %d/%d 页识别失败: %s: %s",
                        idx + 1, total, type(e).__name__, e,
                    )
                    return e

        results = await asyncio.gather(
            *[_one(i, p) for i, p in enumerate(prepared.paths)]
        )

        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        errors: list[Exception] = []
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                errors.append(res)
                pages.append(PageOCRResult(page_num=idx + 1, blocks=[], full_text=""))
                continue
            # 单图识别的 provider 端页码恒为 1，此处按渲染页序重编号
            page_text = res.full_text
            page_blocks = res.pages[0].blocks if res.pages else []
            pages.append(
                PageOCRResult(page_num=idx + 1, blocks=page_blocks, full_text=page_text)
            )
            if res.avg_confidence:
                all_conf.append(res.avg_confidence)

        if errors and len(errors) == total:
            # 每一页都失败：不是"内容为空"而是链路故障，抛出首个错误
            raise errors[0]

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)
        avg_conf = sum(all_conf) / len(all_conf) if all_conf else 0.0
        logger.info(
            "[OCR] 逐页识别完成: %d 页（失败 %d 页），文本长度=%d",
            total, len(errors), len(full_text),
        )
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_conf,
            provider_name=provider.name,
            metadata={
                "ocr_page_count": total,
                "ocr_failed_pages": len(errors),
                "ocr_truncated_pages": prepared.truncated_pages,
            },
        )


__all__ = ["OCRManager", "OCRError"]
