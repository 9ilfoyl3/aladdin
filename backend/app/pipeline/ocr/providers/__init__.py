"""显式 OCR Provider 实现集合

导入本包即完成三种 Provider 到注册表的注册。

导入顺序即注册表顺序，也就是前端下拉的呈现顺序：
按"能力覆盖面 + 通用推荐度"排列（vl 支持 PDF 且出 Markdown，放首位作为默认选项）。
"""

from .vl import VLProvider
from .paddle import PaddleOCRProvider
from .mineru import MinerUProvider

__all__ = ["MinerUProvider", "PaddleOCRProvider", "VLProvider"]
