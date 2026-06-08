"""TableChunker - CSV/XLSX 表格数据切分器

处理已加载的表格文本（Markdown 表格格式或键值对格式），
按行分组为父块，每行作为子块。

适用于：CSV、XLSX 等表格类文件。
"""

import re

from app.pipeline.chunker import ChunkResult
from app.pipeline.chunker_router import BaseChunker, ChunkerFactory

# Markdown 表格行正则（以 | 开头和结尾）
_MD_TABLE_LINE_RE = re.compile(r'^\|.*\|$')

# Markdown 表格分隔行正则（如 | --- | --- |）
_MD_SEP_LINE_RE = re.compile(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)*\|$')

# 默认每组行数（父块包含的数据行数）
_DEFAULT_ROWS_PER_GROUP = 5


class TableChunker(BaseChunker):
    """表格数据切分器

    切分策略：
    - 父块：多行数据组成一组（带表头上下文）
    - 子块：每行数据单独作为一个子块

    支持两种输入格式：
    1. Markdown 表格格式（CsvLoader/XlsxLoader 窄行模式输出）
    2. 键值对格式（CsvLoader 宽行模式输出，以空行分隔）
    """

    def __init__(self, rows_per_group: int = _DEFAULT_ROWS_PER_GROUP):
        self.rows_per_group = max(1, rows_per_group)

    def chunk(self, text: str, metadata: dict | None = None) -> ChunkResult:
        """将表格文本切分为父子 chunk

        Args:
            text: 表格文本（Markdown 表格或键值对格式）
            metadata: 可选元数据

        Returns:
            ChunkResult: 父块为行组，子块为单行
        """
        if not text or not text.strip():
            return ChunkResult(parent_chunks=[], child_chunks=[], parent_child_map={})

        stripped = text.strip()

        # 判断输入格式
        if self._is_markdown_table(stripped):
            return self._chunk_markdown_table(stripped)
        else:
            return self._chunk_kv_format(stripped)

    def _is_markdown_table(self, text: str) -> bool:
        """判断文本是否为 Markdown 表格格式"""
        lines = text.split('\n')
        if len(lines) < 2:
            return False
        # 前两行都是 | 开头 | 结尾
        first_lines = [line.strip() for line in lines[:3] if line.strip()]
        return len(first_lines) >= 2 and all(
            _MD_TABLE_LINE_RE.match(line) for line in first_lines
        )

    def _chunk_markdown_table(self, text: str) -> ChunkResult:
        """切分 Markdown 表格格式的文本

        提取表头行和分隔行作为上下文，数据行按组切分。
        """
        lines = text.split('\n')

        # 提取表头和分隔行
        header_line = ""
        sep_line = ""
        data_lines: list[str] = []

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line:
                continue
            if not header_line and _MD_TABLE_LINE_RE.match(stripped_line):
                header_line = stripped_line
            elif not sep_line and _MD_SEP_LINE_RE.match(stripped_line):
                sep_line = stripped_line
            elif _MD_TABLE_LINE_RE.match(stripped_line):
                data_lines.append(stripped_line)

        if not data_lines:
            # 没有数据行，整段作为单个父块和子块
            return ChunkResult(
                parent_chunks=[text],
                child_chunks=[text],
                parent_child_map={0: [0]},
            )

        # 构建表头前缀（用于每个父块）
        header_prefix = ""
        if header_line:
            header_prefix = header_line
            if sep_line:
                header_prefix += "\n" + sep_line

        # 按组切分
        parent_chunks: list[str] = []
        child_chunks: list[str] = []
        parent_child_map: dict[int, list[int]] = {}

        for i in range(0, len(data_lines), self.rows_per_group):
            group = data_lines[i:i + self.rows_per_group]

            # 父块：表头 + 该组所有数据行
            if header_prefix:
                parent_text = header_prefix + "\n" + "\n".join(group)
            else:
                parent_text = "\n".join(group)

            parent_idx = len(parent_chunks)
            parent_chunks.append(parent_text)

            # 子块：每行数据单独作为子块（带表头上下文）
            child_indices = []
            for row_line in group:
                if header_prefix:
                    child_text = header_prefix + "\n" + row_line
                else:
                    child_text = row_line
                child_indices.append(len(child_chunks))
                child_chunks.append(child_text)

            parent_child_map[parent_idx] = child_indices

        return ChunkResult(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            parent_child_map=parent_child_map,
        )

    def _chunk_kv_format(self, text: str) -> ChunkResult:
        """切分键值对格式的文本

        键值对格式以空行分隔每条记录，每条记录作为子块，
        多条记录组成一组作为父块。
        """
        # 按空行分隔为独立记录
        records = [r.strip() for r in re.split(r'\n\n+', text) if r.strip()]

        if not records:
            return ChunkResult(
                parent_chunks=[text],
                child_chunks=[text],
                parent_child_map={0: [0]},
            )

        parent_chunks: list[str] = []
        child_chunks: list[str] = []
        parent_child_map: dict[int, list[int]] = {}

        for i in range(0, len(records), self.rows_per_group):
            group = records[i:i + self.rows_per_group]

            # 父块：该组所有记录
            parent_text = "\n\n".join(group)
            parent_idx = len(parent_chunks)
            parent_chunks.append(parent_text)

            # 子块：每条记录单独作为子块
            child_indices = []
            for record in group:
                child_indices.append(len(child_chunks))
                child_chunks.append(record)

            parent_child_map[parent_idx] = child_indices

        return ChunkResult(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            parent_child_map=parent_child_map,
        )


# 注册到 ChunkerFactory
ChunkerFactory.register("table", TableChunker)
