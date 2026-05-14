"""AgentOrchestrator 单元测试"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.executor import RetrievalExecutor
from app.agent.orchestrator import AgentOrchestrator, AgentResult
from app.agent.reflector import Reflector, ReflectionVerdict
from app.agent.rewriter import QueryRewriter
from app.agent.router import QueryRouter
from app.retrieval.base import BaseRetriever, RetrievalResult


# --- Fixtures ---


def _make_result(chunk_id: str, score: float = 0.9) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        content=f"content_{chunk_id}",
        score=score,
        doc_id="doc_1",
        metadata={},
    )


@pytest.fixture
def mock_router():
    router = AsyncMock(spec=QueryRouter)
    router.classify = AsyncMock(return_value="complex")
    return router


@pytest.fixture
def mock_rewriter():
    rewriter = AsyncMock(spec=QueryRewriter)
    rewriter.rewrite = AsyncMock(return_value=["rewritten_q1", "rewritten_q2"])
    return rewriter


@pytest.fixture
def mock_executor():
    executor = AsyncMock(spec=RetrievalExecutor)
    executor.execute = AsyncMock(return_value=[_make_result("c1"), _make_result("c2")])
    executor.reset_cache = lambda: None
    return executor


@pytest.fixture
def mock_reflector():
    reflector = AsyncMock(spec=Reflector)
    reflector.evaluate = AsyncMock(
        return_value=ReflectionVerdict(is_sufficient=True)
    )
    return reflector


@pytest.fixture
def mock_retriever():
    retriever = AsyncMock(spec=BaseRetriever)
    retriever.search = AsyncMock(return_value=[_make_result("fast_c1")])
    return retriever


@pytest.fixture
def orchestrator(mock_router, mock_rewriter, mock_executor, mock_reflector, mock_retriever):
    return AgentOrchestrator(
        router=mock_router,
        rewriter=mock_rewriter,
        executor=mock_executor,
        reflector=mock_reflector,
        retriever=mock_retriever,
        max_iterations=3,
        timeout=8.0,
    )


# --- Tests ---


@pytest.mark.asyncio
async def test_simple_query_fast_path(orchestrator, mock_router, mock_retriever):
    """简单查询走快路径，不经过改写和反思"""
    mock_router.classify.return_value = "simple"

    result = await orchestrator.run("什么是Python？", "kb_1")

    assert result.iterations == 0
    assert result.degraded is False
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_id == "fast_c1"
    mock_retriever.search.assert_called_once_with("什么是Python？", "kb_1", top_k=30)


@pytest.mark.asyncio
async def test_complex_query_full_flow(
    orchestrator, mock_router, mock_rewriter, mock_executor, mock_reflector
):
    """复杂查询走完整编排流程"""
    mock_router.classify.return_value = "complex"

    result = await orchestrator.run("对比A和B的优缺点", "kb_1")

    assert result.iterations == 1
    assert result.degraded is False
    assert len(result.chunks) == 2
    mock_rewriter.rewrite.assert_called_once()
    mock_executor.execute.assert_called_once()
    mock_reflector.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_complex_query_multiple_iterations(
    orchestrator, mock_router, mock_executor, mock_reflector
):
    """复杂查询需要多轮迭代"""
    mock_router.classify.return_value = "complex"
    # 第一轮不充分，第二轮充分
    mock_reflector.evaluate.side_effect = [
        ReflectionVerdict(is_sufficient=False, follow_up_queries=["追加查询"]),
        ReflectionVerdict(is_sufficient=True),
    ]

    result = await orchestrator.run("复杂问题", "kb_1")

    assert result.iterations == 2
    assert result.degraded is False
    assert mock_executor.execute.call_count == 2


@pytest.mark.asyncio
async def test_max_iterations_reached(
    orchestrator, mock_router, mock_executor, mock_reflector
):
    """达到最大迭代次数后停止"""
    mock_router.classify.return_value = "complex"
    # 始终不充分，但覆盖度递增（避免覆盖度增幅不足导致提前终止）
    mock_reflector.evaluate.side_effect = [
        ReflectionVerdict(is_sufficient=False, follow_up_queries=["追加1"], coverage_score=0.3),
        ReflectionVerdict(is_sufficient=False, follow_up_queries=["追加2"], coverage_score=0.5),
    ]
    # 每轮返回不同结果，避免"无新增结果"提前终止
    call_count = [0]

    async def varying_results(queries, kb_id, top_k=30):
        call_count[0] += 1
        return [_make_result(f"c{call_count[0]}_1"), _make_result(f"c{call_count[0]}_2")]

    mock_executor.execute = varying_results

    result = await orchestrator.run("超复杂问题", "kb_1")

    assert result.iterations == 3
    assert result.degraded is False
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_timeout_param_ignored(mock_router, mock_rewriter, mock_executor, mock_reflector, mock_retriever):
    """timeout 参数保留兼容性但不再生效，慢操作不会触发降级"""
    # router 正常返回 simple（不再模拟超时，因为 timeout 已移除）
    mock_router.classify = AsyncMock(return_value="simple")

    orchestrator = AgentOrchestrator(
        router=mock_router,
        rewriter=mock_rewriter,
        executor=mock_executor,
        reflector=mock_reflector,
        retriever=mock_retriever,
        max_iterations=3,
        timeout=0.1,  # 参数保留但不生效
    )

    result = await orchestrator.run("正常查询", "kb_1")

    # 正常走快路径，不降级
    assert result.degraded is False
    assert result.iterations == 0


@pytest.mark.asyncio
async def test_exception_degrades_to_fast_path(
    mock_router, mock_rewriter, mock_executor, mock_reflector, mock_retriever
):
    """异常降级到快路径"""
    mock_router.classify = AsyncMock(side_effect=RuntimeError("LLM 不可用"))

    orchestrator = AgentOrchestrator(
        router=mock_router,
        rewriter=mock_rewriter,
        executor=mock_executor,
        reflector=mock_reflector,
        retriever=mock_retriever,
        max_iterations=3,
        timeout=8.0,
    )

    result = await orchestrator.run("会出错的查询", "kb_1")

    assert result.degraded is True
    assert result.iterations == 0


@pytest.mark.asyncio
async def test_agent_result_dataclass():
    """AgentResult 数据类基本行为"""
    result = AgentResult(chunks=[], iterations=2, degraded=False)
    assert result.chunks == []
    assert result.iterations == 2
    assert result.degraded is False

    # 默认 degraded=False
    result2 = AgentResult(chunks=[], iterations=0)
    assert result2.degraded is False
