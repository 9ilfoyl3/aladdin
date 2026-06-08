"""CSV 文件加载器

针对表格数据优化：按行分组输出，每组带表头上下文，
动态计算每组行数确保不超过 embedding 模型的 token 限制。

TODO: [性能] 当前一次性读取所有行到内存，对于超大文件（>500MB）应改为流式读取 + 分批处理
"""

import csv
import os

from app.pipeline.loader import BaseLoader, LoadResult

# embedding 模型最大输入字符数（保守值，BGE-M3 支持 8192 tokens ≈ 6000 中文字符）
_MAX_CHUNK_CHARS = 5000


class CsvLoader(BaseLoader):
    """处理 .csv 文件的加载器

    策略：根据实际行宽动态计算每组行数，确保每组字符数不超过模型限制。
    每组前面附带表头，输出为 Markdown 表格格式。
    """

    SUPPORTED_EXTENSIONS = {".csv"}

    def load(self, file_path: str) -> LoadResult:
        """加载 CSV 文件，按行分组输出

        根据行宽自动选择输出格式：
        - 窄行（多行能放入一个 chunk）：Markdown 表格格式
        - 宽行（单行超过限制）：键值对格式，每行一个 chunk

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型: {ext}，仅支持 .csv")

        file_size = os.path.getsize(file_path)
        print(f"[CsvLoader] 开始加载 CSV 文件: {os.path.basename(file_path)}, 大小: {file_size / 1024 / 1024:.1f}MB")

        rows = self._read_csv(file_path)
        print(f"[CsvLoader] CSV 读取完成，共 {len(rows)} 行")

        if not rows or len(rows) < 2:
            content = self._rows_to_markdown(rows) if rows else ""
            row_count = max(0, len(rows) - 1)
            col_count = len(rows[0]) if rows else 0
            group_count = 0
            mode = "empty"
        else:
            header = rows[0]
            data_rows = rows[1:]
            row_count = len(data_rows)
            col_count = len(header)

            # 动态计算每组行数
            rows_per_chunk = self._calc_rows_per_chunk(header, data_rows)

            if rows_per_chunk >= 2:
                # 窄行模式：Markdown 表格格式，多行一组
                mode = "table"
                chunks = []
                for i in range(0, len(data_rows), rows_per_chunk):
                    group = data_rows[i:i + rows_per_chunk]
                    chunk_text = self._group_to_markdown(header, group)
                    chunks.append(chunk_text)
            else:
                # 宽行模式：键值对格式，每行一个 chunk
                mode = "kv"
                rows_per_chunk = 1
                chunks = []
                for row in data_rows:
                    chunk_text = self._row_to_kv(header, row)
                    chunks.append(chunk_text)

            content = "\n\n".join(chunks)
            group_count = len(chunks)

        print(f"[CsvLoader] 转换完成 (模式: {mode})，内容长度: {len(content)} 字符, {row_count} 行 x {col_count} 列, 分为 {group_count} 组")

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "csv",
            "file_size": file_size,
            "row_count": row_count,
            "col_count": col_count,
        }

        # 表格类文件直接输出预切分的 chunk，跳过 chunker 的二次切分
        pre_chunked = chunks if 'chunks' in dir() and chunks else []

        return LoadResult(content=content, metadata=metadata, pre_chunked=pre_chunked)

    def _calc_rows_per_chunk(self, header: list[str], data_rows: list[list[str]]) -> int:
        """根据实际数据动态计算每组行数，确保每组不超过 _MAX_CHUNK_CHARS

        采样前 200 行，取 P90 行宽（而非平均值）作为估算基准，更保守。
        """
        # 计算表头 + 分隔行的固定开销
        clean_header = [cell.replace("\n", " ").replace("\r", " ").replace("|", "\\|") for cell in header]
        header_line = "| " + " | ".join(clean_header) + " |"
        sep_line = "| " + " | ".join(["---"] * len(header)) + " |"
        header_cost = len(header_line) + len(sep_line) + 2  # +2 for \n

        # 采样计算行宽
        sample = data_rows[:200]
        if not sample:
            return 10

        row_widths = []
        for row in sample:
            col_count = len(header)
            padded = row + [""] * (col_count - len(row))
            padded = padded[:col_count]
            cells = [cell.replace("\n", " ").replace("\r", " ").replace("|", "\\|") for cell in padded]
            line = "| " + " | ".join(cells) + " |"
            row_widths.append(len(line) + 1)  # +1 for \n

        # 取 P90 行宽作为估算基准（比平均值更保守）
        row_widths.sort()
        p90_idx = int(len(row_widths) * 0.9)
        p90_width = row_widths[p90_idx]

        # 计算每组能放多少行
        available = _MAX_CHUNK_CHARS - header_cost
        if available <= 0:
            return 1

        rows_per_chunk = max(1, int(available / p90_width))

        # 限制范围：最少 1 行，最多 30 行
        rows_per_chunk = min(rows_per_chunk, 30)

        return rows_per_chunk

    def _read_csv(self, file_path: str) -> list[list[str]]:
        """读取 CSV 文件，处理编码问题"""
        try:
            with open(file_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                return [row for row in reader]
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                reader = csv.reader(f)
                return [row for row in reader]

    def _row_to_kv(self, header: list[str], row: list[str]) -> str:
        """将单行数据转为键值对格式，更紧凑且适合检索

        格式：
        字段1: 值1
        字段2: 值2
        ...

        对于超长值（>500字符），截断并标注。
        """
        col_count = len(header)
        padded = row + [""] * (col_count - len(row))
        padded = padded[:col_count]

        lines = []
        for field_name, value in zip(header, padded):
            clean_name = field_name.replace("\n", " ").replace("\r", " ").strip()
            clean_value = value.replace("\n", " ").replace("\r", " ").strip()
            if not clean_value:
                continue  # 跳过空值，减少噪音
            # 截断超长值，避免单行 chunk 超过 embedding 模型输入限制
            if len(clean_value) > 500:
                clean_value = clean_value[:500] + "...(截断)"
            lines.append(f"{clean_name}: {clean_value}")

        return "\n".join(lines)

    def _group_to_markdown(self, header: list[str], rows: list[list[str]]) -> str:
        """将一组行（带表头）转为 Markdown 表格"""
        col_count = len(header)
        lines = []
        clean_header = [cell.replace("\n", " ").replace("\r", " ").replace("|", "\\|") for cell in header]
        lines.append("| " + " | ".join(clean_header) + " |")
        lines.append("| " + " | ".join(["---"] * col_count) + " |")

        for row in rows:
            padded = row + [""] * (col_count - len(row))
            padded = padded[:col_count]
            cells = [cell.replace("\n", " ").replace("\r", " ").replace("|", "\\|") for cell in padded]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)

    def _rows_to_markdown(self, rows: list[list[str]]) -> str:
        """将所有行转为 Markdown 表格（用于小文件）"""
        if not rows:
            return ""
        header = rows[0]
        data = rows[1:] if len(rows) > 1 else []
        return self._group_to_markdown(header, data)
