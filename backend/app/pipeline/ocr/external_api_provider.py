"""External API Provider - 外部 HTTP OCR 服务的抽象基类与通用实现

支持三种 provider_type：
- external_api: 通用外部 API（自动探测 PaddleOCR 嵌套格式 / 结构化字段格式）
- external_api_paddle: 明确 PaddleOCR 格式（仅解析嵌套数组）
- external_api_vl: VL/通用格式（跳过 PaddleOCR 探测，按结构化字段解析）
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod

import httpx

from .provider import OCRProvider, OCRResult

logger = logging.getLogger(__name__)


def _parse_paddle_line(line) -> tuple[str, float, tuple[float, float, float, float] | None] | None:
    """解析 PaddleOCR 单行结果，兼容两种常见形状。

    - ``[[[x,y]*4], [text, score]]``（PaddleOCR 原生）
    - ``[[[x,y]*4], text, score]``（部分服务展平了文本与分数）

    Args:
        line: 单行原始结果。

    Returns:
        ``(text, confidence, bbox)``；无法解析时返回 None（调用方跳过该行）。
    """
    if not isinstance(line, (list, tuple)) or len(line) < 2:
        return None

    box = line[0]
    payload = line[1]

    if isinstance(payload, (list, tuple)):
        # [box, [text, score]]
        if not payload or not isinstance(payload[0], str):
            return None
        text = payload[0]
        conf = float(payload[1]) if len(payload) > 1 and isinstance(payload[1], (int, float)) else 0.0
    elif isinstance(payload, str):
        # [box, text, score]
        text = payload
        conf = (
            float(line[2])
            if len(line) > 2 and isinstance(line[2], (int, float))
            else 0.0
        )
    else:
        return None

    bbox: tuple[float, float, float, float] | None = None
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    except (IndexError, TypeError, ValueError):
        bbox = None

    return text, conf, bbox


def _normalize_payload(data) -> dict:
    """把外部服务返回的 JSON 顶层结构统一成 dict。

    部分 OCR 服务直接返回数组（如 ``[{page, content}, ...]`` 或 PaddleOCR 的嵌套数组），
    此时包一层 ``{"data": ...}``，避免下游按 dict 取字段时抛
    ``AttributeError: 'list' object has no attribute 'get'``。
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"data": data}
    if isinstance(data, str):
        return {"data": data}
    return {}


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

        # 顶层可能是数组/字符串，统一成 dict 后再交给各 provider 适配
        result = self._adapt_response(_normalize_payload(data))
        if not result.full_text.strip():
            # 解析不出文本时打印响应样本，便于定位是服务返回为空还是格式未适配
            logger.warning(
                "[OCR][%s] 未解析出文本，响应样本: %.500s", self.name, repr(data)
            )
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

        扫描件常出现空白页（``page`` 为 ``[]`` / ``None``），因此**跳过空页**继续找第一个
        有内容的页再判定；只要任一页存在可解析的 PaddleOCR 行即认定为该格式。
        """
        if not isinstance(inner, list):
            return False

        for page in inner:
            if isinstance(page, dict):
                # 结构化字段格式（{page, content} 等），不是 PaddleOCR 嵌套数组
                return False
            if not isinstance(page, list) or not page:
                continue  # 空白页无法判定，看下一页
            return _parse_paddle_line(page[0]) is not None

        return False

    def _parse_paddleocr(self, inner: list) -> OCRResult:
        """解析 PaddleOCR 原生嵌套格式为统一 OCRResult。每个外层元素视为一页。"""
        from .provider import OCRBlock, PageOCRResult

        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        for idx, page_lines in enumerate(inner):
            blocks: list[OCRBlock] = []
            if not isinstance(page_lines, list):
                page_lines = []
            for line in page_lines:
                parsed = _parse_paddle_line(line)
                if parsed is None:
                    continue
                text, conf, bbox = parsed
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

        # 包装格式 {code, data} / {data} 均解包（部分服务不返回 code）
        if "data" in data:
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
            # 页元素可能不是 dict：字符串（纯文本页）或数组（PaddleOCR 行数组，
            # 探测因空白页/行结构差异未命中时会走到这里），逐类兜底避免 .get 崩溃。
            if isinstance(page_data, str):
                pages.append(PageOCRResult(page_num=idx + 1, blocks=[], full_text=page_data))
                continue
            if isinstance(page_data, list):
                line_blocks: list[OCRBlock] = []
                for line in page_data:
                    parsed = _parse_paddle_line(line)
                    if parsed is None:
                        continue
                    text, conf, bbox = parsed
                    line_blocks.append(OCRBlock(text=text, confidence=conf, bbox=bbox))
                pages.append(PageOCRResult(
                    page_num=idx + 1,
                    blocks=line_blocks,
                    full_text="\n".join(b.text for b in line_blocks),
                ))
                continue
            if not isinstance(page_data, dict):
                continue

            page_num = page_data.get("page_num") or page_data.get("page", idx + 1)
            page_text = (
                page_data.get("full_text")
                or page_data.get("text")
                or page_data.get("content")
                or ""
            )

            blocks: list[OCRBlock] = []
            for block_data in page_data.get("blocks") or []:
                if not isinstance(block_data, dict):
                    continue
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

        metadata = data.get("metadata")
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_confidence,
            provider_name=self.name,
            metadata=metadata if isinstance(metadata, dict) else {},
        )


class PaddleOCRProvider(BaseExternalAPIProvider):
    """明确 PaddleOCR 格式的 Provider

    仅解析 PaddleOCR 原生嵌套格式：data[图][行] = [四点坐标框, [文本, 置信度]]
    如果格式不匹配则抛出异常，不做 fallback 探测。
    """

    @property
    def name(self) -> str:
        return "external_api_paddle"

    def _adapt_response(self, data: dict) -> OCRResult:
        from .provider import OCRBlock, PageOCRResult

        # 解包 {code, data} 格式
        inner = data.get("data", data)

        if not isinstance(inner, list):
            raise ValueError(
                f"PaddleOCR Provider 预期 data 为 list，实际为 {type(inner).__name__}"
            )

        # 直接按 PaddleOCR 格式解析
        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        for idx, page_lines in enumerate(inner):
            blocks: list[OCRBlock] = []
            if not isinstance(page_lines, list):
                # 空白页可能返回 None，占位保持页码连续
                pages.append(PageOCRResult(page_num=idx + 1, blocks=[], full_text=""))
                continue
            for line in page_lines:
                parsed = _parse_paddle_line(line)
                if parsed is None:
                    continue
                text, conf, bbox = parsed
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


class VLOCRProvider(BaseExternalAPIProvider):
    """VL 模型 / 通用格式 Provider

    适用于 VL 模型 OCR 端点等返回结构化 JSON 的服务。
    跳过 PaddleOCR 格式探测，直接按 {text/content/full_text, pages} 字段解析。

    支持的返回格式：
    - {code, data: "纯文本"}
    - {code, data: [{page/page_num, text/content/full_text, blocks?}, ...]}
    - {code, data: {full_text, pages?, confidence?}}
    - {full_text/text/content, pages?, confidence?}
    """

    @property
    def name(self) -> str:
        return "external_api_vl"

    def _adapt_response(self, data: dict) -> OCRResult:
        from .provider import OCRBlock, PageOCRResult

        # 解包 {code, data} / {data} 格式（部分服务不返回 code）
        if "data" in data:
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
            if isinstance(page_data, str):
                # 简单字符串列表
                pages.append(PageOCRResult(page_num=idx + 1, blocks=[], full_text=page_data))
                continue
            if not isinstance(page_data, dict):
                continue
            page_num = page_data.get("page_num") or page_data.get("page", idx + 1)
            page_text = (
                page_data.get("full_text")
                or page_data.get("text")
                or page_data.get("content")
                or ""
            )

            blocks: list[OCRBlock] = []
            for block_data in page_data.get("blocks") or []:
                if not isinstance(block_data, dict):
                    continue
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

        metadata = data.get("metadata")
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_confidence if avg_confidence else (1.0 if full_text else 0.0),
            provider_name=self.name,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
