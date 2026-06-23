# ASR 可配置服务模块（语音识别）

from .manager import ASRManager
from .provider import ASRProvider, ASRResult, ASRSegment

__all__ = [
    "ASRManager",
    "ASRProvider",
    "ASRResult",
    "ASRSegment",
]
