"""Tests for MetadataExtractor class - 测试各文件类型的元数据提取正确性"""

import pytest

from app.pipeline.metadata import MetadataExtractor, ChunkMetadata


@pytest.fixture
def extractor():
    return MetadataExtractor()


@pytest.fixture
def basic_doc_metadata():
    """基础文档元数据"""
    return {
        "filename": "test_doc.pdf",
        "file_type": "pdf",
    }


@pytest.fixture
def markdown_doc_metadata():
    """Markdown 文档元数据"""
    return {
        "filename": "readme.md",
        "file_type": "md",
    }


@pytest.fixture
def docx_doc_metadata():
    """DOCX 文档元数据"""
    return {
        "filename": "report.docx",
        "file_type": "docx",
        "chunker_type": "semantic",
    }


# ============================================================
# extract() 方法测试
# ============================================================


class TestExtract:
    """测试 extract() 方法的基本行为"""

    def test_output_length_equals_input_length(self, extractor, basic_doc_metadata):
        """输出列表长度等于输入 child_chunks 长度"""
        child_chunks = ["chunk1 content", "chunk2 content", "chunk3 content"]
        parent_chunks = ["parent chunk covering all children"]
        parent_child_map = {0: [0, 1, 2]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        assert len(result) == len(child_chunks)

    def test_output_length_single_chunk(self, extractor, basic_doc_metadata):
        """单个 chunk 时输出长度为 1"""
        child_chunks = ["single chunk"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        assert len(result) == 1

    def test_output_length_many_chunks(self, extractor, basic_doc_metadata):
        """多个 chunk 时输出长度正确"""
        child_chunks = [f"chunk {i}" for i in range(10)]
        parent_chunks = ["parent1", "parent2"]
        parent_child_map = {0: [0, 1, 2, 3, 4], 1: [5, 6, 7, 8, 9]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        assert len(result) == 10

    def test_filename_correctly_populated(self, extractor, basic_doc_metadata):
        """filename 从 doc_metadata 正确填充"""
        child_chunks = ["some content"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        assert result[0].filename == "test_doc.pdf"

    def test_file_type_correctly_populated(self, extractor, basic_doc_metadata):
        """file_type 从 doc_metadata 正确填充"""
        child_chunks = ["some content"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        assert result[0].file_type == "pdf"

    def test_file_type_markdown(self, extractor, markdown_doc_metadata):
        """Markdown 文件类型正确"""
        child_chunks = ["# Title\nSome content"]
        parent_chunks = ["# Title\nSome content"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=markdown_doc_metadata,
        )

        assert result[0].filename == "readme.md"
        assert result[0].file_type == "md"

    def test_chunker_type_defaults_to_hierarchical(self, extractor):
        """chunker_type 未指定时默认为 'hierarchical'"""
        doc_metadata = {"filename": "test.pdf", "file_type": "pdf"}
        child_chunks = ["content"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=doc_metadata,
        )

        assert result[0].chunker_type == "hierarchical"

    def test_chunker_type_from_metadata(self, extractor, docx_doc_metadata):
        """chunker_type 从 doc_metadata 中读取"""
        child_chunks = ["content"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=docx_doc_metadata,
        )

        assert result[0].chunker_type == "semantic"

    def test_chunk_index_sequential_zero_based(self, extractor, basic_doc_metadata):
        """chunk_index 从 0 开始顺序递增"""
        child_chunks = ["chunk A", "chunk B", "chunk C", "chunk D"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0, 1, 2, 3]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        for i, meta in enumerate(result):
            assert meta.chunk_index == i

    def test_all_metadata_fields_are_chunk_metadata_instances(
        self, extractor, basic_doc_metadata
    ):
        """所有返回值都是 ChunkMetadata 实例"""
        child_chunks = ["chunk1", "chunk2"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0, 1]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
        )

        for meta in result:
            assert isinstance(meta, ChunkMetadata)


# ============================================================
# _detect_page_num() 方法测试
# ============================================================


class TestDetectPageNum:
    """测试 _detect_page_num() 方法"""

    def test_returns_correct_page_for_pdf(self, extractor):
        """正确返回 PDF chunk 所在页码"""
        page_texts = [
            "第一页的内容，包含一些介绍性文字。",
            "第二页的内容，这里有具体的数据分析。",
            "第三页的内容，总结和结论部分。",
        ]
        chunk = "第二页的内容，这里有具体的数据分析。"

        result = extractor._detect_page_num(chunk, page_texts)

        assert result == 2

    def test_returns_one_based_page_numbers(self, extractor):
        """页码从 1 开始"""
        page_texts = [
            "这是第一页的文本内容，用于测试页码定位功能。",
            "这是第二页的文本内容。",
        ]
        chunk = "这是第一页的文本内容，用于测试页码定位功能。"

        result = extractor._detect_page_num(chunk, page_texts)

        assert result == 1

    def test_returns_none_when_page_texts_is_none(self, extractor, basic_doc_metadata):
        """page_texts 为 None 时，extract 中不调用 _detect_page_num，page_num 为 None"""
        child_chunks = ["some content"]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        result = extractor.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=basic_doc_metadata,
            page_texts=None,
        )

        assert result[0].page_num is None

    def test_returns_none_when_page_texts_is_empty(self, extractor):
        """page_texts 为空列表时返回 None"""
        chunk = "some content that won't be found"
        page_texts: list[str] = []

        # 空列表时 extract 中条件 `if file_type == "pdf" and page_texts` 为 False
        # 直接测试 _detect_page_num 不会被调用的场景
        doc_metadata = {"filename": "test.pdf", "file_type": "pdf"}
        child_chunks = [chunk]
        parent_chunks = ["parent"]
        parent_child_map = {0: [0]}

        extractor_instance = MetadataExtractor()
        result = extractor_instance.extract(
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            parent_child_map=parent_child_map,
            doc_metadata=doc_metadata,
            page_texts=page_texts,
        )

        assert result[0].page_num is None

    def test_returns_none_when_chunk_not_found(self, extractor):
        """chunk 内容在任何页面中都找不到时返回 None"""
        page_texts = [
            "第一页的内容。",
            "第二页的内容。",
        ]
        chunk = "这段文字在任何页面中都不存在"

        result = extractor._detect_page_num(chunk, page_texts)

        assert result is None

    def test_handles_multi_page_documents(self, extractor):
        """正确处理多页文档"""
        page_texts = [
            "引言部分，本文档介绍了系统架构设计。",
            "第二章 系统概述，本章描述系统的整体架构。",
            "第三章 详细设计，本章描述各模块的详细设计。",
            "第四章 测试方案，本章描述测试策略和用例。",
            "附录 A，参考文献列表。",
        ]
        chunk = "第四章 测试方案，本章描述测试策略和用例。"

        result = extractor._detect_page_num(chunk, page_texts)

        assert result == 4

    def test_uses_first_50_chars_as_anchor(self, extractor):
        """使用 chunk 前50字符作为定位锚点"""
        long_text = "这是一段很长的文本内容用于测试前50字符定位功能，后面还有更多内容但不影响定位。"
        page_texts = [
            "其他页面内容",
            long_text + "额外的页面内容",
        ]

        result = extractor._detect_page_num(long_text, page_texts)

        assert result == 2

    def test_returns_earliest_match_page(self, extractor):
        """当多页包含相同前缀时，返回最早出现位置的页码"""
        page_texts = [
            "重复内容出现在这里，但位置靠后。前面有很多其他文字。重复内容出现在这里",
            "重复内容出现在这里，但这是第二页的开头。",
        ]
        chunk = "重复内容出现在这里，但这是第二页的开头。"

        result = extractor._detect_page_num(chunk, page_texts)

        # 第二页开头位置 pos=0 < 第一页中的位置
        assert result == 2


# ============================================================
# _extract_section_path() 方法测试
# ============================================================


class TestExtractSectionPath:
    """测试 _extract_section_path() 方法"""

    def test_extracts_markdown_heading_path(self, extractor):
        """正确提取 Markdown 标题路径"""
        full_text = "# 第一章\n\n## 1.1 概述\n\n这是概述内容。\n\n## 1.2 背景\n\n这是背景内容。"
        chunk = "这是背景内容。"

        result = extractor._extract_section_path(chunk, full_text)

        # 应包含 # 第一章 和 ## 1.2 背景
        assert len(result) >= 1
        assert any("第一章" in h for h in result)

    def test_extracts_chinese_chapter_section_path(self, extractor):
        """正确提取中文章节路径"""
        full_text = "第一章 总则\n\n第一条 适用范围\n\n本规定适用于所有员工。\n\n第二条 定义\n\n以下术语的定义如下。"
        chunk = "以下术语的定义如下。"

        result = extractor._extract_section_path(chunk, full_text)

        assert len(result) >= 1
        # 应包含章级和条级标题
        assert any("第一章" in h for h in result)

    def test_returns_empty_list_when_no_headings(self, extractor):
        """没有标题时返回空列表"""
        full_text = "这是一段没有任何标题的纯文本内容。只有普通的段落文字。"
        chunk = "只有普通的段落文字。"

        result = extractor._extract_section_path(chunk, full_text)

        assert result == []

    def test_returns_empty_list_when_chunk_not_found(self, extractor):
        """chunk 在 full_text 中找不到时返回空列表"""
        full_text = "# 标题\n\n这是正文内容。"
        chunk = "这段文字不存在于全文中"

        result = extractor._extract_section_path(chunk, full_text)

        assert result == []

    def test_returns_empty_list_when_full_text_empty(self, extractor):
        """full_text 为空时返回空列表"""
        result = extractor._extract_section_path("some chunk", "")

        assert result == []

    def test_returns_empty_list_when_chunk_empty(self, extractor):
        """chunk 为空时返回空列表"""
        result = extractor._extract_section_path("", "# Title\nContent")

        assert result == []

    def test_handles_hierarchical_heading_levels(self, extractor):
        """正确处理多层级标题"""
        full_text = (
            "# 第一章 概述\n\n"
            "## 1.1 背景\n\n"
            "背景介绍内容。\n\n"
            "## 1.2 目标\n\n"
            "### 1.2.1 短期目标\n\n"
            "短期目标的具体描述。"
        )
        chunk = "短期目标的具体描述。"

        result = extractor._extract_section_path(chunk, full_text)

        # 应该有多个层级
        assert len(result) >= 2

    def test_higher_level_heading_resets_lower_levels(self, extractor):
        """高层级标题出现时重置低层级"""
        full_text = (
            "# 第一章\n\n"
            "## 第一节\n\n"
            "第一节内容。\n\n"
            "# 第二章\n\n"
            "第二章开头内容。"
        )
        chunk = "第二章开头内容。"

        result = extractor._extract_section_path(chunk, full_text)

        # 第二章出现后，第一节应被清除
        assert not any("第一节" in h for h in result)
        assert any("第二章" in h for h in result)


# ============================================================
# _detect_element_type() 方法测试
# ============================================================


class TestDetectElementType:
    """测试 _detect_element_type() 方法"""

    def test_returns_table_for_markdown_tables(self, extractor):
        """Markdown 表格返回 'table'"""
        content = "| 姓名 | 年龄 | 城市 |\n| --- | --- | --- |\n| 张三 | 30 | 北京 |"

        assert extractor._detect_element_type(content) == "table"

    def test_returns_table_for_tab_separated_data(self, extractor):
        """Tab 分隔数据返回 'table'"""
        content = "名称\t数量\t单价\n苹果\t10\t5.0\n香蕉\t20\t3.5"

        assert extractor._detect_element_type(content) == "table"

    def test_returns_title_for_short_heading(self, extractor):
        """短标题文本返回 'title'"""
        content = "## 系统架构设计"

        assert extractor._detect_element_type(content) == "title"

    def test_returns_text_for_normal_paragraph(self, extractor):
        """普通段落返回 'text'"""
        content = "这是一段普通的正文内容，描述了系统的基本功能和使用方法。"

        assert extractor._detect_element_type(content) == "text"

    def test_returns_text_for_empty_content(self, extractor):
        """空内容返回 'text'"""
        assert extractor._detect_element_type("") == "text"

    def test_returns_text_for_whitespace_only(self, extractor):
        """纯空白返回 'text'"""
        assert extractor._detect_element_type("   \n  ") == "text"
