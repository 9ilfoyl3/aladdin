"""切片器 - 结构感知 + 父子 chunk 切分

切分优先级：
1. 文档结构标记（标题、条款编号、Markdown 标题等）
2. 段落边界（\n\n）
3. 句子边界（。！？.!?）
4. 强制字符切分（兜底）

采用层次化切分策略：
- 先按结构/段落边界切分为父块（~parent_size 字符）
- 再将每个父块细分为子块（~child_size 字符，带 overlap）
- 子块用于精准检索，父块用于上下文返回
"""

import re
from dataclasses import dataclass, field


# 结构化标记正则：中文条款编号、法律文书结构、Markdown 标题
_STRUCTURE_PATTERNS = [
    # 中文条款编号：一、二、三、... 或 （一）（二）...
    r'^[一二三四五六七八九十]+[、．.]',
    r'^（[一二三四五六七八九十]+）',
    r'^\([一二三四五六七八九十]+\)',
    # 阿拉伯数字编号：1. 2. 3. 或 1、2、3、
    r'^\d+[、．.\s]',
    # 法律文书常见结构关键词（行首）
    r'^(原告|被告|第三人|诉讼请求|事实与理由|事实和理由|证据目录|证据清单|判决如下|裁定如下|本院认为|经审理查明|审判长|审判员)',
    # Markdown 标题
    r'^#{1,6}\s+',
    # 带序号的标题格式：第一条、第二章等
    r'^第[一二三四五六七八九十百千\d]+[条章节款项]',
    # VL 模型特有标记（如 [Non-Text]、[Image]、[Figure] 等）
    r'^\[(?:Non-Text|Image|Figure|Chart|Table)\]',
]

_STRUCTURE_RE = re.compile('|'.join(f'(?:{p})' for p in _STRUCTURE_PATTERNS), re.MULTILINE)

# HTML 表格块正则（匹配完整的 <table>...</table>）
_TABLE_BLOCK_RE = re.compile(r'<table>.*?</table>', re.DOTALL)


@dataclass
class ChunkResult:
    """切分结果"""
    parent_chunks: list[str]                    # 大块，用于上下文返回
    child_chunks: list[str]                     # 小块，用于精准检索
    parent_child_map: dict[int, list[int]]      # 父→子映射 (parent_index -> [child_indices])


