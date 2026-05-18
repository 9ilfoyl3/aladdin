"""富化器 - 摘要/关键词生成

初期作为 pass-through 实现，后续可接入 LLM 生成摘要和关键词。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.provider import LLMProvider


class Enricher:
    """Chunk 富化器，为文本块添加摘要/关键词等元信息

    初期实现为直通模式（不做任何处理），后续可启用 LLM 生成摘要。
    """

    def __init__(self, llm: LLMProvider | None = None, enabled: bool = False):
        self.llm = llm
        self.enabled = enabled

    async def enrich(self, chunks: list[str]) -> list[str]:
        """富化 chunk（摘要/关键词），初期直接返回原文

        Args:
            chunks: 待富化的文本块列表

        Returns:
            富化后的文本块列表（当前直接返回原文）
        """
        if not self.enabled or self.llm is None:
            return chunks
        # TODO: 后续实现摘要/关键词生成
        return chunks
