"""query_understanding 模块单元测试

覆盖单轮检索链路的查询理解：改写 + 意图分类 + 安全降级。
"""

from unittest.mock import AsyncMock

import pytest

from app.api.query_understanding import (
    QueryUnderstanding,
    _format_history,
    _parse_output,
    understand_query,
)


# ============================================================
# QueryUnderstanding.needs_retrieval
# ============================================================


def test_needs_retrieval_kb_search():
    assert QueryUnderstanding(intent="kb_search", rewrite_query="x").needs_retrieval is True


@pytest.mark.parametrize("intent", ["greeting", "chitchat", "follow_up"])
def test_needs_retrieval_no_retrieval_intents(intent):
    assert QueryUnderstanding(intent=intent, rewrite_query="x").needs_retrieval is False


# ============================================================
# _format_history
# ============================================================


def test_format_history_empty():
    assert _format_history(None) == "（无历史对话）"
    assert _format_history([]) == "（无历史对话）"


def test_format_history_windows_recent():
    history = [{"role": "user", "content": f"q{i}"} for i in range(10)]
    text = _format_history(history)
    # 只保留最近 6 条
    assert "q9" in text
    assert "q0" not in text
    assert text.count("user:") == 6


# ============================================================
# _parse_output
# ============================================================


def test_parse_output_clean_json():
    result = _parse_output('{"intent":"kb_search","rewrite_query":"RAG架构"}', "原始")
    assert result is not None
    assert result.intent == "kb_search"
    assert result.rewrite_query == "RAG架构"


def test_parse_output_with_markdown_wrapper():
    raw = '```json\n{"intent":"greeting","rewrite_query":"你好"}\n```'
    result = _parse_output(raw, "你好")
    assert result is not None
    assert result.intent == "greeting"


def test_parse_output_unknown_intent_defaults_to_kb_search():
    result = _parse_output('{"intent":"weird","rewrite_query":"x"}', "原始")
    assert result.intent == "kb_search"


def test_parse_output_empty_rewrite_falls_back_to_original():
    result = _parse_output('{"intent":"kb_search","rewrite_query":""}', "原始问题")
    assert result.rewrite_query == "原始问题"


def test_parse_output_overlong_rewrite_falls_back_to_original():
    long_rewrite = "x" * 500
    result = _parse_output(
        f'{{"intent":"kb_search","rewrite_query":"{long_rewrite}"}}', "原始问题"
    )
    assert result.rewrite_query == "原始问题"


def test_parse_output_invalid_json_returns_none():
    assert _parse_output("not json at all", "原始") is None
    assert _parse_output("", "原始") is None


# ============================================================
# understand_query
# ============================================================


@pytest.mark.asyncio
async def test_understand_query_no_history_skips_llm():
    """无历史时直接当新问题处理，不调用 LLM"""
    llm = AsyncMock()
    result = await understand_query(llm, "什么是RAG", history=None)
    assert result.intent == "kb_search"
    assert result.rewrite_query == "什么是RAG"
    llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_understand_query_resolves_coreference():
    """有历史时调用 LLM 消解指代"""
    llm = AsyncMock()
    llm.generate = AsyncMock(
        return_value='{"intent":"kb_search","rewrite_query":"RAG架构和传统搜索的区别"}'
    )
    history = [
        {"role": "user", "content": "什么是RAG架构"},
        {"role": "assistant", "content": "RAG架构是..."},
    ]
    result = await understand_query(llm, "它和传统搜索有什么区别", history)
    assert result.intent == "kb_search"
    assert result.rewrite_query == "RAG架构和传统搜索的区别"
    assert result.needs_retrieval is True


@pytest.mark.asyncio
async def test_understand_query_greeting_skips_retrieval():
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value='{"intent":"greeting","rewrite_query":"好的"}')
    history = [
        {"role": "user", "content": "介绍下RAG"},
        {"role": "assistant", "content": "RAG是..."},
    ]
    result = await understand_query(llm, "好的", history)
    assert result.intent == "greeting"
    assert result.needs_retrieval is False


@pytest.mark.asyncio
async def test_understand_query_llm_failure_degrades_gracefully():
    """LLM 调用异常时降级为原始 query 检索"""
    llm = AsyncMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
    history = [{"role": "user", "content": "之前的问题"}]
    result = await understand_query(llm, "它怎么样", history)
    assert result.intent == "kb_search"
    assert result.rewrite_query == "它怎么样"


@pytest.mark.asyncio
async def test_understand_query_unparseable_degrades_gracefully():
    """LLM 返回无法解析时降级为原始 query 检索"""
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value="对不起我不会")
    history = [{"role": "user", "content": "之前的问题"}]
    result = await understand_query(llm, "它怎么样", history)
    assert result.intent == "kb_search"
    assert result.rewrite_query == "它怎么样"
