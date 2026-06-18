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

TODO: [架构] 实现多 chunker 策略路由，根据文件类型 + 内容特征自动选择最优切分策略：
  - TableChunker: CSV/XLSX 表格数据（已通过 loader pre_chunked 实现）
  - LawsChunker: 法律文书（按条款、判决结构切分）
  - PaperChunker: 学术论文（按 Abstract/Section/References 切分）
  - QAChunker: 问答对格式
  - NaiveChunker: 通用文本（当前 HierarchicalChunker）
  参考 RAGFlow 的 FACTORY 模式，支持基于规则的自动识别或用户手动选择。
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

# Markdown 表格块正则（匹配以 | 开头的连续行，至少包含表头+分隔行+数据行）
_MD_TABLE_LINE_RE = re.compile(r'^\|.*\|$')


# Markdown 标题正则：匹配 # ~ ###### 开头的行
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')

# 非 Markdown 章节标记识别（中/英/德），用于在「无 Markdown 标题」的文档中
# 仍能为 child chunk 生成章节面包屑（context_header），提升检索时的上下文。
# 每项为 (level, 正则, 是否带捕获组标题)。level 越小层级越高。
# 设计要点：仅在文档不含 Markdown 标题时启用（见 _HeaderTracker），因此完全
# 不影响 Markdown 文档的既有 breadcrumb 行为（零回归）。
_NONMD_HEADING_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    # 中文章/编（一级）：第X章 / 第X编
    (1, re.compile(r'^[ \t]*(第[一二三四五六七八九十百千零〇\d]+[章编](?:[ \t]+\S.*)?)$')),
    # 中文节/部分（二级）：第X节 / 第X部分
    (2, re.compile(r'^[ \t]*(第[一二三四五六七八九十百千零〇\d]+[节節部]分?(?:[ \t]+\S.*)?)$')),
    # 中文条（三级）：第X条
    (3, re.compile(r'^[ \t]*(第[一二三四五六七八九十百千零〇\d]+条(?:[ \t]+\S.*)?)$')),
    # 英文章节（一级）：Chapter/Section/Part N
    (1, re.compile(r'^[ \t]*((?:Chapter|Section|Part)\s+(?:\d+|[IVXLCDM]+)\b.*)$', re.IGNORECASE)),
    # 德文章节（一级）：Kapitel/Abschnitt/Teil N
    (1, re.compile(r'^[ \t]*((?:Kapitel|Abschnitt|Teil)\s+(?:\d+|[IVXLCDM]+)\b.*)$', re.IGNORECASE)),
    # 中文数字序号（二级）：一、二、…（后跟标题文字）
    (2, re.compile(r'^[ \t]*([一二三四五六七八九十]+、\s*\S.*)$')),
]


@dataclass
class DocProfile:
    """文档结构 profile，用于自动选择切分策略"""
    heading_count: int       # Markdown 标题数
    structure_count: int     # 结构标记数（条款编号等，如 "第X条"、"Article X"）
    paragraph_count: int     # 段落数（双换行分隔）
    total_chars: int         # 总字符数
    avg_paragraph_len: float # 平均段落长度


def _profile_document(text: str) -> DocProfile:
    """统计文档的结构信号密度，生成 DocProfile。

    分析文本中的 Markdown 标题数、结构标记数（条款编号等）、段落数等，
    用于后续策略选择。
    """
    # 统计 Markdown 标题数
    heading_count = len(re.findall(r'^#{1,6}\s+', text, re.MULTILINE))

    # 统计结构标记数（排除 Markdown 标题，因为已单独统计）
    structure_patterns = [
        r'^[一二三四五六七八九十]+[、．.]',
        r'^（[一二三四五六七八九十]+）',
        r'^\([一二三四五六七八九十]+\)',
        r'^\d+[、．.\s]',
        r'^第[一二三四五六七八九十百千\d]+[条章节款项]',
    ]
    structure_re = re.compile('|'.join(f'(?:{p})' for p in structure_patterns), re.MULTILINE)
    structure_count = len(structure_re.findall(text))

    # 统计段落数（双换行分隔）
    paragraphs = [p.strip() for p in re.split(r'\n\n+', text) if p.strip()]
    paragraph_count = len(paragraphs)

    # 总字符数
    total_chars = len(text)

    # 平均段落长度
    avg_paragraph_len = total_chars / paragraph_count if paragraph_count > 0 else float(total_chars)

    return DocProfile(
        heading_count=heading_count,
        structure_count=structure_count,
        paragraph_count=paragraph_count,
        total_chars=total_chars,
        avg_paragraph_len=avg_paragraph_len,
    )


