"""Document Profiler 和策略链单元测试 [REQ-6]"""

import pytest

from app.pipeline.chunker import (
    DocProfile,
    HierarchicalChunker,
    _profile_document,
    _select_strategy,
    _validate_chunks,
)


class TestDocProfile:
    """DocProfile dataclass 测试"""

    def test_dataclass_fields(self):
        """DocProfile 包含所有必要字段"""
        profile = DocProfile(
            heading_count=3,
            structure_count=5,
            paragraph_count=10,
            total_chars=2000,
            avg_paragraph_len=200.0,
        )
        assert profile.heading_count == 3
        assert profile.structure_count == 5
        assert profile.paragraph_count == 10
        assert profile.total_chars == 2000
        assert profile.avg_paragraph_len == 200.0


class TestProfileDocument:
    """_profile_document() 函数测试"""

    def test_heading_count(self):
        """正确统计 Markdown 标题数"""
        text = "# 标题一\n内容\n## 标题二\n内容\n### 标题三\n内容"
        profile = _profile_document(text)
        assert profile.heading_count == 3

    def test_structure_count_chinese_numbering(self):
        """正确统计中文条款编号"""
        text = "一、第一条\n内容\n二、第二条\n内容\n三、第三条\n内容"
        profile = _profile_document(text)
        assert profile.structure_count >= 3

    def test_structure_count_article_format(self):
        """正确统计 '第X条' 格式"""
        text = "第一条 总则\n内容\n第二条 定义\n内容\n第三条 范围\n内容"
        profile = _profile_document(text)
        assert profile.structure_count >= 3

    def test_paragraph_count(self):
        """正确统计段落数（双换行分隔）"""
        text = "段落一\n\n段落二\n\n段落三"
        profile = _profile_document(text)
        assert profile.paragraph_count == 3

    def test_total_chars(self):
        """正确统计总字符数"""
        text = "Hello World"
        profile = _profile_document(text)
        assert profile.total_chars == 11

    def test_avg_paragraph_len(self):
        """正确计算平均段落长度"""
        text = "AAAA\n\nBBBB\n\nCCCC"
        profile = _profile_document(text)
        # total_chars / paragraph_count
        assert profile.avg_paragraph_len == len(text) / 3

    def test_no_paragraphs(self):
        """单段落文本的平均段落长度等于总字符数"""
        text = "一段没有双换行的文本"
        profile = _profile_document(text)
        assert profile.paragraph_count == 1
        assert profile.avg_paragraph_len == float(len(text))

    def test_mixed_document(self):
        """混合文档（标题 + 结构标记）"""
        text = """# 合同

## 第一章 总则

第一条 本合同由甲乙双方签订。

第二条 合同期限为一年。

## 第二章 权利义务

第三条 甲方应按时付款。"""
        profile = _profile_document(text)
        assert profile.heading_count >= 3  # #, ##, ##
        assert profile.structure_count >= 3  # 第一条, 第二条, 第三条


class TestSelectStrategy:
    """_select_strategy() 函数测试"""

    def test_heading_heavy_document(self):
        """heading_count >= 3 → ["heading", "heuristic", "legacy"]"""
        profile = DocProfile(
            heading_count=5, structure_count=1,
            paragraph_count=10, total_chars=5000, avg_paragraph_len=500.0,
        )
        assert _select_strategy(profile) == ["heading", "heuristic", "legacy"]

    def test_structure_heavy_document(self):
        """structure_count >= 2 → ["heuristic", "legacy"]"""
        profile = DocProfile(
            heading_count=1, structure_count=5,
            paragraph_count=10, total_chars=5000, avg_paragraph_len=500.0,
        )
        assert _select_strategy(profile) == ["heuristic", "legacy"]

    def test_plain_document(self):
        """无明显结构 → ["legacy"]"""
        profile = DocProfile(
            heading_count=0, structure_count=0,
            paragraph_count=10, total_chars=5000, avg_paragraph_len=500.0,
        )
        assert _select_strategy(profile) == ["legacy"]

    def test_heading_priority_over_structure(self):
        """heading 优先级高于 structure"""
        profile = DocProfile(
            heading_count=3, structure_count=5,
            paragraph_count=10, total_chars=5000, avg_paragraph_len=500.0,
        )
        result = _select_strategy(profile)
        assert result[0] == "heading"

    def test_boundary_heading_count_3(self):
        """heading_count 恰好为 3 时触发 heading 策略"""
        profile = DocProfile(
            heading_count=3, structure_count=0,
            paragraph_count=5, total_chars=3000, avg_paragraph_len=600.0,
        )
        assert _select_strategy(profile) == ["heading", "heuristic", "legacy"]

    def test_boundary_heading_count_2(self):
        """heading_count 为 2 时不触发 heading 策略"""
        profile = DocProfile(
            heading_count=2, structure_count=0,
            paragraph_count=5, total_chars=3000, avg_paragraph_len=600.0,
        )
        assert _select_strategy(profile) == ["legacy"]

    def test_boundary_structure_count_2(self):
        """structure_count 恰好为 2 时触发 heuristic 策略"""
        profile = DocProfile(
            heading_count=0, structure_count=2,
            paragraph_count=5, total_chars=3000, avg_paragraph_len=600.0,
        )
        assert _select_strategy(profile) == ["heuristic", "legacy"]

    def test_boundary_structure_count_1(self):
        """structure_count 为 1 时不触发 heuristic 策略"""
        profile = DocProfile(
            heading_count=0, structure_count=1,
            paragraph_count=5, total_chars=3000, avg_paragraph_len=600.0,
        )
        assert _select_strategy(profile) == ["legacy"]


