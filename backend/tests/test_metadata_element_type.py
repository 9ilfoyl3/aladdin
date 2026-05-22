"""Tests for MetadataExtractor._detect_element_type() method"""

import pytest

from app.pipeline.metadata import MetadataExtractor


@pytest.fixture
def extractor():
    return MetadataExtractor()


class TestDetectElementTypeText:
    """默认返回 text 类型"""

    def test_normal_paragraph(self, extractor):
        content = "这是一段普通的正文内容，用于测试元素类型检测功能。"
        assert extractor._detect_element_type(content) == "text"

    def test_empty_string(self, extractor):
        assert extractor._detect_element_type("") == "text"

    def test_whitespace_only(self, extractor):
        assert extractor._detect_element_type("   \n  \t  ") == "text"

    def test_long_text(self, extractor):
        content = "这是一段很长的文本内容。" * 50
        assert extractor._detect_element_type(content) == "text"

    def test_multiline_paragraph(self, extractor):
        content = "第一行正文内容\n第二行正文内容\n第三行正文内容\n第四行正文内容"
        assert extractor._detect_element_type(content) == "text"


class TestDetectElementTypeTable:
    """表格类型检测"""

    def test_markdown_table(self, extractor):
        content = "| 列1 | 列2 | 列3 |\n| --- | --- | --- |\n| 数据1 | 数据2 | 数据3 |"
        assert extractor._detect_element_type(content) == "table"

    def test_markdown_table_with_header(self, extractor):
        content = (
            "| Name | Age | City |\n"
            "|------|-----|------|\n"
            "| Alice | 30 | Beijing |\n"
            "| Bob | 25 | Shanghai |"
        )
        assert extractor._detect_element_type(content) == "table"

    def test_tab_separated_data(self, extractor):
        content = "名称\t数量\t价格\n苹果\t10\t5.0\n香蕉\t20\t3.5\n橙子\t15\t4.0"
        assert extractor._detect_element_type(content) == "table"

    def test_single_pipe_line_not_table(self, extractor):
        """单行含 | 但不足以判定为表格"""
        content = "这是一段文本 | 包含一个竖线\n但其他行都是普通文本\n没有表格结构\n也没有分隔符"
        assert extractor._detect_element_type(content) == "text"

    def test_majority_threshold(self, extractor):
        """超过50%的行是表格行才判定为表格"""
        content = "| a | b |\n| c | d |\n普通文本行"
        # 2/3 > 0.5, should be table
        assert extractor._detect_element_type(content) == "table"

    def test_below_majority_threshold(self, extractor):
        """不到50%的行是表格行则不判定为表格"""
        content = "| a | b |\n普通文本行1\n普通文本行2\n普通文本行3"
        # 1/4 < 0.5, should be text
        assert extractor._detect_element_type(content) == "text"


class TestDetectElementTypeTitle:
    """标题类型检测"""

    def test_markdown_h1(self, extractor):
        content = "# 第一章 概述"
        assert extractor._detect_element_type(content) == "title"

    def test_markdown_h2(self, extractor):
        content = "## 1.1 背景介绍"
        assert extractor._detect_element_type(content) == "title"

    def test_markdown_h3(self, extractor):
        content = "### 详细说明"
        assert extractor._detect_element_type(content) == "title"

    def test_chinese_chapter(self, extractor):
        content = "第三章 合同条款"
        assert extractor._detect_element_type(content) == "title"

    def test_chinese_section(self, extractor):
        content = "第二节 权利义务"
        assert extractor._detect_element_type(content) == "title"

    def test_chinese_article(self, extractor):
        content = "第十五条 违约责任"
        assert extractor._detect_element_type(content) == "title"

    def test_numbered_heading(self, extractor):
        content = "1.2 系统架构"
        assert extractor._detect_element_type(content) == "title"

    def test_long_text_not_title(self, extractor):
        """超过100字符的文本即使匹配标题模式也不判定为 title"""
        content = "# " + "这是一个非常长的标题" * 20
        assert len(content) >= 100
        assert extractor._detect_element_type(content) == "text"

    def test_chinese_numbered_heading(self, extractor):
        content = "一、总则"
        assert extractor._detect_element_type(content) == "title"

    def test_parenthesized_number(self, extractor):
        content = "（一）适用范围"
        assert extractor._detect_element_type(content) == "title"
