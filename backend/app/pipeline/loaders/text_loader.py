"""Markdown / TXT 文本加载器"""

import os

from app.pipeline.loader import BaseLoader, LoadResult


class TextLoader(BaseLoader):
    """处理 .md 和 .txt 文件的加载器"""

    # 支持的文件扩展名
    SUPPORTED_EXTENSIONS = {".md", ".txt"}

    def load(self, file_path: str) -> LoadResult:
        """加载文本文件，返回内容和元数据

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 不支持的文件类型
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 校验文件类型
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型: {ext}，仅支持 .md 和 .txt")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 读取文件内容，优先 utf-8，失败则忽略错误字符
        content = self._read_file(file_path)

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": ext.lstrip("."),
            "file_size": file_size,
        }

        return LoadResult(content=content, metadata=metadata)

    def _read_file(self, file_path: str) -> str:
        """读取文件内容，处理编码错误

        先尝试严格 utf-8 解码，失败则用 errors='ignore' 重试
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            # 回退：忽略无法解码的字符
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
