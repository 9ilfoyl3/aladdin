"""文档缩略图渲染（复用 pymupdf/fitz，无新依赖）。

为没有「原生页面可渲染」的文本类文档（md / txt）生成预览缩略图：

- :func:`image_bytes_to_png`：把任意图片字节（如链接转存抓到的 og:image 封面）
  转成统一的 PNG，供前端 preview 接口返回（方案 A）。
- :func:`render_text_card`：用 fitz 把「标题 + 正文摘要」画成一张文字卡片 PNG
  （方案 B 兜底）。纯 CPU、毫秒级，CJK 用 MuPDF 内置 ``china-s`` 字体，无需字体文件。

两者均为纯函数，失败返回 ``None``（由调用方静默降级，不影响主流程）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 卡片尺寸（与前端文件卡片 16:20 ≈ 4:5 宽高比一致），点为单位。
_CARD_W = 480
_CARD_H = 600
# 留白与排版常量。
_MARGIN = 44
_TITLE_FONTSIZE = 30
_BODY_FONTSIZE = 18
_TITLE_MAX_CHARS = 60   # 标题超长时截断，避免压满整张卡片
_BODY_MAX_CHARS = 360   # 正文摘要字符上限

# 缩略图封面图的最大边长（等比缩放上限，控制存储体积）。
_COVER_MAX_EDGE = 600


def image_bytes_to_png(data: bytes) -> bytes | None:
    """把任意格式的图片字节转为 PNG（并按最大边长等比缩小）。

    用于链接转存抓到的封面图（jpeg/png/webp 等）统一成 PNG 存储。无法解码时返回 None。
    """
    if not data:
        return None
    try:
        import fitz

        pix = fitz.Pixmap(data)
        # 带 alpha 的图先转 RGB，避免某些查看器显示异常。
        if pix.alpha:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        # 等比缩小到最大边长以内（放大无意义，仅缩小）。
        max_edge = max(pix.width, pix.height)
        if max_edge > _COVER_MAX_EDGE:
            scale = _COVER_MAX_EDGE / max_edge
            mat = fitz.Matrix(scale, scale)
            pix = fitz.Pixmap(pix, 0) if pix.alpha else pix
            # 用 fitz 的图片重采样：经由临时 Pixmap 变换。
            pix = _scale_pixmap(pix, mat)
        return pix.tobytes("png")
    except Exception as e:  # noqa: BLE001 - 解码失败静默降级
        logger.warning("封面图转 PNG 失败: %s", e)
        return None


def _scale_pixmap(pix, mat):
    """对 Pixmap 应用矩阵缩放（fitz 无直接 resize，借助 shrink 近似）。

    采用 ``Pixmap.shrink`` 的整数因子降采样：取最接近的 2 的幂因子，简单高效，
    缩略图场景对精度不敏感。失败则原样返回。
    """
    try:
        import math

        scale = mat.a  # 等比缩放，a==d
        if scale >= 1.0:
            return pix
        # shrink(n) 将边长缩为 1/2**n。取满足 1/2**n >= scale 的最小 n（不过度缩小）。
        n = max(0, int(math.floor(math.log2(1.0 / scale))))
        if n > 0:
            pix.shrink(n)
        return pix
    except Exception:  # noqa: BLE001
        return pix


def render_text_card(title: str, body: str) -> bytes | None:
    """把标题 + 正文摘要渲染为一张文字卡片 PNG（方案 B 兜底）。

    Args:
        title: 文档标题（通常取文件名去扩展名 / 文章标题）。
        body: 正文纯文本（取前若干字符作为摘要）。

    Returns:
        PNG 字节；渲染失败返回 None。
    """
    try:
        import fitz

        title_text = _clean(title)[:_TITLE_MAX_CHARS] or "文档"
        body_text = _clean(body)[:_BODY_MAX_CHARS]

        doc = fitz.open()
        page = doc.new_page(width=_CARD_W, height=_CARD_H)

        # 背景：浅色卡片。
        page.draw_rect(page.rect, color=None, fill=(0.97, 0.98, 0.99))
        # 顶部品牌色装饰条。
        page.draw_rect(
            fitz.Rect(0, 0, _CARD_W, 8), color=None, fill=(0.40, 0.74, 0.26)
        )

        # CJK 用 MuPDF 内置简体字体 china-s，无需外部字体文件。
        fontname = "china-s"

        title_rect = fitz.Rect(_MARGIN, _MARGIN + 16, _CARD_W - _MARGIN, _MARGIN + 220)
        page.insert_textbox(
            title_rect, title_text,
            fontsize=_TITLE_FONTSIZE, fontname=fontname,
            color=(0.10, 0.12, 0.15), align=0,
        )

        # 标题与正文之间分隔线。
        sep_y = _MARGIN + 232
        page.draw_line(
            fitz.Point(_MARGIN, sep_y), fitz.Point(_CARD_W - _MARGIN, sep_y),
            color=(0.85, 0.87, 0.90), width=1,
        )

        if body_text:
            body_rect = fitz.Rect(_MARGIN, sep_y + 18, _CARD_W - _MARGIN, _CARD_H - _MARGIN)
            page.insert_textbox(
                body_rect, body_text,
                fontsize=_BODY_FONTSIZE, fontname=fontname,
                color=(0.40, 0.44, 0.50), align=0,
            )

        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x 提升清晰度
        data = pix.tobytes("png")
        doc.close()
        return data
    except Exception as e:  # noqa: BLE001 - 渲染失败静默降级
        logger.warning("文字卡片缩略图渲染失败: %s", e)
        return None


def _clean(text: str) -> str:
    """折叠空白、去 markdown 抬头符号，得到适合展示的纯文本摘要。"""
    if not text:
        return ""
    out_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去常见 markdown 前缀标记（# 标题、> 引用、- * 列表、| 表格）。
        line = line.lstrip("#>*-| ").strip()
        # 跳过我们组装的来源抬头行（原文链接 / 来源），它们不适合进摘要。
        if line.startswith(("原文链接", "来源", "作者", "发布时间", "http://", "https://")):
            continue
        if line:
            out_lines.append(line)
    return " ".join(out_lines)
