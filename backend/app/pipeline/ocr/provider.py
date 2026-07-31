"""OCR Provider 抽象基类、能力声明与统一数据结构"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# 输入类型常量：Provider 用它声明可接受的输入，input_prep 用它做决策
INPUT_IMAGE = "image"
INPUT_PDF = "pdf"

# 扩展名 → 输入类型映射（不在表内的扩展名视为不支持 OCR）
INPUT_KIND_BY_EXT: dict[str, str] = {
    "jpg": INPUT_IMAGE,
    "jpeg": INPUT_IMAGE,
    "png": INPUT_IMAGE,
    "bmp": INPUT_IMAGE,
    "webp": INPUT_IMAGE,
    "tif": INPUT_IMAGE,
    "tiff": INPUT_IMAGE,
    "pdf": INPUT_PDF,
}


@dataclass(frozen=True)
class OCRCapability:
    """Provider 的能力声明。

    由 Provider 类以常量声明（**不是**用户配置项）：能力决定 pipeline 如何为它
    准备输入（整文件直送 / 按页渲染成图片），从而不必在运行时"先试一次再看结果猜"。

    Attributes:
        accepts: 可直接接受的输入类型集合，取值为 ``INPUT_IMAGE`` / ``INPUT_PDF``。
        outputs_markdown: 是否保留表格与版面结构（输出 Markdown）。
        paginated: 响应是否天然带页码（影响 ``OCRResult.pages`` 的可信度）。
        recommended_timeout: UI 推荐的超时秒数（不同服务耗时量级差异很大）。
    """

    accepts: frozenset[str]
    outputs_markdown: bool
    paginated: bool
    recommended_timeout: float

    def accepts_kind(self, input_kind: str) -> bool:
        """该能力是否可直接接受给定输入类型"""
        return input_kind in self.accepts


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
    """OCR 服务抽象基类

    每个实现绑定**固定**的传输协议与响应契约，并声明自身能力；
    收到契约外的响应时抛 :class:`~app.pipeline.ocr.errors.OCRResponseFormatError`，
    不做跨格式探测或回落解析。
    """

    #: 能力声明，子类必须覆盖
    capability: OCRCapability

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 唯一标识名"""
        ...

    @abstractmethod
    async def recognize(self, file_path: str) -> OCRResult:
        """对单个文件执行 OCR 识别（文件须为本 Provider 可接受的类型）

        Args:
            file_path: 文件路径（PDF/图片）

        Returns:
            OCRResult: 统一格式的识别结果
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查该 Provider 是否可用（配置是否完整等）"""
        ...
