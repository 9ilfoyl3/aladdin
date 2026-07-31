"""PaddleOCR HTTP 服务 Provider

契约（固定，不做探测）::

    {"code": 200, "msg": "success",
     "data": [ [ [四点框, ["文本", 置信度]], ... ], ... ]}

``data`` 外层每个元素是一页的行数组，空页允许为 ``[]`` 或 ``null``。
单行接受两种形状，均属契约内：

- ``[box, [text, score]]``（PaddleOCR 原生）
- ``[box, text, score]``（部分服务把文本与分数展平）

该服务**只接受图片**：PDF 输入会被服务端返回 ``data: [[]]``（成功但空），
因此能力声明为 ``accepts={image}``，PDF 由 input_prep 按页渲染成图片后逐页识别。
"""

from __future__ import annotations

from ..http_provider import HTTPOCRProvider
from ..provider import (
    INPUT_IMAGE,
    OCRBlock,
    OCRCapability,
    OCRResult,
    PageOCRResult,
)
from ..registry import register_provider


def parse_paddle_line(
    line: object,
) -> tuple[str, float, tuple[float, float, float, float] | None] | None:
    """解析 PaddleOCR 单行结果，兼容契约声明的两种行形状。

    Args:
        line: 单行原始结果。

    Returns:
        ``(text, confidence, bbox)``；形状无法识别时返回 None（调用方跳过该行）。
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
        conf = (
            float(payload[1])
            if len(payload) > 1 and isinstance(payload[1], (int, float))
            else 0.0
        )
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


@register_provider("paddle")
class PaddleOCRProvider(HTTPOCRProvider):
    """PaddleOCR HTTP 服务（纯文字识别，仅接受图片）"""

    capability = OCRCapability(
        accepts=frozenset({INPUT_IMAGE}),
        outputs_markdown=False,
        paginated=False,
        recommended_timeout=60.0,
    )

    @property
    def name(self) -> str:
        return "paddle"

    def _adapt_response(self, data: object, file_path: str) -> OCRResult:
        if not isinstance(data, dict):
            raise self._format_error(
                f"期望顶层为 JSON 对象（含 data 字段），实际为 {type(data).__name__}", data
            )

        if "data" not in data:
            raise self._format_error("响应缺少 data 字段", data)

        inner = data["data"]
        if not isinstance(inner, list):
            raise self._format_error(
                f"期望 data 为 PaddleOCR 嵌套数组，实际为 {type(inner).__name__}", data
            )

        # 首个非空元素若是 dict，说明这是"按页 {page, content}"的文件解析服务响应
        for item in inner:
            if isinstance(item, dict):
                raise self._format_error(
                    "data 元素为 {page, content} 对象，不是 PaddleOCR 嵌套数组"
                    "（该端点应选择 VL 文件解析服务类型）",
                    data,
                )
            if item:
                break

        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        for idx, page_lines in enumerate(inner):
            blocks: list[OCRBlock] = []
            if isinstance(page_lines, list):
                for line in page_lines:
                    parsed = parse_paddle_line(line)
                    if parsed is None:
                        continue
                    text, conf, bbox = parsed
                    blocks.append(OCRBlock(text=text, confidence=conf, bbox=bbox))
                    all_conf.append(conf)
            elif page_lines is not None:
                raise self._format_error(
                    f"期望 data[{idx}] 为行数组或 null，实际为 {type(page_lines).__name__}",
                    data,
                )
            pages.append(
                PageOCRResult(
                    page_num=idx + 1,
                    blocks=blocks,
                    full_text="\n".join(b.text for b in blocks),
                )
            )

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)
        avg_conf = sum(all_conf) / len(all_conf) if all_conf else 0.0
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_conf,
            provider_name=self.name,
        )
