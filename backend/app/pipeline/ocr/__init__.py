# OCR 可配置服务模块

from .manager import OCRManager
from .provider import OCRBlock, OCRProvider, OCRResult, PageOCRResult

__all__ = [
    "OCRBlock",
    "OCRManager",
    "OCRProvider",
    "OCRResult",
    "PageOCRResult",
]
