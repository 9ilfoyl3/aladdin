"""图片文档加载器（通过 OCR 服务识别）"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from app.pipeline.loader import BaseLoader, LoadResult

_executor = ThreadPoolExecutor(max_workers=1)


class ImageLoader(BaseLoader):
    """处理图片文件（jpg/jpeg/png）的加载器，通过 OCR 服务提取文字"""

    def load(self, file_path: str) -> LoadResult:
        """加载图片文件，调用 OCR 服务提取文字

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含 OCR 识别的文本内容

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: OCR 识别失败或未配置 OCR 服务
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)

        # 在当前事件循环中调用异步 OCR
        import nest_asyncio
        nest_asyncio.apply()
        
        loop = asyncio.get_event_loop()
        content = loop.run_until_complete(self._ocr_recognize(file_path))

        if not content or len(content.strip()) < 5:
            raise ValueError("图片 OCR 识别结果为空，请检查图片是否包含可识别的文字")

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": os.path.splitext(file_path)[1].lstrip("."),
            "file_size": file_size,
            "ocr": True,
        }

        return LoadResult(content=content, metadata=metadata)

    async def _ocr_recognize(self, file_path: str) -> str:
        """调用 OCR 管理器识别图片"""
        from app.pipeline.ocr.manager import get_ocr_manager

        manager = get_ocr_manager()
        if not manager.is_enabled():
            raise ValueError("OCR 服务未启用，无法处理图片文件。请在 OCR 服务管理中配置并启用 OCR 服务。")

        result = await manager.recognize(file_path)
        return result.full_text
