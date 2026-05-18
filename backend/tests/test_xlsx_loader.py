"""测试 XlsxLoader（Excel 文档加载器）"""

import os

import pytest
from openpyxl import Workbook

from app.pipeline.loaders.xlsx_loader import XlsxLoader
from app.pipeline.loader import LoadResult


class TestXlsxLoader:
    """XlsxLoader 单元测试"""

    def setup_method(self):
        self.loader = XlsxLoader()

    def _create_xlsx(self, tmp_path, filename: str, sheets: dict[str, list[list]]) -> str:
        """辅助方法：创建包含指定工作表数据的 xlsx 文件

        Args:
            tmp_path: pytest 临时目录
            filename: 文件名
            sheets: {工作表名: [[行数据], ...]}
        """
        file_path = str(tmp_path / filename)
        wb = Workbook()
        # 删除默认工作表
        wb.remove(wb.active)
        for sheet_name, rows in sheets.items():
            ws = wb.create_sheet(title=sheet_name)
            for row in rows:
                ws.append(row)
        wb.save(file_path)
        return file_path

    def test_load_single_sheet(self, tmp_path):
        """加载单工作表 xlsx"""
        sheets = {"Sheet1": [["姓名", "年龄"], ["张三", 25]]}
        file_path = self._create_xlsx(tmp_path, "single.xlsx", sheets)

        result = self.loader.load(file_path)

        assert isinstance(result, LoadResult)
        assert "姓名\t年龄" in result.content
        assert "张三\t25" in result.content
        assert result.metadata["filename"] == "single.xlsx"
        assert result.metadata["file_type"] == "xlsx"
        assert result.metadata["file_size"] > 0
        assert result.metadata["sheet_count"] == 1

    def test_load_multi_sheet(self, tmp_path):
        """加载多工作表 xlsx"""
        sheets = {
            "员工": [["姓名", "部门"], ["李四", "研发"]],
            "部门": [["部门名", "人数"], ["研发", 10]],
        }
        file_path = self._create_xlsx(tmp_path, "multi.xlsx", sheets)

        result = self.loader.load(file_path)

        # 验证两个工作表内容都存在
        assert "[员工]" in result.content
        assert "[部门]" in result.content
        assert "李四\t研发" in result.content
        assert "研发\t10" in result.content
        assert result.metadata["sheet_count"] == 2

    def test_sheets_separated_by_double_newline(self, tmp_path):
        """工作表之间用双换行分隔"""
        sheets = {
            "A": [["数据A"]],
            "B": [["数据B"]],
        }
        file_path = self._create_xlsx(tmp_path, "sep.xlsx", sheets)

        result = self.loader.load(file_path)

        assert "\n\n" in result.content

    def test_none_cells_as_empty_string(self, tmp_path):
        """空单元格转为空字符串"""
        sheets = {"Sheet1": [["a", None, "c"]]}
        file_path = self._create_xlsx(tmp_path, "none.xlsx", sheets)

        result = self.loader.load(file_path)

        assert "a\t\tc" in result.content

    def test_file_not_found(self):
        """文件不存在时抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            self.loader.load("/nonexistent/path/file.xlsx")

    def test_invalid_xlsx(self, tmp_path):
        """无效 xlsx 文件抛出 ValueError"""
        file_path = tmp_path / "invalid.xlsx"
        file_path.write_text("this is not an xlsx", encoding="utf-8")

        with pytest.raises(ValueError, match="无法解析 xlsx 文件"):
            self.loader.load(str(file_path))

    def test_file_size_metadata(self, tmp_path):
        """file_size 反映实际文件字节数"""
        sheets = {"Sheet1": [["测试"]]}
        file_path = self._create_xlsx(tmp_path, "size.xlsx", sheets)

        result = self.loader.load(file_path)

        actual_size = os.path.getsize(file_path)
        assert result.metadata["file_size"] == actual_size
