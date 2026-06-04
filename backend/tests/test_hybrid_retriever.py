"""HybridRetriever 单元测试"""

import sys
from unittest.mock import MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import asyncio  # noqa: E402
import copy  # noqa: E402

import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.retrieval.base import BaseRetriever, RetrievalResult  # noqa: E402
from app.retrieval.config import RetrievalConfig  # noqa: E402
from app.retrieval.hybrid import HybridRetriever  # noqa: E402


class FakeRetriever(BaseRetriever):
    """模拟检索器，返回预设结果"""

    def __init__(self, results: list[RetrievalResult]):
        self._results = results

    async def search(self, query: str, kb_id: str, top_k: int = 10, **kwargs) -> list[RetrievalResult]:
        return self._results[:top_k]


class FakeRerankProvider:
    """模拟 Reranker，按原始顺序返回并赋予递减分数"""

    async def rerank(self, query: str, documents: list[str], top_k: int = 10) -> list[tuple[int, float]]:
        # 返回前 top_k 个，分数递减
        count = min(top_k, len(documents))
        return [(i, 1.0 - i * 0.1) for i in range(count)]


class FakeRerankProviderReverse:
    """模拟 Reranker，反转顺序"""

    async def rerank(self, query: str, documents: list[str], top_k: int = 10) -> list[tuple[int, float]]:
        count = min(top_k, len(documents))
        # 反转顺序：最后一个得分最高
        return [(count - 1 - i, 1.0 - i * 0.1) for i in range(count)]


class FakeAsyncSession:
    """模拟异步数据库会话"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeRow:
    """模拟数据库行"""

    def __init__(self, id: str, content: str):
        self.id = id
        self.content = content


class FakeSessionFactory:
    """模拟 async_sessionmaker"""

    def __init__(self, rows=None):
        self._rows = rows or []

    def __call__(self):
        return FakeAsyncSession(self._rows)


@pytest.mark.asyncio
async def test_hybrid_search_basic():
    """测试基本混合检索流程：融合 + rerank + 返回结果"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="内容2", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="内容2", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
        RetrievalResult(chunk_id="c3", content="内容3", score=0.7, doc_id="d2", metadata={"parent_id": "", "chunk_index": 0}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=3)

    # 应返回结果
    assert len(results) > 0
    assert all(isinstance(r, RetrievalResult) for r in results)


@pytest.mark.asyncio
async def test_rrf_fusion_overlapping_results():
    """测试 RRF 融合：重叠结果获得更高分数"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="内容2", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="内容2", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
        RetrievalResult(chunk_id="c3", content="内容3", score=0.7, doc_id="d2", metadata={"parent_id": "", "chunk_index": 0}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    # 直接测试 RRF 融合
    fused = hybrid._rrf_fusion([dense_results, sparse_results])

    # c2 在两路结果中都出现，RRF 分数应最高
    assert fused[0].chunk_id == "c2"
    assert len(fused) == 3  # c1, c2, c3 去重后共 3 个


@pytest.mark.asyncio
async def test_parent_expansion():
    """测试父块扩展：子块内容被替换为父块内容"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="子块内容", score=0.9, doc_id="d1", metadata={"parent_id": "p1", "chunk_index": 0}),
    ]
    sparse_results = []

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()

    # 模拟数据库返回父块内容
    parent_rows = [FakeRow(id="p1", content="父块完整内容")]
    db_factory = FakeSessionFactory(rows=parent_rows)

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("查询", kb_id="kb_001", top_k=5)

    # 内容应被替换为父块内容
    assert results[0].content == "父块完整内容"


@pytest.mark.asyncio
async def test_no_parent_expansion_when_no_parent_id():
    """测试无 parent_id 时保留原始内容"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="原始内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
    ]
    sparse_results = []

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("查询", kb_id="kb_001", top_k=5)

    # 无 parent_id，内容不变
    assert results[0].content == "原始内容"


@pytest.mark.asyncio
async def test_empty_results():
    """测试两路检索均无结果时返回空列表"""
    vector_retriever = FakeRetriever([])
    sparse_retriever = FakeRetriever([])
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("无结果查询", kb_id="kb_001", top_k=5)

    assert results == []


@pytest.mark.asyncio
async def test_rerank_reorders_results():
    """测试 Rerank 能重新排序结果"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容A", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="内容B", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    sparse_results = []

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    # 使用反转 reranker
    reranker = FakeRerankProviderReverse()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("查询", kb_id="kb_001", top_k=2)

    # 反转 reranker 会把最后一个排到第一位
    assert results[0].chunk_id == "c2"
    assert results[1].chunk_id == "c1"


