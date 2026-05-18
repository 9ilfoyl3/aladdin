"""检索器基类与结果数据结构"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RetrievalResult:
    """单条检索结果"""

    chunk_id: str
    content: str  # Parent chunk 内容（上下文完整）
    score: float
    doc_id: str
    metadata: dict = field(default_factory=dict)
    child_content: str = ""  # 子块原始内容（精准命中部分）


class BaseRetriever(ABC):
    """检索器抽象基类，所有检索实现需继承此类"""

    @abstractmethod
    async def search(
        self, query: str, kb_id: str, top_k: int = 10, **kwargs
    ) -> list[RetrievalResult]:
        """执行检索，返回结果列表"""
        ...
