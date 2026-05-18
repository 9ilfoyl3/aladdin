"""测试 TextLoader（Markdown / TXT 加载器）"""

import os
import tempfile

import pytest

from app.pipeline.loaders.text_loader import TextLoader
from app.pipeline.loader import LoadResult


class TestTextLoader:
    """TextLoader 单元测试"""

    def setup_method(self):
        self.loader = TextLoader()

    def test_load_txt_file(self, tmp_path):
        """加载普通 txt 文件"""
        file = tmp_path / "test.txt"
        file.write_text("Hello, World!", encoding="utf-8")

        result = self.loader.load(str(file))

        assert isinstance(result, LoadResult)
        assert result.content == "Hello, World!"
        assert result.metadata["filename"] == "test.txt"
        assert result.metadata["file_type"] == "txt"
        assert result.metadata["file_size"] == len("Hello, World!".encode("utf-8"))

    def test_load_md_file(self, tmp_path):
        """加载 Markdown 文件"""
        content = "# 标题\n\n这是一段中文内容。"
        file = tmp_path / "readme.md"
        file.write_text(content, encoding="utf-8")

        result = self.loader.load(str(file))

        assert result.content == content
        assert result.metadata["filename"] == "readme.md"
        assert result.metadata["file_type"] == "md"

    def test_load_empty_file(self, tmp_path):
        """加载空文件"""
        file = tmp_path / "empty.txt"
        file.write_text("", encoding="utf-8")

        result = self.loader.load(str(file))

        assert result.content == ""
        assert result.metadata["file_size"] == 0

    def test_file_not_found(self):
        """文件不存在时抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            self.loader.load("/nonexistent/path/file.txt")

    def test_unsupported_extension(self, tmp_path):
        """不支持的扩展名抛出 ValueError"""
        file = tmp_path / "data.csv"
        file.write_text("a,b,c", encoding="utf-8")

        with pytest.raises(ValueError, match="不支持的文件类型"):
            self.loader.load(str(file))

    def test_encoding_fallback(self, tmp_path):
        """遇到无法解码的字节时，回退到 errors='ignore'"""
        file = tmp_path / "bad.txt"
        # 写入包含非法 utf-8 字节的内容
        content = b"Hello \xff\xfe World"
        file.write_bytes(content)

        result = self.loader.load(str(file))

        # 非法字节被忽略，合法部分保留
        assert "Hello" in result.content
        assert "World" in result.content

    def test_chinese_content(self, tmp_path):
        """正确处理中文内容"""
        content = "这是一段中文测试内容\n包含多行\n第三行"
        file = tmp_path / "chinese.md"
        file.write_text(content, encoding="utf-8")

        result = self.loader.load(str(file))

        assert result.content == content
        assert result.metadata["file_type"] == "md"

    def test_file_size_metadata(self, tmp_path):
        """file_size 反映实际字节数"""
        content = "你好"  # utf-8 编码为 6 字节
        file = tmp_path / "size.txt"
        file.write_text(content, encoding="utf-8")

        result = self.loader.load(str(file))

        assert result.metadata["file_size"] == len(content.encode("utf-8"))