@pytest.mark.asyncio
async def test_rrf_fusion_table_type_downweight():
    """测试 RRF 融合对 table 类型施加 0.8 降权"""
    # c1 是 text 类型，c2 是 table 类型，两者在同一位置（rank=0）
    dense_results = [
        RetrievalResult(chunk_id="c1", content="文本内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0, "element_type": "text"}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="表格内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1, "element_type": "table"}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    # 直接测试 RRF 融合
    fused = hybrid._rrf_fusion([dense_results, sparse_results])

    # c1 (text) 和 c2 (table) 都在 rank=0，基础 RRF 分数相同
    # 但 c2 是 table 类型，被施加 0.8 降权，所以 c1 排在前面
    assert fused[0].chunk_id == "c1"
    assert fused[1].chunk_id == "c2"


@pytest.mark.asyncio
async def test_rrf_fusion_custom_type_weights():
    """测试 RRF 融合支持自定义 type_weights"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="文本内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0, "element_type": "text"}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="标题内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1, "element_type": "title"}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    # 使用自定义权重：title 降权 0.5
    fused = hybrid._rrf_fusion([dense_results, sparse_results], type_weights={"title": 0.5})

    # c2 是 title 类型，被施加 0.5 降权，所以 c1 排在前面
    assert fused[0].chunk_id == "c1"
    assert fused[1].chunk_id == "c2"


@pytest.mark.asyncio
async def test_rrf_fusion_no_element_type_defaults_to_text():
    """测试 RRF 融合：metadata 中无 element_type 时默认为 text（权重 1.0）"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="内容2", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1, "element_type": "table"}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    # c1 无 element_type（默认 text，权重 1.0），c2 是 table（权重 0.8）
    fused = hybrid._rrf_fusion([dense_results, sparse_results])

    # c1 分数不变，c2 被降权，c1 排在前面
    assert fused[0].chunk_id == "c1"
    assert fused[1].chunk_id == "c2"


# ===== Composite Scoring 测试 =====


class TestCompositeScore:
    """_composite_score() 单元测试"""

    def setup_method(self):
        """创建 HybridRetriever 实例用于测试静态方法"""
        pass

    def test_basic_formula(self):
        """测试基本公式: composite = 0.6*rerank + 0.3*base + 0.1*source_weight"""
        # 0.6*0.8 + 0.3*0.5 + 0.1*1.0 = 0.48 + 0.15 + 0.1 = 0.73
        result = HybridRetriever._composite_score(0.8, 0.5, 1.0)
        assert abs(result - 0.73) < 1e-9

    def test_all_ones(self):
        """所有输入为 1.0 时结果为 1.0"""
        result = HybridRetriever._composite_score(1.0, 1.0, 1.0)
        assert abs(result - 1.0) < 1e-9

    def test_all_zeros(self):
        """所有输入为 0.0 时结果为 0.0"""
        result = HybridRetriever._composite_score(0.0, 0.0, 0.0)
        assert result == 0.0

    def test_clamp_upper_bound(self):
        """结果超过 1.0 时 clamp 到 1.0"""
        # 0.6*1.5 + 0.3*1.5 + 0.1*1.5 = 0.9 + 0.45 + 0.15 = 1.5 → clamp to 1.0
        result = HybridRetriever._composite_score(1.5, 1.5, 1.5)
        assert result == 1.0

    def test_clamp_lower_bound(self):
        """结果低于 0.0 时 clamp 到 0.0"""
        # 0.6*(-1) + 0.3*(-1) + 0.1*(-1) = -0.6 - 0.3 - 0.1 = -1.0 → clamp to 0.0
        result = HybridRetriever._composite_score(-1.0, -1.0, -1.0)
        assert result == 0.0

    def test_default_source_weight(self):
        """source_weight 默认值为 1.0"""
        # 0.6*0.5 + 0.3*0.5 + 0.1*1.0 = 0.3 + 0.15 + 0.1 = 0.55
        result = HybridRetriever._composite_score(0.5, 0.5)
        assert abs(result - 0.55) < 1e-9

    def test_rerank_dominates(self):
        """rerank 分数权重最大（0.6），对结果影响最大"""
        high_rerank = HybridRetriever._composite_score(1.0, 0.0, 0.0)
        high_base = HybridRetriever._composite_score(0.0, 1.0, 0.0)
        high_source = HybridRetriever._composite_score(0.0, 0.0, 1.0)
        assert high_rerank > high_base > high_source


class TestApplyCompositeScoring:
    """_apply_composite_scoring() 集成测试"""

    def _make_hybrid(self):
        """创建一个最小化的 HybridRetriever 实例"""
        vector_retriever = FakeRetriever([])
        sparse_retriever = FakeRetriever([])
        reranker = FakeRerankProvider()
        db_factory = FakeSessionFactory()
        return HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    def test_empty_results(self):
        """空列表直接返回"""
        hybrid = self._make_hybrid()
        result = hybrid._apply_composite_scoring([])
        assert result == []

    def test_single_result(self):
        """单个结果：source_weight = 1.0"""
        hybrid = self._make_hybrid()
        results = [
            RetrievalResult(
                chunk_id="c1", content="内容", score=0.9,
                doc_id="d1", metadata={"_rrf_score": 0.02}
            ),
        ]
        scored = hybrid._apply_composite_scoring(results)
        assert len(scored) == 1
        # 0.6*0.9 + 0.3*0.02 + 0.1*1.0 = 0.54 + 0.006 + 0.1 = 0.646
        assert abs(scored[0].score - 0.646) < 1e-9

    def test_reorders_by_composite(self):
        """composite scoring 后按新分数重新排序"""
        hybrid = self._make_hybrid()
        # c1: rerank=0.5, rrf=0.05 → 低 composite
        # c2: rerank=0.4, rrf=0.5 → 高 composite（base_score 贡献大）
        results = [
            RetrievalResult(
                chunk_id="c1", content="内容1", score=0.5,
                doc_id="d1", metadata={"_rrf_score": 0.01}
            ),
            RetrievalResult(
                chunk_id="c2", content="内容2", score=0.4,
                doc_id="d1", metadata={"_rrf_score": 0.5}
            ),
        ]
        scored = hybrid._apply_composite_scoring(results)
        # c1: 0.6*0.5 + 0.3*0.01 + 0.1*1.0 = 0.3 + 0.003 + 0.1 = 0.403
        # c2: 0.6*0.4 + 0.3*0.5 + 0.1*0.5 = 0.24 + 0.15 + 0.05 = 0.44
        assert scored[0].chunk_id == "c2"
        assert scored[1].chunk_id == "c1"

    def test_missing_rrf_score_defaults_to_zero(self):
        """metadata 中无 _rrf_score 时默认为 0.0"""
        hybrid = self._make_hybrid()
        results = [
            RetrievalResult(
                chunk_id="c1", content="内容", score=0.8,
                doc_id="d1", metadata={}
            ),
        ]
        scored = hybrid._apply_composite_scoring(results)
        # 0.6*0.8 + 0.3*0.0 + 0.1*1.0 = 0.48 + 0 + 0.1 = 0.58
        assert abs(scored[0].score - 0.58) < 1e-9

    def test_scores_clamped(self):
        """composite scoring 结果 clamp 到 [0.0, 1.0]"""
        hybrid = self._make_hybrid()
        results = [
            RetrievalResult(
                chunk_id="c1", content="内容", score=2.0,
                doc_id="d1", metadata={"_rrf_score": 2.0}
            ),
        ]
        scored = hybrid._apply_composite_scoring(results)
        assert scored[0].score <= 1.0
        assert scored[0].score >= 0.0


# ===== Jaccard Tokens 测试 =====


class TestJaccardTokens:
    """_jaccard_tokens() 单元测试"""

    def test_identical_texts(self):
        """完全相同的文本 Jaccard = 1.0"""
        result = HybridRetriever._jaccard_tokens("你好世界", "你好世界")
        assert result == 1.0

    def test_completely_different(self):
        """完全不同的文本 Jaccard = 0.0"""
        result = HybridRetriever._jaccard_tokens("abc", "xyz")
        assert result == 0.0

    def test_partial_overlap(self):
        """部分重叠的文本"""
        # set_a = {'a', 'b', 'c', 'd'}  set_b = {'c', 'd', 'e', 'f'}
        # intersection = {'c', 'd'} = 2, union = {'a','b','c','d','e','f'} = 6
        # Jaccard = 2/6 = 1/3
        result = HybridRetriever._jaccard_tokens("abcd", "cdef")
        assert abs(result - 2.0 / 6.0) < 1e-9

    def test_both_empty(self):
        """两个空字符串返回 0.0"""
        result = HybridRetriever._jaccard_tokens("", "")
        assert result == 0.0

    def test_one_empty(self):
        """一个空字符串返回 0.0"""
        result = HybridRetriever._jaccard_tokens("hello", "")
        assert result == 0.0

    def test_chinese_character_level(self):
        """中文文本使用字符级分词"""
        # "检索结果" vs "检索优化" → intersection={'检','索'}, union={'检','索','结','果','优','化'}
        result = HybridRetriever._jaccard_tokens("检索结果", "检索优化")
        assert abs(result - 2.0 / 6.0) < 1e-9

    def test_high_overlap(self):
        """高度重叠的文本 Jaccard > 0.7"""
        # 两段文本只有少量字符不同
        text_a = "这是一段很长的测试文本内容用于验证"
        text_b = "这是一段很长的测试文本内容用于检验"
        result = HybridRetriever._jaccard_tokens(text_a, text_b)
        assert result > 0.7


# ===== MMR 去冗余测试 =====


class TestApplyMmr:
    """_apply_mmr() 单元测试"""

    def test_empty_input(self):
        """空列表直接返回"""
        result = HybridRetriever._apply_mmr([])
        assert result == []

    def test_no_redundancy(self):
        """无冗余时保留所有结果"""
        results = [
            RetrievalResult(chunk_id="c1", content="完全不同的内容ABC", score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content="另一段完全不同XYZ", score=0.8, doc_id="d1", metadata={}),
        ]
        filtered = HybridRetriever._apply_mmr(results)
        assert len(filtered) == 2

    def test_removes_highly_overlapping_chunks(self):
        """去除高度重叠的 chunk"""
        # c1 和 c2 内容几乎相同（只差一个字符）
        base_content = "这是一段用于测试MMR去冗余功能的长文本内容，包含足够多的字符来确保高度重叠"
        results = [
            RetrievalResult(chunk_id="c1", content=base_content, score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content=base_content + "额", score=0.8, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c3", content="完全不同的内容，与前两段没有任何关系", score=0.7, doc_id="d2", metadata={}),
        ]
        filtered = HybridRetriever._apply_mmr(results)
        # c2 与 c1 高度重叠，应被去除
        assert len(filtered) == 2
        chunk_ids = [r.chunk_id for r in filtered]
        assert "c1" in chunk_ids
        assert "c3" in chunk_ids
        assert "c2" not in chunk_ids

    def test_preserves_order(self):
        """保持原始分数顺序"""
        results = [
            RetrievalResult(chunk_id="c1", content="内容A独特的文本", score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content="内容B不同的文本", score=0.8, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c3", content="内容C另外的文本", score=0.7, doc_id="d2", metadata={}),
        ]
        filtered = HybridRetriever._apply_mmr(results)
        # 无冗余，顺序不变
        assert filtered[0].chunk_id == "c1"
        assert filtered[1].chunk_id == "c2"
        assert filtered[2].chunk_id == "c3"

    def test_custom_threshold(self):
        """自定义 threshold 参数"""
        # 使用极低的 threshold，几乎所有有重叠的都会被过滤
        results = [
            RetrievalResult(chunk_id="c1", content="abcdef", score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content="abcxyz", score=0.8, doc_id="d1", metadata={}),
        ]
        # threshold=0.1 → 只要有少量重叠就过滤
        filtered = HybridRetriever._apply_mmr(results, threshold=0.1)
        # 'abc' 重叠，Jaccard > 0.1，c2 应被过滤
        assert len(filtered) == 1
        assert filtered[0].chunk_id == "c1"

    def test_single_result(self):
        """单个结果直接返回"""
        results = [
            RetrievalResult(chunk_id="c1", content="唯一内容", score=0.9, doc_id="d1", metadata={}),
        ]
        filtered = HybridRetriever._apply_mmr(results)
        assert len(filtered) == 1
        assert filtered[0].chunk_id == "c1"

    def test_all_identical_keeps_first(self):
        """所有内容完全相同时只保留第一个"""
        same_content = "完全相同的内容"
        results = [
            RetrievalResult(chunk_id="c1", content=same_content, score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content=same_content, score=0.8, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c3", content=same_content, score=0.7, doc_id="d1", metadata={}),
        ]
        filtered = HybridRetriever._apply_mmr(results)
        assert len(filtered) == 1
        assert filtered[0].chunk_id == "c1"


@pytest.mark.asyncio
async def test_mmr_integrated_in_search():
    """测试 MMR 集成到 search() 流程中：重复 chunk 被过滤"""
    base_content = "这是一段用于测试MMR去冗余功能的长文本内容包含足够多的字符来确保高度重叠"
    dense_results = [
        RetrievalResult(chunk_id="c1", content=base_content, score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content=base_content + "微", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
        RetrievalResult(chunk_id="c3", content="完全不同的独立内容，与前面没有任何关系", score=0.7, doc_id="d2", metadata={"parent_id": "", "chunk_index": 0}),
    ]
    sparse_results = []

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=10)

    # c2 与 c1 高度重叠，应被 MMR 过滤
    chunk_ids = [r.chunk_id for r in results]
    assert "c1" in chunk_ids
    assert "c3" in chunk_ids
    # c2 应该被过滤掉
    assert "c2" not in chunk_ids


@pytest.mark.asyncio
async def test_search_with_trace_route_attribution():
    """测试 search_with_trace：路由归属、漏斗阶段、每条结果的多维分数"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容一", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="内容二", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="内容二", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    bm25_results = [
        RetrievalResult(chunk_id="c3", content="内容三", score=0.7, doc_id="d2", metadata={"parent_id": "", "chunk_index": 0}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    bm25_retriever = FakeRetriever(bm25_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(
        vector_retriever, sparse_retriever, reranker, db_factory, bm25_retriever=bm25_retriever
    )

    results, trace = await hybrid.search_with_trace("测试查询", kb_id="kb_001", top_k=5)

    # 返回结果
    assert len(results) > 0

    # 三路召回统计
    routes = {r["name"]: r for r in trace["routes"]}
    assert routes["dense"]["recalled"] == 2
    assert routes["sparse"]["recalled"] == 1
    assert routes["bm25"]["recalled"] == 1
    assert routes["bm25"]["enabled"] is True

    # 漏斗阶段存在
    funnel_stages = [f["stage"] for f in trace["funnel"]]
    assert "RRF 融合" in funnel_stages

    # 路由归属：c2 命中 dense + sparse 两路
    per_result = trace["per_result"]
    assert set(per_result["c2"]["routes"]) == {"dense", "sparse"}
    assert per_result["c1"]["routes"] == ["dense"]
    assert per_result["c3"]["routes"] == ["bm25"]

    # RRF 分数已记录
    assert per_result["c2"]["rrf_score"] is not None
    # rerank 分数已记录（c2/c1/c3 都进入了 rerank 候选）
    assert per_result["c1"]["rerank_score"] is not None


@pytest.mark.asyncio
async def test_search_with_trace_bm25_disabled():
    """测试 search_with_trace：未配置 BM25 时标记 enabled=False"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容一", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
    ]
    sparse_results = []

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    results, trace = await hybrid.search_with_trace("测试查询", kb_id="kb_001", top_k=5)

    routes = {r["name"]: r for r in trace["routes"]}
    assert routes["bm25"]["enabled"] is False
    assert routes["bm25"]["recalled"] == 0


# ===== H2 三路容错测试（search 三路降级 / 全失败抛错 / 全成功不变式）=====


class RaisingRetriever(BaseRetriever):
    """模拟检索器，调用即抛指定异常（用于 H2 单路 / 多路失败场景）"""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("模拟检索失败")

    async def search(self, query: str, kb_id: str, top_k: int = 10, **kwargs) -> list[RetrievalResult]:
        raise self._exc


@pytest.mark.asyncio
async def test_h2_dense_route_failure_degrades_to_empty():
    """H2：dense 路抛异常 → 该路当空，sparse 路正常融合返回，标记 route_degraded"""
    sparse_results = [
        RetrievalResult(chunk_id="c1", content="合同违约责任的认定标准", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="知识产权侵权的赔偿计算", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]

    vector_retriever = RaisingRetriever(RuntimeError("dense 路超时"))
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=5)

    # dense 路降级为空，sparse 路结果照常融合返回，不整体失败
    chunk_ids = {r.chunk_id for r in results}
    assert chunk_ids == {"c1", "c2"}
    # 标记本次检索发生路级降级（供任务 3 透传）
    assert hybrid._last_route_degraded is True


@pytest.mark.asyncio
async def test_h2_sparse_route_failure_degrades_to_empty():
    """H2：sparse 路抛异常 → dense 路正常融合返回，标记 route_degraded"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="稠密内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = RaisingRetriever(ValueError("sparse 路偶发错误"))
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=5)

    assert {r.chunk_id for r in results} == {"c1"}
    assert hybrid._last_route_degraded is True


@pytest.mark.asyncio
async def test_h2_bm25_route_failure_degrades_to_empty():
    """H2：bm25 路（idx=2）抛异常 → dense/sparse 正常融合返回，标记 route_degraded"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="稠密内容", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="稀疏内容", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    bm25_retriever = RaisingRetriever(RuntimeError("bm25 路错误"))
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(
        vector_retriever, sparse_retriever, reranker, db_factory, bm25_retriever=bm25_retriever
    )
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=5)

    assert {r.chunk_id for r in results} == {"c1", "c2"}
    assert hybrid._last_route_degraded is True


@pytest.mark.asyncio
async def test_h2_all_routes_fail_raises_runtime_error():
    """H2：三路全部失败 → 抛 RuntimeError 交上层降级（不静默返回空）"""
    vector_retriever = RaisingRetriever(RuntimeError("dense 失败"))
    sparse_retriever = RaisingRetriever(RuntimeError("sparse 失败"))
    bm25_retriever = RaisingRetriever(RuntimeError("bm25 失败"))
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(
        vector_retriever, sparse_retriever, reranker, db_factory, bm25_retriever=bm25_retriever
    )

    with pytest.raises(RuntimeError):
        await hybrid.search("测试查询", kb_id="kb_001", top_k=5)


@pytest.mark.asyncio
async def test_h2_two_routes_fail_no_bm25_raises_runtime_error():
    """H2：仅两路（dense+sparse，无 bm25）全部失败 → 抛 RuntimeError"""
    vector_retriever = RaisingRetriever(RuntimeError("dense 失败"))
    sparse_retriever = RaisingRetriever(RuntimeError("sparse 失败"))
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)

    with pytest.raises(RuntimeError):
        await hybrid.search("测试查询", kb_id="kb_001", top_k=5)


@pytest.mark.asyncio
async def test_h2_empty_results_not_degraded_no_raise():
    """H2 不变式：三路均成功但都无结果 → route_degraded=False，返回空不抛错（区别于全失败）"""
    vector_retriever = FakeRetriever([])
    sparse_retriever = FakeRetriever([])
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("无结果查询", kb_id="kb_001", top_k=5)

    assert results == []
    # 正常无结果不算降级，不抛错
    assert hybrid._last_route_degraded is False


@pytest.mark.asyncio
async def test_h2_all_routes_success_no_degradation():
    """H2 不变式：三路全成功 → route_degraded=False，结果与容错前一致"""
    dense_results = [
        RetrievalResult(chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={"parent_id": "", "chunk_index": 0}),
        RetrievalResult(chunk_id="c2", content="内容2", score=0.8, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
    ]
    sparse_results = [
        RetrievalResult(chunk_id="c2", content="内容2", score=0.85, doc_id="d1", metadata={"parent_id": "", "chunk_index": 1}),
        RetrievalResult(chunk_id="c3", content="内容3", score=0.7, doc_id="d2", metadata={"parent_id": "", "chunk_index": 0}),
    ]

    vector_retriever = FakeRetriever(dense_results)
    sparse_retriever = FakeRetriever(sparse_results)
    reranker = FakeRerankProvider()
    db_factory = FakeSessionFactory()

    hybrid = HybridRetriever(vector_retriever, sparse_retriever, reranker, db_factory)
    results = await hybrid.search("测试查询", kb_id="kb_001", top_k=3)

    assert len(results) > 0
    assert hybrid._last_route_degraded is False


# ===== H2 属性测试（design.md Property 1：检索容错的结果组合）=====
#
# Property 1（Bug 2 / H2）：三路检索成功/失败任意组合下——
#   ① 融合输入恰为「所有成功路结果」的并集（失败路贡献空）；
#   ② 当且仅当所有参与路全失败时抛 RuntimeError；
#   ③ 三路全成功时，容错版 search() 输出与「无容错直接 RRF 融合」逐条相同（不变式）。
#
# 复用任务 2 已建的 FakeRetriever / RaisingRetriever / FakeRerankProvider / FakeSessionFactory。
# 为在大量 hypothesis 迭代下保持快速、确定且不触达 DB，向 HybridRetriever 注入轻量
# fake 配置/平台 store（构造函数已支持注入），search() 的配置/TTL 读取因此恒定且无 IO。


class _FakeConfigStore:
    """返回全默认 RetrievalConfig 的检索配置 store（不打 DB）。"""

    async def get_effective(self, tenant_id):  # noqa: ARG002 - 属性测试固定全默认
        return RetrievalConfig()


class _FakePlatformStore:
    """返回固定 Load_Cache_TTL 的平台配置 store（不打 DB）。"""

    async def get_load_cache_ttl(self):
        return 30


def _h2_make_result(chunk_id: str) -> RetrievalResult:
    """构造一条 H2 属性测试用检索结果。

    parent_id 置空避免触发父块 DB 查询；content 由 chunk_id 决定，保证同一 chunk_id
    跨多路时内容一致（RRF 融合按 chunk_id 去重，要求同 id 内容一致）。
    """
    return RetrievalResult(
        chunk_id=chunk_id,
        content=f"内容-{chunk_id}",
        score=0.5,
        doc_id="d1",
        metadata={"parent_id": "", "chunk_index": 0},
    )


# chunk_id 公共池：允许多路抽到重叠 id（RRF 去重），覆盖「交集/并集」融合语义。
_H2_CHUNK_POOL = [f"c{i}" for i in range(6)]


def _h2_build_hybrid(has_bm25: bool, routes: list) -> HybridRetriever:
    """按 routes 组装注入 Fake/Raising 子检索器 + fake store 的 HybridRetriever。

    routes 按 dense / sparse [/ bm25] 顺序给出，每项为
    ("success", list[RetrievalResult]) 或 ("fail", None)。
    """

    def _sub(route):
        kind, payload = route
        if kind == "success":
            return FakeRetriever(payload)
        return RaisingRetriever(RuntimeError("模拟该路检索失败"))

    vector_retriever = _sub(routes[0])
    sparse_retriever = _sub(routes[1])
    bm25_retriever = _sub(routes[2]) if has_bm25 else None
    return HybridRetriever(
        vector_retriever,
        sparse_retriever,
        FakeRerankProvider(),
        FakeSessionFactory(),
        bm25_retriever=bm25_retriever,
        config_store=_FakeConfigStore(),
        platform_store=_FakePlatformStore(),
    )


@st.composite
def _h2_route_scenarios(draw):
    """生成三路（dense/sparse/bm25）成功/失败任意组合。

    - has_bm25：bm25 路是否参与（对应是否注入 bm25_retriever）。
    - 每个参与路独立成功/失败。
    - 成功路返回**非空**结果（从公共池抽取，允许跨路重叠）；失败路抛异常。
      约束成功路非空使「iff 全失败抛错」边界清晰：成功但为空的退化场景已由
      example 用例 test_h2_empty_results_not_degraded_no_raise 覆盖。
    """
    has_bm25 = draw(st.booleans())
    n_routes = 3 if has_bm25 else 2

    routes = []
    for _ in range(n_routes):
        if draw(st.booleans()):
            ids = draw(
                st.lists(st.sampled_from(_H2_CHUNK_POOL), min_size=1, max_size=4, unique=True)
            )
            routes.append(("success", [_h2_make_result(cid) for cid in ids]))
        else:
            routes.append(("fail", None))
    return has_bm25, routes


@settings(max_examples=150, deadline=None)
@given(scenario=_h2_route_scenarios())
def test_property1_fusion_combination_and_all_fail_raises(scenario):
    """Property 1 ①②：融合输入 = 所有成功路结果的并集（失败路贡献空）；
    当且仅当所有参与路全失败时抛 RuntimeError。

    **Validates: Requirements Bug 2 (H2) — Property 1**
    """
    has_bm25, routes = scenario

    # 期望融合输入 = 成功路 chunk_id 并集（失败路贡献空集）
    expected_ids: set[str] = set()
    any_fail = False
    for kind, payload in routes:
        if kind == "success":
            expected_ids |= {r.chunk_id for r in payload}
        else:
            any_fail = True
    all_fail = all(kind == "fail" for kind, _ in routes)

    hybrid = _h2_build_hybrid(has_bm25, routes)

    async def _run():
        # skip_rerank 隔离「三路容错 + RRF 融合」段，直接观察融合输入集合
        return await hybrid.search("查询", kb_id="kb_001", top_k=5, skip_rerank=True)

    if all_fail:
        # ② 当且仅当全失败 → 抛 RuntimeError（不静默返回空）
        with pytest.raises(RuntimeError):
            asyncio.run(_run())
    else:
        results = asyncio.run(_run())
        # ① 融合输入恰为成功路结果的并集
        assert {r.chunk_id for r in results} == expected_ids
        # 存在失败路（未全失败）→ 标记降级；全成功 → 不降级（与 ③ 不变式一致）
        assert hybrid._last_route_degraded is any_fail


@st.composite
def _h2_all_success_scenarios(draw):
    """生成三路全成功场景（每路非空），用于全成功不变式校验。"""
    has_bm25 = draw(st.booleans())
    n_routes = 3 if has_bm25 else 2
    route_results = []
    for _ in range(n_routes):
        ids = draw(st.lists(st.sampled_from(_H2_CHUNK_POOL), min_size=1, max_size=4, unique=True))
        route_results.append([_h2_make_result(cid) for cid in ids])
    return has_bm25, route_results


@settings(max_examples=100, deadline=None)
@given(scenario=_h2_all_success_scenarios())
def test_property1_all_success_invariant(scenario):
    """Property 1 ③：三路全成功时，容错版 search() 输出与「无容错直接 RRF 融合」逐条相同。

    无容错版 = 直接对各路原始结果做 RRF 融合（容错包装 _safe 在全成功时应为无操作）。

    **Validates: Requirements Bug 2 (H2) — Property 1**
    """
    has_bm25, route_results = scenario

    routes = [("success", r) for r in route_results]
    hybrid = _h2_build_hybrid(has_bm25, routes)

    # 参考值（无容错版）：对各路原始结果深拷贝后直接 RRF 融合，避免 search 的 metadata 副作用。
    config = RetrievalConfig()
    ref_lists = [copy.deepcopy(r) for r in route_results]
    expected = hybrid._rrf_fusion(ref_lists, k=config.rrf_k)

    async def _run():
        return await hybrid.search("查询", kb_id="kb_001", top_k=5, skip_rerank=True)

    actual = asyncio.run(_run())

    # 逐条相同：chunk_id 顺序一致 + RRF 分数一致
    assert [r.chunk_id for r in actual] == [r.chunk_id for r in expected]
    for a, e in zip(actual, expected):
        assert a.chunk_id == e.chunk_id
        assert abs(a.metadata.get("_rrf_score", 0.0) - e.metadata.get("_rrf_score", 0.0)) < 1e-9
    # 不变式：三路全成功不降级
    assert hybrid._last_route_degraded is False
