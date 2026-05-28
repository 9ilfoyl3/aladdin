"""FinalAnswerTool - 提交最终答案工具"""

from __future__ import annotations

from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult


class FinalAnswerTool(BaseTool):
    """提交最终答案的工具

    当 Agent 收集到足够信息后，调用此工具提交最终答案。
    注意：AgentEngine._analyze_response 会在 execute() 之前拦截 final_answer 调用，
    此 execute() 实现作为 fallback 保证完整性。
    """

    def __init__(self, state: AgentState, event_bus: EventBus, session_id: str):
        self._state = state
        self._event_bus = event_bus
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "final_answer"

    @property
    def description(self) -> str:
        return "提交最终答案。当你已经收集到足够的信息来回答用户问题时，调用此工具提交你的最终答案。答案应该完整、准确、结构清晰。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "最终答案内容，支持 Markdown 格式",
                }
            },
            "required": ["answer"],
        }

    async def execute(self, args: dict) -> ToolResult:
        answer = args.get("answer", "")
        self._state.final_answer = answer
        self._state.is_complete = True
        await self._event_bus.emit(
            AgentEvent(
                type=EventType.FINAL_ANSWER,
                session_id=self._session_id,
                data={"content": answer},
                done=True,
            )
        )
        return ToolResult(success=True, output="Answer submitted")
