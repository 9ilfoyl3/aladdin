"""CSV 文件加载器"""

import csv
import os

from app.pipeline.loader import BaseLoader, LoadResult


class CsvLoader(BaseLoader):
    """处理 .csv 文件的加载器

    将 CSV 转换为 Markdown 表格格式的文本，便于后续分块和检索。
    """

    SUPPORTED_EXTENSIONS = {".csv"}

    def load(self, file_path: str) -> LoadResult:
        """加载 CSV 文件，返回 Markdown 表格格式的内容

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 不支持的文件类型或文件为空
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型: {ext}，仅支持 .csv")

        file_size = os.path.getsize(file_path)

        rows = self._read_csv(file_path)

        if not rows:
            content = ""
            row_count = 0
            col_count = 0
        else:
            content = self._to_markdown_table(rows)
            row_count = len(rows) - 1  # 减去表头
            col_count = len(rows[0]) if rows else 0

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "csv",
            "file_size": file_size,
            "row_count": row_count,
            "col_count": col_count,
        }

        return LoadResult(content=content, metadata=metadata)

    def _read_csv(self, file_path: str) -> list[list[str]]:
        """读取 CSV 文件，处理编码问题

        先尝试 utf-8，失败则用 utf-8 errors='ignore' 重试。
        """
        try:
            with open(file_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                return [row for row in reader]
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                reader = csv.reader(f)
                return [row for row in reader]

    def _to_markdown_table(self, rows: list[list[str]]) -> str:
        """将 CSV 行数据转换为 Markdown 表格格式"""
        if not rows:
            return ""

        # 第一行作为表头
        header = rows[0]
        lines = []
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")

        # 数据行
        for row in rows[1:]:
            # 补齐列数不足的行
            padded = row + [""] * (len(header) - len(row))
            # 截断多余列
            padded = padded[:len(header)]
            lines.append("| " + " | ".join(padded) + " |")

        return "\n".join(lines)
