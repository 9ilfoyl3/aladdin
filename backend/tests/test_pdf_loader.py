"""测试 PdfLoader（PDF 文档加载器）"""

import os

import fitz  # pymupdf
import pytest

from app.pipeline.loaders.pdf_loader import PdfLoader
from app.pipeline.loader import LoadResult


class TestPdfLoader:
    """PdfLoader 单元测试"""

    def setup_method(self):
        self.loader = PdfLoader()

    def _create_pdf(self, tmp_path, filename: str, pages: list[str]) -> str:
        """辅助方法：创建包含指定页面文本的 PDF 文件"""
        file_path = str(tmp_path / filename)
        doc = fitz.open()
        for text in pages:
            page = doc.new_page()
            # 在页面上插入文本
            page.insert_text((72, 72), text, fontsize=12)
        doc.save(file_path)
        doc.close()
        return file_path

    def test_load_single_page_pdf(self, tmp_path):
        """加载单页 PDF"""
        file_path = self._create_pdf(tmp_path, "single.pdf", ["Hello PDF"])

        result = self.loader.load(file_path)

        assert isinstance(result, LoadResult)
        assert "Hello PDF" in result.content
        assert result.metadata["filename"] == "single.pdf"
        assert result.metadata["file_type"] == "pdf"
        assert result.metadata["page_count"] == 1
        assert result.metadata["file_size"] > 0

    def test_load_multi_page_pdf(self, tmp_path):
        """加载多页 PDF"""
        pages = ["Page One Content", "Page Two Content", "Page Three Content"]
        file_path = self._create_pdf(tmp_path, "multi.pdf", pages)

        result = self.loader.load(file_path)

        assert result.metadata["page_count"] == 3
        assert "Page One Content" in result.content
        assert "Page Two Content" in result.content
        assert "Page Three Content" in result.content

    def test_load_empty_pdf(self, tmp_path):
        """加载空白页 PDF"""
        file_path = self._create_pdf(tmp_path, "empty.pdf", [""])

        result = self.loader.load(file_path)

        assert result.metadata["page_count"] == 1
        assert result.metadata["filename"] == "empty.pdf"

    def test_file_not_found(self):
        """文件不存在时抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            self.loader.load("/nonexistent/path/file.pdf")

    def test_invalid_pdf(self, tmp_path):
        """无效 PDF 文件抛出 ValueError"""
        file_path = tmp_path / "invalid.pdf"
        file_path.write_text("this is not a pdf", encoding="utf-8")

        with pytest.raises(ValueError, match="无法解析 PDF 文件"):
            self.loader.load(str(file_path))

    def test_file_size_metadata(self, tmp_path):
        """file_size 反映实际文件字节数"""
        file_path = self._create_pdf(tmp_path, "size.pdf", ["测试内容"])

        result = self.loader.load(file_path)

        actual_size = os.path.getsize(file_path)
        assert result.metadata["file_size"] == actual_size