def _select_strategy(profile: DocProfile) -> list[str]:
    """根据 DocProfile 返回策略链（优先级列表）。

    - heading_count >= 3 → ["heading", "heuristic", "legacy"]
    - structure_count >= 2 → ["heuristic", "legacy"]
    - 其他 → ["legacy"]
    """
    if profile.heading_count >= 3:
        return ["heading", "heuristic", "legacy"]
    if profile.structure_count >= 2:
        return ["heuristic", "legacy"]
    return ["legacy"]


def _validate_chunks(chunks: list[str], min_size: int, max_size: int) -> bool:
    """验证切分质量。

    - 至少有 1 个 chunk
    - 所有 chunk 长度在 [min_size, max_size] 范围内

    Returns:
        True 如果验证通过，False 否则。
    """
    if not chunks:
        return False
    return all(min_size <= len(c) <= max_size for c in chunks)


class _HeaderTracker:
    """标题栈追踪器，维护 Markdown 标题层级并生成面包屑字符串。

    当遇到新标题时，弹出同级或更低级别的标题，压入新标题，
    然后可通过 breadcrumb() 获取当前层级的面包屑（如 `# 顶级标题 > ## 二级标题`）。
    """

    def __init__(self, enable_nonmd: bool = False):
        # 栈元素: (level, heading_text, is_md)，level 为 1~6
        # is_md=True 表示 Markdown 标题（breadcrumb 带 # 前缀，保持既有格式）；
        # is_md=False 表示非 Markdown 章节标记（第X章/Chapter 等，breadcrumb 用原文）。
        self._stack: list[tuple[int, str, bool]] = []
        # 是否启用非 Markdown 章节标记识别。默认关闭，仅在文档不含 Markdown 标题
        # 时由调用方开启，从而完全不影响 Markdown 文档的既有 breadcrumb 行为。
        self._enable_nonmd = enable_nonmd

    def push(self, level: int, text: str, is_md: bool = True) -> None:
        """遇到新标题时更新栈：弹出同级或更低级别的标题，压入新标题。"""
        # 弹出所有 level >= 当前 level 的标题（同级或子级）
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text.strip(), is_md))

    def breadcrumb(self) -> str:
        """生成当前标题栈的面包屑字符串。

        Markdown 标题格式: `# 顶级标题 > ## 二级标题 > ### 三级标题`
        非 Markdown 章节标记（第X章等）直接用原文，不加 # 前缀。
        栈为空时返回空字符串。
        """
        if not self._stack:
            return ""
        parts = []
        for level, text, is_md in self._stack:
            if is_md:
                parts.append(f"{'#' * level} {text}")
            else:
                parts.append(text)
        return " > ".join(parts)

    def feed_line(self, line: str) -> bool:
        """检测一行文本是否为标题，如果是则更新栈。

        优先识别 Markdown 标题；若未命中且开启了非 Markdown 识别，再尝试匹配
        中/英/德章节标记。

        Returns:
            True 如果该行是标题并已更新栈，False 否则。
        """
        stripped = line.strip()
        match = _HEADING_RE.match(stripped)
        if match:
            level = len(match.group(1))
            text = match.group(2)
            self.push(level, text, is_md=True)
            return True
        if self._enable_nonmd:
            for level, pattern in _NONMD_HEADING_PATTERNS:
                m = pattern.match(line)
                if m:
                    self.push(level, m.group(1).strip(), is_md=False)
                    return True
        return False

    def feed_text(self, text: str) -> None:
        """逐行扫描文本块，检测并追踪所有 Markdown 标题。"""
        for line in text.split('\n'):
            self.feed_line(line)

    def current_level(self) -> int:
        """返回当前栈顶标题级别，栈为空时返回 0。"""
        return self._stack[-1][0] if self._stack else 0

    def reset(self) -> None:
        """清空标题栈。"""
        self._stack.clear()


