"""Reflector 单元测试"""

import pytest

from app.agent.reflector import Reflector, ReflectionVerdict
from app.models.provider import LLMProvider
from app.retrieval.base import RetrievalResult


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


def _make_result(chunk_id: str = "c1", content: str = "测试内容", score: float = 0.9) -> RetrievalResult:
    """创建测试用 RetrievalResult"""
    return RetrievalResult(
        chunk_id=chunk_id,
        content=content,
        score=score,
        doc_id="doc1",
        metadata={},
    )


@pytest.mark.asyncio
async def test_evaluate_sufficient():
    """LLM 判定结果充分时，返回 is_sufficient=True"""
    llm = FakeLLM('{"sufficient": true}')
    reflector = Reflector(llm)
    results = [_make_result()]

    verdict = await reflector.evaluate("什么是 RAG？", results)

    assert verdict.is_sufficient is True
    assert verdict.follow_up_queries == []


@pytest.mark.asyncio
async def test_evaluate_insufficient_with_follow_up():
    """LLM 判定结果不充分时，返回追加查询"""
    llm = FakeLLM('{"sufficient": false, "follow_up_queries": ["RAG 的优势", "RAG 的实现方式"]}')
    reflector = Reflector(llm)
    # 使用中间分数（0.5），触发 LLM 深度评估路径
    results = [_make_result(score=0.5)]

    verdict = await reflector.evaluate("详细介绍 RAG", results)

    assert verdict.is_sufficient is False
    assert len(verdict.follow_up_queries) == 2
    assert "RAG 的优势" in verdict.follow_up_queries


@pytest.mark.asyncio
async def test_evaluate_empty_results():
    """无检索结果时，直接判定不充分"""
    llm = FakeLLM("")
    reflector = Reflector(llm)

    verdict = await reflector.evaluate("测试查询", [])

    assert verdict.is_sufficient is False
    assert verdict.follow_up_queries == ["测试查询"]


@pytest.mark.asyncio
async def test_evaluate_parse_failure_defaults_sufficient():
    """LLM 返回无法解析的内容时，默认判定充分（避免无限循环）"""
    llm = FakeLLM("这不是有效的 JSON")
    reflector = Reflector(llm)
    results = [_make_result()]

    verdict = await reflector.evaluate("测试", results)

    assert verdict.is_sufficient is True


@pytest.mark.asyncio
async def test_evaluate_json_in_code_block():
    """LLM 返回 markdown 代码块包裹的 JSON 时，正确解析"""
    llm = FakeLLM('```json\n{"sufficient": false, "follow_up_queries": ["补充查询"]}\n```')
    reflector = Reflector(llm)
    # 使用中间分数，触发 LLM 深度评估路径
    results = [_make_result(score=0.5)]

    verdict = await reflector.evaluate("测试", results)

    assert verdict.is_sufficient is False
    assert verdict.follow_up_queries == ["补充查询"]


@pytest.mark.asyncio
async def test_evaluate_insufficient_no_follow_up_uses_original():
    """LLM 判定不充分但未提供追加查询时，使用原始查询"""
    llm = FakeLLM('{"sufficient": false}')
    reflector = Reflector(llm)
    # 使用中间分数，触发 LLM 深度评估路径
    results = [_make_result(score=0.5)]

    verdict = await reflector.evaluate("原始查询", results)

    assert verdict.is_sufficient is False
    assert verdict.follow_up_queries == ["原始查询"]


@pytest.mark.asyncio
async def test_evaluate_passes_correct_prompt():
    """验证传递给 LLM 的 prompt 包含查询和检索内容"""
    llm = FakeLLM('{"sufficient": true}')
    reflector = Reflector(llm)
    # 使用中间分数，触发 LLM 深度评估路径
    results = [_make_result(content="关于 RAG 的内容", score=0.5)]

    await reflector.evaluate("什么是 RAG？", results)

    assert llm.last_messages is not None
    assert len(llm.last_messages) == 2
    assert llm.last_messages[0]["role"] == "system"
    assert llm.last_messages[1]["role"] == "user"
    assert "什么是 RAG？" in llm.last_messages[1]["content"]
    assert "关于 RAG 的内容" in llm.last_messages[1]["content"]
