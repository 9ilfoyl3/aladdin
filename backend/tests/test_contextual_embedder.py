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
