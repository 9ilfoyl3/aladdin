"""PDF 文档加载器（基于 pymupdf）"""

import os

import fitz  # pymupdf

from app.pipeline.loader import BaseLoader, LoadResult


class PdfLoader(BaseLoader):
    """处理 .pdf 文件的加载器"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PDF 文件，提取全部页面文本

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 PDF 文件
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 打开并提取文本
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 PDF 文件: {file_path}，错误: {e}")

        # 逐页提取文本，用换行符连接
        pages_text = []
        for page in doc:
            text = page.get_text()
            pages_text.append(text)

        page_count = len(pages_text)
        content = "\n".join(pages_text)

        doc.close()

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "pdf",
            "file_size": file_size,
            "page_count": page_count,
        }

        return LoadResult(content=content, metadata=metadata)
