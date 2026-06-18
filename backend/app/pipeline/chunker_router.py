"""Chunker 策略路由 - 根据文件类型和内容特征自动选择最优切分策略

提供：
- BaseChunker 抽象基类
- ChunkerFactory 工厂（注册/实例化）
- ChunkerRouter 路由器（规则优先级匹配）
- ChunkResult 数据结构（复用 chunker.py 中的定义）
"""

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
    """根据文件类型选择 Chunker。

    设计（对齐 WeKnora ``internal/infrastructure/chunker``）：
    WeKnora 不做"按文档体裁（法律/论文/QA）猜类型"的脆弱路由——那种基于
    关键词绝对计数的猜测在超长文本上必然误判（例如一本几百万字的小说里
    "第X条"会出现成百上千次，被误判成法律文书，再用无 size 上限的体裁切分器
    切出几万字的巨块，最终问答时撑爆模型上下文）。

    取而代之，WeKnora 用"结构特征 profiling + 策略链(heading/heuristic/legacy)
    + 质量校验回退 + 绝对大小护栏"统一处理所有普通文本。这套逻辑在 aladdin 中
    由 :class:`~app.pipeline.chunker.HierarchicalChunker`（即 ``naive``）承载。

    因此本路由只区分两类：
    - ``csv``/``xlsx``：loader 已做结构化预切分，交给 ``table`` 专用处理；
    - 其它一切：统一交给结构感知的 ``naive``，由其内部 profiler 自动选择
      heading / heuristic / legacy 策略，并受绝对大小护栏保护。

    法律/论文/QA 等体裁切分器仍注册在 :class:`ChunkerFactory` 中，供用户在
    知识库 config 里**手动**指定 ``chunker_type`` 时使用，但不再参与自动路由。
    """

    @classmethod
    def select(cls, file_type: str, content: str) -> str:
        """返回 chunker 类型名称。

        - csv/xlsx → table（结构化表格，loader 预切分）
        - 其它一律 → naive（结构感知切分，内部自动选择策略 + 大小护栏）

        Args:
            file_type: 文件扩展名（小写，无点）
            content: 文档文本内容（保留入参以兼容调用方签名，当前不参与判定）
        """
        if file_type in ("csv", "xlsx"):
            return "table"
        return "naive"
