"""测试 DocxLoader（Word 文档加载器）"""

import os

import pytest
from docx import Document

from app.pipeline.loaders.docx_loader import DocxLoader
from app.pipeline.loader import LoadResult


class TestDocxLoader:
    """DocxLoader 单元测试"""

    def setup_method(self):
        self.loader = DocxLoader()

    def _create_docx(self, tmp_path, filename: str, paragraphs: list[str]) -> str:
        """辅助方法：创建包含指定段落的 docx 文件"""
        file_path = str(tmp_path / filename)
        doc = Document()
        for text in paragraphs:
            doc.add_paragraph(text)
        doc.save(file_path)
        return file_path

    def test_load_single_paragraph(self, tmp_path):
        """加载单段落 docx"""
        file_path = self._create_docx(tmp_path, "single.docx", ["Hello Word"])

        result = self.loader.load(file_path)

        assert isinstance(result, LoadResult)
        assert "Hello Word" in result.content
        assert result.metadata["filename"] == "single.docx"
        assert result.metadata["file_type"] == "docx"
        assert result.metadata["file_size"] > 0

    def test_load_multi_paragraph(self, tmp_path):
        """加载多段落 docx"""
        paragraphs = ["第一段内容", "第二段内容", "第三段内容"]
        file_path = self._create_docx(tmp_path, "multi.docx", paragraphs)

        result = self.loader.load(file_path)

        assert "第一段内容" in result.content
        assert "第二段内容" in result.content
        assert "第三段内容" in result.content

    def test_paragraphs_joined_with_newline(self, tmp_path):
        """段落之间用换行符连接"""
        paragraphs = ["段落A", "段落B"]
        file_path = self._create_docx(tmp_path, "join.docx", paragraphs)

        result = self.loader.load(file_path)

        # 内容中应包含换行符分隔的段落
        assert "段落A" in result.content
        assert "段落B" in result.content
        assert "\n" in result.content

    def test_file_not_found(self):
        """文件不存在时抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            self.loader.load("/nonexistent/path/file.docx")

    def test_invalid_docx(self, tmp_path):
        """无效 docx 文件抛出 ValueError"""
        file_path = tmp_path / "invalid.docx"
        file_path.write_text("this is not a docx", encoding="utf-8")

        with pytest.raises(ValueError, match="无法解析 docx 文件"):
            self.loader.load(str(file_path))

    def test_file_size_metadata(self, tmp_path):
        """file_size 反映实际文件字节数"""
        file_path = self._create_docx(tmp_path, "size.docx", ["测试内容"])

        result = self.loader.load(file_path)

        actual_size = os.path.getsize(file_path)
        assert result.metadata["file_size"] == actual_size
