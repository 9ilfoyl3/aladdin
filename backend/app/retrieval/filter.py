"""检索过滤条件模块

提供 RetrievalFilter dataclass，用于构造 Milvus pre-filter 表达式，
支持按 doc_id 和 file_type 进行组合过滤。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetrievalFilter:
    """检索过滤条件"""

    doc_ids: list[str] | None = None  # 限定文档范围
    file_types: list[str] | None = None  # 限定文件类型

    def to_milvus_expr(self) -> str | None:
        """转换为 Milvus filter 表达式

        Returns:
            合法的 Milvus expr 字符串，多条件用 " and " 连接；
            无过滤条件时返回 None。
        """
        parts = []
        if self.doc_ids:
            ids_str = ", ".join(f'"{d}"' for d in self.doc_ids)
            parts.append(f"doc_id in [{ids_str}]")
        if self.file_types:
            types_str = ", ".join(f'"{t}"' for t in self.file_types)
            parts.append(f"file_type in [{types_str}]")
        return " and ".join(parts) if parts else None
