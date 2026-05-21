"""Word (docx) 文档加载器（基于 python-docx）

支持提取文本和嵌入图片，图片将由 pipeline 的 OCR 流程处理。
"""

import os

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片数据大小（字节），过小的图片跳过
_MIN_IMAGE_BYTES = 1024


class DocxLoader(BaseLoader):
    """处理 .docx 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 Word 文件，提取全部段落文本和嵌入图片

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

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

        # 提取嵌入图片
        images = self._extract_images(doc)

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "docx",
            "file_size": file_size,
            "embedded_image_count": len(images),
        }

        return LoadResult(content=content, metadata=metadata, images=images)

    @staticmethod
    def _extract_images(doc: Document) -> list[EmbeddedImage]:
        """从 Word 文档中提取所有嵌入图片

        通过遍历文档的 relationship 找到所有图片资源。

        Args:
            doc: python-docx Document 对象

        Returns:
            提取到的 EmbeddedImage 列表
        """
        images: list[EmbeddedImage] = []
        img_index = 0

        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                try:
                    image_part = rel.target_part
                    image_bytes = image_part.blob
                except Exception:
                    continue

                if not image_bytes or len(image_bytes) < _MIN_IMAGE_BYTES:
                    continue

                # 从 content_type 推断格式
                content_type = getattr(image_part, "content_type", "")
                img_format = "png"
                if "jpeg" in content_type or "jpg" in content_type:
                    img_format = "jpeg"
                elif "png" in content_type:
                    img_format = "png"
                elif "gif" in content_type:
                    img_format = "gif"
                elif "bmp" in content_type:
                    img_format = "bmp"

                img_index += 1
                images.append(
                    EmbeddedImage(
                        data=image_bytes,
                        format=img_format,
                        page_or_index=img_index,
                        description=f"docx_img{img_index}",
                    )
                )

        return images
