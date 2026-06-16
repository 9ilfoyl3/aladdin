"""ASR Provider 抽象基类与统一数据结构"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ASRSegment:
    """单个语音片段（带时间戳）"""

    start: float  # 起始秒
    end: float    # 结束秒
    text: str


@dataclass
class ASRResult:
    """统一 ASR 输出结构"""

    full_text: str               # 全文转写结果
    segments: list[ASRSegment]   # 按片段结果（可能为空）
    provider_name: str           # 使用的 Provider 名称
    metadata: dict = field(default_factory=dict)


class ASRProvider(ABC):
    """ASR 服务抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 唯一标识名"""
        ...

    @abstractmethod
    async def transcribe(self, file_path: str) -> ASRResult:
        """对音频文件执行语音识别转写

        Args:
            file_path: 音频文件路径（mp3/wav/m4a 等）

        Returns:
            ASRResult: 统一格式的转写结果
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查该 Provider 是否可用（依赖配置是否完整等）"""
        ...
