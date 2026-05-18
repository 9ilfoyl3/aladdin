"""External API Provider - 外部 HTTP OCR 服务的抽象基类与通用实现"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod

import httpx

from .provider import OCRProvider, OCRResult

logger = logging.getLogger(__name__)


class BaseExternalAPIProvider(OCRProvider):
    """外部 HTTP API OCR 服务的抽象基类

    提供通用的 HTTP 文件上传逻辑，子类只需实现 _adapt_response 方法
    来解析各自 OCR 服务的响应格式。
    """

    def __init__(
        self, api_url: str, api_key: str = "", timeout: float = 30.0
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "external_api"

    async def recognize(self, file_path: str) -> OCRResult:
        """通过 HTTP POST 将文件发送到外部 OCR 服务"""
        headers = self._build_headers()

        logger.info("[OCR][%s] 发送文件到 %s, 文件: %s", self.name, self._api_url, file_path)

        start = time.time()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.split("/")[-1], f)}
                response = await client.post(
                    self._api_url, files=files, headers=headers
                )

            response.raise_for_status()
            data = response.json()

        elapsed_ms = (time.time() - start) * 1000
        logger.info("[OCR][%s] 响应状态码: %d, 耗时: %.0fms", self.name, response.status_code, elapsed_ms)

        result = self._adapt_response(data)
        logger.info(
            "[OCR][%s] 适配结果: full_text 长度=%d, pages=%d, confidence=%.3f",
            self.name, len(result.full_text), len(result.pages), result.avg_confidence,
        )
        return result

    def _build_headers(self) -> dict[str, str]:
        """构建请求头，子类可覆盖以自定义认证方式"""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @abstractmethod
    def _adapt_response(self, data: dict) -> OCRResult:
        """将外部 API 的 JSON 响应适配为统一 OCRResult

        每个具体的 OCR 服务实现此方法，处理各自的响应格式。

        Args:
            data: 外部 API 返回的完整 JSON 字典

        Returns:
            OCRResult: 统一格式的识别结果
        """
        ...

    def is_available(self) -> bool:
        return bool(self._api_url)


class ExternalAPIProvider(BaseExternalAPIProvider):
    """通用外部 API Provider（向后兼容）

    尝试自动识别常见的响应格式：
    - 包装格式：{code, message, data: [{page, content}]}
    - 扁平格式：{full_text, pages, confidence}
    """

    @property
    def name(self) -> str:
        return "external_api"

    def _adapt_response(self, data: dict) -> OCRResult:
        from .provider import OCRBlock, PageOCRResult

        # 如果是包装格式 {code, data}，解包
        if "code" in data and "data" in data:
            inner = data["data"]
            if isinstance(inner, str):
                return OCRResult(
                    full_text=inner,
                    pages=[PageOCRResult(page_num=1, blocks=[], full_text=inner)] if inner else [],
                    avg_confidence=1.0 if inner else 0.0,
                    provider_name=self.name,
                )
            elif isinstance(inner, list):
                data = {"pages": inner}
            elif isinstance(inner, dict):
                data = inner

        # 提取完整文本
        full_text = data.get("full_text") or data.get("text") or data.get("content") or ""

        # 提取置信度
        avg_confidence = float(data.get("avg_confidence") or data.get("confidence") or 0.0)

        # 提取按页结果
        pages: list[PageOCRResult] = []
        for idx, page_data in enumerate(data.get("pages") or []):
            page_num = page_data.get("page_num") or page_data.get("page", idx + 1)
            page_text = (
                page_data.get("full_text")
                or page_data.get("text")
                or page_data.get("content")
                or ""
            )

            blocks: list[OCRBlock] = []
            for block_data in page_data.get("blocks") or []:
                blocks.append(OCRBlock(
                    text=block_data.get("text", ""),
                    confidence=float(block_data.get("confidence", 0.0)),
                    bbox=tuple(block_data["bbox"]) if block_data.get("bbox") and len(block_data["bbox"]) == 4 else None,
                ))

            if not page_text and blocks:
                page_text = "\n".join(b.text for b in blocks)

            pages.append(PageOCRResult(page_num=page_num, blocks=blocks, full_text=page_text))

        # 拼接全文
        if not full_text and pages:
            full_text = "\n\n".join(p.full_text for p in pages if p.full_text)

        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_confidence,
            provider_name=self.name,
            metadata=data.get("metadata") or {},
        )