@dataclass
class ChunkResult:
    """切分结果"""
    parent_chunks: list[str]                    # 大块，用于上下文返回
    child_chunks: list[str]                     # 小块，用于精准检索
    parent_child_map: dict[int, list[int]]      # 父→子映射 (parent_index -> [child_indices])
    context_headers: list[str] = field(default_factory=list)  # 与 child_chunks 一一对应的面包屑标题


class HierarchicalChunker:
    """结构感知的父子 chunk 切分器"""

    def __init__(self, parent_size: int = 2500, child_size: int = 450, overlap: int = 70, min_child_size: int = 20):
        self.parent_size = parent_size
        self.child_size = child_size
        self.overlap = overlap
        self.min_child_size = min_child_size

    def chunk(self, text: str, metadata: dict = None) -> ChunkResult:
        """先按结构/语义边界切父块，再将父块细分为子块。

        使用 Document Profiler 分析文档结构，按策略链顺序尝试切分，
        每次切分后验证质量，不合格自动降级到下一个策略。
        """
        if not text or not text.strip():
            return ChunkResult(parent_chunks=[], child_chunks=[], parent_child_map={}, context_headers=[])

        stripped = text.strip()

        # 是否启用非 Markdown 章节标记识别：仅当文档不含 Markdown 标题时启用，
        # 从而对 Markdown 文档零影响（其 breadcrumb 行为与之前完全一致）。
        has_md_heading = bool(re.search(r'^#{1,6}\s+', stripped, re.MULTILINE))
        enable_nonmd = not has_md_heading

        # 文本短于 child_size，直接作为单个父块和子块
        if len(stripped) <= self.child_size:
            # 用 _HeaderTracker 检测短文本中的标题
            tracker = _HeaderTracker(enable_nonmd=enable_nonmd)
            tracker.feed_text(stripped)
            return ChunkResult(
                parent_chunks=[stripped],
                child_chunks=[stripped],
                parent_child_map={0: [0]},
                context_headers=[tracker.breadcrumb()],
            )

        # Document Profiler: 分析文档结构并选择策略链
        profile = _profile_document(stripped)
        strategies = _select_strategy(profile)

        # 按策略链顺序尝试切分，验证通过则使用该结果
        parent_chunks = None
        for i, strategy in enumerate(strategies):
            candidate_chunks = self._split_by_strategy(stripped, strategy)
            # 最后一个策略（legacy）无论如何都使用
            if i == len(strategies) - 1:
                parent_chunks = candidate_chunks
                break
            # 验证切分质量
            if _validate_chunks(candidate_chunks, self.min_child_size, self.parent_size):
                parent_chunks = candidate_chunks
                break

        # 兜底：如果所有策略都没产出结果（不应发生），使用 legacy
        if parent_chunks is None:
            parent_chunks = self._split_parent_chunks(stripped)

        # 对每个父块切分子块，构建映射，同时追踪标题生成 context_headers
        child_chunks: list[str] = []
        context_headers: list[str] = []
        parent_child_map: dict[int, list[int]] = {}
        tracker = _HeaderTracker(enable_nonmd=enable_nonmd)

        for parent_idx, parent_text in enumerate(parent_chunks):
            children = self._split_child_chunks(parent_text)

            # 先为该父块的每个子块计算 breadcrumb（与原逻辑一致）
            sub_texts: list[str] = []
            sub_headers: list[str] = []
            for child_text in children:
                current_breadcrumb = tracker.breadcrumb()
                tracker.feed_text(child_text)
                new_breadcrumb = tracker.breadcrumb()
                child_breadcrumb = new_breadcrumb if new_breadcrumb else current_breadcrumb
                sub_texts.append(child_text)
                sub_headers.append(child_breadcrumb)

            # 碎片合并（移植自 WeKnora coalesceTinyChunks）：把同一父块内、
            # breadcrumb 相同且相邻的「过小」子块合并，避免 FAQ / 短条目类文档
            # 产生大量无信息量碎片。仅合并到 child_size/2 的目标且不超 child_size，
            # 故对块大小已接近目标的正常文档无影响（零回归）。
            sub_texts, sub_headers = self._coalesce_tiny_children(sub_texts, sub_headers)

            child_indices = []
            for child_text, child_breadcrumb in zip(sub_texts, sub_headers):
                child_indices.append(len(child_chunks))
                child_chunks.append(child_text)
                context_headers.append(child_breadcrumb)
            parent_child_map[parent_idx] = child_indices

        return ChunkResult(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            parent_child_map=parent_child_map,
            context_headers=context_headers,
        )

    def _coalesce_tiny_children(
        self, texts: list[str], headers: list[str]
    ) -> tuple[list[str], list[str]]:
        """合并同一父块内相邻的「过小」子块（移植自 WeKnora coalesceTinyChunks）。

        合并条件（全部满足才合并到当前累积块）：
        - 与当前块 breadcrumb 相同（保证上下文一致，不跨章节混合）；
        - 当前累积块仍小于目标（child_size/2，至少 200）；
        - 合并后不超过 child_size（不制造超大块）。

        正常文档的子块大小已接近 child_size，远超 child_size/2 阈值，第一项
        size 条件即不满足，因此原样返回（零回归）。仅 FAQ / 短条目类文档受益。

        Args:
            texts: 子块文本列表（同一父块内，顺序）
            headers: 与 texts 一一对应的 breadcrumb 列表

        Returns:
            (合并后的 texts, 合并后的 headers)
        """
        if len(texts) <= 1:
            return texts, headers

        target = max(self.child_size // 2, 200)

        out_texts: list[str] = [texts[0]]
        out_headers: list[str] = [headers[0]]
        cur_len = len(texts[0])

        for i in range(1, len(texts)):
            t = texts[i]
            h = headers[i]
            tlen = len(t)
            same_header = h == out_headers[-1]
            if same_header and cur_len < target and cur_len + tlen <= self.child_size:
                # 合并到当前块（用换行拼接，保留可读边界）
                out_texts[-1] = out_texts[-1] + "\n" + t
                cur_len += tlen + 1
            else:
                out_texts.append(t)
                out_headers.append(h)
                cur_len = tlen

        return out_texts, out_headers

    def _split_by_strategy(self, text: str, strategy: str) -> list[str]:
        """根据策略名称执行对应的切分逻辑。

        - "heading": 主要按 Markdown 标题切分
        - "heuristic": 按结构标记（条款编号等）切分
        - "legacy": 现有的完整切分逻辑（结构标记 + 段落 + 表格保护）
        """
        if strategy == "heading":
            return self._split_by_headings(text)
        elif strategy == "heuristic":
            return self._split_by_heuristic(text)
        else:
            return self._split_parent_chunks(text)

    def _split_by_headings(self, text: str) -> list[str]:
        """按 Markdown 标题切分父块，每个标题开始一个新段落。

        切分后对过长/过短的块进行 normalize。
        """
        lines = text.split('\n')
        sections: list[str] = []
        current_lines: list[str] = []

        for line in lines:
            stripped_line = line.strip()
            # 检测当前行是否为 Markdown 标题
            if stripped_line and re.match(r'^#{1,6}\s+', stripped_line):
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

        result = sections if sections else [text]
        return self._normalize_chunks(result)

    def _split_by_heuristic(self, text: str) -> list[str]:
        """按结构标记（条款编号等）切分父块。

        使用 _STRUCTURE_RE 检测结构标记，每个标记开始一个新段落。
        切分后对过长/过短的块进行 normalize。
        """
        sections = self._split_by_structure(text)
        return self._normalize_chunks(sections)

    def _split_parent_chunks(self, text: str) -> list[str]:
        """按结构标记和段落边界切分父块

        优先级：表格整块保护 > 结构标记 > 双换行段落 > 句子边界 > 强制切分
        """
        # 先将表格块（HTML 和 Markdown）提取为独立段落，避免被切断
        segments = self._split_preserving_tables(text)

        result: list[str] = []
        for segment in segments:
            if segment.startswith("<table>") or self._is_md_table(segment):
                # 表格块直接作为独立段落，不再细分
                result.append(segment)
            else:
                # 非表格部分按原有逻辑切分
                has_structure = bool(_STRUCTURE_RE.search(segment))
                if has_structure:
                    result.extend(self._split_by_structure(segment))
                else:
                    result.extend(self._split_by_paragraphs(segment))

        # 合并过短的 section，拆分过长的 section（表格块跳过拆分）
        return self._normalize_chunks(result)

    def _split_preserving_tables(self, text: str) -> list[str]:
        """将文本按表格块拆分（HTML <table> 和 Markdown 表格），保持表格完整性

        返回交替的 [普通文本, 表格块, 普通文本, ...] 列表
        表格块以 "<table>" 或 "|" 开头，可通过前缀判断类型
        """
        # 先处理 HTML 表格
        if "<table>" in text:
            segments: list[str] = []
            last_end = 0

            for match in _TABLE_BLOCK_RE.finditer(text):
                before = text[last_end:match.start()].strip()
                if before:
                    segments.append(before)
                segments.append(match.group())
                last_end = match.end()

            after = text[last_end:].strip()
            if after:
                segments.append(after)

            # 对非 HTML 表格的段落，再检查是否包含 Markdown 表格
            result = []
            for seg in segments:
                if seg.startswith("<table>"):
                    result.append(seg)
                else:
                    result.extend(self._split_preserving_md_tables(seg))
            return result if result else [text]

        # 没有 HTML 表格，检查 Markdown 表格
        return self._split_preserving_md_tables(text)

    def _split_preserving_md_tables(self, text: str) -> list[str]:
        """识别并保护 Markdown 表格块

        Markdown 表格特征：连续的以 | 开头且以 | 结尾的行
        """
        lines = text.split('\n')
        segments: list[str] = []
        current_normal: list[str] = []
        current_table: list[str] = []

        for line in lines:
            stripped = line.strip()
            is_table_line = bool(stripped and _MD_TABLE_LINE_RE.match(stripped))

            if is_table_line:
                # 进入或继续表格
                if current_normal:
                    normal_text = '\n'.join(current_normal).strip()
                    if normal_text:
                        segments.append(normal_text)
                    current_normal = []
                current_table.append(line)
            else:
                # 非表格行
                if current_table:
                    # 表格结束，保存表格块
                    table_text = '\n'.join(current_table).strip()
                    if table_text:
                        segments.append(table_text)
                    current_table = []
                current_normal.append(line)

        # 处理末尾
        if current_table:
            table_text = '\n'.join(current_table).strip()
            if table_text:
                segments.append(table_text)
        if current_normal:
            normal_text = '\n'.join(current_normal).strip()
            if normal_text:
                segments.append(normal_text)

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

    @staticmethod
    def _is_md_table(text: str) -> bool:
        """判断文本是否为 Markdown 表格块"""
        lines = text.strip().split('\n')
        if len(lines) < 2:
            return False
        # 至少前两行都是 | 开头 | 结尾
        return all(_MD_TABLE_LINE_RE.match(line.strip()) for line in lines[:3] if line.strip())

    def _normalize_chunks(self, sections: list[str]) -> list[str]:
        """合并过短的段落，拆分过长的段落，确保每个父块在合理范围内

        表格块（HTML 和 Markdown）不会被拆分，保持完整性。
        """
        chunks: list[str] = []
        current = ""

        for section in sections:
            if not section:
                continue

            # 表格块不拆分，直接作为独立 chunk
            is_table = section.startswith("<table>") or self._is_md_table(section)
            if is_table:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(section)
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

        # 如果整个父块是 Markdown 表格，不再细分（loader 已按行分组）
        if self._is_md_table(text):
            return [text]

        # 如果包含 HTML 表格，先按表格拆分保护
        if "<table>" in text:
            segments = self._split_preserving_tables(text)
            chunks: list[str] = []
            for segment in segments:
                if segment.startswith("<table>") or self._is_md_table(segment):
                    # 表格块作为独立子块（即使超过 child_size 也不切断）
                    chunks.append(segment)
                elif len(segment) <= self.child_size:
                    chunks.append(segment)
                else:
                    chunks.extend(self._split_child_by_size(segment))
            chunks = self._merge_short_chunks(chunks)
            return chunks if chunks else [text]

        # 如果包含 Markdown 表格，按表格拆分保护
        if _MD_TABLE_LINE_RE.match(text.strip().split('\n')[0].strip()):
            return [text]

        # 如果父块内有结构标记，先按结构切分
        has_structure = bool(_STRUCTURE_RE.search(text))
        if has_structure:
            sections = self._split_by_structure(text)
            # 对过长的 section 再按字符数切分
            chunks: list[str] = []
            for section in sections:
                if len(section) <= self.child_size:
                    chunks.append(section)
                elif self._is_md_table(section):
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


# 默认父/子块大小（与 HierarchicalChunker 默认值一致，作为护栏的兜底尺寸）
DEFAULT_PARENT_SIZE = 2500
DEFAULT_CHILD_SIZE = 450
DEFAULT_OVERLAP = 70


def enforce_size_limits(
    result: ChunkResult,
    parent_size: int = DEFAULT_PARENT_SIZE,
    child_size: int = DEFAULT_CHILD_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> ChunkResult:
    """对任意 ChunkResult 施加绝对大小护栏，拆分超限的父块 / 子块。

    对齐 WeKnora ``internal/infrastructure/chunker`` 中 ``mergeUnits`` 的
    ``absoluteMaxSize`` 思路：无论上游 chunker 用何种策略切分，最终产出的
    每个父块、子块都不得超过预算上限。这是防止"超大块 → 检索/问答时撑爆模型
    上下文"的最后一道兜底（例如体裁切分器 laws/paper/qa 仅按结构标记切分、
    本身无 size 控制，单块可能达到几万字）。

    处理规则：
    - 父块 ``<= parent_size``：保留；其名下子块逐个检查，超 ``child_size`` 的
      按字符/句子边界再切，子块语义（如 QA 的 Q/A 拆分）在不超限时不受影响。
    - 父块 ``> parent_size``：按句子边界重切为多个子父块，每个子父块再派生子块
      （原子块因父块被拆分而失去对应关系，故就该父块范围重新派生）。

    Args:
        result: 上游 chunker 产出的切分结果
        parent_size: 父块字符上限
        child_size: 子块字符上限
        overlap: 子块切分时的重叠字符数

    Returns:
        施加大小护栏后的新 ChunkResult，父子映射与面包屑同步重建。
    """
    if not result.parent_chunks:
        return result

    helper = HierarchicalChunker(
        parent_size=parent_size, child_size=child_size, overlap=overlap
    )
    headers = result.context_headers or []

    new_parents: list[str] = []
    new_children: list[str] = []
    new_map: dict[int, list[int]] = {}
    new_headers: list[str] = []

    for p_idx, parent_text in enumerate(result.parent_chunks):
        child_indices = result.parent_child_map.get(p_idx, [])

        if len(parent_text) <= parent_size:
            # 父块合规：保留父块，仅对超限子块再切
            np_idx = len(new_parents)
            new_parents.append(parent_text)
            kept: list[int] = []
            for c_idx in child_indices:
                if c_idx >= len(result.child_chunks):
                    continue
                ctext = result.child_chunks[c_idx]
                cheader = headers[c_idx] if c_idx < len(headers) else ""
                if len(ctext) <= child_size:
                    kept.append(len(new_children))
                    new_children.append(ctext)
                    new_headers.append(cheader)
                else:
                    for sub in helper._split_child_by_size(ctext):
                        kept.append(len(new_children))
                        new_children.append(sub)
                        new_headers.append(cheader)
            new_map[np_idx] = kept
        else:
            # 父块超限：按句子边界重切为子父块，逐个派生子块
            base_header = ""
            if child_indices and child_indices[0] < len(headers):
                base_header = headers[child_indices[0]]
            for sub_parent in helper._split_by_sentences(parent_text, parent_size):
                np_idx = len(new_parents)
                new_parents.append(sub_parent)
                kept = []
                for sub_child in helper._split_child_chunks(sub_parent):
                    kept.append(len(new_children))
                    new_children.append(sub_child)
                    new_headers.append(base_header)
                new_map[np_idx] = kept

    return ChunkResult(
        parent_chunks=new_parents,
        child_chunks=new_children,
        parent_child_map=new_map,
        context_headers=new_headers,
    )
