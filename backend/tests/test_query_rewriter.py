"""QueryRewriter 单元测试"""

import pytest

from app.agent.rewriter import QueryRewriter
from app.models.provider import LLMProvider


class FakeLLM(LLMProvider):
    """用于测试的 LLM 假实现"""

    def __init__(self, response: str):
        self.response = response
        self.last_messages = None

    async def generate(self, messages: list[dict], **kwargs) -> str:
        self.last_messages = messages
        return self.response

    async def stream(self, messages, **kwargs):
        yield self.response


@pytest.mark.asyncio
async def test_rewrite_returns_multiple_queries():
    """LLM 返回多行时，解析为多个查询"""
    llm = FakeLLM("Python 是什么编程语言\nPython 的特点和用途\nPython 入门教程")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("什么是 Python？")

    # 原始查询不在 LLM 输出中，应被插入到首位
    assert result[0] == "什么是 Python？"
    assert "Python 是什么编程语言" in result
    assert "Python 的特点和用途" in result
    assert "Python 入门教程" in result


@pytest.mark.asyncio
async def test_rewrite_includes_original_query():
    """结果始终包含原始查询"""
    llm = FakeLLM("改写查询1\n改写查询2")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("原始查询")

    assert "原始查询" in result
    assert result[0] == "原始查询"


@pytest.mark.asyncio
async def test_rewrite_no_duplicate_original():
    """如果 LLM 输出已包含原始查询，不重复添加"""
    llm = FakeLLM("原始查询\n改写查询1\n改写查询2")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("原始查询")

    assert result.count("原始查询") == 1


@pytest.mark.asyncio
async def test_rewrite_handles_empty_lines():
    """忽略 LLM 输出中的空行"""
    llm = FakeLLM("查询1\n\n查询2\n\n")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("测试")

    # 不应包含空字符串
    assert "" not in result
    assert all(q.strip() for q in result)


@pytest.mark.asyncio
async def test_rewrite_strips_whitespace():
    """去除每个查询的首尾空白"""
    llm = FakeLLM("  查询1  \n  查询2  ")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("测试")

    assert "查询1" in result
    assert "查询2" in result


@pytest.mark.asyncio
async def test_rewrite_passes_correct_prompt():
    """验证传递给 LLM 的 prompt 格式正确"""
    llm = FakeLLM("改写结果")
    rewriter = QueryRewriter(llm)
    await rewriter.rewrite("测试查询")

    assert llm.last_messages is not None
    assert len(llm.last_messages) == 2
    assert llm.last_messages[0]["role"] == "system"
    assert llm.last_messages[1]["role"] == "user"
    assert llm.last_messages[1]["content"] == "测试查询"


@pytest.mark.asyncio
async def test_rewrite_single_line_response():
    """LLM 只返回一行时，结果包含原始查询和该行"""
    llm = FakeLLM("单行改写结果")
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite("原始问题")

    assert len(result) == 2
    assert result[0] == "原始问题"
    assert result[1] == "单行改写结果"
