"""Chat API 单元测试

通过 mock 重型依赖（FlagEmbedding 等）来测试 Chat API 逻辑。
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock 重型依赖模块，避免导入 FlagEmbedding / torch 等
sys.modules.setdefault("pymilvus", MagicMock())

from app.retrieval.base import RetrievalResult
from app.schema.api import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ReferenceItem,
    UsageInfo,
)


# ============================================================
# 辅助函数测试
# ============================================================


def test_estimate_tokens():
    """测试 token 估算函数"""
    from app.api.chat import _estimate_tokens

    assert _estimate_tokens("hello") == 2  # 5 // 2 = 2
    assert _estimate_tokens("") == 1  # max(1, 0) = 1
    assert _estimate_tokens("你好世界测试") == 3  # 6 // 2 = 3
    assert _estimate_tokens("a" * 100) == 50  # 100 // 2 = 50


def test_build_context_empty():
    """测试空检索结果的上下文构建"""
    from app.api.chat import _build_context

    result = _build_context([])
    assert "未找到相关内容" in result


def test_build_context_with_chunks():
    """测试有检索结果时的上下文构建"""
    from app.api.chat import _build_context

    chunks = [
        RetrievalResult(
            chunk_id="c1", content="内容一", score=0.9, doc_id="d1", metadata={}
        ),
        RetrievalResult(
            chunk_id="c2", content="内容二", score=0.8, doc_id="d2", metadata={}
        ),
    ]
    result = _build_context(chunks)
    assert "[1] 内容一" in result
    assert "[2] 内容二" in result


def test_build_references():
    """测试引用来源构建"""
    from app.api.chat import _build_references

    chunks = [
        RetrievalResult(
            chunk_id="c1", content="测试内容", score=0.9234, doc_id="d1", metadata={}
        ),
    ]
    refs = _build_references(chunks)
    assert len(refs) == 1
    assert refs[0].chunk_id == "c1"
    assert refs[0].doc_id == "d1"
    assert refs[0].score == 0.9234
    assert refs[0].content == "测试内容"


def test_build_references_truncates_long_content():
    """测试引用来源截断过长内容"""
    from app.api.chat import _build_references

    long_content = "x" * 1000
    chunks = [
        RetrievalResult(
            chunk_id="c1", content=long_content, score=0.9, doc_id="d1", metadata={}
        ),
    ]
    refs = _build_references(chunks)
    assert len(refs[0].content) == 500


# ============================================================
# 请求/响应模型验证测试
# ============================================================


def test_request_model_valid():
    """测试有效请求模型"""
    req = ChatCompletionRequest(
        model="rag",
        messages=[ChatMessage(role="user", content="你好")],
        stream=False,
        knowledge_base_id="kb_001",
    )
    assert req.knowledge_base_id == "kb_001"
    assert req.retrieval_mode is None
    assert req.stream is False
    assert req.model == "rag"


def test_request_model_with_retrieval_mode():
    """测试带检索模式的请求"""
    req = ChatCompletionRequest(
        model="rag",
        messages=[ChatMessage(role="user", content="测试")],
        knowledge_base_id="kb_001",
        retrieval_mode="agent",
        stream=True,
    )
    assert req.retrieval_mode == "agent"
    assert req.stream is True


def test_request_model_defaults():
    """测试请求模型默认值"""
    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="hi")],
        knowledge_base_id="kb_001",
    )
    assert req.model == "rag"
    assert req.stream is False
    assert req.temperature is None
    assert req.max_tokens is None


def test_response_model():
    """测试响应模型序列化"""
    from app.schema.api import ChatChoice, ResponseMessage

    resp = ChatCompletionResponse(
        id="chatcmpl-test123",
        choices=[ChatChoice(message=ResponseMessage(content="回答内容"))],
        usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        references=[
            ReferenceItem(doc_id="d1", chunk_id="c1", content="参考", score=0.9)
        ],
        metadata={"retrieval_mode": "hybrid", "degraded": False},
    )
    data = resp.model_dump()
    assert data["id"] == "chatcmpl-test123"
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "回答内容"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] == 15
    assert len(data["references"]) == 1
    assert data["metadata"]["retrieval_mode"] == "hybrid"


# ============================================================
# 端点集成测试（mock 外部依赖）
# ============================================================


@pytest.fixture
def test_client():
    """创建测试客户端，mock 重型依赖"""
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


@patch("app.api.chat._retrieve_chunks")
@patch("app.api.chat._retrieve_chunks")
@patch("app.api.chat.get_effective_preset_config", new_callable=AsyncMock)
@patch("app.api.chat.get_model_manager")
def test_chat_completions_non_stream(mock_manager, mock_preset, mock_retrieve, test_client):
    """测试非流式 Chat Completion 响应"""
    # Mock 生效预设：hybrid 模式
    mock_preset.return_value = {"agent_mode": "hybrid"}

    # Mock 检索结果
    mock_retrieve.return_value = (
        [
            RetrievalResult(
                chunk_id="c1",
                content="密码重置步骤：1. 点击忘记密码",
                score=0.92,
                doc_id="d1",
                metadata={},
            )
        ],
        False,
    )

    # Mock LLM 生成
    mock_llm = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="您可以通过点击忘记密码来重置密码。")
    mock_manager_instance = MagicMock()
    mock_manager_instance.llm = mock_llm
    mock_manager.return_value = mock_manager_instance

    response = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "rag",
            "messages": [{"role": "user", "content": "如何重置密码？"}],
            "stream": False,
            "knowledge_base_id": "kb_001",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert len(data["choices"]) == 1
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "您可以通过点击忘记密码来重置密码。"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] > 0
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0
    assert len(data["references"]) == 1
    assert data["references"][0]["chunk_id"] == "c1"
    assert data["references"][0]["score"] == 0.92
    assert data["metadata"]["retrieval_mode"] == "hybrid"
    assert data["metadata"]["degraded"] is False


@patch("app.api.chat._retrieve_chunks")
@patch("app.api.chat.get_effective_preset_config", new_callable=AsyncMock)
@patch("app.api.chat.get_model_manager")
def test_chat_completions_stream(mock_manager, mock_preset, mock_retrieve, test_client):
    """测试流式 SSE 响应"""
    mock_preset.return_value = {"agent_mode": "direct"}

    mock_retrieve.return_value = (
        [
            RetrievalResult(
                chunk_id="c1",
                content="相关内容",
                score=0.85,
                doc_id="d1",
                metadata={},
            )
        ],
        False,
    )

    # Mock LLM 流式生成
    async def mock_stream(messages, **kwargs):
        for token in ["你好", "，", "这是", "回答"]:
            yield token

    mock_llm = MagicMock()
    mock_llm.stream = mock_stream
    mock_manager_instance = MagicMock()
    mock_manager_instance.llm = mock_llm
    mock_manager.return_value = mock_manager_instance

    response = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "rag",
            "messages": [{"role": "user", "content": "测试"}],
            "stream": True,
            "knowledge_base_id": "kb_001",
            "retrieval_mode": "direct",
        },
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    # 解析 SSE 事件
    events = []
    for line in response.text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            if data_str:
                events.append(json.loads(data_str))

    # 至少有：role chunk + 4 content chunks + finish chunk + meta chunk
    assert len(events) >= 4

    # 第一个事件应包含 role
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"

    # 最后一个事件应包含 references 和 metadata
    last_event = events[-1]
    assert "references" in last_event
    assert "metadata" in last_event
    assert last_event["metadata"]["retrieval_mode"] == "direct"


def test_chat_completions_missing_user_message(test_client):
    """测试缺少 user 消息时返回 400"""
    response = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "rag",
            "messages": [{"role": "system", "content": "你是助手"}],
            "stream": False,
            "knowledge_base_id": "kb_001",
        },
    )

    assert response.status_code == 400
    assert "user" in response.json()["detail"]


@patch("app.api.chat._retrieve_chunks")
@patch("app.api.chat.get_effective_preset_config", new_callable=AsyncMock)
@patch("app.api.chat.get_model_manager")
def test_chat_completions_with_degraded(mock_manager, mock_preset, mock_retrieve, test_client):
    """测试降级模式标记"""
    mock_preset.return_value = {"agent_mode": "agent"}

    # 模拟 Agent 降级
    mock_retrieve.return_value = (
        [
            RetrievalResult(
                chunk_id="c1",
                content="降级内容",
                score=0.7,
                doc_id="d1",
                metadata={},
            )
        ],
        True,  # degraded=True
    )

    mock_llm = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="降级回答")
    mock_manager_instance = MagicMock()
    mock_manager_instance.llm = mock_llm
    mock_manager.return_value = mock_manager_instance

    response = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "rag",
            "messages": [{"role": "user", "content": "复杂问题"}],
            "stream": False,
            "knowledge_base_id": "kb_001",
            "retrieval_mode": "agent",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["metadata"]["degraded"] is True
    assert data["metadata"]["retrieval_mode"] == "agent"


@patch("app.api.chat._retrieve_chunks")
@patch("app.api.chat.get_effective_preset_config", new_callable=AsyncMock)
def test_chat_completions_retrieval_error(mock_preset, mock_retrieve, test_client):
    """测试检索失败时返回 500"""
    mock_preset.return_value = {"agent_mode": "hybrid"}
    mock_retrieve.side_effect = Exception("Milvus 连接超时")

    response = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "rag",
            "messages": [{"role": "user", "content": "测试"}],
            "stream": False,
            "knowledge_base_id": "kb_001",
        },
    )

    assert response.status_code == 500
    assert "检索失败" in response.json()["detail"]


# ============================================================
# 历史 assistant 轮次结构化还原测试
# ============================================================


def test_reconstruct_turn_plain_answer_no_steps():
    """无 agent_steps（普通 RAG / 旧数据）应退化为单条 assistant 消息"""
    from app.api.chat import _reconstruct_assistant_turn

    msgs = _reconstruct_assistant_turn("最终答案", None)
    assert msgs == [{"role": "assistant", "content": "最终答案"}]


def test_reconstruct_turn_empty_answer_returns_nothing():
    """空答案且无步骤应返回空列表，不污染上下文"""
    from app.api.chat import _reconstruct_assistant_turn

    assert _reconstruct_assistant_turn("", None) == []
    assert _reconstruct_assistant_turn("   ", []) == []


def test_reconstruct_turn_strips_legacy_tool_annotation():
    """历史正文中残留的旧版 [Agent used: ...] 注解应被剥除"""
    from app.api.chat import _reconstruct_assistant_turn

    content = "这是答案。[Agent used: grep_chunks(27ms), list_knowledge_chunks(16ms)]"
    msgs = _reconstruct_assistant_turn(content, None)
    assert msgs == [{"role": "assistant", "content": "这是答案。"}]
    # 工具名绝不出现在还原后的任何内容中
    assert "grep_chunks" not in json.dumps(msgs, ensure_ascii=False)


def test_reconstruct_turn_builds_structured_tool_calls():
    """带工具调用的历史应还原为 assistant(tool_calls) + tool 消息对"""
    from app.api.chat import _HISTORY_TOOL_OUTPUT_PLACEHOLDER, _reconstruct_assistant_turn

    steps = [
        {"type": "thought", "content": "先检索", "iteration": 0},
        {
            "type": "tool_call",
            "tool_name": "grep_chunks",
            "tool_call_id": "tc_1",
            "arguments": {"query": "反分裂国家法 第三条"},
            "iteration": 0,
        },
        {"type": "tool_result", "tool_call_id": "tc_1", "tool_name": "grep_chunks", "success": True},
        {"type": "final_answer", "content": "答案正文", "done": True},
    ]
    msgs = _reconstruct_assistant_turn("答案正文", steps)

    # 结构：assistant(tool_calls) → tool → assistant(final)
    assert len(msgs) == 3
    assistant_call, tool_msg, final_msg = msgs

    assert assistant_call["role"] == "assistant"
    assert assistant_call["content"] == "先检索"
    assert len(assistant_call["tool_calls"]) == 1
    tc = assistant_call["tool_calls"][0]
    assert tc["id"] == "tc_1"
    assert tc["function"]["name"] == "grep_chunks"
    # 参数序列化为 JSON 字符串，且保留非 ASCII 原文
    assert "反分裂国家法" in tc["function"]["arguments"]

    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "tc_1"
    assert tool_msg["content"] == _HISTORY_TOOL_OUTPUT_PLACEHOLDER

    assert final_msg == {"role": "assistant", "content": "答案正文"}


def test_reconstruct_turn_skips_final_answer_as_tool_call():
    """final_answer 是终止信号，不应作为中间工具调用重放"""
    from app.api.chat import _reconstruct_assistant_turn

    steps = [
        {
            "type": "tool_call",
            "tool_name": "final_answer",
            "tool_call_id": "tc_final",
            "arguments": {"answer": "x"},
            "iteration": 0,
        },
    ]
    msgs = _reconstruct_assistant_turn("最终答案", steps)
    # 只剩末尾 assistant 答案，没有任何 tool_calls 消息
    assert msgs == [{"role": "assistant", "content": "最终答案"}]


def test_reconstruct_turn_groups_parallel_calls_by_iteration():
    """同一 iteration 的多个工具调用应聚合到一条 assistant 消息"""
    from app.api.chat import _reconstruct_assistant_turn

    steps = [
        {"type": "tool_call", "tool_name": "grep_chunks", "tool_call_id": "a", "arguments": {}, "iteration": 0},
        {"type": "tool_call", "tool_name": "knowledge_search", "tool_call_id": "b", "arguments": {}, "iteration": 0},
        {"type": "tool_result", "tool_call_id": "a", "tool_name": "grep_chunks", "success": True},
        {"type": "tool_result", "tool_call_id": "b", "tool_name": "knowledge_search", "success": True},
    ]
    msgs = _reconstruct_assistant_turn("答案", steps)

    # assistant(2 tool_calls) + 2 tool + assistant(final)
    assert len(msgs) == 4
    assert len(msgs[0]["tool_calls"]) == 2
    assert msgs[1]["role"] == "tool" and msgs[2]["role"] == "tool"
    assert msgs[3] == {"role": "assistant", "content": "答案"}


def test_reconstruct_turn_multi_iteration_order_preserved():
    """跨多个 iteration 的调用应按 iteration 顺序还原"""
    from app.api.chat import _reconstruct_assistant_turn

    steps = [
        {"type": "tool_call", "tool_name": "grep_chunks", "tool_call_id": "a", "arguments": {}, "iteration": 0},
        {"type": "tool_result", "tool_call_id": "a", "tool_name": "grep_chunks", "success": True},
        {"type": "tool_call", "tool_name": "list_knowledge_chunks", "tool_call_id": "b", "arguments": {}, "iteration": 1},
        {"type": "tool_result", "tool_call_id": "b", "tool_name": "list_knowledge_chunks", "success": True},
    ]
    msgs = _reconstruct_assistant_turn("答案", steps)

    # assistant → tool → assistant → tool → assistant(final)
    assert len(msgs) == 5
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "grep_chunks"
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "list_knowledge_chunks"
    assert msgs[4] == {"role": "assistant", "content": "答案"}
