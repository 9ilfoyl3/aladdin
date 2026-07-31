"""HTTP OCR 服务的公共基类

只承担传输层共性：multipart 上传、Bearer 认证、耗时日志、空结果告警。
响应契约由各子类的 ``_adapt_response`` 独占，不在本层做任何格式推断。
"""

from __future__ import annotations

import logging
import os
import time
from abc import abstractmethod

import httpx

from .errors import OCRResponseFormatError
from .provider import OCRProvider, OCRResult

logger = logging.getLogger(__name__)


class HTTPOCRProvider(OCRProvider):
    """通过 HTTP multipart 上传文件的 OCR Provider 基类"""

    #: multipart 中的文件字段名（MinerU 用复数 files）
    FILE_FIELD: str = "file"

    def __init__(
        self, api_url: str, api_key: str = "", timeout: float = 30.0,
        extra_config: dict | None = None,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._timeout = timeout
        self._extra_config = extra_config or {}

    @property
    def api_url(self) -> str:
        return self._api_url

    def is_available(self) -> bool:
        return bool(self._api_url)

    def build_form_data(self, file_path: str) -> dict[str, str]:
        """附加的 multipart 表单字段，子类按需覆盖（如 MinerU 的 backend）"""
        return {}

    def build_headers(self) -> dict[str, str]:
        """请求头，子类可覆盖以自定义认证方式"""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def recognize(self, file_path: str) -> OCRResult:
        """上传文件到外部 OCR 服务并按本 Provider 的契约解析响应"""
        headers = self.build_headers()
        form_data = self.build_form_data(file_path)
        filename = os.path.basename(file_path)

        logger.info(
            "[OCR][%s] 发送文件到 %s, 文件: %s", self.name, self._api_url, filename
        )

        start = time.time()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            with open(file_path, "rb") as f:
                files = {self.FILE_FIELD: (filename, f)}
                response = await client.post(
                    self._api_url, files=files, data=form_data or None, headers=headers
                )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise OCRResponseFormatError(
                    provider=self.name,
                    endpoint=self._api_url,
                    reason="响应不是合法 JSON",
                    sample=response.text[:200],
                ) from e

        elapsed_ms = (time.time() - start) * 1000
        logger.info(
            "[OCR][%s] 响应状态码: %d, 耗时: %.0fms",
            self.name, response.status_code, elapsed_ms,
        )

        result = self._adapt_response(data, file_path)

        if not result.full_text.strip():
            # 契约匹配但没识别出文本：区分"服务确实返回空"与"格式未适配"的关键线索
            logger.warning(
                "[OCR][%s] 未识别出文本（响应格式符合契约），文件: %s, 响应样本: %.300s",
                self.name, filename, repr(data),
            )
        else:
            logger.info(
                "[OCR][%s] 识别完成: 文本长度=%d, 页数=%d, 置信度=%.3f",
                self.name, len(result.full_text), len(result.pages), result.avg_confidence,
            )
        return result

    def _format_error(self, reason: str, sample: object) -> OCRResponseFormatError:
        """构造带端点与样本的契约错误（子类校验失败时使用）"""
        return OCRResponseFormatError(
            provider=self.name, endpoint=self._api_url, reason=reason, sample=sample
        )

    @abstractmethod
    def _adapt_response(self, data: object, file_path: str) -> OCRResult:
        """把外部服务响应适配为统一 :class:`OCRResult`

        契约由子类写死；不符合契约必须抛
        :class:`~app.pipeline.ocr.errors.OCRResponseFormatError`，
        不得回落到其他格式的解析分支。

        Args:
            data: 已解析的 JSON 响应（类型不保证，需自行校验）。
            file_path: 本次上传的文件路径（部分服务按文件名组织结果）。
        """
        ...
