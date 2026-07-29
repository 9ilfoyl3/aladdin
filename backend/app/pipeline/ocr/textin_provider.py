"""TextIn OCR Provider - 合合信息 TextIn OCR 服务适配

响应格式：
{
    "code": 200,
    "message": "success",
    "data": [
        {"page": 1, "content": "...识别文本..."},
        {"page": 2, "content": "..."},
        ...
    ]
}
"""

from __future__ import annotations

from .external_api_provider import BaseExternalAPIProvider
from .provider import OCRResult, PageOCRResult


class TextInProvider(BaseExternalAPIProvider):
    """TextIn OCR 服务 Provider

    适配 {code, message, data: [{page, content}]} 格式的响应。
    """

    @property
    def name(self) -> str:
        return "textin"

    def _adapt_response(self, data: dict) -> OCRResult:
        # 解包 data 字段
        pages_data = data.get("data") or []
        if not isinstance(pages_data, list):
            pages_data = []

        pages: list[PageOCRResult] = []
        for idx, item in enumerate(pages_data):
            if isinstance(item, str):
                pages.append(PageOCRResult(page_num=idx + 1, blocks=[], full_text=item))
                continue
            if not isinstance(item, dict):
                continue
            page_num = item.get("page", idx + 1)
            content = item.get("content") or ""
            pages.append(PageOCRResult(page_num=page_num, blocks=[], full_text=content))

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)

        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=1.0 if full_text else 0.0,
            provider_name=self.name,
        )
