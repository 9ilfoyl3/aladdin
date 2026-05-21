"""PaperChunker 单元测试"""

import pytest

from app.pipeline.chunkers.paper import PaperChunker
from app.pipeline.chunker_router import ChunkerFactory


class TestPaperChunkerRegistration:
    """注册测试"""

    def test_registered_in_factory(self):
        """paper 类型已注册到 ChunkerFactory"""
        assert "paper" in ChunkerFactory.REGISTRY
        assert ChunkerFactory.REGISTRY["paper"] is PaperChunker

    def test_factory_create(self):
        """通过工厂创建 PaperChunker 实例"""
        chunker = ChunkerFactory.create("paper")
        assert isinstance(chunker, PaperChunker)


class TestPaperChunkerEdgeCases:
    """边界情况测试"""

    def setup_method(self):
        self.chunker = PaperChunker()

    def test_empty_text(self):
        result = self.chunker.chunk("")
        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_whitespace_only(self):
        result = self.chunker.chunk("   \n\n  ")
        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_no_section_headers(self):
        """无章节标题时整段作为一个父块"""
        text = "This is a plain text without any section headers."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 1
        assert result.parent_chunks[0] == text
        assert result.parent_child_map == {0: [0]}

    def test_metadata_parameter_accepted(self):
        """metadata 参数可传入"""
        result = self.chunker.chunk("Some text", metadata={"source": "paper.pdf"})
        assert len(result.parent_chunks) == 1


class TestPaperChunkerSectionSplit:
    """章节切分测试"""

    def setup_method(self):
        self.chunker = PaperChunker()

    def test_split_by_standard_sections(self):
        """按标准学术论文章节切分"""
        text = "Abstract\nThis is the abstract.\n\nIntroduction\nThis is the intro.\n\nConclusion\nThis is the conclusion."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 3
        assert result.parent_chunks[0].startswith("Abstract")
        assert result.parent_chunks[1].startswith("Introduction")
        assert result.parent_chunks[2].startswith("Conclusion")

    def test_split_by_methods_section(self):
        """识别 Methods/Methodology 章节"""
        text = "Abstract\nContent.\n\nMethods\nOur method.\n\nMethodology\nAnother approach."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 3

    def test_split_by_results_discussion(self):
        """识别 Results 和 Discussion 章节"""
        text = "Results\nWe found X.\n\nDiscussion\nThis means Y."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2
        assert result.parent_chunks[0].startswith("Results")
        assert result.parent_chunks[1].startswith("Discussion")

    def test_references_section(self):
        """识别 References 章节"""
        text = "Conclusion\nWe conclude.\n\nReferences\n[1] Smith 2020."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2
        assert result.parent_chunks[1].startswith("References")

    def test_preamble_before_first_section(self):
        """第一个章节标题之前的内容（标题、作者）作为独立父块"""
        text = "My Paper Title\nAuthor Name\n\nAbstract\nThis is the abstract."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2
        assert "My Paper Title" in result.parent_chunks[0]
        assert result.parent_chunks[1].startswith("Abstract")

    def test_numbered_sections(self):
        """支持带编号的章节标题（如 1. Introduction）"""
        text = "1. Introduction\nIntro content.\n\n2. Methods\nMethods content.\n\n3. Results\nResults content."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 3

    def test_case_insensitive_matching(self):
        """章节标题匹配不区分大小写"""
        text = "ABSTRACT\nContent.\n\nINTRODUCTION\nMore content."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2


class TestPaperChunkerChildSplit:
    """子块切分测试"""

    def setup_method(self):
        self.chunker = PaperChunker()

    def test_paragraphs_as_children(self):
        """章节内多段落切分为子块"""
        text = "Introduction\nFirst paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 1
        # 章节标题 + 3 个段落 → 按双换行切分
        assert len(result.child_chunks) == 3

    def test_single_paragraph_section(self):
        """单段落章节只有一个子块"""
        text = "Abstract\nShort abstract content.\n\nConclusion\nShort conclusion."
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2
        # 每个章节只有标题+一行内容，按单换行切分
        assert len(result.parent_child_map[0]) >= 1
        assert len(result.parent_child_map[1]) >= 1


class TestPaperChunkerStructureValidity:
    """结构有效性测试"""

    def setup_method(self):
        self.chunker = PaperChunker()

    def test_parent_child_map_indices_valid(self):
        """parent_child_map 索引有效"""
        text = """Title and Authors

Abstract
This paper presents a novel approach.
We demonstrate improvements.

Introduction
The problem has been studied.
Previous work has limitations.

Methods
We designed our experiment.
Data was collected.

Results
Our method achieved 95% accuracy.

Conclusion
We presented a novel approach.

References
[1] Smith 2020.
[2] Jones 2021."""
        result = self.chunker.chunk(text)

        for k, v in result.parent_child_map.items():
            assert 0 <= k < len(result.parent_chunks)
            for ci in v:
                assert 0 <= ci < len(result.child_chunks)

    def test_all_children_mapped_exactly_once(self):
        """每个子块恰好被映射一次"""
        text = "Abstract\nContent A.\n\nIntroduction\nContent B.\n\nConclusion\nContent C."
        result = self.chunker.chunk(text)

        all_child_indices = []
        for v in result.parent_child_map.values():
            all_child_indices.extend(v)

        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))

    def test_full_paper_structure(self):
        """完整论文结构测试"""
        text = """Deep Learning for NLP
John Doe, MIT

Abstract
We present a new model for NLP tasks.
Our approach outperforms baselines.

Introduction
Natural language processing has seen rapid progress.

Recent advances in deep learning have enabled new approaches.

Methods
We use a transformer-based architecture.

The model is trained on a large corpus.

Results
Our model achieves state-of-the-art results.

Table 1 shows the comparison.

Discussion
The results demonstrate effectiveness.

However, computational cost remains high.

Conclusion
We have presented a novel approach.

Future work will explore efficiency improvements.

References
[1] Vaswani et al. 2017. Attention is all you need.
[2] Devlin et al. 2019. BERT."""
        result = self.chunker.chunk(text)

        # 应有：preamble + Abstract + Introduction + Methods + Results + Discussion + Conclusion + References
        assert len(result.parent_chunks) == 8

        # 验证结构完整性
        all_child_indices = []
        for v in result.parent_child_map.values():
            all_child_indices.extend(v)
        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))
