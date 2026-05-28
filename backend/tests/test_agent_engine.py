"""AgentEngine 核心模块单元测试

测试 ReAct 循环、重试机制、stuck loop 检测、graceful degradation、
ToolRegistry、EventBus 等核心组件。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.models.provider import ChatResponse, LLMToolCall


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
        mock_llm.chat_with_tools = AsyncMock(
            return_value=make_final_answer_response("这是最终答案")
        )

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

        mock_llm.chat_with_tools = AsyncMock(
            side_effect=[search_response, final_response]
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
        """Mock LLM 连续返回相同 content 且无 tool_calls → 验证 stuck_loop 终止"""
        mock_llm = AsyncMock()

        # 连续 3 次返回相同内容（无 tool_calls, finish_reason != "stop"）
        # 注意：_MAX_REPEATED_RESPONSES = 2，需要 3 次相同内容触发
        # 但 natural_stop 会在 finish_reason=="stop" 时先触发
        # 所以用 finish_reason="length" 来避免 natural_stop
        same_response = ChatResponse(
            content="重复内容",
            tool_calls=[],
            finish_reason="length",
        )

        mock_llm.chat_with_tools = AsyncMock(return_value=same_response)

        config = AgentConfig(max_iterations=10, system_prompt="Test")
        engine, event_bus, registry = create_engine(mock_llm, config=config)

        state = await engine.execute("session-1", "测试问题")

        assert state.is_complete is True
        assert state.final_answer == "重复内容"

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
        mock_llm.chat_with_tools = AsyncMock(return_value=tool_response)

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

        # 前 2 次抛出瞬态错误，第 3 次成功
        mock_llm.chat_with_tools = AsyncMock(
            side_effect=[
                RuntimeError("HTTP 429 Too Many Requests"),
                RuntimeError("HTTP 429 Too Many Requests"),
                make_final_answer_response("重试后的答案"),
            ]
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
        mock_llm.chat_with_tools = AsyncMock(
            side_effect=[
                make_empty_response(),
                make_final_answer_response("nudge 后的答案"),
            ]
        )

        # 空响应的 finish_reason 需要不是 "stop" 才不会触发 natural_stop
        # 但 make_empty_response 返回 content="" 且 tool_calls=[]
        # 在 engine 中，空响应检测是 not response.content and not response.tool_calls
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

        mock_llm.chat_with_tools = AsyncMock(
            side_effect=[search_response, final_response]
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

        mock_llm.chat_with_tools = AsyncMock(
            side_effect=[parallel_response, final_response]
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
