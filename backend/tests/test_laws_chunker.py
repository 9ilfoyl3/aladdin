"""LawsChunker 单元测试"""

import pytest

from app.pipeline.chunkers.laws import LawsChunker
from app.pipeline.chunker_router import ChunkerFactory


class TestLawsChunkerRegistration:
    """注册测试"""

    def test_registered_in_factory(self):
        """laws 类型已注册到 ChunkerFactory"""
        assert "laws" in ChunkerFactory.REGISTRY
        assert ChunkerFactory.REGISTRY["laws"] is LawsChunker

    def test_factory_create(self):
        """通过工厂创建 LawsChunker 实例"""
        chunker = ChunkerFactory.create("laws")
        assert isinstance(chunker, LawsChunker)


class TestLawsChunkerEdgeCases:
    """边界情况测试"""

    def setup_method(self):
        self.chunker = LawsChunker()

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

    def test_no_legal_markers(self):
        """无法律标记时整段作为一个父块"""
        text = "这是一段普通文本。"
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 1
        assert result.parent_chunks[0] == text
        assert result.parent_child_map == {0: [0]}


class TestLawsChunkerArticleSplit:
    """条款切分测试"""

    def setup_method(self):
        self.chunker = LawsChunker()

    def test_split_by_article_number(self):
        """按第X条切分"""
        text = "第一条 保护合法权益。\n\n第二条 调整民事关系。\n\n第三条 不得侵犯。"
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 3
        assert result.parent_chunks[0].startswith("第一条")
        assert result.parent_chunks[1].startswith("第二条")
        assert result.parent_chunks[2].startswith("第三条")

    def test_split_by_judgment_structure(self):
        """按判决结构切分"""
        text = "案件基本情况\n\n本院认为，被告违约。\n\n判决如下\n一、赔偿损失。"
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 3
        assert result.parent_chunks[0] == "案件基本情况"
        assert result.parent_chunks[1].startswith("本院认为")
        assert result.parent_chunks[2].startswith("判决如下")

    def test_preamble_before_first_marker(self):
        """第一个标记之前的内容作为独立父块"""
        text = "中华人民共和国民法典\n\n第一条 保护合法权益。"
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2
        assert result.parent_chunks[0] == "中华人民共和国民法典"
        assert result.parent_chunks[1].startswith("第一条")

    def test_numeric_article_number(self):
        """支持阿拉伯数字条款号"""
        text = "第1条 内容一。\n\n第2条 内容二。"
        result = self.chunker.chunk(text)
        assert len(result.parent_chunks) == 2


class TestLawsChunkerChildSplit:
    """子块切分测试"""

    def setup_method(self):
        self.chunker = LawsChunker()

    def test_paragraphs_as_children(self):
        """父块内多段落切分为子块"""
        text = "本院认为，被告违约。\n\n根据合同法规定，应承担责任。\n\n综上所述，判决如下。"
        result = self.chunker.chunk(text)
        # 只有一个父块（本院认为），内含3个段落作为子块
        assert len(result.parent_chunks) == 1
        assert len(result.child_chunks) == 3

    def test_single_line_parent_has_one_child(self):
        """单行父块只有一个子块"""
        text = "第一条 简短内容。\n\n第二条 也很简短。"
        result = self.chunker.chunk(text)
        assert result.parent_child_map[0] == [0]
        assert result.parent_child_map[1] == [1]


class TestLawsChunkerStructureValidity:
    """结构有效性测试"""

    def setup_method(self):
        self.chunker = LawsChunker()

    def test_parent_child_map_indices_valid(self):
        """parent_child_map 索引有效"""
        text = """标题

第一条 内容一。
详细说明。

第二条 内容二。

本院认为，被告违约。
根据法律规定处理。

判决如下
一、赔偿。
二、驳回。"""
        result = self.chunker.chunk(text)

        for k, v in result.parent_child_map.items():
            assert 0 <= k < len(result.parent_chunks)
            for ci in v:
                assert 0 <= ci < len(result.child_chunks)

    def test_all_children_mapped_exactly_once(self):
        """每个子块恰好被映射一次"""
        text = "标题\n\n第一条 内容。\n详细。\n\n第二条 另一条。\n说明。"
        result = self.chunker.chunk(text)

        all_child_indices = []
        for v in result.parent_child_map.values():
            all_child_indices.extend(v)

        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))
