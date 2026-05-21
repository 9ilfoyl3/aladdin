"""PDF 文档加载器（基于 pymupdf）

支持提取文本和嵌入图片。图片写入临时目录（避免内存压力），
由 pipeline 的 OCR 流程处理后清理。
"""

import hashlib
import os
import tempfile

import fitz  # pymupdf

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片尺寸阈值（像素），过小的图片（如装饰图标）跳过
_MIN_IMAGE_SIZE = 50
# 最小图片数据大小（字节），过小的图片数据跳过
_MIN_IMAGE_BYTES = 1024
# 单文档最大提取图片数量
_MAX_IMAGES_PER_DOC = 50


class PdfLoader(BaseLoader):
    """处理 .pdf 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PDF 文件，提取全部页面文本和嵌入图片

        图片写入临时目录，通过 content_hash 去重避免重复 OCR（如水印、logo）。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、按页文本、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 PDF 文件
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)

        try:
            doc = fitz.open(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 PDF 文件: {file_path}，错误: {e}")

        # 创建临时目录存放提取的图片
        tmp_dir = tempfile.mkdtemp(prefix="pdf_images_")

        pages_text: list[str] = []
        images: list[EmbeddedImage] = []
        seen_hashes: set[str] = set()  # 用于去重
        total_images_extracted = 0

        for page_idx, page in enumerate(doc):
            # 提取文本
            text = page.get_text()
            pages_text.append(text)

            # 达到图片上限后不再提取
            if total_images_extracted >= _MAX_IMAGES_PER_DOC:
                continue

            # 提取该页嵌入的图片
            page_images = self._extract_page_images(
                doc, page, page_idx + 1, tmp_dir, seen_hashes
            )
            total_images_extracted += len(page_images)
            images.extend(page_images)

        page_count = len(pages_text)
        content = "\n".join(pages_text)

        doc.close()

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "pdf",
            "file_size": file_size,
            "page_count": page_count,
            "embedded_image_count": len(images),
        }

        return LoadResult(
            content=content,
            metadata=metadata,
            images=images,
            page_texts=pages_text,
        )

    @staticmethod
    def _extract_page_images(
        doc: fitz.Document,
        page: fitz.Page,
        page_num: int,
        tmp_dir: str,
        seen_hashes: set[str],
    ) -> list[EmbeddedImage]:
        """提取单页中的嵌入图片，写入临时目录

        通过 content_hash 去重，过滤装饰性小图。

        Args:
            doc: fitz 文档对象
            page: 当前页面对象
            page_num: 页码（从1开始）
            tmp_dir: 临时目录路径
            seen_hashes: 已见图片 hash 集合（用于去重，会被修改）

        Returns:
            该页提取到的 EmbeddedImage 列表
        """
        images: list[EmbeddedImage] = []

        for img_info in page.get_images(full=True):
            xref = img_info[0]

            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue

            if not base_image:
                continue

            image_bytes = base_image.get("image")
            if not image_bytes or len(image_bytes) < _MIN_IMAGE_BYTES:
                continue

            # 检查图片尺寸，过滤装饰性小图
            width = base_image.get("width", 0)
            height = base_image.get("height", 0)
            if width < _MIN_IMAGE_SIZE or height < _MIN_IMAGE_SIZE:
                continue

            # 计算 hash 去重（水印、logo 等重复图片只处理一次）
            img_hash = hashlib.md5(image_bytes).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

            # 写入临时文件
            img_ext = base_image.get("ext", "png")
            img_filename = f"page{page_num}_img{len(images)+1}_{img_hash[:8]}.{img_ext}"
            img_path = os.path.join(tmp_dir, img_filename)

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            images.append(
                EmbeddedImage(
                    file_path=img_path,
                    format=img_ext,
                    page_or_index=page_num,
                    content_hash=img_hash,
                    description=f"pdf_page{page_num}_img{len(images)+1}",
                )
            )

        return images
