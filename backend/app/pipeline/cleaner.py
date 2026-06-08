"""文档文本去噪模块。

在 chunk 切分之前去除页眉页脚、页码等噪音文本，
提升后续元数据提取和检索的准确性。
"""

from __future__ import annotations

import re


# 页码正则模式
_PAGE_NUMBER_PATTERNS = [
    r'^\s*-\s*\d+\s*-\s*$',                # - 3 -
    r'^\s*第\s*\d+\s*页\s*$',               # 第 3 页
    r'^\s*Page\s+\d+\s*(of\s+\d+)?\s*$',   # Page 3 of 10
    r'^\s*\d+\s*/\s*\d+\s*$',              # 3/10
    r'^\s*\d{1,4}\s*$',                     # 纯数字（1-4位）
]
_PAGE_NUM_RE = re.compile(
    '|'.join(f'(?:{p})' for p in _PAGE_NUMBER_PATTERNS),
    re.MULTILINE | re.IGNORECASE,
)


class TextCleaner:
    """文档文本去噪器。

    通过 bbox 位置过滤、跨页重复检测和正则页码清理三种策略，
    去除文档中的页眉、页脚和页码等噪音文本。
    """

    # 页面顶部/底部区域比例阈值
    HEADER_FOOTER_RATIO: float = 0.05
    # 跨页重复判定频率阈值
    REPEAT_FREQUENCY_THRESHOLD: float = 0.5

    def clean(
        self,
        content: str,
        page_texts: list[str] | None = None,
        page_blocks: list[list[dict]] | None = None,
    ) -> str:
        """执行去噪流程: bbox过滤 → 重复检测 → 正则清理。

        Args:
            content: 原始文档文本内容。
            page_texts: 按页分割的文本列表，用于跨页重复检测。
            page_blocks: 每页的文本块列表（含 bbox 信息），用于位置过滤。

        Returns:
            去噪后的文本内容。
        """
        result = content

        # Step 1: bbox 过滤 - 如果有 page_blocks，过滤边缘短文本后重建文本
        if page_blocks:
            # 假设标准 A4 页面高度约 842 点（PDF 单位）
            page_height = 842.0
            filtered_blocks = self._filter_by_bbox(page_blocks, page_height)
            # 从过滤后的 blocks 重建文本
            page_texts_rebuilt = []
            for page in filtered_blocks:
                page_text = "\n".join(block["text"] for block in page)
                page_texts_rebuilt.append(page_text)
            result = "\n".join(page_texts_rebuilt)
            # 使用重建后的 page_texts 进行后续处理
            page_texts = page_texts_rebuilt

        # Step 2: 重复检测 - 检测并去除跨页重复的页眉页脚
        if page_texts and len(page_texts) >= 2:
            repeated = self._detect_repeated_headers(page_texts)
            if repeated:
                lines = result.split('\n')
                result = '\n'.join(
                    line for line in lines if line.strip() not in repeated
                )

        # Step 3: 正则清理 - 去除页码行
        result = self._remove_page_numbers(result)

        return result

    def _filter_by_bbox(
        self, page_blocks: list[list[dict]], page_height: float
    ) -> list[list[dict]]:
        """过滤页面顶部/底部5%区域的短文本块。

        Args:
            page_blocks: 每页的文本块列表，每个块包含 bbox (x0, y0, x1, y1) 和 text。
            page_height: 页面高度（用于计算边缘区域阈值）。

        Returns:
            过滤后的每页文本块列表。
        """
        if page_height <= 0:
            return page_blocks

        header_threshold = page_height * self.HEADER_FOOTER_RATIO
        footer_threshold = page_height * (1 - self.HEADER_FOOTER_RATIO)

        result = []
        for page in page_blocks:
            filtered = []
            for block in page:
                y0 = block["bbox"][1]
                y1 = block["bbox"][3]
                text = block.get("text", "")

                # 顶部/底部5%区域的短文本过滤
                is_edge = y0 < header_threshold or y1 > footer_threshold
                is_short = len(text.strip()) < 100

                if is_edge and is_short:
                    continue  # 跳过边缘短文本（疑似页眉页脚）
                filtered.append(block)
            result.append(filtered)

        return result

    def _detect_repeated_headers(
        self, page_texts: list[str]
    ) -> set[str]:
        """检测跨页重复短文本（出现频率>50%判定为页眉页脚）。

        取每页首3行和尾3行中的短文本（长度在 2~50 之间），
        统计其跨页出现频率，超过 50% 的判定为页眉页脚。

        Args:
            page_texts: 按页分割的文本列表。

        Returns:
            被判定为页眉页脚的重复文本集合。
        """
        if len(page_texts) < 2:
            return set()

        total_pages = len(page_texts)
        candidates: dict[str, int] = {}

        for page_text in page_texts:
            lines = page_text.strip().split('\n')
            # 取首3行和尾3行
            check_lines = lines[:3] + lines[-3:]
            for line in check_lines:
                text = line.strip()
                if 2 < len(text) < 50:  # 短文本才可能是页眉页脚
                    candidates[text] = candidates.get(text, 0) + 1

        # 出现频率 > 50% 判定为页眉页脚
        threshold = total_pages * self.REPEAT_FREQUENCY_THRESHOLD
        return {text for text, count in candidates.items() if count > threshold}

    def _remove_page_numbers(self, text: str) -> str:
        """正则去除纯页码行。

        匹配以下页码模式并移除整行：
        - "- 3 -" 格式
        - "第 3 页" 格式
        - "Page 3 of 10" 格式
        - "3/10" 格式
        - 纯数字（1-4位）

        Args:
            text: 输入文本。

        Returns:
            去除页码行后的文本。
        """
        lines = text.split('\n')
        cleaned = [line for line in lines if not _PAGE_NUM_RE.match(line)]
        return '\n'.join(cleaned)
