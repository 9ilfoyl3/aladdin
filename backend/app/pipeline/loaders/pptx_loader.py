"""PPT (pptx) 文档加载器（基于 python-pptx）"""

import os

from pptx import Presentation

from app.pipeline.loader import BaseLoader, LoadResult


class PptxLoader(BaseLoader):
    """处理 .pptx 文件的加载器"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PPT 文件，提取全部幻灯片文本

        格式：每张幻灯片以 "[Slide N]" 标题开头，幻灯片之间以双换行分隔。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容和元数据

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 pptx 文件
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 打开并提取文本
        try:
            prs = Presentation(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 pptx 文件: {file_path}，错误: {e}")

        # 逐幻灯片提取文本
        slides_text = []
        for idx, slide in enumerate(prs.slides, start=1):
            paragraphs = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            paragraphs.append(text)
            # 幻灯片标题 + 内容
            slide_content = f"[Slide {idx}]\n" + "\n".join(paragraphs)
            slides_text.append(slide_content)

        slide_count = len(slides_text)
        # 幻灯片之间用双换行分隔
        content = "\n\n".join(slides_text)

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "pptx",
            "file_size": file_size,
            "slide_count": slide_count,
        }

        return LoadResult(content=content, metadata=metadata)
