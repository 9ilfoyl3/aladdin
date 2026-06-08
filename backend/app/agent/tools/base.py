"""BaseTool 抽象接口和 ToolResult 数据结构"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool
    output: str = ""
    data: dict[str, Any] | None = None
    error: str = ""


class BaseTool(ABC):
    """工具基类 - 所有 Agent 工具必须继承此类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于注册和调用"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，展示给 LLM 用于决策"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """工具参数的 JSON Schema 定义"""
        ...

    @abstractmethod
    async def execute(self, args: dict) -> ToolResult:
        """执行工具逻辑，返回 ToolResult"""
        ...
