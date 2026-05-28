"""Tests for ContextualEmbedder - 测试上下文拼接格式、父块去重逻辑"""

import pytest

from app.pipeline.context_embedder import ContextualEmbedder
from app.pipeline.metadata import ChunkMetadata


@pytest.fixture
def embedder():
    return ContextualEmbedder()


@pytest.fixture
def basic_metadata():
    """基础元数据，无 section_path"""
    return ChunkMetadata(
        filename="report.pdf",
        file_type="pdf",
        chunker_type="hierarchical",
        chunk_index=0,
    )


@pytest.fixture
def metadata_with_section():
    """带 section_path 的元数据"""
    return ChunkMetadata(
        filename="contract.pdf",
        file_type="pdf",
        chunker_type="hierarchical",
        chunk_index=2,
        section_path=["第三章", "合同条款"],
    )


# ============================================================
# 基本格式测试
# ============================================================


class TestBasicFormat:
    """测试 build_embed_text 输出的基本格式"""

    def test_output_starts_with_bracket_no_section(self, embedder, basic_metadata):
        """无 section_path 时，输出以 [filename] 开头"""
        result = embedder.build_embed_text("child content", basic_metadata)

        assert result.startswith("[report.pdf]")

    def test_output_starts_with_bracket_with_section(self, embedder, metadata_with_section):
        """有 section_path 时，输出以 [filename | section1 | section2] 开头"""
        result = embedder.build_embed_text("child content", metadata_with_section)

        assert result.startswith("[contract.pdf | 第三章 | 合同条款]")

    def test_output_always_starts_with_open_bracket(self, embedder, basic_metadata):
        """输出始终以 '[' 开头"""
        result = embedder.build_embed_text("any text", basic_metadata)

        assert result[0] == "["

    def test_empty_section_path_only_filename_in_prefix(self, embedder):
        """section_path 为空列表时，前缀只包含 filename"""
        metadata = ChunkMetadata(
            filename="notes.md",
            file_type="md",
            chunker_type="hierarchical",
            chunk_index=0,
            section_path=[],
        )

        result = embedder.build_embed_text("some text", metadata)

        first_line = result.split("\n")[0]
        assert first_line == "[notes.md]"


# ============================================================
# 父块上下文测试
# ============================================================


class TestParentContext:
    """测试父块上下文拼接逻辑"""

    def test_parent_context_included_when_provided(self, embedder, basic_metadata):
        """提供 parent_chunk 时，其前 150 字符出现在输出中"""
        parent = "这是父块的内容，提供上下文信息。" * 10  # 超过 150 字符
        child = "这是子块的具体内容。"

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=parent)

        parent_context = parent[:150].strip()
        assert parent_context in result

    def test_parent_dedup_when_prefix_matches_child(self, embedder, basic_metadata):
        """当 parent[:150] == child[:150] 时，不包含父块上下文"""
        # 构造 parent 和 child 前 150 字符完全相同的场景
        shared_prefix = "A" * 150
        parent = shared_prefix + "父块后续内容不同"
        child = shared_prefix + "子块后续内容不同"

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=parent)

        # 输出应该只有前缀行和 child，不包含单独的 parent context 行
        lines = result.split("\n")
        assert len(lines) == 2  # [prefix] 和 child，没有 parent context
        assert lines[0] == "[report.pdf]"
        assert lines[1] == child

    def test_no_parent_only_prefix_and_child(self, embedder, basic_metadata):
        """parent_chunk 为 None 时，输出只有前缀 + child"""
        child = "这是子块内容。"

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=None)

        lines = result.split("\n")
        assert len(lines) == 2
        assert lines[0] == "[report.pdf]"
        assert lines[1] == child

    def test_child_always_included_in_output(self, embedder, metadata_with_section):
        """child_chunk 完整文本始终出现在输出中"""
        child = "这是一段完整的子块文本，包含重要的业务逻辑描述和技术细节。"
        parent = "父块提供的上下文背景信息。"

        result = embedder.build_embed_text(child, metadata_with_section, parent_chunk=parent)

        assert child in result

    def test_child_included_without_parent(self, embedder, basic_metadata):
        """无 parent 时 child 仍完整包含"""
        child = "独立的子块内容，不需要父块上下文。"

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=None)

        assert child in result

    def test_parent_context_truncated_to_150_chars(self, embedder, basic_metadata):
        """父块上下文截取不超过 150 字符"""
        parent = "A" * 300  # 300 字符的父块
        child = "B" * 50  # 不同于 parent 的子块

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=parent)

        # 输出中不应包含完整的 300 字符 parent
        assert "A" * 300 not in result
        # 但应包含前 150 字符
        assert "A" * 150 in result



# ============================================================
# context_header 优先使用测试
# ============================================================


class TestContextHeader:
    """测试 context_header 参数优先使用逻辑"""

    def test_context_header_prepended_to_child(self, embedder, basic_metadata):
        """context_header 非空时，输出以 context_header 开头"""
        header = "# 顶级标题 > ## 二级标题"
        child = "这是子块内容。"

        result = embedder.build_embed_text(child, basic_metadata, context_header=header)

        assert result.startswith(header)

    def test_context_header_format(self, embedder, basic_metadata):
        """context_header 非空时，格式为 {header}\n\n{child}"""
        header = "# 文档标题 > ## 章节"
        child = "子块文本内容。"

        result = embedder.build_embed_text(child, basic_metadata, context_header=header)

        assert result == f"{header}\n\n{child}"

    def test_context_header_overrides_metadata_prefix(self, embedder, metadata_with_section):
        """context_header 非空时，不使用 metadata 拼接逻辑"""
        header = "# 自定义面包屑"
        child = "子块内容。"

        result = embedder.build_embed_text(child, metadata_with_section, context_header=header)

        # 不应包含 metadata 拼接的 [filename | section] 格式
        assert "[contract.pdf" not in result
        assert result == f"{header}\n\n{child}"

    def test_context_header_overrides_parent_context(self, embedder, basic_metadata):
        """context_header 非空时，parent_chunk 不参与拼接"""
        header = "# 标题面包屑"
        child = "子块内容。"
        parent = "父块上下文信息，很长的一段文字。"

        result = embedder.build_embed_text(child, basic_metadata, parent_chunk=parent, context_header=header)

        assert parent[:50] not in result
        assert result == f"{header}\n\n{child}"

    def test_none_context_header_fallback_to_metadata(self, embedder, basic_metadata):
        """context_header 为 None 时，fallback 到现有 metadata 拼接逻辑"""
        child = "子块内容。"

        result = embedder.build_embed_text(child, basic_metadata, context_header=None)

        assert result.startswith("[report.pdf]")

    def test_empty_string_context_header_fallback_to_metadata(self, embedder, basic_metadata):
        """context_header 为空字符串时，fallback 到现有 metadata 拼接逻辑"""
        child = "子块内容。"

        result = embedder.build_embed_text(child, basic_metadata, context_header="")

        assert result.startswith("[report.pdf]")
