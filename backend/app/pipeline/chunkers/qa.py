"""QAChunker - 问答对格式切分器

解析 Q:/A: 或 问:/答: 配对，每个 Q+A 配对作为父块，Q 和 A 分别作为子块。
"""

import re

from app.pipeline.chunker import ChunkResult
from app.pipeline.chunker_router import BaseChunker, ChunkerFactory

# 匹配 Q:/A: 或 问:/答: 开头的行（支持全角/半角冒号）
_QA_PAIR_PATTERN = re.compile(
    r'(?:^|\n)\s*(?:Q|问)\s*[：:]\s*(.*?)(?=\n\s*(?:A|答)\s*[：:])'
    r'\n\s*(?:A|答)\s*[：:]\s*(.*?)(?=\n\s*(?:Q|问)\s*[：:]|\Z)',
    re.DOTALL,
)


class QAChunker(BaseChunker):
    """问答对切分器

    解析文本中的 Q:/A: 或 问:/答: 配对：
    - 每个 Q+A 配对的完整文本作为父块
    - Q 部分和 A 部分分别作为子块（每个父块对应 2 个子块）
    """

    def chunk(self, text: str, metadata: dict | None = None) -> ChunkResult:
        """将文本按 QA 配对切分为父子 chunk"""
        if not text or not text.strip():
            return ChunkResult(parent_chunks=[], child_chunks=[], parent_child_map={})

        pairs = _QA_PAIR_PATTERN.findall(text)

        if not pairs:
            # 没有匹配到 QA 配对，整段文本作为单个父块和子块
            stripped = text.strip()
            return ChunkResult(
                parent_chunks=[stripped],
                child_chunks=[stripped],
                parent_child_map={0: [0]},
            )

        parent_chunks: list[str] = []
        child_chunks: list[str] = []
        parent_child_map: dict[int, list[int]] = {}

        for question, answer in pairs:
            q_text = question.strip()
            a_text = answer.strip()

            if not q_text and not a_text:
                continue

            # 父块：Q+A 组合文本
            parent_text = f"Q: {q_text}\nA: {a_text}"
            parent_idx = len(parent_chunks)
            parent_chunks.append(parent_text)

            # 子块：Q 和 A 分别作为子块
            q_child_idx = len(child_chunks)
            child_chunks.append(q_text)

            a_child_idx = len(child_chunks)
            child_chunks.append(a_text)

            parent_child_map[parent_idx] = [q_child_idx, a_child_idx]

        # 如果所有配对都是空的，回退
        if not parent_chunks:
            stripped = text.strip()
            return ChunkResult(
                parent_chunks=[stripped],
                child_chunks=[stripped],
                parent_child_map={0: [0]},
            )

        return ChunkResult(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            parent_child_map=parent_child_map,
        )


# 注册到 ChunkerFactory
ChunkerFactory.register("qa", QAChunker)
