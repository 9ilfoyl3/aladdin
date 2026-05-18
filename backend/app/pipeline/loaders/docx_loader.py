"""Word (docx) 文档加载器（基于 python-docx）"""

import os

from docx import Document

from app.pipeline.loader import BaseLoader, LoadResult


class DocxLoader(BaseLoader):
    """处理 .docx 文件的加载器"""

    def load(self, file_path: str) -> LoadResult:
        """加载 Word 文件，提取全部段落文本

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 docx 文件
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 打开并提取文本
        try:
            doc = Document(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 docx 文件: {file_path}，错误: {e}")

        # 逐段落提取文本，用双换行连接（保持段落边界清晰）
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        content = "\n\n".join(paragraphs)

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "docx",
            "file_size": file_size,
        }

        return LoadResult(content=content, metadata=metadata)
