"""测试 KnowledgeSearchTool 的 session 级 chunk 去重功能 [REQ-8]"""

import pytest

from app.agent.state import AgentState
from app.agent.tools.knowledge_search import KnowledgeSearchTool
from app.retrieval.base import RetrievalResult


class FakeRetriever:
    """Fake retriever for testing"""

    def __init__(self, results: list[RetrievalResult]):
        self._results = results

    async def search(self, query: str, kb_id: str, top_k: int = 10, **kwargs):
        return self._results[:top_k]


def _make_result(chunk_id: str, content: str, score: float = 0.9) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        content=content,
        score=score,
        doc_id="doc-1",
    )


@pytest.mark.asyncio
async def test_first_call_returns_full_content():
    """首次检索返回完整内容"""
    results = [_make_result("chunk-1", "Hello world content")]
    retriever = FakeRetriever(results)
    state = AgentState()
    tool = KnowledgeSearchTool(retriever=retriever, kb_id="kb-1", state=state)

    result = await tool.execute({"queries": ["hello"]})

    assert result.success
    assert "Hello world content" in result.output
    assert "(content omitted, already returned)" not in result.output
    assert "chunk-1" in state.seen_chunk_ids


@pytest.mark.asyncio
async def test_duplicate_chunk_returns_omitted_marker():
    """同一 session 内重复 chunk 返回简短标记"""
    results = [_make_result("chunk-1", "Hello world content")]
    retriever = FakeRetriever(results)
    state = AgentState()
    tool = KnowledgeSearchTool(retriever=retriever, kb_id="kb-1", state=state)

    # 第一次调用
    await tool.execute({"queries": ["hello"]})

    # 第二次调用 - 相同 chunk_id
    result2 = await tool.execute({"queries": ["hello"]})

    assert result2.success
    assert "(content omitted, already returned)" in result2.output
    assert "Hello world content" not in result2.output


@pytest.mark.asyncio
async def test_empty_chunk_id_skips_dedup():
    """chunk_id 为空时跳过去重逻辑，始终返回完整内容"""
    results = [_make_result("", "Content with empty id")]
    retriever = FakeRetriever(results)
    state = AgentState()
    tool = KnowledgeSearchTool(retriever=retriever, kb_id="kb-1", state=state)

    # 第一次调用
    await tool.execute({"queries": ["test"]})

    # 第二次调用 - 空 chunk_id 不应被去重
    result2 = await tool.execute({"queries": ["test"]})

    assert result2.success
    assert "Content with empty id" in result2.output
    assert "(content omitted, already returned)" not in result2.output


@pytest.mark.asyncio
async def test_mixed_seen_and_new_chunks():
    """混合已见和新 chunk 的情况"""
    results_first = [
        _make_result("chunk-1", "First content", 0.9),
        _make_result("chunk-2", "Second content", 0.8),
    ]
    results_second = [
        _make_result("chunk-1", "First content", 0.9),  # 重复
        _make_result("chunk-3", "Third content", 0.7),  # 新的
    ]

    state = AgentState()

    # 第一次调用
    retriever1 = FakeRetriever(results_first)
    tool1 = KnowledgeSearchTool(retriever=retriever1, kb_id="kb-1", state=state)
    await tool1.execute({"queries": ["test"]})

    # 第二次调用
    retriever2 = FakeRetriever(results_second)
    tool2 = KnowledgeSearchTool(retriever=retriever2, kb_id="kb-1", state=state)
    result2 = await tool2.execute({"queries": ["test"]})

    assert result2.success
    # chunk-1 应该被标记为已返回
    assert "(content omitted, already returned)" in result2.output
    # chunk-3 应该返回完整内容
    assert "Third content" in result2.output


@pytest.mark.asyncio
async def test_seen_chunks_not_added_to_knowledge_refs():
    """已见 chunk 不应被添加到 knowledge_refs"""
    results = [_make_result("chunk-1", "Content")]
    retriever = FakeRetriever(results)
    state = AgentState()
    tool = KnowledgeSearchTool(retriever=retriever, kb_id="kb-1", state=state)

    # 第一次调用 - 添加到 refs
    await tool.execute({"queries": ["test"]})
    assert len(state.knowledge_refs) == 1

    # 第二次调用 - 不应再添加
    await tool.execute({"queries": ["test"]})
    assert len(state.knowledge_refs) == 1
