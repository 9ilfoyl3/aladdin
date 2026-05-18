"""Excel (xlsx) 文档加载器（基于 openpyxl）"""

import os

from openpyxl import load_workbook

from app.pipeline.loader import BaseLoader, LoadResult


class XlsxLoader(BaseLoader):
    """处理 .xlsx 文件的加载器"""

    def load(self, file_path: str) -> LoadResult:
        """加载 Excel 文件，提取全部工作表文本

        格式：每行单元格以 tab 分隔，行以换行分隔，工作表之间以双换行 + 表名标题分隔。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 xlsx 文件
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 打开并提取文本
        try:
            wb = load_workbook(file_path, read_only=True, data_only=True)
        except Exception as e:
            raise ValueError(f"无法解析 xlsx 文件: {file_path}，错误: {e}")

        # 逐工作表提取文本
        sheets_text = []
        for sheet in wb.worksheets:
            rows_text = []
            for row in sheet.iter_rows(values_only=True):
                # 将单元格值转为字符串，None 转为空字符串，tab 分隔
                cells = [str(cell) if cell is not None else "" for cell in row]
                rows_text.append("\t".join(cells))
            # 工作表标题 + 内容
            sheet_content = f"[{sheet.title}]\n" + "\n".join(rows_text)
            sheets_text.append(sheet_content)

        sheet_count = len(sheets_text)
        # 工作表之间用双换行分隔
        content = "\n\n".join(sheets_text)

        wb.close()

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "xlsx",
            "file_size": file_size,
            "sheet_count": sheet_count,
        }

        return LoadResult(content=content, metadata=metadata)
