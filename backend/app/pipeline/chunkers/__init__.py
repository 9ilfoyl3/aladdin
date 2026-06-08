"""Chunker 策略实现模块

各 Chunker 在此注册到 ChunkerFactory。
"""

from app.pipeline.chunkers.laws import LawsChunker  # noqa: F401
from app.pipeline.chunkers.naive import NaiveChunker  # noqa: F401
from app.pipeline.chunkers.paper import PaperChunker  # noqa: F401
from app.pipeline.chunkers.qa import QAChunker  # noqa: F401
from app.pipeline.chunkers.table import TableChunker  # noqa: F401
