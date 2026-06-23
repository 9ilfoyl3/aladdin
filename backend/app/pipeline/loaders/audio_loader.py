"""音频文档加载器

音频文件本身没有文本内容，load 返回空文本，并在 metadata 中标记 is_audio，
由 pipeline 检测到后调用 ASR Manager 进行语音转写。
"""

import os

from app.pipeline.loader import BaseLoader, LoadResult


class AudioLoader(BaseLoader):
    """处理音频文件（mp3/wav/m4a/flac/ogg）的加载器

    返回空文本并标记 is_audio=True，由 pipeline 的 ASR 逻辑自动处理。
    """

    def load(self, file_path: str) -> LoadResult:
        """加载音频文件（返回空文本，触发 pipeline 的 ASR 流程）

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 空文本内容，metadata.is_audio=True

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
            "is_audio": True,
        }

        # 返回空文本，pipeline 检测到 is_audio 后会调用 ASR
        return LoadResult(content="", metadata=metadata)
