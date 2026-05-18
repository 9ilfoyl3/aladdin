"""PaddleOCR Provider - 基于 PaddleOCR 引擎的本地 OCR 实现"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .provider import OCRBlock, OCRProvider, OCRResult, PageOCRResult

logger = logging.getLogger(__name__)


class PaddleOCRProvider(OCRProvider):
    """PaddleOCR 本地识别 Provider

    采用懒加载方式初始化 OCR 引擎，首次调用时才创建 PaddleOCR 实例。
    支持 PDF（多页）和图片（单页）输入。
    """

    def __init__(self, lang: str = "ch", use_gpu: bool = False) -> None:
        """初始化配置，不立即创建引擎实例

        Args:
            lang: OCR 识别语言，默认 "ch"（中文）
            use_gpu: 是否使用 GPU 加速，默认 False
        """
        self._lang = lang
        self._use_gpu = use_gpu
        self._engine: Any | None = None

    @property
    def name(self) -> str:
        """Provider 唯一标识名"""
        return "paddleocr"

    def _get_engine(self) -> Any:
        """懒加载：首次调用时创建 PaddleOCR 实例并缓存

        Returns:
            PaddleOCR 引擎实例
        """
        if self._engine is None:
            from paddleocr import PaddleOCR

            logger.info(
                "初始化 PaddleOCR 引擎 (lang=%s, use_gpu=%s)",
                self._lang,
                self._use_gpu,
            )
            self._engine = PaddleOCR(
                use_angle_cls=True,
                lang=self._lang,
                use_gpu=self._use_gpu,
                show_log=False,
            )
        return self._engine

    async def recognize(self, file_path: str) -> OCRResult:
        """调用 PaddleOCR 引擎执行识别，将结果适配为 OCRResult

        PaddleOCR 返回格式：
        - PDF: list of pages, 每页为 list of [bbox, (text, confidence)]
        - 图片: 单页结果, list of [bbox, (text, confidence)]

        Args:
            file_path: 文件路径（PDF 或图片）

        Returns:
            OCRResult: 统一格式的识别结果
        """
        engine = self._get_engine()

        # 使用 asyncio.to_thread 包装阻塞的 PaddleOCR 调用
        raw_result = await asyncio.to_thread(engine.ocr, file_path, cls=True)

        # PaddleOCR 对图片返回单页结果（list of lines），对 PDF 返回多页结果（list of pages）
        # 统一处理为多页格式
        if not raw_result:
            # 空结果
            return OCRResult(
                full_text="",
                pages=[],
                avg_confidence=0.0,
                provider_name=self.name,
            )

        # 判断是否为多页结果：如果第一个元素是 list of list，则为多页
        # PaddleOCR 对 PDF 返回 [page1_lines, page2_lines, ...]
        # 对图片返回 [line1, line2, ...] 其中每个 line 是 [bbox, (text, conf)]
        pages_data = self._normalize_to_pages(raw_result)

        pages: list[PageOCRResult] = []
        all_confidences: list[float] = []

        for page_idx, page_lines in enumerate(pages_data):
            blocks: list[OCRBlock] = []

            if not page_lines:
                pages.append(
                    PageOCRResult(page_num=page_idx + 1, blocks=[], full_text="")
                )
                continue

            for line in page_lines:
                if not line or len(line) < 2:
                    continue

                bbox_raw = line[0]
                text_info = line[1]

                # text_info 格式为 (text, confidence)
                text = text_info[0] if text_info else ""
                confidence = float(text_info[1]) if len(text_info) > 1 else 0.0

                # bbox_raw 格式为 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                # 转换为 (x_min, y_min, x_max, y_max)
                bbox = self._convert_bbox(bbox_raw)

                blocks.append(
                    OCRBlock(text=text, confidence=confidence, bbox=bbox)
                )
                all_confidences.append(confidence)

            page_text = "\n".join(block.text for block in blocks)
            pages.append(
                PageOCRResult(
                    page_num=page_idx + 1, blocks=blocks, full_text=page_text
                )
            )

        full_text = "\n\n".join(page.full_text for page in pages if page.full_text)
        avg_confidence = (
            sum(all_confidences) / len(all_confidences) if all_confidences else 0.0
        )

        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_confidence,
            provider_name=self.name,
        )

    def is_available(self) -> bool:
        """检查 paddleocr 包是否已安装

        Returns:
            True 如果 paddleocr 可导入，否则 False
        """
        try:
            import paddleocr  # noqa: F401

            return True
        except ImportError:
            return False

    def _normalize_to_pages(self, raw_result: list) -> list[list]:
        """将 PaddleOCR 原始结果统一为多页格式

        PaddleOCR 对 PDF 返回 [page1_lines, page2_lines, ...]
        对图片返回 [line1, line2, ...] 其中每个 line 是 [bbox, (text, conf)]

        Args:
            raw_result: PaddleOCR 原始返回结果

        Returns:
            统一的多页格式 [[page1_lines], [page2_lines], ...]
        """
        if not raw_result:
            return []

        # 检查第一个元素判断是单页还是多页
        first_item = raw_result[0]

        if first_item is None:
            # PDF 某页为空的情况，视为多页结果
            return raw_result

        # 如果第一个元素是 list，且其第一个元素也是 list（包含 bbox 坐标点）
        # 则判断为单页图片结果
        if (
            isinstance(first_item, list)
            and len(first_item) >= 2
            and isinstance(first_item[0], list)
            and len(first_item[0]) > 0
            and isinstance(first_item[0][0], (list, tuple))
        ):
            # 单页图片结果：[line1, line2, ...]，包装为 [[lines]]
            return [raw_result]

        # 多页 PDF 结果：[page1_lines, page2_lines, ...]
        return raw_result

    @staticmethod
    def _convert_bbox(
        bbox_raw: list[list[float]],
    ) -> tuple[float, float, float, float] | None:
        """将 PaddleOCR 的 4 点 bbox 转换为 (x_min, y_min, x_max, y_max) 格式

        Args:
            bbox_raw: PaddleOCR 原始 bbox，格式为 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]

        Returns:
            (x_min, y_min, x_max, y_max) 或 None
        """
        if not bbox_raw or len(bbox_raw) < 4:
            return None

        try:
            xs = [point[0] for point in bbox_raw]
            ys = [point[1] for point in bbox_raw]
            return (min(xs), min(ys), max(xs), max(ys))
        except (IndexError, TypeError):
            return None
