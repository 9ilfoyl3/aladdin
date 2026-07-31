"""按 Provider 能力准备 OCR 输入

把"文档"转换成目标 Provider 能直接接受的输入形态。这是 pipeline 的职责，
不该由 Provider 承担，也不该靠"先试一次、返回空再回落"在运行时试探。

当前唯一需要转换的场景：只接受图片的 Provider（如 PaddleOCR）遇到 PDF
→ 用 PyMuPDF 按页渲染**整页图片**。相比旧的"提取嵌入图片"方案，按页渲染
不会因内容哈希去重把重复的扫描页当水印丢掉，混合排版页也能拿到完整版面。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # pymupdf

from .errors import OCRUnsupportedInputError
from .provider import INPUT_KIND_BY_EXT, INPUT_PDF, OCRCapability

logger = logging.getLogger(__name__)

# 渲染 DPI：清晰度与体积的平衡点，OCR 识别率在 200 DPI 已趋于饱和
_RENDER_DPI = 200
# 单文档最大渲染页数，防止超长 PDF 打爆 OCR 服务
_MAX_RENDER_PAGES = 200


@dataclass
class PreparedInput:
    """准备好的 OCR 输入

    Attributes:
        kind: ``"whole"`` 整文件直送；``"pages"`` 按页图片逐页识别。
        paths: 待识别文件路径；``pages`` 时按页序排列。
        temp_dir: 需要调用方清理的临时目录（无临时产物时为 None）。
        truncated_pages: 因页数上限被跳过的页数（>0 时记 WARNING）。
    """

    kind: str
    paths: list[str] = field(default_factory=list)
    temp_dir: str | None = None
    truncated_pages: int = 0

    def cleanup(self) -> None:
        """清理渲染产生的临时目录（幂等）"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logger.debug("已清理 OCR 渲染临时目录: %s", self.temp_dir)


def detect_input_kind(file_path: str) -> str | None:
    """按扩展名判定输入类型，不可 OCR 的类型返回 None"""
    ext = Path(file_path).suffix.lstrip(".").lower()
    return INPUT_KIND_BY_EXT.get(ext)


def prepare_input(
    file_path: str, capability: OCRCapability, provider_name: str = ""
) -> PreparedInput:
    """按 Provider 能力决定如何把文件交给它

    Args:
        file_path: 原始文件路径。
        capability: 目标 Provider 的能力声明。
        provider_name: Provider 标识（仅用于错误信息）。

    Returns:
        PreparedInput: 整文件或按页图片。

    Raises:
        OCRUnsupportedInputError: 输入类型不可 OCR，或能力无法通过转换弥合。
    """
    input_kind = detect_input_kind(file_path)
    if input_kind is None:
        ext = Path(file_path).suffix.lstrip(".").lower() or "未知"
        raise OCRUnsupportedInputError(provider_name, ext, capability.accepts)

    # Provider 直接吃这种输入 → 原文件直送
    if capability.accepts_kind(input_kind):
        return PreparedInput(kind="whole", paths=[file_path])

    # 仅图片的 Provider 遇到 PDF → 按页渲染整页图片
    if input_kind == INPUT_PDF:
        return _render_pdf_pages(file_path)

    raise OCRUnsupportedInputError(provider_name, input_kind, capability.accepts)


def _render_pdf_pages(file_path: str) -> PreparedInput:
    """把 PDF 每页渲染为 PNG，返回按页序排列的图片路径"""
    tmp_dir = tempfile.mkdtemp(prefix="ocr_pages_")
    paths: list[str] = []
    truncated = 0

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(f"无法打开 PDF 文件: {file_path}，错误: {e}") from e

    try:
        total = doc.page_count
        if total > _MAX_RENDER_PAGES:
            truncated = total - _MAX_RENDER_PAGES
            logger.warning(
                "PDF 共 %d 页，超过 OCR 渲染上限 %d 页，超出部分将被跳过",
                total, _MAX_RENDER_PAGES,
            )

        for page_idx in range(min(total, _MAX_RENDER_PAGES)):
            page = doc.load_page(page_idx)
            pix = page.get_pixmap(dpi=_RENDER_DPI)
            img_path = os.path.join(tmp_dir, f"page{page_idx + 1:04d}.png")
            pix.save(img_path)
            paths.append(img_path)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        doc.close()

    logger.info("PDF 按页渲染完成: %d 页 -> %s (dpi=%d)", len(paths), tmp_dir, _RENDER_DPI)
    return PreparedInput(
        kind="pages", paths=paths, temp_dir=tmp_dir, truncated_pages=truncated
    )
