"""QueryRouter 单元测试"""

import pytest
from unittest.mock import AsyncMock

from app.agent.router import QueryRouter
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
async def test_classify_simple_query():
    """LLM 返回 simple 时，路由结果为 simple"""
    llm = FakeLLM("simple")
    router = QueryRouter(llm)
    result = await router.classify("什么是 Python？")
    assert result == "simple"


@pytest.mark.asyncio
async def test_classify_complex_query():
    """LLM 返回 complex 时，路由结果为 complex"""
    llm = FakeLLM("complex")
    router = QueryRouter(llm)
    result = await router.classify("对比三种排序算法的时间复杂度并分析适用场景")
    assert result == "complex"


@pytest.mark.asyncio
async def test_classify_defaults_to_simple():
    """LLM 返回无法识别的内容时，默认为 simple"""
    llm = FakeLLM("我不确定")
    router = QueryRouter(llm)
    result = await router.classify("你好")
    assert result == "simple"


@pytest.mark.asyncio
async def test_classify_complex_with_extra_text():
    """LLM 返回包含 complex 的文本时，识别为 complex"""
    llm = FakeLLM("This is a complex query.")
    router = QueryRouter(llm)
    result = await router.classify("分析多个文档之间的关联性")
    assert result == "complex"


@pytest.mark.asyncio
async def test_classify_case_insensitive():
    """分类结果不区分大小写"""
    llm = FakeLLM("COMPLEX")
    router = QueryRouter(llm)
    result = await router.classify("综合分析")
    assert result == "complex"


@pytest.mark.asyncio
async def test_classify_passes_correct_prompt():
    """验证传递给 LLM 的 prompt 格式正确"""
    llm = FakeLLM("simple")
    router = QueryRouter(llm)
    await router.classify("测试查询")

    assert llm.last_messages is not None
    assert len(llm.last_messages) == 2
    assert llm.last_messages[0]["role"] == "system"
    assert llm.last_messages[1]["role"] == "user"
    assert llm.last_messages[1]["content"] == "测试查询"
