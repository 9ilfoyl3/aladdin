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
        return (
            "A tool for dynamic, reflective reasoning. Use it to plan your retrieval "
            "strategy, reflect on whether the information you have is sufficient, and "
            "decide your next action. Each thought can build on, question, or revise "
            "earlier ones.\n\n"
            "## Critical rules\n"
            "1. NEVER include the final answer in a thought. Thoughts are your private "
            "reasoning; they are shown in a separate 'thinking' panel, NOT as the answer. "
            "When your thinking is complete, you MUST call the `final_answer` tool to "
            "deliver the actual answer to the user.\n"
            "2. Write thoughts in natural language. Do NOT mention internal tool names "
            "(say '搜索知识库' not 'knowledge_search') or internal IDs.\n"
            "3. Set `next_thought_needed` to false ONLY when you are done reasoning and "
            "ready to either call a retrieval tool or call `final_answer`."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "thought": {
                    "type": "string",
                    "description": (
                        "Your current reasoning step (planning, reflection, analysis). "
                        "Natural language, no tool names or IDs. NEVER put the final "
                        "user-facing answer here — that goes in final_answer."
                    ),
                },
                "next_thought_needed": {
                    "type": "boolean",
                    "description": (
                        "True if you need another thinking step. False when reasoning is "
                        "complete and you are ready to call a retrieval tool or "
                        "final_answer next."
                    ),
                },
            },
            "required": ["thought", "next_thought_needed"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """记录思考内容到 AgentStep 并发射 THOUGHT 事件"""
        thought: str = args.get("thought", "")
        next_thought_needed = args.get("next_thought_needed", True)

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

        # 工具结果回传给模型时，显式提醒"想完了就去调 final_answer / 检索工具"，
        # 把 thinking → 收尾 的过渡固化进观察（observe）环节，减少模型停在思考、
        # 把答案当纯文本吐出的概率。
        if next_thought_needed:
            output = "Thought recorded. Continue reasoning, then act."
        else:
            output = (
                "Thought recorded. Your reasoning is complete — now take action: call a "
                "retrieval tool if you still need information, or call final_answer to "
                "deliver your complete answer to the user. Do NOT write the answer as "
                "plain text."
            )
        return ToolResult(success=True, output=output)
