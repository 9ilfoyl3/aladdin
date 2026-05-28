"""thinking 工具 - 内部思考/规划/反思

内部思考工具，用于规划检索策略、反思已有信息是否充分、分析下一步行动。
输出不会展示给用户（前端可选择不渲染 THOUGHT 事件）。
"""

from __future__ import annotations

from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult


class ThinkingTool(BaseTool):
    """内部思考工具

    LLM 调用此工具进行显式的规划和反思，替代独立的 Reflector 模块。
    思考内容记录到当前 AgentStep.thought 字段，并通过 EventBus 发射 THOUGHT 事件。
    """

    def __init__(self, state: AgentState, event_bus: EventBus, session_id: str):
        self._state = state
        self._event_bus = event_bus
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "thinking"

    @property
    def description(self) -> str:
        return "内部思考工具。用于规划检索策略、反思已有信息是否充分、分析下一步行动。输出不会展示给用户。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "thought": {
                    "type": "string",
                    "description": "思考内容：规划、反思、分析等",
                }
            },
            "required": ["thought"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """记录思考内容到 AgentStep 并发射 THOUGHT 事件"""
        thought: str = args.get("thought", "")

        if not thought:
            return ToolResult(success=False, error="thought parameter is required")

        # 记录到当前步骤的 thought 字段
        if self._state.steps:
            current_step = self._state.steps[-1]
            # 追加思考内容（同一步骤可能多次思考）
            if current_step.thought:
                current_step.thought += "\n" + thought
            else:
                current_step.thought = thought

        # 通过 EventBus 发射 THOUGHT 事件
        await self._event_bus.emit(
            AgentEvent(
                type=EventType.THOUGHT,
                session_id=self._session_id,
                data={"content": thought},
            )
        )

        return ToolResult(success=True, output="Thought recorded")
