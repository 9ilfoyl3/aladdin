"""AgentEngine 核心模块单元测试

测试 ReAct 循环、重试机制、stuck loop 检测、graceful degradation、
ToolRegistry、EventBus 等核心组件。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine, _redact_history_kb_results
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.models.provider import ChatResponse, LLMToolCall, StreamChunk


# ============================================================
# Fixtures & Helpers
# ============================================================


class MockTool(BaseTool):
    """测试用工具"""

    def __init__(self, name: str = "mock_tool", delay: float = 0):
        self._name = name
        self._delay = delay
        self.call_count = 0
        self.last_args: dict = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Mock tool: {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, args: dict) -> ToolResult:
        self.call_count += 1
        self.last_args = args
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return ToolResult(success=True, output=f"Result from {self._name}")


def make_final_answer_response(answer: str) -> ChatResponse:
    """构造包含 final_answer tool_call 的 ChatResponse"""
    return ChatResponse(
        content="",
        tool_calls=[
            LLMToolCall(
                id="call_final",
                function_name="final_answer",
                arguments=json.dumps({"answer": answer}),
            )
        ],
        finish_reason="tool_calls",
    )


def make_tool_call_response(
    tool_name: str, args: dict, call_id: str = "call_1", content: str = ""
) -> ChatResponse:
    """构造包含工具调用的 ChatResponse"""
    return ChatResponse(
        content=content,
        tool_calls=[
            LLMToolCall(
                id=call_id,
                function_name=tool_name,
                arguments=json.dumps(args),
            )
        ],
        finish_reason="tool_calls",
    )


def make_text_response(content: str, finish_reason: str = "stop") -> ChatResponse:
    """构造纯文本 ChatResponse（无 tool_calls）"""
    return ChatResponse(
        content=content,
        tool_calls=[],
        finish_reason=finish_reason,
    )


def make_empty_response() -> ChatResponse:
    """构造空响应"""
    return ChatResponse(content="", tool_calls=[], finish_reason="stop")


async def _async_iter_chunks(chunks: list[StreamChunk]):
    """将 StreamChunk 列表转为 async iterator"""
    for chunk in chunks:
        yield chunk


def _response_to_chunks(response: ChatResponse) -> list[StreamChunk]:
    """将 ChatResponse 转为 StreamChunk 列表（用于 mock stream_with_tools）"""
    chunks: list[StreamChunk] = []
    if response.content:
        chunks.append(StreamChunk(
            content=response.content,
            response_type="content",
        ))
    if response.tool_calls:
        # final_answer 工具的 content 用 "answer" response_type
        for tc in response.tool_calls:
            if tc.function_name == "final_answer":
                try:
                    args = json.loads(tc.arguments)
                    answer = args.get("answer", "")
                except (json.JSONDecodeError, TypeError):
                    answer = tc.arguments
                chunks.append(StreamChunk(
                    content=answer,
                    response_type="answer",
                ))
        chunks.append(StreamChunk(
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
        ))
    else:
        chunks.append(StreamChunk(
            finish_reason=response.finish_reason,
        ))
    return chunks


def mock_stream_with_tools_from_responses(responses: list[ChatResponse]):
    """创建 stream_with_tools 的 mock，按顺序返回 responses 对应的 stream chunks"""
    call_count = [0]

    async def _stream_side_effect(messages, tools, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(responses):
            resp = responses[idx]
        else:
            # 超出预期调用次数时返回最后一个
            resp = responses[-1]
        chunks = _response_to_chunks(resp)
        async for chunk in _async_iter_chunks(chunks):
            yield chunk

    return _stream_side_effect


def mock_stream_with_tools_single(response: ChatResponse):
    """创建 stream_with_tools 的 mock，始终返回同一个 response"""
    async def _stream_side_effect(messages, tools, **kwargs):
        chunks = _response_to_chunks(response)
        async for chunk in _async_iter_chunks(chunks):
            yield chunk

    return _stream_side_effect


def mock_stream_with_tools_error_then_success(
    errors: list[Exception], success_response: ChatResponse
):
    """创建 stream_with_tools 的 mock，先抛出错误再成功"""
    call_count = [0]

    async def _stream_side_effect(messages, tools, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(errors):
            raise errors[idx]
        chunks = _response_to_chunks(success_response)
        async for chunk in _async_iter_chunks(chunks):
            yield chunk

    return _stream_side_effect


def create_engine(
    mock_llm: AsyncMock,
    config: AgentConfig | None = None,
    tools: list[BaseTool] | None = None,
) -> tuple[AgentEngine, EventBus, ToolRegistry]:
    """创建 AgentEngine 实例及其依赖"""
    if config is None:
        config = AgentConfig(max_iterations=5, system_prompt="Test system prompt")
    event_bus = EventBus()
    registry = ToolRegistry()
    if tools:
        for tool in tools:
            registry.register(tool)
    engine = AgentEngine(
        config=config,
        llm=mock_llm,
        tool_registry=registry,
        event_bus=event_bus,
    )
    return engine, event_bus, registry


# ============================================================
# AgentEngine Tests
# ============================================================


class TestAgentEngineSimple:
    """AgentEngine 基本场景测试"""

    @pytest.mark.asyncio
    async def test_simple_final_answer(self):
        """Mock LLM 第一轮返回 final_answer → 验证 state 正确设置"""
        mock_llm = AsyncMock()
        responses = [make_final_answer_response("这是最终答案")]
        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(responses)

        engine, event_bus, registry = create_engine(mock_llm)

        state = await engine.execute("session-1", "你好")

        assert state.is_complete is True
        assert state.final_answer == "这是最终答案"
        assert len(state.steps) == 1

    @pytest.mark.asyncio
    async def test_multi_iteration_search_then_answer(self):
        """Mock LLM 先返回 knowledge_search 调用，再返回 final_answer → 验证 2 步"""
        mock_llm = AsyncMock()

        # 第一轮：调用 knowledge_search
        search_response = make_tool_call_response(
            "knowledge_search",
            {"queries": ["test query"]},
            call_id="call_search_1",
        )
        # 第二轮：返回 final_answer
        final_response = make_final_answer_response("基于检索结果的答案")

        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(
            [search_response, final_response]
        )

        # 注册 knowledge_search mock 工具
        search_tool = MockTool(name="knowledge_search")
        engine, event_bus, registry = create_engine(mock_llm, tools=[search_tool])

        state = await engine.execute("session-1", "搜索问题")

        assert state.is_complete is True
        assert state.final_answer == "基于检索结果的答案"
        assert len(state.steps) == 2
        assert search_tool.call_count == 1

    @pytest.mark.asyncio
    async def test_stuck_loop_detection(self):
        """REQ-7: 连续 3 轮相同 content 且无 tool call 时自动终止并返回最后内容"""
        mock_llm = AsyncMock()

        # 连续返回相同内容（无 tool_calls, finish_reason="length" 避免 natural_stop）
        # _STUCK_LOOP_THRESHOLD = 3，需要连续 3 轮相同 content 触发
        same_response = ChatResponse(
            content="重复内容",
            tool_calls=[],
            finish_reason="length",
        )

        mock_llm.stream_with_tools = mock_stream_with_tools_single(same_response)

        config = AgentConfig(max_iterations=10, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config)

        state = await engine.execute("session-1", "测试问题")

        assert state.is_complete is True
        assert state.final_answer == "重复内容"

    @pytest.mark.asyncio
    async def test_stuck_loop_resets_on_tool_call(self):
        """REQ-7: 有 tool call 时重置 stuck loop 计数器，不会误触发"""
        mock_llm = AsyncMock()

        # 第 1 轮：相同 content（无 tool_calls）
        text_resp_1 = ChatResponse(content="重复内容", tool_calls=[], finish_reason="length")
        # 第 2 轮：有 tool_call → 重置计数器
        tool_resp = make_tool_call_response("knowledge_search", {"queries": ["q"]}, call_id="call_1")
        # 第 3-5 轮：又连续 3 轮相同 content → 触发 stuck loop
        text_resp_2 = ChatResponse(content="新的重复内容", tool_calls=[], finish_reason="length")

        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(
            [text_resp_1, tool_resp, text_resp_2, text_resp_2, text_resp_2]
        )

        search_tool = MockTool(name="knowledge_search")
        config = AgentConfig(max_iterations=10, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config, tools=[search_tool])

        state = await engine.execute("session-1", "测试问题")

        assert state.is_complete is True
        assert state.final_answer == "新的重复内容"
        # 工具被调用了 1 次（第 2 轮）
        assert search_tool.call_count == 1

    @pytest.mark.asyncio
    async def test_stuck_loop_not_triggered_with_different_content(self):
        """REQ-7: 不同 content 不触发 stuck loop"""
        mock_llm = AsyncMock()

        # 每轮返回不同内容，不应触发 stuck loop
        responses = [
            ChatResponse(content=f"内容{i}", tool_calls=[], finish_reason="length")
            for i in range(5)
        ]
        # 最后一轮返回 final_answer
        responses.append(make_final_answer_response("最终答案"))

        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(responses)

        config = AgentConfig(max_iterations=10, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config)

        state = await engine.execute("session-1", "测试问题")

        assert state.is_complete is True
        assert state.final_answer == "最终答案"

    @pytest.mark.asyncio
    async def test_max_iterations_graceful_degradation(self):
        """Mock LLM 始终返回 tool_calls（非 final_answer）→ 验证 max_iterations 后合成答案"""
        mock_llm = AsyncMock()

        # 每轮都返回工具调用（非 final_answer）
        tool_response = make_tool_call_response(
            "knowledge_search",
            {"queries": ["query"]},
            call_id="call_1",
        )
        mock_llm.stream_with_tools = mock_stream_with_tools_single(tool_response)

        # 合成答案时需要 stream 方法
        async def mock_stream(messages, **kwargs):
            yield "合成的答案"
        mock_llm.stream = mock_stream

        search_tool = MockTool(name="knowledge_search")
        config = AgentConfig(max_iterations=3, system_prompt="Test")
        engine, event_bus, registry = create_engine(
            mock_llm, config=config, tools=[search_tool]
        )

        state = await engine.execute("session-1", "测试问题")

        assert state.is_complete is True
        # max_iterations 耗尽后调用 _synthesize_final_answer
        assert state.final_answer != ""
        assert search_tool.call_count == 3

    @pytest.mark.asyncio
    async def test_transient_error_retry(self):
        """Mock LLM 前 2 次抛出 HTTP 429 错误，第 3 次成功 → 验证重试机制"""
        mock_llm = AsyncMock()

        # 使用 error_then_success mock
        mock_llm.stream_with_tools = mock_stream_with_tools_error_then_success(
            errors=[
                RuntimeError("HTTP 429 Too Many Requests"),
                RuntimeError("HTTP 429 Too Many Requests"),
            ],
            success_response=make_final_answer_response("重试后的答案"),
        )

        config = AgentConfig(max_iterations=5, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config)

        # Patch asyncio.sleep 避免实际等待
        with patch("app.agent.engine.asyncio.sleep", new_callable=AsyncMock):
            state = await engine.execute("session-1", "测试重试")

        assert state.is_complete is True
        assert state.final_answer == "重试后的答案"

    @pytest.mark.asyncio
    async def test_empty_response_nudge(self):
        """Mock LLM 第一次返回空响应，第二次正常 → 验证 nudge 消息追加"""
        mock_llm = AsyncMock()

        # 第一次空响应，第二次正常
        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses([
            make_empty_response(),
            make_final_answer_response("nudge 后的答案"),
        ])

        config = AgentConfig(max_iterations=5, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config)

        state = await engine.execute("session-1", "测试 nudge")

        assert state.is_complete is True
        assert state.final_answer == "nudge 后的答案"

    @pytest.mark.asyncio
    async def test_event_bus_emissions(self):
        """验证 TOOL_CALL, TOOL_RESULT, FINAL_ANSWER 事件按正确顺序发射"""
        mock_llm = AsyncMock()

        # 第一轮：工具调用
        search_response = make_tool_call_response(
            "knowledge_search",
            {"queries": ["test"]},
            call_id="call_1",
        )
        # 第二轮：final_answer
        final_response = make_final_answer_response("最终答案")

        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(
            [search_response, final_response]
        )

        search_tool = MockTool(name="knowledge_search")
        config = AgentConfig(max_iterations=5, system_prompt="Test")
        engine, event_bus, registry = create_engine(
            mock_llm, config=config, tools=[search_tool]
        )

        # 收集所有事件
        events: list[AgentEvent] = []

        async def collect_events(event: AgentEvent):
            events.append(event)

        event_bus.on(None, collect_events)

        state = await engine.execute("session-1", "测试事件")

        # 验证事件顺序
        event_types = [e.type for e in events]
        assert EventType.TOOL_CALL in event_types
        assert EventType.TOOL_RESULT in event_types
        assert EventType.FINAL_ANSWER in event_types

        # TOOL_CALL 应在 TOOL_RESULT 之前
        tc_idx = event_types.index(EventType.TOOL_CALL)
        tr_idx = event_types.index(EventType.TOOL_RESULT)
        fa_idx = event_types.index(EventType.FINAL_ANSWER)
        assert tc_idx < tr_idx
        assert tr_idx < fa_idx

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self):
        """parallel_tool_calls=True 时，多个工具调用并行执行"""
        mock_llm = AsyncMock()

        # 返回 2 个并行工具调用
        parallel_response = ChatResponse(
            content="",
            tool_calls=[
                LLMToolCall(
                    id="call_a",
                    function_name="tool_a",
                    arguments=json.dumps({"query": "a"}),
                ),
                LLMToolCall(
                    id="call_b",
                    function_name="tool_b",
                    arguments=json.dumps({"query": "b"}),
                ),
            ],
            finish_reason="tool_calls",
        )
        final_response = make_final_answer_response("并行结果")

        mock_llm.stream_with_tools = mock_stream_with_tools_from_responses(
            [parallel_response, final_response]
        )

        # 创建带延迟的工具来验证并行执行
        tool_a = MockTool(name="tool_a", delay=0.1)
        tool_b = MockTool(name="tool_b", delay=0.1)

        config = AgentConfig(
            max_iterations=5,
            parallel_tool_calls=True,
            system_prompt="Test",
        )
        engine, event_bus, registry = create_engine(
            mock_llm, config=config, tools=[tool_a, tool_b]
        )

        import time

        start = time.time()
        state = await engine.execute("session-1", "并行测试")
        elapsed = time.time() - start

        assert state.is_complete is True
        assert tool_a.call_count == 1
        assert tool_b.call_count == 1
        # 并行执行时，总时间应接近单个工具的延迟（0.1s），而非串行的 0.2s
        # 给一些余量
        assert elapsed < 0.3


# ============================================================
# ToolRegistry Tests (补充 test_tool_registry.py 中未覆盖的场景)
# ============================================================


class TestToolRegistryExtended:
    """ToolRegistry 扩展测试"""

    def test_register_and_get_definitions(self):
        """注册工具后能获取正确的 function definitions"""
        registry = ToolRegistry()
        tool = MockTool(name="test_tool")
        registry.register(tool)

        defs = registry.get_function_definitions()
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        assert defs[0]["function"]["name"] == "test_tool"

    @pytest.mark.asyncio
    async def test_execute_returns_result(self):
        """执行已注册工具返回正确结果"""
        registry = ToolRegistry()
        tool = MockTool(name="test_tool")
        registry.register(tool)

        result = await registry.execute("test_tool", {"query": "hello"})
        assert result.success is True
        assert "test_tool" in result.output

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self):
        """执行未注册工具返回错误"""
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {})
        assert result.success is False
        assert "not found" in result.error

    def test_get_function_definitions_empty(self):
        """空注册表返回空列表"""
        registry = ToolRegistry()
        assert registry.get_function_definitions() == []


# ============================================================
# EventBus Tests
# ============================================================


class TestEventBus:
    """EventBus 事件总线测试"""

    @pytest.mark.asyncio
    async def test_on_and_emit(self):
        """注册 handler 后 emit 事件能正确触发"""
        bus = EventBus()
        received: list[AgentEvent] = []

        async def handler(event: AgentEvent):
            received.append(event)

        bus.on(EventType.TOOL_CALL, handler)

        event = AgentEvent(
            type=EventType.TOOL_CALL,
            session_id="s1",
            data={"tool_name": "test"},
        )
        await bus.emit(event)

        assert len(received) == 1
        assert received[0].type == EventType.TOOL_CALL
        assert received[0].data["tool_name"] == "test"

    @pytest.mark.asyncio
    async def test_global_handler(self):
        """全局 handler（event_type=None）接收所有事件"""
        bus = EventBus()
        received: list[AgentEvent] = []

        async def global_handler(event: AgentEvent):
            received.append(event)

        bus.on(None, global_handler)

        await bus.emit(AgentEvent(type=EventType.TOOL_CALL, session_id="s1"))
        await bus.emit(AgentEvent(type=EventType.FINAL_ANSWER, session_id="s1"))
        await bus.emit(AgentEvent(type=EventType.ERROR, session_id="s1"))

        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_type_specific_handler(self):
        """类型特定 handler 只接收匹配类型的事件"""
        bus = EventBus()
        tool_events: list[AgentEvent] = []
        answer_events: list[AgentEvent] = []

        async def tool_handler(event: AgentEvent):
            tool_events.append(event)

        async def answer_handler(event: AgentEvent):
            answer_events.append(event)

        bus.on(EventType.TOOL_CALL, tool_handler)
        bus.on(EventType.FINAL_ANSWER, answer_handler)

        await bus.emit(AgentEvent(type=EventType.TOOL_CALL, session_id="s1"))
        await bus.emit(AgentEvent(type=EventType.FINAL_ANSWER, session_id="s1"))
        await bus.emit(AgentEvent(type=EventType.ERROR, session_id="s1"))

        assert len(tool_events) == 1
        assert len(answer_events) == 1

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_type(self):
        """同一类型可注册多个 handler，按顺序调用"""
        bus = EventBus()
        order: list[int] = []

        async def handler_1(event: AgentEvent):
            order.append(1)

        async def handler_2(event: AgentEvent):
            order.append(2)

        bus.on(EventType.THOUGHT, handler_1)
        bus.on(EventType.THOUGHT, handler_2)

        await bus.emit(AgentEvent(type=EventType.THOUGHT, session_id="s1"))

        assert order == [1, 2]

    @pytest.mark.asyncio
    async def test_emit_no_handlers(self):
        """没有注册 handler 时 emit 不报错"""
        bus = EventBus()
        event = AgentEvent(type=EventType.COMPLETE, session_id="s1")
        # 不应抛出异常
        await bus.emit(event)

    @pytest.mark.asyncio
    async def test_type_specific_before_global(self):
        """类型特定 handler 先于全局 handler 调用"""
        bus = EventBus()
        order: list[str] = []

        async def specific_handler(event: AgentEvent):
            order.append("specific")

        async def global_handler(event: AgentEvent):
            order.append("global")

        bus.on(EventType.TOOL_RESULT, specific_handler)
        bus.on(None, global_handler)

        await bus.emit(AgentEvent(type=EventType.TOOL_RESULT, session_id="s1"))

        assert order == ["specific", "global"]


# ============================================================
# _redact_history_kb_results Tests
# ============================================================


class TestRedactHistoryKbResults:
    """_redact_history_kb_results 历史 KB 结果脱敏测试"""

    def test_empty_messages(self):
        """空消息列表返回空列表"""
        result = _redact_history_kb_results([])
        assert result == []

    def test_no_tool_messages(self):
        """没有 tool 消息时原样返回"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = _redact_history_kb_results(messages)
        assert result == messages

    def test_redacts_historical_knowledge_search(self):
        """历史轮次的 knowledge_search 结果被脱敏"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "大量检索结果..."},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_2", "content": "当前轮次结果"},
        ]
        result = _redact_history_kb_results(messages)

        # 第一轮（历史）应被脱敏
        assert result[1]["content"] == "[Previous retrieval omitted — please perform a fresh search.]"
        # 最后一轮（当前）保持原样
        assert result[3]["content"] == "当前轮次结果"

    def test_redacts_historical_grep_chunks(self):
        """历史轮次的 grep_chunks 结果被脱敏"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "grep_chunks", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "grep 结果..."},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "final_answer", "arguments": "{}"}}
            ], "content": None},
        ]
        result = _redact_history_kb_results(messages)

        # 第一轮的 grep_chunks 应被脱敏（最后一组是 final_answer 的迭代）
        assert result[1]["content"] == "[Previous retrieval omitted — please perform a fresh search.]"

    def test_preserves_non_kb_tool_results(self):
        """非 KB 工具的结果不被脱敏"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "other_tool", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "其他工具结果"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_2", "content": "当前检索结果"},
        ]
        result = _redact_history_kb_results(messages)

        # 非 KB 工具不脱敏
        assert result[1]["content"] == "其他工具结果"
        # 当前轮次 KB 工具不脱敏
        assert result[3]["content"] == "当前检索结果"

    def test_preserves_current_iteration_kb_results(self):
        """最后一组迭代的 KB 结果不被脱敏"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}},
                {"id": "call_2", "function": {"name": "grep_chunks", "arguments": "{}"}},
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "结果1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "结果2"},
        ]
        result = _redact_history_kb_results(messages)

        # 只有一组迭代（最后一组），不脱敏
        assert result[1]["content"] == "结果1"
        assert result[2]["content"] == "结果2"

    def test_skips_malformed_messages(self):
        """格式异常的消息跳过脱敏，保持原样"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "历史结果"},
            # 格式异常：tool_calls 不是列表
            {"role": "assistant", "tool_calls": "invalid"},
            {"role": "tool", "tool_call_id": "call_2", "content": "当前结果"},
        ]
        result = _redact_history_kb_results(messages)

        # 第一轮被脱敏（因为有第二个 assistant 消息，虽然格式异常但 iteration 仍递增不了）
        # 实际上格式异常的 assistant 消息会被跳过，所以只有 1 个迭代
        # call_1 属于迭代 0，也是最后一个迭代，所以不脱敏
        assert result[1]["content"] == "历史结果"
        assert result[3]["content"] == "当前结果"

    def test_handles_missing_tool_call_id(self):
        """tool 消息缺少 tool_call_id 时保持原样"""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "content": "没有 tool_call_id 的消息"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_2", "content": "当前结果"},
        ]
        result = _redact_history_kb_results(messages)

        # 缺少 tool_call_id 的消息无法匹配到任何工具，保持原样
        assert result[1]["content"] == "没有 tool_call_id 的消息"
        # 当前轮次不脱敏
        assert result[3]["content"] == "当前结果"

    def test_multiple_historical_iterations(self):
        """多个历史迭代都被脱敏，只保留最后一组"""
        messages = [
            # 迭代 0
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "第一轮结果"},
            # 迭代 1
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "grep_chunks", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_2", "content": "第二轮结果"},
            # 迭代 2（最后一组）
            {"role": "assistant", "tool_calls": [
                {"id": "call_3", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_3", "content": "第三轮结果"},
        ]
        result = _redact_history_kb_results(messages)

        marker = "[Previous retrieval omitted — please perform a fresh search.]"
        # 迭代 0 和 1 被脱敏
        assert result[1]["content"] == marker
        assert result[3]["content"] == marker
        # 迭代 2（最后一组）保持原样
        assert result[5]["content"] == "第三轮结果"

    def test_does_not_mutate_original(self):
        """脱敏不修改原始消息列表"""
        original_content = "原始检索结果"
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": original_content},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "knowledge_search", "arguments": "{}"}}
            ], "content": None},
            {"role": "tool", "tool_call_id": "call_2", "content": "当前结果"},
        ]
        _redact_history_kb_results(messages)

        # 原始消息不应被修改
        assert messages[1]["content"] == original_content