class TestValidateChunks:
    """_validate_chunks() 函数测试"""

    def test_valid_chunks(self):
        """所有 chunk 在范围内时返回 True"""
        chunks = ["a" * 50, "b" * 100, "c" * 200]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is True

    def test_empty_chunks(self):
        """空列表返回 False"""
        assert _validate_chunks([], min_size=20, max_size=300) is False

    def test_chunk_too_short(self):
        """有 chunk 低于 min_size 时返回 False"""
        chunks = ["a" * 5, "b" * 100]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is False

    def test_chunk_too_long(self):
        """有 chunk 超过 max_size 时返回 False"""
        chunks = ["a" * 100, "b" * 500]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is False

    def test_single_valid_chunk(self):
        """单个有效 chunk 返回 True"""
        chunks = ["a" * 50]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is True

    def test_boundary_min_size(self):
        """chunk 长度恰好等于 min_size 时返回 True"""
        chunks = ["a" * 20]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is True

    def test_boundary_max_size(self):
        """chunk 长度恰好等于 max_size 时返回 True"""
        chunks = ["a" * 300]
        assert _validate_chunks(chunks, min_size=20, max_size=300) is True


class TestStrategyChainIntegration:
    """策略链集成测试：验证 chunk() 方法正确使用 profiler"""

    def test_heading_strategy_selected(self):
        """含多个标题的文档使用 heading 策略，子块按标题切分"""
        chunker = HierarchicalChunker(parent_size=500, child_size=150, overlap=30)
        text = """# 第一章 概述

这是第一章的内容，介绍了系统的基本概念和设计目标。

## 1.1 背景

项目背景描述，包含了需求分析和市场调研的结果。

## 1.2 目标

系统设计目标，包括性能指标和功能需求。

# 第二章 设计

这是第二章的内容，详细描述了系统架构设计。""" + "补充内容。" * 30

        result = chunker.chunk(text)
        # heading 策略应该成功切分，产出多个子块
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= 3  # 至少按标题切出多个子块
        # context_headers 应包含标题面包屑
        assert any("第一章" in h for h in result.context_headers)

    def test_heuristic_strategy_selected(self):
        """含结构标记的文档使用 heuristic 策略"""
        chunker = HierarchicalChunker(parent_size=500, child_size=150, overlap=30)
        text = """第一条 本合同由甲乙双方签订，自签订之日起生效。

第二条 合同期限为一年，自2024年1月1日起至2024年12月31日止。

第三条 甲方应按时支付合同约定的款项，逾期支付的应承担违约责任。""" + "\n\n补充条款内容。" * 30

        result = chunker.chunk(text)
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= 1

    def test_legacy_fallback(self):
        """无明显结构的文档使用 legacy 策略"""
        chunker = HierarchicalChunker(parent_size=500, child_size=150, overlap=30)
        text = "这是一段普通的文本内容，没有任何结构标记。" * 50

        result = chunker.chunk(text)
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= 1

    def test_strategy_degradation(self):
        """策略降级：heading 策略验证失败时降级到下一个策略"""
        # 构造一个有标题但标题间内容极短的文档（heading 策略可能产生过短的 chunk）
        chunker = HierarchicalChunker(parent_size=500, child_size=150, overlap=30, min_child_size=50)
        text = "# A\nx\n# B\ny\n# C\nz\n# D\n" + "正常内容。" * 100

        result = chunker.chunk(text)
        # 无论使用哪个策略，都应该产出有效结果
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= 1

    def test_all_strategies_produce_results(self):
        """所有策略最终都能产出结果（legacy 兜底）"""
        chunker = HierarchicalChunker(parent_size=200, child_size=80, overlap=20)
        text = "短文本但超过 child_size。" * 20

        result = chunker.chunk(text)
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= 1
        assert len(result.context_headers) == len(result.child_chunks)

    def test_parent_child_map_still_consistent(self):
        """策略链不影响父子映射一致性"""
        chunker = HierarchicalChunker(parent_size=300, child_size=100, overlap=20)
        text = """# 标题一

内容一比较长需要切分。""" + "填充。" * 30 + """

## 标题二

内容二也比较长。""" + "填充。" * 30 + """

### 标题三

内容三。""" + "填充。" * 30

        result = chunker.chunk(text)

        # 验证映射完整性
        all_child_indices = []
        for parent_idx, child_indices in result.parent_child_map.items():
            assert parent_idx < len(result.parent_chunks)
            for ci in child_indices:
                assert ci < len(result.child_chunks)
                all_child_indices.append(ci)

        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))
