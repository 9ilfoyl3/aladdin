# Agent Skills 技能扩展模块

import os

from app.agent.skills.loader import Skill, load_skill_file
from app.agent.skills.manager import SkillManager, SkillMetadata

__all__ = [
    "Skill",
    "SkillManager",
    "SkillMetadata",
    "load_skill_file",
    "default_skill_dirs",
]


def default_skill_dirs() -> list[str]:
    """返回内置技能搜索目录列表（当前仅预置技能目录 preloaded/）。

    预置技能随代码发布，位于本包下的 preloaded/ 子目录。后续如需支持用户自定义
    技能目录，在此追加即可，调用方无需改动。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    preloaded = os.path.join(base, "preloaded")
    return [preloaded]
