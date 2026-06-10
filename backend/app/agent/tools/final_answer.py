"""FinalAnswerTool - 提交最终答案工具"""

from __future__ import annotations

from app.agent.events import EventBus
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
        return (
            "Submit your final answer to the user. This is the ONLY channel through which "
            "your response reaches the user — anything you write as plain text is treated "
            "as internal reasoning and is NOT shown as the answer.\n\n"
            "## When to use\n"
            "Call this as your LAST action once you have gathered enough information "
            "(via retrieval / reading / thinking) to answer. Synthesize everything into one "
            "complete, well-formatted response and submit it here.\n\n"
            "## Mandatory rules\n"
            "1. NEVER end your turn without calling this tool — even for greetings, "
            "acknowledgements, or follow-ups you can answer from context.\n"
            "2. NEVER write the user-facing answer as plain text and then stop. If you find "
            "yourself typing the answer (names, lists, conclusions, '答案是…', '原告是…'), "
            "STOP and put it in this tool's `answer` field instead.\n"
            "3. The `answer` field must contain your COMPLETE response with all citations, "
            "structure, and formatting. Never call it with an empty `answer`.\n"
            "4. This is always the last tool you call."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": (
                        "Your COMPLETE, user-facing final answer in Markdown. Include all "
                        "content, citations ([1], [2]…), structure, and formatting. This is "
                        "the only text the user sees — do not assume they can read your "
                        "reasoning or thinking."
                    ),
                }
            },
            "required": ["answer"],
        }

    async def execute(self, args: dict) -> ToolResult:
        # 注意：在 AgentEngine 的 ReAct 循环中，final_answer 调用会被
        # _analyze_response 拦截并由引擎统一发射 FINAL_ANSWER 事件（含 done 标记）。
        # 本 execute() 不再发射任何事件，避免与引擎重复发 done；仅作为兜底，
        # 保证 state 一致并返回成功结果。
        answer = args.get("answer", "")
        self._state.final_answer = answer
        self._state.is_complete = True
        return ToolResult(success=True, output="Answer submitted")
