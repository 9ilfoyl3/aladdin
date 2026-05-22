"""Chunk 元数据提取模块

提供 ChunkMetadata dataclass 和 MetadataExtractor 类，
在文档入库时自动为每个 child chunk 提取结构化元数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# 标题正则模式，按层级从高到低排列
# 每个元素为 (level, compiled_regex)
# level 越小表示层级越高（如 level=1 是章级，level=2 是节级）
_HEADING_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    # Markdown 标题: #### Title (level 4) - 必须在 ### 之前匹配
    (4, re.compile(r"^####[^\S\n]+(.+)$", re.MULTILINE)),
    # Markdown 标题: ### Title (level 3)
    (3, re.compile(r"^###[^\S\n]+(.+)$", re.MULTILINE)),
    # Markdown 标题: ## Title (level 2)
    (2, re.compile(r"^##[^\S\n]+(.+)$", re.MULTILINE)),
    # Markdown 标题: # Title (level 1)
    (1, re.compile(r"^#[^\S\n]+(.+)$", re.MULTILINE)),
    # 中文章节: 第X章, 第X编 (后面可跟空格+标题文字 或 行尾)
    (1, re.compile(
        r"^[^\S\n]*(第[一二三四五六七八九十百千\d]+[章编])(?:[^\S\n]+\S[^\n]*)?$",
        re.MULTILINE,
    )),
    # 中文节: 第X节, 第X部分
    (2, re.compile(
        r"^[^\S\n]*(第[一二三四五六七八九十百千\d]+[节部]分?)(?:[^\S\n]+\S[^\n]*)?$",
        re.MULTILINE,
    )),
    # 中文条: 第X条
    (3, re.compile(
        r"^[^\S\n]*(第[一二三四五六七八九十百千\d]+条)(?:[^\S\n]+\S[^\n]*)?$",
        re.MULTILINE,
    )),
    # 中文数字序号: 一、 二、 三、
    (2, re.compile(r"^[^\S\n]*([一二三四五六七八九十]+)、([^\n]+)$", re.MULTILINE)),
    # 带括号中文序号: （一） （二）
    (3, re.compile(r"^[^\S\n]*[（\(]([一二三四五六七八九十]+)[）\)]([^\n]+)$", re.MULTILINE)),
    # 数字编号: 1.1.1 (三级) - 必须在 1.1 和 1. 之前匹配
    (4, re.compile(r"^[^\S\n]*(\d+\.\d+\.\d+)[^\S\n]+([^\n]+)$", re.MULTILINE)),
    # 数字编号: 1.1 1.2 (二级) - 必须在 1. 之前匹配
    (3, re.compile(r"^[^\S\n]*(\d+\.\d+)[^\S\n]+([^\n]+)$", re.MULTILINE)),
    # 数字编号: 1. 2. 3. (顶级)
    (2, re.compile(r"^[^\S\n]*(\d+)\.[^\S\n]+([^\n]+)$", re.MULTILINE)),
    # 数字加括号: 1） 2） 3）
    (4, re.compile(r"^[^\S\n]*(\d+)[）\)][^\S\n]*([^\n]+)$", re.MULTILINE)),
]


@dataclass
class ChunkMetadata:
    """Chunk 元数据，入库时自动提取"""

    filename: str
    file_type: str
    chunker_type: str
    chunk_index: int
    page_num: Optional[int] = None
    section_path: list[str] = field(default_factory=list)
    element_type: str = "text"


class MetadataExtractor:
    """元数据提取器 - 从切分结果中提取结构化信息"""

    def extract(
        self,
        child_chunks: list[str],
        parent_chunks: list[str],
        parent_child_map: dict[int, list[int]],
        doc_metadata: dict,
        page_texts: list[str] | None = None,
    ) -> list[ChunkMetadata]:
        """为每个 child chunk 生成元数据

        Args:
            child_chunks: 子块文本列表
            parent_chunks: 父块文本列表
            parent_child_map: 父块索引 -> 子块索引列表的映射
            doc_metadata: 文档级元数据，包含 filename, file_type 等
            page_texts: 按页顺序的文本列表（PDF 专用，用于页码定位）

        Returns:
            与 child_chunks 等长的 ChunkMetadata 列表
        """
        filename = doc_metadata.get("filename", "")
        file_type = doc_metadata.get("file_type", "")
        chunker_type = doc_metadata.get("chunker_type", "hierarchical")

        # 拼接全文用于章节路径提取
        full_text = "\n".join(parent_chunks) if parent_chunks else ""

        metadata_list: list[ChunkMetadata] = []
        for child_idx, child_text in enumerate(child_chunks):
            # 定位页码（仅 PDF 且有 page_texts 时）
            page_num = None
            if file_type == "pdf" and page_texts:
                page_num = self._detect_page_num(child_text, page_texts)

            # 提取章节路径
            section_path = self._extract_section_path(child_text, full_text)

            # 判断元素类型
            element_type = self._detect_element_type(child_text)

            metadata_list.append(
                ChunkMetadata(
                    filename=filename,
                    file_type=file_type,
                    chunker_type=chunker_type,
                    chunk_index=child_idx,
                    page_num=page_num,
                    section_path=section_path,
                    element_type=element_type,
                )
            )

        return metadata_list

    def _detect_page_num(
        self, chunk_content: str, page_texts: list[str]
    ) -> int | None:
        """根据 chunk 前50字符在 page_texts 中定位页码

        取 chunk 前50字符作为定位锚点，在所有页面文本中查找，
        返回最早出现该锚点的页码（从1开始）。

        Args:
            chunk_content: chunk 文本内容
            page_texts: 按页顺序的文本列表

        Returns:
            页码（从1开始），无法定位时返回 None
        """
        # 取 chunk 前50字符作为定位锚点
        anchor = chunk_content[:50].strip()
        if not anchor:
            return None

        best_page: int | None = None
        best_pos = float("inf")

        for page_idx, page_text in enumerate(page_texts):
            pos = page_text.find(anchor)
            if pos != -1 and pos < best_pos:
                best_pos = pos
                best_page = page_idx + 1  # 页码从1开始

        return best_page

    def _extract_section_path(
        self, chunk_content: str, full_text: str
    ) -> list[str]:
        """提取 chunk 所属的章节标题路径

        基于正则匹配在 full_text 中扫描所有标题，找到 chunk 位置之前的
        标题层级结构，构建从高到低的章节路径。

        Args:
            chunk_content: chunk 文本内容
            full_text: 完整文档文本

        Returns:
            章节标题路径列表，如 ["第三章", "第二节"]
        """
        if not full_text or not chunk_content:
            return []

        # 用 chunk 前50字符定位 chunk 在 full_text 中的位置
        anchor = chunk_content[:50].strip()
        if not anchor:
            return []

        chunk_pos = full_text.find(anchor)
        if chunk_pos == -1:
            return []

        # 扫描 full_text 中所有标题及其位置和层级
        headings: list[tuple[int, int, str]] = []  # (position, level, title)

        for level, pattern in _HEADING_PATTERNS:
            for match in pattern.finditer(full_text):
                pos = match.start()
                # 只关注 chunk 位置之前的标题
                if pos >= chunk_pos:
                    continue
                # 提取标题文本
                title = match.group(0).strip()
                headings.append((pos, level, title))

        if not headings:
            return []

        # 按位置排序
        headings.sort(key=lambda x: x[0])

        # 构建层级路径：高层级标题会重置低层级
        # 用 dict 记录每个层级当前的标题
        level_titles: dict[int, str] = {}

        for _pos, level, title in headings:
            level_titles[level] = title
            # 当出现某层级标题时，清除所有更低层级（数字更大）的标题
            keys_to_remove = [k for k in level_titles if k > level]
            for k in keys_to_remove:
                del level_titles[k]

        # 按层级从高到低排列输出
        if not level_titles:
            return []

        sorted_levels = sorted(level_titles.keys())
        return [level_titles[lv] for lv in sorted_levels]

    def _detect_element_type(self, chunk_content: str) -> str:
        """识别 chunk 的元素类型: text/table/title

        Table 检测: markdown 表格 (|...|...|)、tab 分隔列、CSV 类多分隔符行
        Title 检测: 短文本 (<100字符) 且匹配 _HEADING_PATTERNS 中的标题模式
        默认返回 "text"

        Args:
            chunk_content: chunk 文本内容

        Returns:
            元素类型字符串: "text", "table", 或 "title"
        """
        content = chunk_content.strip()
        if not content:
            return "text"

        lines = content.split('\n')
        non_empty_lines = [l for l in lines if l.strip()]

        # Table detection: check if majority of lines contain table-like patterns
        if non_empty_lines:
            table_line_count = 0
            for line in non_empty_lines:
                stripped = line.strip()
                # Markdown table: |col1|col2| or separator ---
                if '|' in stripped and stripped.count('|') >= 2:
                    table_line_count += 1
                # Tab-separated (3+ tabs)
                elif stripped.count('\t') >= 2:
                    table_line_count += 1

            # If >50% of lines look like table rows, it's a table
            if table_line_count > len(non_empty_lines) * 0.5:
                return "table"

        # Title detection: short text matching heading patterns
        if len(content) < 100 and len(non_empty_lines) <= 2:
            for _level, pattern in _HEADING_PATTERNS:
                if pattern.match(content):
                    return "title"

        return "text"
