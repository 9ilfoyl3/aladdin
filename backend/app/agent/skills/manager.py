"""SkillManager - 技能管理器，实现 Progressive Disclosure 模式

Level 1: get_all_metadata() - 扫描所有技能目录，返回 name+description（轻量）
Level 2: load_skill(name) - 按需加载完整技能指令
"""

import logging
import os
from dataclasses import dataclass

from app.agent.skills.loader import Skill, load_skill_file

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"


@dataclass
class SkillMetadata:
    """技能元数据（Level 1 - 仅名称和描述）"""

    name: str
    description: str
    base_path: str


class SkillManager:
    """技能管理器 - Progressive Disclosure 三级加载"""

    def __init__(
        self,
        skill_dirs: list[str],
        allowed_skills: list[str] | None = None,
    ):
        """
        Args:
            skill_dirs: 技能搜索目录列表
            allowed_skills: 允许加载的技能名称白名单，None 表示全部允许
        """
        self.skill_dirs = skill_dirs
        self.allowed_skills = allowed_skills
        self._metadata_cache: list[SkillMetadata] = []
        self._skill_cache: dict[str, Skill] = {}

    def get_all_metadata(self) -> list[SkillMetadata]:
        """Level 1: 扫描 skill_dirs 中所有 SKILL.md，返回元数据列表

        扫描逻辑：遍历每个 skill_dir 下的子目录，查找 SKILL.md 文件，
        解析 frontmatter 提取 name 和 description。
        """
        if self._metadata_cache:
            return self._metadata_cache

        metadata_list: list[SkillMetadata] = []

        for skill_dir in self.skill_dirs:
            if not os.path.isdir(skill_dir):
                logger.warning(f"技能目录不存在: {skill_dir}")
                continue

            for entry in os.listdir(skill_dir):
                entry_path = os.path.join(skill_dir, entry)
                if not os.path.isdir(entry_path):
                    continue

                skill_file = os.path.join(entry_path, SKILL_FILENAME)
                if not os.path.isfile(skill_file):
                    continue

                try:
                    skill = load_skill_file(skill_file)
                except Exception as e:
                    logger.warning(f"解析技能文件失败 {skill_file}: {e}")
                    continue

                # 白名单过滤
                if self.allowed_skills is not None and skill.name not in self.allowed_skills:
                    continue

                metadata = SkillMetadata(
                    name=skill.name,
                    description=skill.description,
                    base_path=skill.base_path,
                )
                metadata_list.append(metadata)

                # 同时缓存完整 Skill 以避免重复解析
                self._skill_cache[skill.name] = skill

        self._metadata_cache = metadata_list
        logger.info(f"发现 {len(metadata_list)} 个技能")
        return metadata_list

    def load_skill(self, name: str) -> Skill:
        """Level 2: 按需加载指定技能的完整指令内容

        Args:
            name: 技能名称

        Returns:
            Skill 对象（包含 instructions）

        Raises:
            ValueError: 技能未找到
        """
        # 先检查缓存
        if name in self._skill_cache:
            return self._skill_cache[name]

        # 未缓存则搜索所有目录
        for skill_dir in self.skill_dirs:
            if not os.path.isdir(skill_dir):
                continue

            # 先尝试按目录名匹配
            direct_path = os.path.join(skill_dir, name, SKILL_FILENAME)
            if os.path.isfile(direct_path):
                try:
                    skill = load_skill_file(direct_path)
                    if self.allowed_skills is None or skill.name in self.allowed_skills:
                        self._skill_cache[skill.name] = skill
                        return skill
                except Exception as e:
                    logger.warning(f"加载技能失败 {direct_path}: {e}")

            # 遍历所有子目录查找匹配的 name
            for entry in os.listdir(skill_dir):
                entry_path = os.path.join(skill_dir, entry)
                if not os.path.isdir(entry_path):
                    continue

                skill_file = os.path.join(entry_path, SKILL_FILENAME)
                if not os.path.isfile(skill_file):
                    continue

                try:
                    skill = load_skill_file(skill_file)
                    if skill.name == name:
                        if self.allowed_skills is None or skill.name in self.allowed_skills:
                            self._skill_cache[skill.name] = skill
                            return skill
                except Exception:
                    continue

        raise ValueError(f"技能未找到: {name}")
