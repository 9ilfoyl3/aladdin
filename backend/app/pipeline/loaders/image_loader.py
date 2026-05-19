"""图片文档加载器

图片文件本身没有文本内容，load 返回空文本。
pipeline 检测到空文本后会自动调用 OCR Manager 识别。
"""

import os

from app.pipeline.loader import BaseLoader, LoadResult


class ImageLoader(BaseLoader):
    """处理图片文件（jpg/jpeg/png）的加载器

    返回空文本，由 pipeline 的 OCR 逻辑自动处理。
    """

    def load(self, file_path: str) -> LoadResult:
        """加载图片文件（返回空文本，触发 pipeline 的 OCR 流程）

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 空文本内容，pipeline 会检测到并调用 OCR

        Raises:
            FileNotFoundError: 文件不存在
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": os.path.splitext(file_path)[1].lstrip("."),
            "file_size": file_size,
        }

        # 返回空文本，pipeline 检测到 len < 10 后会自动触发 OCR
        return LoadResult(content="", metadata=metadata)
