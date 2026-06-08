"""read_skill 工具 - LLM 按需读取技能指令（Level 2 加载）"""

from app.agent.skills.manager import SkillManager
from app.agent.tools.base import BaseTool, ToolResult


class ReadSkillTool(BaseTool):
    """读取技能内容工具 - 允许 Agent 按需加载技能的完整指令"""

    def __init__(self, skill_manager: SkillManager):
        self._skill_manager = skill_manager

    @property
    def name(self) -> str:
        return "read_skill"

    @property
    def description(self) -> str:
        return (
            "按需读取技能的完整指令内容。当用户请求匹配某个可用技能的描述时，"
            "调用此工具加载该技能的详细操作指南。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "要读取的技能名称",
                },
            },
            "required": ["skill_name"],
        }

    async def execute(self, args: dict) -> ToolResult:
        skill_name = args.get("skill_name", "")
        if not skill_name:
            return ToolResult(
                success=False,
                error="skill_name 参数不能为空",
            )

        try:
            skill = self._skill_manager.load_skill(skill_name)
        except ValueError as e:
            return ToolResult(
                success=False,
                error=str(e),
            )

        return ToolResult(
            success=True,
            output=skill.instructions,
            data={
                "skill_name": skill.name,
                "description": skill.description,
            },
        )
