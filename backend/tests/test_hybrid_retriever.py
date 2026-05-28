"""HybridRetriever 单元测试"""

import sys
from unittest.mock import MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.retrieval.base import BaseRetriever, RetrievalResult  # noqa: E402
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
