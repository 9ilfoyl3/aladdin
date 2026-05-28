"""Agent 状态数据结构：AgentState / AgentStep / ToolCallRecord"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.tools.base import ToolResult
    from app.retrieval.base import RetrievalResult


@dataclass
class ToolCallRecord:
    """单次工具调用记录"""

    id: str
    name: str
    args: dict[str, Any]
    result: ToolResult | None = None
    duration_ms: int = 0


@dataclass
class AgentStep:
    """Agent 单轮执行步骤"""

    iteration: int
    thought: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class AgentState:
    """Agent 执行状态"""

    current_round: int = 0
    steps: list[AgentStep] = field(default_factory=list)
    is_complete: bool = False
    final_answer: str = ""
    knowledge_refs: list[RetrievalResult] = field(default_factory=list)
    seen_chunk_ids: set[str] = field(default_factory=set)
