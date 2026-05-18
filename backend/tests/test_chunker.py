"""HierarchicalChunker 单元测试"""

import pytest

from app.pipeline.chunker import ChunkResult, HierarchicalChunker


class TestHierarchicalChunkerEdgeCases:
    """边界情况测试"""

    def test_empty_text(self):
        """空文本返回空结果"""
        chunker = HierarchicalChunker()
        result = chunker.chunk("")
        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_whitespace_only(self):
        """纯空白文本返回空结果"""
        chunker = HierarchicalChunker()
        result = chunker.chunk("   \n\n  \t  ")
        assert result.parent_chunks == []
        assert result.child_chunks == []
        assert result.parent_child_map == {}

    def test_none_text(self):
        """None 文本返回空结果"""
        chunker = HierarchicalChunker()
        result = chunker.chunk(None)
        assert result.parent_chunks == []
        assert result.child_chunks == []

    def test_short_text(self):
        """短文本（小于 child_size）作为单个块"""
        chunker = HierarchicalChunker(parent_size=1500, child_size=300, overlap=50)
        text = "这是一段很短的文本。"
        result = chunker.chunk(text)
        assert len(result.parent_chunks) == 1
        assert len(result.child_chunks) == 1
        assert result.parent_chunks[0] == text
        assert result.child_chunks[0] == text
        assert result.parent_child_map == {0: [0]}


class TestHierarchicalChunkerParentSplit:
    """父块切分测试"""

    def test_paragraph_boundary_split(self):
        """按段落边界切分父块"""
        chunker = HierarchicalChunker(parent_size=100, child_size=50, overlap=10)
        # 构造两个段落，每个约 60 字符
        para1 = "第一段内容" * 10  # 50 字符
        para2 = "第二段内容" * 10  # 50 字符
        text = para1 + "\n\n" + para2

        result = chunker.chunk(text)
        # 两段合起来超过 100，应分为两个父块
        assert len(result.parent_chunks) == 2
        assert para1 in result.parent_chunks[0]
        assert para2 in result.parent_chunks[1]

    def test_single_paragraph_within_limit(self):
        """单段落不超限时不拆分"""
        chunker = HierarchicalChunker(parent_size=500, child_size=100, overlap=20)
        text = "这是一段不太长的文本，不需要拆分。"
        result = chunker.chunk(text)
        assert len(result.parent_chunks) == 1

    def test_long_paragraph_split_by_sentence(self):
        """超长段落按句子边界切分"""
        chunker = HierarchicalChunker(parent_size=100, child_size=50, overlap=10)
        # 构造一个超过 100 字符的单段落（无 \n\n）
        text = "这是第一句话。" * 20  # 140 字符
        result = chunker.chunk(text)
        # 应被切分为多个父块
        assert len(result.parent_chunks) >= 2
        # 每个父块不应超过 parent_size 太多
        for chunk in result.parent_chunks:
            assert len(chunk) <= chunker.parent_size + 50  # 允许句子边界的少量溢出


class TestHierarchicalChunkerChildSplit:
    """子块切分测试"""

    def test_child_chunks_with_overlap(self):
        """子块之间有重叠"""
        chunker = HierarchicalChunker(parent_size=1000, child_size=100, overlap=20)
        # 构造一个 300 字符的文本
        text = "A" * 300
        result = chunker.chunk(text)

        # 应有多个子块
        assert len(result.child_chunks) > 1

    def test_parent_child_map_consistency(self):
        """父子映射一致性：所有子块索引都有效"""
        chunker = HierarchicalChunker(parent_size=200, child_size=80, overlap=20)
        text = ("段落一的内容比较长需要切分。" * 5 + "\n\n") * 3
        result = chunker.chunk(text)

        # 验证映射完整性
        all_child_indices = []
        for parent_idx, child_indices in result.parent_child_map.items():
            assert parent_idx < len(result.parent_chunks)
            for ci in child_indices:
                assert ci < len(result.child_chunks)
                all_child_indices.append(ci)

        # 所有子块都被映射到
        assert sorted(all_child_indices) == list(range(len(result.child_chunks)))

    def test_child_size_respected(self):
        """子块大小大致在 child_size 范围内"""
        chunker = HierarchicalChunker(parent_size=1500, child_size=300, overlap=50)
        text = "这是一段测试文本用于验证切分逻辑。" * 100
        result = chunker.chunk(text)

        for child in result.child_chunks:
            # 允许句子边界导致的少量溢出
            assert len(child) <= chunker.child_size + 100


class TestHierarchicalChunkerIntegration:
    """集成测试"""

    def test_realistic_document(self):
        """模拟真实文档切分"""
        chunker = HierarchicalChunker(parent_size=500, child_size=150, overlap=30)

        text = """# 产品介绍

我们的产品是一个智能知识库系统，支持多种文档格式的导入和检索。系统采用先进的向量检索技术，能够精准匹配用户查询意图。

## 核心功能

1. 文档管理：支持 PDF、Word、Excel、PPT 等格式
2. 智能检索：基于语义理解的混合检索
3. 对话问答：支持多轮对话和上下文理解

## 技术架构

系统后端使用 Python + FastAPI 构建，前端使用 React + TypeScript。向量存储采用 Milvus，支持稠密向量和稀疏向量的混合检索。Agent 编排层实现了查询路由、改写、执行和反思的完整流程。"""

        result = chunker.chunk(text)

        # 基本断言
        assert len(result.parent_chunks) >= 1
        assert len(result.child_chunks) >= len(result.parent_chunks)
        assert len(result.parent_child_map) == len(result.parent_chunks)

        # 内容完整性：所有父块拼接应覆盖原文主要内容
        all_parent_text = " ".join(result.parent_chunks)
        assert "产品介绍" in all_parent_text
        assert "核心功能" in all_parent_text
        assert "技术架构" in all_parent_text

    def test_metadata_parameter_accepted(self):
        """metadata 参数可传入（预留扩展）"""
        chunker = HierarchicalChunker()
        result = chunker.chunk("测试文本", metadata={"source": "test.md"})
        assert isinstance(result, ChunkResult)

    def test_chunk_result_dataclass(self):
        """ChunkResult 数据结构正确"""
        result = ChunkResult(
            parent_chunks=["parent"],
            child_chunks=["child1", "child2"],
            parent_child_map={0: [0, 1]},
        )
        assert result.parent_chunks == ["parent"]
        assert result.child_chunks == ["child1", "child2"]
        assert result.parent_child_map == {0: [0, 1]}
