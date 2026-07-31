"""连通性测试样张生成

用 PyMuPDF 就地生成带已知字符的图片 / 单页 PDF，不引入二进制资源文件。
测试文本用纯 ASCII，避免中文字库缺失导致"识别不出"被误判成服务故障。
"""

from __future__ import annotations

import os
import tempfile

import fitz  # pymupdf

# 样张中的已知内容：纯 ASCII + 数字，各类 OCR 服务均应稳定识别
TEST_TEXT = "OCR TEST 12345"

# 页面尺寸（点）与渲染 DPI：足够大以保证 OCR 可读
_PAGE_WIDTH = 420
_PAGE_HEIGHT = 200
_RENDER_DPI = 200


def _build_page(doc: fitz.Document) -> fitz.Page:
    """在文档中新建一页并写入测试文本"""
    page = doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    page.insert_text((40, 110), TEST_TEXT, fontsize=36)
    return page


def build_test_image(dir_path: str) -> str:
    """生成测试图片（PNG），返回文件路径"""
    doc = fitz.open()
    try:
        page = _build_page(doc)
        pix = page.get_pixmap(dpi=_RENDER_DPI)
        path = os.path.join(dir_path, "ocr_test_sample.png")
        pix.save(path)
        return path
    finally:
        doc.close()


def build_test_pdf(dir_path: str) -> str:
    """生成单页测试 PDF，返回文件路径"""
    doc = fitz.open()
    try:
        _build_page(doc)
        path = os.path.join(dir_path, "ocr_test_sample.pdf")
        doc.save(path)
        return path
    finally:
        doc.close()


def make_sample_dir() -> str:
    """创建存放样张的临时目录（调用方负责清理）"""
    return tempfile.mkdtemp(prefix="ocr_test_")
