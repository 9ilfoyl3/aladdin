"""OCR Provider 抽象基类与统一数据结构"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class OCRBlock:
    """单个识别区块"""

    text: str
    confidence: float
    bbox: tuple[float, float, float, float] | None = None  # (x1, y1, x2, y2)


@dataclass
class PageOCRResult:
    """单页 OCR 结果"""

    page_num: int
    blocks: list[OCRBlock]
    full_text: str  # 该页完整文本（blocks 拼接）


@dataclass
class OCRResult:
    """统一 OCR 输出结构"""

    full_text: str  # 全文本拼接
    pages: list[PageOCRResult]  # 按页结果
    avg_confidence: float  # 平均置信度
    provider_name: str  # 使用的 Provider 名称
    metadata: dict = field(default_factory=dict)


class OCRProvider(ABC):
    """OCR 服务抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 唯一标识名"""
        ...

    @abstractmethod
    async def recognize(self, file_path: str) -> OCRResult:
        """对文件执行 OCR 识别

        Args:
            file_path: 文件路径（PDF/图片）

        Returns:
            OCRResult: 统一格式的识别结果
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查该 Provider 是否可用（依赖是否安装等）"""
        ...
