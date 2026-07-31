# OCR 可配置服务模块

from .errors import OCRError, OCRResponseFormatError, OCRUnsupportedInputError
from .manager import OCRManager
from .provider import (
    INPUT_IMAGE,
    INPUT_PDF,
    OCRBlock,
    OCRCapability,
    OCRProvider,
    OCRResult,
    PageOCRResult,
)
from .registry import PROVIDER_META, PROVIDER_REGISTRY, provider_types

__all__ = [
    "INPUT_IMAGE",
    "INPUT_PDF",
    "OCRBlock",
    "OCRCapability",
    "OCRError",
    "OCRManager",
    "OCRProvider",
    "OCRResponseFormatError",
    "OCRResult",
    "OCRUnsupportedInputError",
    "PROVIDER_META",
    "PROVIDER_REGISTRY",
    "PageOCRResult",
    "provider_types",
]
