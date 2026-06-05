"""KnowledgeSearchTool 多源装配（SearchTarget + expr）测试

agent-session-source-unification Task 1.1：验证
- 会话源带 session_id expr、正式 KB 源 expr=None；
- LLM 经 knowledge_base_ids 入参只能在已授权 KB 子集内取交集，不引入新 kb、不移除会话源（Property 5）；
- 多源 × 多 query 的检索调用组合正确。
"""

import pytest

from app.agent.state import AgentState
from app.agent.tools.knowledge_search import KnowledgeSearchTool, SearchTarget
from app.retrieval.base import RetrievalResult

SESSION_EXPR = 'session_id == "sess-1"'


class RecordingRetriever:
    """记录每次 search_with_degraded 的 (kb_id, expr) 调用，便于断言源装配。"""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def search_with_degraded(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None, **kwargs
    ):
        self.calls.append((kb_id, expr))
        # 每个源回一条带 kb 标识的结果，便于区分来源
        r = RetrievalResult(
            chunk_id=f"{kb_id}-c1", content=f"content of {kb_id}", score=0.9, doc_id=f"{kb_id}-d1"
        )
        return [r], False


def _targets_kb_and_session():
    return [
        SearchTarget(kb_id="kbA", expr=None),
        SearchTarget(kb_id="kbB", expr=None),
        SearchTarget(kb_id="session_files", expr=SESSION_EXPR),
    ]


@pytest.mark.asyncio
async def test_session_source_carries_expr_kb_source_none():
    """会话源带 session_id expr，KB 源 expr=None。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool(
        retriever=retriever, state=AgentState(), search_targets=_targets_kb_and_session()
    )
    await tool.execute({"queries": ["驻军法第三条"]})

    by_kb = dict(retriever.calls)
    assert by_kb["kbA"] is None
    assert by_kb["kbB"] is None
    assert by_kb["session_files"] == SESSION_EXPR


@pytest.mark.asyncio
async def test_multi_source_multi_query_cartesian():
    """N 源 × M query → N*M 次检索调用。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool(
        retriever=retriever, state=AgentState(), search_targets=_targets_kb_and_session()
    )
    await tool.execute({"queries": ["q1", "q2"]})
    # 3 源 × 2 query = 6 次
    assert len(retriever.calls) == 6
    assert {kb for kb, _ in retriever.calls} == {"kbA", "kbB", "session_files"}


@pytest.mark.asyncio
async def test_llm_kb_subset_intersects_authorized_and_keeps_session():
    """LLM 指定 knowledge_base_ids 子集 → 只检索授权 KB 交集 + 始终保留会话源（Property 5）。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool(
        retriever=retriever, state=AgentState(), search_targets=_targets_kb_and_session()
    )
    await tool.execute({"queries": ["q"], "knowledge_base_ids": ["kbA"]})

    kbs = {kb for kb, _ in retriever.calls}
    assert "kbA" in kbs            # 被选中的授权库
    assert "kbB" not in kbs        # 未选中的库被排除
    assert "session_files" in kbs  # 会话源不受 LLM 入参影响，始终保留


@pytest.mark.asyncio
async def test_llm_cannot_inject_unauthorized_kb():
    """LLM 指定未授权 kb / 伪造会话 id → 被忽略，不引入新源（Property 5）。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool(
        retriever=retriever, state=AgentState(), search_targets=_targets_kb_and_session()
    )
    # 指定一个不存在的 kb 和会话源 id（试图绕过）
    await tool.execute({"queries": ["q"], "knowledge_base_ids": ["evil-kb", "session_files"]})

    kbs = {kb for kb, _ in retriever.calls}
    assert "evil-kb" not in kbs  # 未授权 kb 不引入
    # 交集为空（evil-kb 未授权，session_files 是会话源非正式 KB）→ 回退到全部授权源
    assert kbs == {"kbA", "kbB", "session_files"}


@pytest.mark.asyncio
async def test_from_kb_ids_factory_all_expr_none():
    """便捷工厂 from_kb_ids 构造的源全部 expr=None（无会话源）。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool.from_kb_ids(retriever, ["kbA", "kbB"], state=AgentState())
    await tool.execute({"queries": ["q"]})

    assert all(expr is None for _, expr in retriever.calls)
    assert {kb for kb, _ in retriever.calls} == {"kbA", "kbB"}


@pytest.mark.asyncio
async def test_backward_compat_kb_id_constructor():
    """旧式 kb_id 构造仍可用（向后兼容），无会话源、expr=None。"""
    retriever = RecordingRetriever()
    tool = KnowledgeSearchTool(retriever=retriever, kb_id="kb-legacy", state=AgentState())
    await tool.execute({"queries": ["q"]})

    assert retriever.calls == [("kb-legacy", None)]


class FailingSessionRetriever:
    """会话源（带 expr）检索抛异常，KB 源正常 —— 用于断言失败源透传。"""

    async def search_with_degraded(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None, **kwargs
    ):
        if expr is not None:  # 会话源
            raise RuntimeError("session source milvus timeout")
        r = RetrievalResult(
            chunk_id=f"{kb_id}-c1", content=f"content of {kb_id}", score=0.9, doc_id=f"{kb_id}-d1"
        )
        return [r], False


@pytest.mark.asyncio
async def test_session_source_failure_records_failed_source_id():
    """会话源检索失败 → state.failed_source_ids 含 SESSION_FILES_KB_ID，degraded=True（降级透传）。"""
    state = AgentState()
    tool = KnowledgeSearchTool(
        retriever=FailingSessionRetriever(), state=state, search_targets=_targets_kb_and_session()
    )
    result = await tool.execute({"queries": ["驻军法第三条"]})

    # KB 源仍命中 → 工具整体成功（不因单源失败而失败）
    assert result.success
    assert state.degraded is True
    assert "session_files" in state.failed_source_ids
    # 正常 KB 源不应被记为失败
    assert "kbA" not in state.failed_source_ids
    assert "kbB" not in state.failed_source_ids


@pytest.mark.asyncio
async def test_route_degraded_records_failed_source_id():
    """某源返回路级降级（route_degraded=True）→ 记入 failed_source_ids 且 degraded=True。"""

    class RouteDegradedRetriever:
        async def search_with_degraded(self, query, kb_id, top_k=10, expr=None, **kwargs):
            degraded = kb_id == "kbB"  # kbB 路级降级
            r = RetrievalResult(chunk_id=f"{kb_id}-c1", content="x", score=0.5, doc_id=f"{kb_id}-d1")
            return [r], degraded

    state = AgentState()
    tool = KnowledgeSearchTool(
        retriever=RouteDegradedRetriever(), state=state,
        search_targets=[SearchTarget("kbA", None), SearchTarget("kbB", None)],
    )
    await tool.execute({"queries": ["q"]})

    assert state.degraded is True
    assert state.failed_source_ids == {"kbB"}
