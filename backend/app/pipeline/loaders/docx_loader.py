"""Word (docx) 文档加载器（基于 python-docx）

支持提取文本和嵌入图片。图片写入临时目录（避免内存压力），
由 pipeline 的 OCR 流程处理后清理。
"""

import hashlib
import os
import tempfile

from docx import Document

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片数据大小（字节），过小的图片跳过
_MIN_IMAGE_BYTES = 1024
# 单文档最大提取图片数量
_MAX_IMAGES_PER_DOC = 50


class DocxLoader(BaseLoader):
    """处理 .docx 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 Word 文件，提取全部段落文本和嵌入图片

        图片写入临时目录，通过 content_hash 去重。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 docx 文件
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)

        try:
            doc = Document(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 docx 文件: {file_path}，错误: {e}")

        # 逐段落提取文本
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        content = "\n\n".join(paragraphs)

        # 提取嵌入图片
        images = self._extract_images(doc)

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "docx",
            "file_size": file_size,
            "embedded_image_count": len(images),
        }

        return LoadResult(content=content, metadata=metadata, images=images)

    @staticmethod
    def _extract_images(doc: Document) -> list[EmbeddedImage]:
        """从 Word 文档中提取所有嵌入图片，写入临时目录

        通过 content_hash 去重，过滤过小的图片。

        Args:
            doc: python-docx Document 对象

        Returns:
            提取到的 EmbeddedImage 列表
        """
        tmp_dir = tempfile.mkdtemp(prefix="docx_images_")
        images: list[EmbeddedImage] = []
        seen_hashes: set[str] = set()
        img_index = 0

        for rel in doc.part.rels.values():
            if "image" not in rel.reltype:
                continue

            if len(images) >= _MAX_IMAGES_PER_DOC:
                break

            try:
                image_part = rel.target_part
                image_bytes = image_part.blob
            except Exception:
                continue

            if not image_bytes or len(image_bytes) < _MIN_IMAGE_BYTES:
                continue

            # 计算 hash 去重
            img_hash = hashlib.md5(image_bytes).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

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
            img_filename = f"docx_img{img_index}_{img_hash[:8]}.{img_format}"
            img_path = os.path.join(tmp_dir, img_filename)

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            images.append(
                EmbeddedImage(
                    file_path=img_path,
                    format=img_format,
                    page_or_index=img_index,
                    content_hash=img_hash,
                    description=f"docx_img{img_index}",
                )
            )

        return images
