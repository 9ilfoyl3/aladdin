"""QAChunker 单元测试"""

import pytest

from app.pipeline.chunker import ChunkResult
from app.pipeline.chunkers.qa import QAChunker
from app.pipeline.chunker_router import ChunkerFactory


class TestQAChunkerBasic:
    """基本功能测试"""

    def test_english_qa_pairs(self):
        """解析 Q:/A: 格式的英文问答对"""
        chunker = QAChunker()
        text = "Q: What is Python?\nA: Python is a programming language.\n\nQ: What is Java?\nA: Java is another programming language."

        result = chunker.chunk(text)

        assert len(result.parent_chunks) == 2
        assert len(result.child_chunks) == 4
        assert result.parent_child_map == {0: [0, 1], 1: [2, 3]}

    def test_chinese_qa_pairs(self):
        """解析 问:/答: 格式的中文问答对"""
        chunker = QAChunker()
        text = "问: 什么是Python？\n答: Python是一种编程语言。\n\n问: 什么是Java？\n答: Java是另一种编程语言。"

        result = chunker.chunk(text)

        assert len(result.parent_chunks) == 2
        assert len(result.child_chunks) == 4
        assert result.parent_child_map == {0: [0, 1], 1: [2, 3]}

    def test_fullwidth_colon(self):
        """支持全角冒号（：）"""
        chunker = QAChunker()
        text = "问：什么是机器学习？\n答：机器学习是AI的分支。\n\n问：什么是深度学习？\n答：深度学习使用神经网络。"

        result = chunker.chunk(text)

        assert len(result.parent_chunks) == 2
        assert len(result.child_chunks) == 4

    def test_parent_contains_combined_qa(self):
        """父块包含 Q+A 组合文本"""
        chunker = QAChunker()
        text = "Q: What is AI?\nA: Artificial Intelligence."

        result = chunker.chunk(text)

        assert len(result.parent_chunks) == 1
        assert "Q: What is AI?" in result.parent_chunks[0]
        assert "A: Artificial Intelligence." in result.parent_chunks[0]

    def test_children_are_separate_q_and_a(self):
        """子块分别是 Q 和 A 的内容"""
        chunker = QAChunker()
        text = "Q: What is AI?\nA: Artificial Intelligence."

        result = chunker.chunk(text)

        assert result.child_chunks[0] == "What is AI?"
        assert result.child_chunks[1] == "Artificial Intelligence."


class TestQAChunkerEdgeCases:
    """边界情况测试"""

    def test_empty_text(self):
        """空文本返回空结果"""
        chunker = QAChunker()
        result = chunker.chunk("")

        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_whitespace_only(self):
        """纯空白文本返回空结果"""
        chunker = QAChunker()
        result = chunker.chunk("   \n\n  ")

        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_no_qa_pairs(self):
        """无 QA 配对时整段作为单个块"""
        chunker = QAChunker()
        text = "This is just regular text without any QA pairs."

        result = chunker.chunk(text)

        assert len(result.parent_chunks) == 1
        assert len(result.child_chunks) == 1
        assert result.parent_child_map == {0: [0]}


class TestQAChunkerStructure:
    """ChunkResult 结构验证"""

    def test_parent_child_map_valid_indices(self):
        """parent_child_map 中所有索引有效"""
        chunker = QAChunker()
        text = "Q: Q1?\nA: A1.\n\nQ: Q2?\nA: A2.\n\nQ: Q3?\nA: A3."

        result = chunker.chunk(text)

        for parent_idx, child_indices in result.parent_child_map.items():
            assert 0 <= parent_idx < len(result.parent_chunks)
            for ci in child_indices:
                assert 0 <= ci < len(result.child_chunks)

    def test_each_parent_has_two_children(self):
        """每个父块对应 2 个子块（Q 和 A）"""
        chunker = QAChunker()
        text = "Q: Q1?\nA: A1.\n\nQ: Q2?\nA: A2."

        result = chunker.chunk(text)

        for child_indices in result.parent_child_map.values():
            assert len(child_indices) == 2

    def test_all_children_mapped_exactly_once(self):
        """每个子块恰好被映射一次"""
        chunker = QAChunker()
        text = "Q: Q1?\nA: A1.\n\nQ: Q2?\nA: A2.\n\nQ: Q3?\nA: A3."

        result = chunker.chunk(text)

        all_child_indices = []
        for indices in result.parent_child_map.values():
            all_child_indices.extend(indices)

        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))


class TestQAChunkerRegistration:
    """工厂注册测试"""

    def test_registered_in_factory(self):
        """QAChunker 已注册到 ChunkerFactory"""
        assert "qa" in ChunkerFactory.REGISTRY
        assert ChunkerFactory.REGISTRY["qa"] is QAChunker

    def test_factory_create(self):
        """通过工厂创建 QAChunker 实例"""
        chunker = ChunkerFactory.create("qa")
        assert isinstance(chunker, QAChunker)

    def test_metadata_parameter_accepted(self):
        """metadata 参数可传入"""
        chunker = QAChunker()
        result = chunker.chunk("Q: test?\nA: answer.", metadata={"source": "faq.txt"})
        assert isinstance(result, ChunkResult)
