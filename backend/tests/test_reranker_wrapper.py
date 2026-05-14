"""RerankerWrapper 单元测试"""

import pytest

from app.models.provider import RerankProvider
from app.retrieval.base import RetrievalResult
from app.retrieval.reranker import RerankerWrapper


class FakeRerankProvider(RerankProvider):
    """测试用 mock provider，按文档长度倒序排列"""

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        # 按文档长度降序打分
        scored = [(i, float(len(doc))) for i, doc in enumerate(documents)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


@pytest.fixture
def wrapper():
    return RerankerWrapper(FakeRerankProvider())


@pytest.fixture
def sample_results():
    return [
        RetrievalResult(chunk_id="c1", content="短", score=0.5, doc_id="d1", metadata={"page": 1}),
        RetrievalResult(chunk_id="c2", content="中等长度文本", score=0.8, doc_id="d2", metadata={}),
        RetrievalResult(chunk_id="c3", content="这是一段比较长的文本内容用于测试", score=0.3, doc_id="d3", metadata={"page": 3}),
    ]


@pytest.mark.asyncio
async def test_rerank_results_empty(wrapper):
    """空列表直接返回空"""
    result = await wrapper.rerank_results("query", [])
    assert result == []


@pytest.mark.asyncio
async def test_rerank_results_order(wrapper, sample_results):
    """验证重排序后按分数降序排列"""
    result = await wrapper.rerank_results("query", sample_results)
    # FakeProvider 按长度降序，c3 最长排第一
    assert result[0].chunk_id == "c3"
    assert result[1].chunk_id == "c2"
    assert result[2].chunk_id == "c1"


@pytest.mark.asyncio
async def test_rerank_results_scores_updated(wrapper, sample_results):
    """验证分数被更新为 provider 返回的新分数"""
    result = await wrapper.rerank_results("query", sample_results)
    # 分数应为文档长度的 float 值，而非原始 score
    for r in result:
        assert r.score == float(len(
            next(s.content for s in sample_results if s.chunk_id == r.chunk_id)
        ))


@pytest.mark.asyncio
async def test_rerank_results_metadata_preserved(wrapper, sample_results):
    """验证 metadata 被正确保留"""
    result = await wrapper.rerank_results("query", sample_results)
    c1_result = next(r for r in result if r.chunk_id == "c1")
    assert c1_result.metadata == {"page": 1}


@pytest.mark.asyncio
async def test_rerank_results_top_k(wrapper, sample_results):
    """验证 top_k 限制返回数量"""
    result = await wrapper.rerank_results("query", sample_results, top_k=2)
    assert len(result) == 2
