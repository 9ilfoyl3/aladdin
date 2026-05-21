"""PaperChunker - 学术论文切分器

按学术论文章节标题（Abstract, Introduction, Methods/Methodology,
Results, Discussion, Conclusion, References）切分为父块，
每个章节内按段落切分为子块。

适用于：学术论文、研究报告、技术白皮书等。
"""

import re

from app.pipeline.chunker import ChunkResult
from app.pipeline.chunker_router import BaseChunker, ChunkerFactory


# 学术论文章节标题正则
# 匹配行首的章节标题，支持带编号（1. Introduction）和不带编号（Introduction）
_SECTION_PATTERN = re.compile(
    r'^(?:\d+\.?\s*)?'  # 可选的编号前缀：1. 或 1 或 无
    r'(Abstract'
    r'|Introduction'
    r'|Methods?|Methodology'
    r'|Materials?\s+and\s+Methods?'
    r'|Results?'
    r'|Discussion'
    r'|Conclusions?'
    r'|References'
    r'|Bibliography'
    r'|Acknowledgements?'
    r'|Appendix'
    r'|Related\s+Work'
    r'|Background'
    r'|Experiment(?:s|al)?(?:\s+(?:Setup|Results))?'
    r'|Evaluation'
    r'|Limitations?'
    r'|Future\s+Work'
    r')\s*$',
    re.MULTILINE | re.IGNORECASE,
)


class PaperChunker(BaseChunker):
    """学术论文切分器

    切分策略：
    - 父块：按学术论文章节标题切分（Abstract, Introduction, Methods 等）
    - 子块：每个章节内按段落（双换行）切分
    """

    def chunk(self, text: str, metadata: dict | None = None) -> ChunkResult:
        """将学术论文切分为父子 chunk

        Args:
            text: 学术论文文本
            metadata: 可选元数据

        Returns:
            ChunkResult: 父块为章节，子块为段落
        """
        if not text or not text.strip():
            return ChunkResult(parent_chunks=[], child_chunks=[], parent_child_map={})

        stripped = text.strip()

        # 按章节标题切分父块
        parent_chunks = self._split_into_sections(stripped)

        # 对每个父块切分子块
        child_chunks: list[str] = []
        parent_child_map: dict[int, list[int]] = {}

        for parent_idx, parent_text in enumerate(parent_chunks):
            children = self._split_into_paragraphs(parent_text)
            child_indices = []
            for child_text in children:
                child_indices.append(len(child_chunks))
                child_chunks.append(child_text)
            parent_child_map[parent_idx] = child_indices

        return ChunkResult(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            parent_child_map=parent_child_map,
        )

    def _split_into_sections(self, text: str) -> list[str]:
        """按学术论文章节标题切分为父块

        每个匹配到的章节标题开始一个新的父块。
        标题之前的内容（如论文标题、作者信息）作为第一个父块。
        """
        matches = list(_SECTION_PATTERN.finditer(text))

        if not matches:
            # 没有章节标题，整段作为一个父块
            return [text]

        sections: list[str] = []

        # 第一个标题之前的内容（论文标题、作者、摘要前信息）
        before = text[:matches[0].start()].strip()
        if before:
            sections.append(before)

        # 按章节标题切分
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].strip()
            if section:
                sections.append(section)

        return sections if sections else [text]

    def _split_into_paragraphs(self, text: str) -> list[str]:
        """将章节内容按段落切分为子块

        优先按双换行切分，若只有单段则按单换行切分非空行。
        如果切分后只有一个段落，直接返回整个章节作为唯一子块。
        """
        # 先尝试双换行切分
        paragraphs = [p.strip() for p in re.split(r'\n\n+', text) if p.strip()]

        if len(paragraphs) > 1:
            return paragraphs

        # 双换行切分只得到一段，尝试按单换行切分
        lines = [line.strip() for line in text.split('\n') if line.strip()]

        if len(lines) > 1:
            return lines

        # 只有一行/一段，直接返回
        return [text.strip()]


# 注册到 ChunkerFactory
ChunkerFactory.register("paper", PaperChunker)
