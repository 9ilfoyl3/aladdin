"""RetrievalExecutor 单元测试"""

import pytest

from app.agent.executor import RetrievalExecutor
from app.retrieval.base import BaseRetriever, RetrievalResult


class FakeRetriever(BaseRetriever):
    """模拟检索器，根据查询返回预设结果"""

    def __init__(self, results_map: dict[str, list[RetrievalResult]]):
        self.results_map = results_map
        self.call_log: list[tuple[str, str, int]] = []

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, **kwargs
    ) -> list[RetrievalResult]:
        self.call_log.append((query, kb_id, top_k))
        return self.results_map.get(query, [])


@pytest.fixture
def sample_results():
    """构造测试用检索结果"""
    return {
        "query1": [
            RetrievalResult(chunk_id="c1", content="内容1", score=0.9, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c2", content="内容2", score=0.8, doc_id="d1", metadata={}),
        ],
        "query2": [
            RetrievalResult(chunk_id="c2", content="内容2", score=0.85, doc_id="d1", metadata={}),
            RetrievalResult(chunk_id="c3", content="内容3", score=0.7, doc_id="d2", metadata={}),
        ],
    }


@pytest.mark.asyncio
async def test_execute_parallel_and_dedup(sample_results):
    """测试并行执行和去重逻辑：相同 chunk_id 保留最高分"""
    retriever = FakeRetriever(sample_results)
    executor = RetrievalExecutor(retriever)

    results = await executor.execute(["query1", "query2"], kb_id="kb_001", top_k=5)

    # 应该去重：c2 出现两次，保留 score=0.9 的那个（来自 query1）
    assert len(results) == 3
    chunk_ids = [r.chunk_id for r in results]
    assert "c1" in chunk_ids
    assert "c2" in chunk_ids
    assert "c3" in chunk_ids

    # c2 保留最高分 0.85（query1 中 c2=0.8, query2 中 c2=0.85，取 0.85）
    c2_result = next(r for r in results if r.chunk_id == "c2")
    assert c2_result.score == 0.85

    # 结果按分数降序排列
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_execute_calls_retriever_with_correct_params(sample_results):
    """测试执行器正确传递参数给检索器"""
    retriever = FakeRetriever(sample_results)
    executor = RetrievalExecutor(retriever)

    await executor.execute(["query1", "query2"], kb_id="kb_test", top_k=7)

    # 验证检索器被调用了两次，参数正确
    assert len(retriever.call_log) == 2
    assert retriever.call_log[0] == ("query1", "kb_test", 7)
    assert retriever.call_log[1] == ("query2", "kb_test", 7)


@pytest.mark.asyncio
async def test_execute_empty_queries():
    """测试空查询列表返回空结果"""
    retriever = FakeRetriever({})
    executor = RetrievalExecutor(retriever)

    results = await executor.execute([], kb_id="kb_001")

    assert results == []
    assert len(retriever.call_log) == 0


@pytest.mark.asyncio
async def test_execute_single_query():
    """测试单个查询正常工作"""
    results_map = {
        "single": [
            RetrievalResult(chunk_id="c1", content="内容", score=0.95, doc_id="d1", metadata={}),
        ]
    }
    retriever = FakeRetriever(results_map)
    executor = RetrievalExecutor(retriever)

    results = await executor.execute(["single"], kb_id="kb_001")

    assert len(results) == 1
    assert results[0].chunk_id == "c1"
    assert results[0].score == 0.95


@pytest.mark.asyncio
async def test_execute_dedup_keeps_higher_score():
    """测试去重时保留更高分数的结果"""
    results_map = {
        "q1": [
            RetrievalResult(chunk_id="same", content="低分版本", score=0.5, doc_id="d1", metadata={"src": "q1"}),
        ],
        "q2": [
            RetrievalResult(chunk_id="same", content="高分版本", score=0.95, doc_id="d1", metadata={"src": "q2"}),
        ],
    }
    retriever = FakeRetriever(results_map)
    executor = RetrievalExecutor(retriever)

    results = await executor.execute(["q1", "q2"], kb_id="kb_001")

    assert len(results) == 1
    # 保留高分版本
    assert results[0].score == 0.95
    assert results[0].content == "高分版本"
