"""ToolRegistry 单元测试"""

import pytest

from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.registry import ToolRegistry


class FakeTool(BaseTool):
    """测试用工具"""

    @property
    def name(self) -> str:
        return "fake_tool"

    @property
    def description(self) -> str:
        return "A fake tool for testing"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(success=True, output=f"Result for: {args.get('query', '')}")


class FailingTool(BaseTool):
    """执行时抛异常的工具"""

    @property
    def name(self) -> str:
        return "failing_tool"

    @property
    def description(self) -> str:
        return "A tool that always fails"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(success=False, error="Something went wrong")


class TestToolRegistry:
    """ToolRegistry 测试"""

    def test_register_and_list_tools(self):
        registry = ToolRegistry()
        tool = FakeTool()
        registry.register(tool)

        assert registry.list_tools() == ["fake_tool"]

    def test_list_tools_empty(self):
        registry = ToolRegistry()
        assert registry.list_tools() == []

    def test_register_multiple_tools(self):
        registry = ToolRegistry()
        registry.register(FakeTool())
        registry.register(FailingTool())

        tools = registry.list_tools()
        assert "fake_tool" in tools
        assert "failing_tool" in tools
        assert len(tools) == 2

    def test_get_function_definitions_format(self):
        registry = ToolRegistry()
        registry.register(FakeTool())

        definitions = registry.get_function_definitions()

        assert len(definitions) == 1
        defn = definitions[0]
        assert defn["type"] == "function"
        assert defn["function"]["name"] == "fake_tool"
        assert defn["function"]["description"] == "A fake tool for testing"
        assert defn["function"]["parameters"] == {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
            },
            "required": ["query"],
        }

    def test_get_function_definitions_multiple(self):
        registry = ToolRegistry()
        registry.register(FakeTool())
        registry.register(FailingTool())

        definitions = registry.get_function_definitions()
        assert len(definitions) == 2

        names = [d["function"]["name"] for d in definitions]
        assert "fake_tool" in names
        assert "failing_tool" in names

    def test_get_function_definitions_empty(self):
        registry = ToolRegistry()
        assert registry.get_function_definitions() == []

    @pytest.mark.asyncio
    async def test_execute_existing_tool(self):
        registry = ToolRegistry()
        registry.register(FakeTool())

        result = await registry.execute("fake_tool", {"query": "hello"})

        assert result.success is True
        assert result.output == "Result for: hello"

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self):
        registry = ToolRegistry()

        result = await registry.execute("nonexistent", {})

        assert result.success is False
        assert "Tool 'nonexistent' not found" in result.error

    @pytest.mark.asyncio
    async def test_execute_failing_tool(self):
        registry = ToolRegistry()
        registry.register(FailingTool())

        result = await registry.execute("failing_tool", {})

        assert result.success is False
        assert "Something went wrong" in result.error

    def test_register_overwrites_same_name(self):
        """同名工具注册时后者覆盖前者"""
        registry = ToolRegistry()
        registry.register(FakeTool())
        registry.register(FakeTool())  # 重复注册

        assert len(registry.list_tools()) == 1
