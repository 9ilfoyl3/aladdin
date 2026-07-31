"""VL 文件解析服务 Provider

契约（固定，不做探测）::

    {"code": 0, "message": "success", "data": [{"page": 1, "content": "markdown..."}]}

``data`` 允许两种形态，二者都属契约内：

- ``list[{page, content}]``：按页结果（``page`` 缺失时按序号补）。
- ``str``：整篇文本 / Markdown。

其余任何结构一律抛 :class:`OCRResponseFormatError`。
"""

from __future__ import annotations

from ..http_provider import HTTPOCRProvider
from ..provider import (
    INPUT_IMAGE,
    INPUT_PDF,
    OCRCapability,
    OCRResult,
    PageOCRResult,
)
from ..registry import register_provider


@register_provider("vl")
class VLProvider(HTTPOCRProvider):
    """多模态模型文件解析服务（直接接受 PDF，输出 Markdown）"""

    capability = OCRCapability(
        accepts=frozenset({INPUT_IMAGE, INPUT_PDF}),
        outputs_markdown=True,
        paginated=True,
        recommended_timeout=180.0,
    )

    @property
    def name(self) -> str:
        return "vl"

    def _adapt_response(self, data: object, file_path: str) -> OCRResult:
        if not isinstance(data, dict):
            raise self._format_error(
                f"期望顶层为 JSON 对象（含 data 字段），实际为 {type(data).__name__}", data
            )

        if "data" not in data:
            raise self._format_error("响应缺少 data 字段", data)

        inner = data["data"]

        if isinstance(inner, str):
            return self._result_from_text(inner)

        if not isinstance(inner, list):
            raise self._format_error(
                f"期望 data 为按页数组或字符串，实际为 {type(inner).__name__}", data
            )

        pages: list[PageOCRResult] = []
        for idx, item in enumerate(inner):
            if not isinstance(item, dict):
                raise self._format_error(
                    f"期望 data[{idx}] 为 {{page, content}} 对象，实际为 "
                    f"{type(item).__name__}（若服务返回的是 PaddleOCR 嵌套数组，"
                    f"请把服务类型改选为 PaddleOCR）",
                    data,
                )
            page_num = item.get("page") or item.get("page_num") or idx + 1
            content = item.get("content") or item.get("text") or item.get("full_text") or ""
            if not isinstance(content, str):
                raise self._format_error(
                    f"期望 data[{idx}].content 为字符串，实际为 {type(content).__name__}",
                    data,
                )
            pages.append(
                PageOCRResult(page_num=int(page_num), blocks=[], full_text=content)
            )

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=1.0 if full_text else 0.0,
            provider_name=self.name,
        )

    def _result_from_text(self, text: str) -> OCRResult:
        """data 为整篇字符串时的结果构造"""
        return OCRResult(
            full_text=text,
            pages=[PageOCRResult(page_num=1, blocks=[], full_text=text)] if text else [],
            avg_confidence=1.0 if text else 0.0,
            provider_name=self.name,
        )
