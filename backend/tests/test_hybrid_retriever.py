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
