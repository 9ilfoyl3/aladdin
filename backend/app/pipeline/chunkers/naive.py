"""NaiveChunker - 通用文本切分器

包装现有 HierarchicalChunker，作为默认的通用切分策略。
适用于：无特殊结构的普通文本文档。
"""

from app.pipeline.chunker import ChunkResult, HierarchicalChunker
from app.pipeline.chunker_router import BaseChunker, ChunkerFactory


class NaiveChunker(BaseChunker):
    """通用文本切分器

    内部委托给 HierarchicalChunker，使用结构感知 + 父子 chunk 切分策略。
    """

    def __init__(self, parent_size: int = 2500, child_size: int = 450, overlap: int = 70):
        self._chunker = HierarchicalChunker(
            parent_size=parent_size,
            child_size=child_size,
            overlap=overlap,
        )

    def chunk(self, text: str, metadata: dict | None = None) -> ChunkResult:
        """将文本切分为父子 chunk

        Args:
            text: 待切分的文本内容
            metadata: 可选的元数据

        Returns:
            ChunkResult: 包含 parent_chunks、child_chunks 和 parent_child_map
        """
        return self._chunker.chunk(text, metadata)


# 注册到 ChunkerFactory
ChunkerFactory.register("naive", NaiveChunker)
