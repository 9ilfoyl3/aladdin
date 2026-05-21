"""PPT (pptx) 文档加载器（基于 python-pptx）

支持提取文本和嵌入图片，图片将由 pipeline 的 OCR 流程处理。
"""

import os

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片数据大小（字节），过小的图片跳过
_MIN_IMAGE_BYTES = 1024


class PptxLoader(BaseLoader):
    """处理 .pptx 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PPT 文件，提取全部幻灯片文本和嵌入图片

        格式：每张幻灯片以 "[Slide N]" 标题开头，幻灯片之间以双换行分隔。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

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

        # 逐幻灯片提取文本和图片
        slides_text = []
        images: list[EmbeddedImage] = []

        for idx, slide in enumerate(prs.slides, start=1):
            paragraphs = []
            for shape in slide.shapes:
                # 提取文本
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            paragraphs.append(text)

                # 提取图片
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    try:
                        image = shape.image
                        image_bytes = image.blob
                        if image_bytes and len(image_bytes) >= _MIN_IMAGE_BYTES:
                            # 从 content_type 推断格式
                            content_type = image.content_type or ""
                            img_format = "png"
                            if "jpeg" in content_type or "jpg" in content_type:
                                img_format = "jpeg"
                            elif "png" in content_type:
                                img_format = "png"

                            images.append(
                                EmbeddedImage(
                                    data=image_bytes,
                                    format=img_format,
                                    page_or_index=idx,
                                    description=f"slide{idx}_img{len(images)+1}",
                                )
                            )
                    except Exception:
                        continue

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
            "embedded_image_count": len(images),
        }

        return LoadResult(content=content, metadata=metadata, images=images)
