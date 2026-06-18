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

    @staticmethod
    def _looks_like_paddleocr(inner) -> bool:
        """判断是否为 PaddleOCR 原生嵌套格式。

        结构特征：data[图][行] = [四点坐标框, [文本, 置信度]]
            inner          = [ page0, page1, ... ]
            page           = [ line0, line1, ... ]
            line           = [ [[x,y],[x,y],[x,y],[x,y]], ["文本", score] ]
        """
        try:
            line = inner[0][0]
            return (
                isinstance(line, list)
                and len(line) == 2
                and isinstance(line[0], list) and len(line[0]) == 4  # 四点框
                and isinstance(line[1], (list, tuple)) and len(line[1]) >= 1
                and isinstance(line[1][0], str)                      # 文本
            )
        except (IndexError, TypeError):
            return False

    def _parse_paddleocr(self, inner: list) -> OCRResult:
        """解析 PaddleOCR 原生嵌套格式为统一 OCRResult。每个外层元素视为一页。"""
        from .provider import OCRBlock, PageOCRResult

        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        for idx, page_lines in enumerate(inner):
            blocks: list[OCRBlock] = []
            for line in page_lines or []:
                try:
                    box, (text, *rest) = line[0], line[1]
                except (IndexError, TypeError, ValueError):
                    continue
                conf = float(rest[0]) if rest else 0.0
                # 四点框 [[x,y]*4] → (x1, y1, x2, y2)
                bbox = None
                try:
                    xs = [float(p[0]) for p in box]
                    ys = [float(p[1]) for p in box]
                    bbox = (min(xs), min(ys), max(xs), max(ys))
                except (IndexError, TypeError, ValueError):
                    bbox = None
                blocks.append(OCRBlock(text=text, confidence=conf, bbox=bbox))
                all_conf.append(conf)
            page_text = "\n".join(b.text for b in blocks)
            pages.append(PageOCRResult(page_num=idx + 1, blocks=blocks, full_text=page_text))

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)
        avg_conf = sum(all_conf) / len(all_conf) if all_conf else 0.0
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_conf,
            provider_name=self.name,
        )

    def _adapt_response(self, data: dict) -> OCRResult:
        from .provider import OCRBlock, PageOCRResult

        # 如果是包装格式 {code, data}，解包
        if "code" in data and "data" in data:
            inner = data["data"]
            # PaddleOCR 原生嵌套格式优先识别（data[图][行]=[四点框,[文本,分数]]）
            if isinstance(inner, list) and self._looks_like_paddleocr(inner):
                return self._parse_paddleocr(inner)
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