class HierarchicalChunker:
    """结构感知的父子 chunk 切分器"""

    def __init__(self, parent_size: int = 1500, child_size: int = 300, overlap: int = 50, min_child_size: int = 20):
        self.parent_size = parent_size
        self.child_size = child_size
        self.overlap = overlap
        self.min_child_size = min_child_size

    def chunk(self, text: str, metadata: dict = None) -> ChunkResult:
        """先按结构/语义边界切父块，再将父块细分为子块"""
        if not text or not text.strip():
            return ChunkResult(parent_chunks=[], child_chunks=[], parent_child_map={})

        stripped = text.strip()

        # 文本短于 child_size，直接作为单个父块和子块
        if len(stripped) <= self.child_size:
            return ChunkResult(
                parent_chunks=[stripped],
                child_chunks=[stripped],
                parent_child_map={0: [0]},
            )

        # 切分父块（优先结构标记，回退到段落边界）
        parent_chunks = self._split_parent_chunks(stripped)

        # 对每个父块切分子块，构建映射
        child_chunks: list[str] = []
        parent_child_map: dict[int, list[int]] = {}

        for parent_idx, parent_text in enumerate(parent_chunks):
            children = self._split_child_chunks(parent_text)
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

    def _split_parent_chunks(self, text: str) -> list[str]:
        """按结构标记和段落边界切分父块

        优先级：表格整块保护 > 结构标记 > 双换行段落 > 句子边界 > 强制切分
        """
        # 先将 <table>...</table> 块提取为独立段落，避免被切断
        segments = self._split_preserving_tables(text)

        result: list[str] = []
        for segment in segments:
            if segment.startswith("<table>"):
                # 表格块直接作为独立段落
                result.append(segment)
            else:
                # 非表格部分按原有逻辑切分
                has_structure = bool(_STRUCTURE_RE.search(segment))
                if has_structure:
                    result.extend(self._split_by_structure(segment))
                else:
                    result.extend(self._split_by_paragraphs(segment))

        # 合并过短的 section，拆分过长的 section
        return self._normalize_chunks(result)

    def _split_preserving_tables(self, text: str) -> list[str]:
        """将文本按 <table>...</table> 块拆分，保持表格完整性

        返回交替的 [普通文本, 表格块, 普通文本, ...] 列表
        """
        segments: list[str] = []
        last_end = 0

        for match in _TABLE_BLOCK_RE.finditer(text):
            # 表格前的普通文本
            before = text[last_end:match.start()].strip()
            if before:
                segments.append(before)
            # 表格块本身
            segments.append(match.group())
            last_end = match.end()

        # 最后一段普通文本
        after = text[last_end:].strip()
        if after:
            segments.append(after)

        return segments if segments else [text]

    def _split_by_structure(self, text: str) -> list[str]:
        """按结构标记切分，每个标记开始一个新段落"""
        lines = text.split('\n')
        sections: list[str] = []
        current_lines: list[str] = []

        for line in lines:
            stripped_line = line.strip()
            # 检测当前行是否是结构标记的开始
            if stripped_line and _STRUCTURE_RE.match(stripped_line):
                # 保存之前积累的内容
                if current_lines:
                    content = '\n'.join(current_lines).strip()
                    if content:
                        sections.append(content)
                current_lines = [line]
            else:
                current_lines.append(line)

        # 最后一段
        if current_lines:
            content = '\n'.join(current_lines).strip()
            if content:
                sections.append(content)

        return sections if sections else [text]

    def _split_by_paragraphs(self, text: str) -> list[str]:
        """按双换行分段（通用文档的默认策略）"""
        paragraphs = re.split(r'\n\n+', text)
        return [p.strip() for p in paragraphs if p.strip()]

    def _normalize_chunks(self, sections: list[str]) -> list[str]:
        """合并过短的段落，拆分过长的段落，确保每个父块在合理范围内"""
        chunks: list[str] = []
        current = ""

        for section in sections:
            if not section:
                continue

            # 当前段落本身超过 parent_size，需要拆分
            if len(section) > self.parent_size:
                # 先保存之前积累的内容
                if current:
                    chunks.append(current)
                    current = ""
                # 拆分超长段落
                sub_chunks = self._split_by_sentences(section, self.parent_size)
                chunks.extend(sub_chunks)
                continue

            # 尝试合并到当前块
            candidate = (current + "\n\n" + section) if current else section
            if len(candidate) <= self.parent_size:
                current = candidate
            else:
                # 当前块已满，保存并开始新块
                if current:
                    chunks.append(current)
                current = section

        if current:
            chunks.append(current)

        return chunks if chunks else sections

    def _split_by_sentences(self, text: str, max_size: int) -> list[str]:
        """按句子边界切分文本，确保每块不超过 max_size"""
        sentences = re.split(r'(?<=[。！？.!?\n])', text)
        chunks: list[str] = []
        current = ""

        for sent in sentences:
            if not sent:
                continue
            candidate = current + sent
            if len(candidate) <= max_size:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                if len(sent) > max_size:
                    chunks.extend(self._force_split(sent, max_size))
                    current = ""
                else:
                    current = sent

        if current.strip():
            chunks.append(current.strip())

        return chunks if chunks else [text]

    def _split_child_chunks(self, text: str) -> list[str]:
        """将父块切分为子块，保护表格完整性，优先按结构标记切分"""
        if len(text) <= self.child_size:
            return [text]

        # 如果包含表格，先按表格拆分保护
        if "<table>" in text:
            segments = self._split_preserving_tables(text)
            chunks: list[str] = []
            for segment in segments:
                if segment.startswith("<table>"):
                    # 表格块作为独立子块（即使超过 child_size 也不切断）
                    chunks.append(segment)
                elif len(segment) <= self.child_size:
                    chunks.append(segment)
                else:
                    chunks.extend(self._split_child_by_size(segment))
            chunks = self._merge_short_chunks(chunks)
            return chunks if chunks else [text]

        # 如果父块内有结构标记，先按结构切分
        has_structure = bool(_STRUCTURE_RE.search(text))
        if has_structure:
            sections = self._split_by_structure(text)
            # 对过长的 section 再按字符数切分
            chunks: list[str] = []
            for section in sections:
                if len(section) <= self.child_size:
                    chunks.append(section)
                else:
                    chunks.extend(self._split_child_by_size(section))
            chunks = self._merge_short_chunks(chunks)
            return chunks if chunks else [text]

        # 无结构标记，按字符数+句子边界切分
        return self._split_child_by_size(text)

    def _merge_short_chunks(self, chunks: list[str]) -> list[str]:
        """合并过短的子块到相邻块，避免产生无信息量的碎片"""
        if not chunks:
            return chunks

        merged: list[str] = []
        for chunk in chunks:
            if len(chunk) < self.min_child_size and merged:
                # 过短的块合并到前一个块
                merged[-1] = merged[-1] + "\n" + chunk
            else:
                merged.append(chunk)

        # 如果第一个块也过短，合并到后一个
        if len(merged) > 1 and len(merged[0]) < self.min_child_size:
            merged[1] = merged[0] + "\n" + merged[1]
            merged.pop(0)

        return merged

    def _split_child_by_size(self, text: str) -> list[str]:
        """按字符数切分子块，优先在句子边界断开，带 overlap"""
        if len(text) <= self.child_size:
            return [text]

        chunks: list[str] = []
        start = 0

        while start < len(text):
            end = start + self.child_size

            # 未到末尾时，尝试在句子边界处断开
            if end < len(text):
                search_start = max(start, end - 50)
                search_end = min(len(text), end + 50)
                segment = text[search_start:search_end]

                boundary = -1
                for match in re.finditer(r'[。！？.!?\n]', segment):
                    pos = search_start + match.end()
                    if pos >= start + self.child_size // 2:
                        boundary = pos
                        break

                if boundary > start:
                    end = boundary

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            if end >= len(text):
                break
            start = end - self.overlap

        return chunks if chunks else [text]

    def _force_split(self, text: str, max_size: int) -> list[str]:
        """强制按字符数切分（兜底方案）"""
        chunks = []
        for i in range(0, len(text), max_size):
            chunk = text[i:i + max_size].strip()
            if chunk:
                chunks.append(chunk)
        return chunks
