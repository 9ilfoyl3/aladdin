"""Chunker 策略路由 - 根据文件类型和内容特征自动选择最优切分策略

提供：
- BaseChunker 抽象基类
- ChunkerFactory 工厂（注册/实例化）
- ChunkerRouter 路由器（规则优先级匹配）
- ChunkResult 数据结构（复用 chunker.py 中的定义）
"""

import re
from abc import ABC, abstractmethod

# 复用现有 chunker.py 中的 ChunkResult 定义
from app.pipeline.chunker import ChunkResult

__all__ = ["BaseChunker", "ChunkerFactory", "ChunkerRouter", "ChunkResult"]


class BaseChunker(ABC):
    """切分器抽象基类"""

    @abstractmethod
    def chunk(self, text: str, metadata: dict | None = None) -> ChunkResult:
        """将文本切分为父子 chunk

        Args:
            text: 待切分的文本内容
            metadata: 可选的元数据（如文件类型、来源等）

        Returns:
            ChunkResult: 包含 parent_chunks、child_chunks 和 parent_child_map
        """
        ...


class ChunkerFactory:
    """Chunker 工厂，管理注册和实例化"""

    REGISTRY: dict[str, type[BaseChunker]] = {}

    @classmethod
    def register(cls, name: str, chunker_cls: type[BaseChunker]) -> None:
        """注册 Chunker 类型"""
        cls.REGISTRY[name] = chunker_cls

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseChunker:
        """根据类型名创建 Chunker 实例"""
        if name not in cls.REGISTRY:
            raise ValueError(f"Unknown chunker type: {name}")
        return cls.REGISTRY[name](**kwargs)


class ChunkerRouter:
    """根据文件类型和内容特征选择 Chunker"""

    # 法律关键词正则
    _LAW_PATTERN = re.compile(r'本院认为|判决如下|第[一二三四五六七八九十\d]+条')

    # QA 配对正则
    _QA_PATTERN = re.compile(r'(?:Q:|A:|问:|答:)')

    @classmethod
    def select(cls, file_type: str, content: str) -> str:
        """返回 chunker 类型名称

        优先级：
        1. csv/xlsx → table
        2. 法律关键词 ≥ 3 → laws
        3. Abstract + References/Bibliography → paper
        4. QA 配对 ≥ 10 次匹配（5 对） → qa
        5. 其他 → naive
        """
        # 优先级 1：表格文件
        if file_type in ("csv", "xlsx"):
            return "table"
        # 优先级 2：法律文书
        if len(cls._LAW_PATTERN.findall(content)) >= 3:
            return "laws"
        # 优先级 3：学术论文
        if "Abstract" in content and ("References" in content or "Bibliography" in content):
            return "paper"
        # 优先级 4：QA 格式
        if len(cls._QA_PATTERN.findall(content)) >= 10:  # 5 对 = 10 次匹配
            return "qa"
        # 默认
        return "naive"
