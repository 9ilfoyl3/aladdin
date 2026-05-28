"""Skill 文件加载器 - 解析 SKILL.md 文件"""

from dataclasses import dataclass

import yaml


@dataclass
class Skill:
    """完整技能数据（Level 2 - 包含指令内容）"""

    name: str
    description: str
    instructions: str
    base_path: str


def load_skill_file(path: str) -> Skill:
    """解析 SKILL.md 文件，提取 YAML frontmatter 和 body 指令内容

    SKILL.md 格式：
    ---
    name: skill-name
    description: 技能描述
    ---

    # 指令内容（Markdown body）
    ...
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 YAML frontmatter
    stripped = content.strip()
    if not stripped.startswith("---"):
        raise ValueError(f"SKILL.md 必须以 YAML frontmatter (---) 开头: {path}")

    # 找到第二个 --- 分隔符
    second_marker = stripped.find("---", 3)
    if second_marker == -1:
        raise ValueError(f"SKILL.md frontmatter 未正确关闭 (缺少第二个 ---): {path}")

    frontmatter_str = stripped[3:second_marker].strip()
    body = stripped[second_marker + 3:].strip()

    # 解析 YAML
    frontmatter = yaml.safe_load(frontmatter_str)
    if not isinstance(frontmatter, dict):
        raise ValueError(f"SKILL.md frontmatter 必须是有效的 YAML 映射: {path}")

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not name:
        raise ValueError(f"SKILL.md frontmatter 缺少 name 字段: {path}")
    if not description:
        raise ValueError(f"SKILL.md frontmatter 缺少 description 字段: {path}")

    # 从路径推断 base_path（SKILL.md 所在目录）
    import os

    base_path = os.path.dirname(os.path.abspath(path))

    return Skill(
        name=name,
        description=description,
        instructions=body,
        base_path=base_path,
    )
