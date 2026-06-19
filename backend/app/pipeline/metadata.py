"""Chunk 元数据提取模块

提供 ChunkMetadata dataclass 和 MetadataExtractor 类，
在文档入库时自动为每个 child chunk 提取结构化元数据。
"""

from __future__ import annotations

import bisect
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
    # 注意：「第X条」不作为标题层级（与 chunker._NONMD_HEADING_PATTERNS 保持一致）。
    # 法条「第X条　正文…」条号与正文同行，若当标题会把整段正文吸入 section_path，
    # 污染 context_header。与 WeKnora ChineseChapterPattern（仅 章/节/節/部分/篇）对齐。
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

# 章节标题文字长度上限。超过此长度的「标题」实为「序号 + 正文同行」的正文行
# （法条款项、条文等），不应进入 section_path，否则污染 breadcrumb。
# 取 40：章/节名与常规短标题远低于此值，正文行远高于此值，区分度足够。
_MAX_HEADING_LEN = 40


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

        # 判断是否需要章节路径提取（CSV 预切分等场景跳过，避免 O(n²) 全文扫描）
        # 当每个 parent 只有一个 child 且数量 > 100 时，认为是预切分场景
        skip_section_path = (
            len(parent_chunks) > 100
            and all(len(children) == 1 for children in parent_child_map.values())
        )

        # 拼接全文用于章节路径提取（仅在需要时）
        full_text = ""
        heading_index: list[tuple[int, int, str]] = []
        path_snapshots: list[list[str]] = []
        if not skip_section_path and parent_chunks:
            full_text = "\n".join(parent_chunks)
            # 预扫描全文所有标题（位置/层级/标题文本），只做一次。
            # 旧实现对每个子块都重新 finditer 全文 → 子块数 × 全文 的 O(n²)，
            # 在超长文档（如百万字小说切出数千子块）上会卡死。提到循环外预建
            # 有序标题表后，每个子块仅需一次定位 + 二分查找。
            heading_index = self._build_heading_index(full_text)
            # 进一步预计算「每个标题位置处的层级路径快照」，只折叠一次（O(m)）。
            # 子块定位后直接二分取最近快照即可（O(1)），避免对每个子块重新折叠
            # 其前面的全部标题（旧版 O(子块数 × 标题数)，在超多章节文档上仍慢）。
            path_snapshots = self._build_path_snapshots(heading_index)

        metadata_list: list[ChunkMetadata] = []
        for child_idx, child_text in enumerate(child_chunks):
            # 定位页码（仅 PDF 且有 page_texts 时）
            page_num = None
            if file_type == "pdf" and page_texts:
                page_num = self._detect_page_num(child_text, page_texts)

            # 提取章节路径（预切分场景跳过）
            section_path = []
            if not skip_section_path and full_text:
                section_path = self._section_path_from_index(
                    child_text, full_text, heading_index, path_snapshots
                )

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

    def _build_heading_index(self, full_text: str) -> list[tuple[int, int, str]]:
        """预扫描全文，构建按位置升序的标题表 [(position, level, title), ...]。

        只在 extract() 中对整篇文档调用一次。把原本"每个子块各扫一遍全文"的
        O(子块数 × 全文) 降为一次全文扫描，杜绝超长文档的 O(n²) 卡死。

        Args:
            full_text: 完整文档文本

        Returns:
            按 position 升序排列的 (position, level, title) 列表
        """
        if not full_text:
            return []

        headings: list[tuple[int, int, str]] = []
        for level, pattern in _HEADING_PATTERNS:
            for match in pattern.finditer(full_text):
                title = match.group(0).strip()
                # 长度护栏：真正的章节标题都很短（章/节名、短编号标题）。若匹配到的
                # 标题文字过长，说明这是「序号 + 正文同行」的正文行（如法条款项
                # 「（二）在保险公司被撤销时…救济；」），不应作为章节标题，否则会把
                # 整段正文灌入 section_path 污染 breadcrumb。跳过即可（与「第X条」
                # 不进标题层级同理，是对所有标题模式的通用防御）。
                if len(title) > _MAX_HEADING_LEN:
                    continue
                headings.append((match.start(), level, title))

        headings.sort(key=lambda x: x[0])
        return headings

    def _build_path_snapshots(
        self, heading_index: list[tuple[int, int, str]]
    ) -> list[list[str]]:
        """预计算每个标题位置处的「层级路径快照」。

        ``snapshots[i]`` 表示依次应用 ``heading_index[0..i]`` 后的层级路径
        （高层级标题重置更低层级）。只折叠一次，O(m)。子块定位后取
        ``snapshots[cut-1]`` 即得其章节路径，无需对每个子块重新折叠。

        与旧版逐块折叠的产出**逐项等价**：折叠规则（高层级 reset 低层级、
        按层级升序输出）完全一致，只是把重复折叠提取为一次前缀扫描。

        Args:
            heading_index: 按 position 升序的 (position, level, title) 列表

        Returns:
            与 heading_index 等长的路径快照列表
        """
        snapshots: list[list[str]] = []
        level_titles: dict[int, str] = {}
        for _pos, level, title in heading_index:
            level_titles[level] = title
            # 出现某层级标题时，清除所有更低层级（数字更大）的标题
            keys_to_remove = [k for k in level_titles if k > level]
            for k in keys_to_remove:
                del level_titles[k]
            # 按层级从高到低（数字从小到大）排列输出，与旧实现一致
            sorted_levels = sorted(level_titles.keys())
            snapshots.append([level_titles[lv] for lv in sorted_levels])
        return snapshots

    def _section_path_from_index(
        self,
        chunk_content: str,
        full_text: str,
        heading_index: list[tuple[int, int, str]],
        path_snapshots: list[list[str]],
    ) -> list[str]:
        """基于预建标题表 + 路径快照，计算 chunk 所属的章节标题路径。

        定位 chunk 在全文中的位置后，用二分取出该位置之前最后一个标题的
        路径快照（O(1)）。定位规则与旧实现一致（chunk 前 50 字符 anchor +
        ``full_text.find``），保证逐块产出完全相同。

        Args:
            chunk_content: chunk 文本内容
            full_text: 完整文档文本（用于定位 chunk 位置）
            heading_index: _build_heading_index 预建的有序标题表
            path_snapshots: _build_path_snapshots 预建的路径快照

        Returns:
            章节标题路径列表，如 ["第三章", "第二节"]
        """
        if not heading_index or not chunk_content:
            return []

        anchor = chunk_content[:50].strip()
        if not anchor:
            return []

        chunk_pos = full_text.find(anchor)
        if chunk_pos == -1:
            return []

        # 二分定位：cut = position < chunk_pos 的标题数量（heading_index 已按 position 升序）。
        # 该 chunk 的章节路径 = 应用前 cut 个标题后的快照，即 snapshots[cut-1]。
        cut = bisect.bisect_left(heading_index, (chunk_pos,))
        if cut <= 0:
            return []
        return list(path_snapshots[cut - 1])

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
