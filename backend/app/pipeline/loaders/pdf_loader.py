"""PDF 文档加载器（基于 pymupdf）

支持提取文本和嵌入图片，图片将由 pipeline 的 OCR 流程处理。
"""

import os

import fitz  # pymupdf

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片尺寸阈值（像素），过小的图片（如装饰图标）跳过
_MIN_IMAGE_SIZE = 50
# 最小图片数据大小（字节），过小的图片数据跳过
_MIN_IMAGE_BYTES = 1024


class PdfLoader(BaseLoader):
    """处理 .pdf 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PDF 文件，提取全部页面文本和嵌入图片

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 PDF 文件
        """
        # 校验文件存在
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 打开并提取文本
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 PDF 文件: {file_path}，错误: {e}")

        # 逐页提取文本和图片
        pages_text = []
        images: list[EmbeddedImage] = []

        for page_idx, page in enumerate(doc):
            # 提取文本
            text = page.get_text()
            pages_text.append(text)

            # 提取该页嵌入的图片
            page_images = self._extract_page_images(doc, page, page_idx + 1)
            images.extend(page_images)

        page_count = len(pages_text)
        content = "\n".join(pages_text)

        doc.close()

        # 构建元数据
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "pdf",
            "file_size": file_size,
            "page_count": page_count,
            "embedded_image_count": len(images),
        }

        return LoadResult(content=content, metadata=metadata, images=images)

    @staticmethod
    def _extract_page_images(
        doc: fitz.Document, page: fitz.Page, page_num: int
    ) -> list[EmbeddedImage]:
        """提取单页中的嵌入图片

        过滤掉过小的装饰性图片（图标、分隔线等），
        只保留可能包含有意义内容的图片。

        Args:
            doc: fitz 文档对象
            page: 当前页面对象
            page_num: 页码（从1开始）

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

            # 获取图片格式
            img_ext = base_image.get("ext", "png")

            images.append(
                EmbeddedImage(
                    data=image_bytes,
                    format=img_ext,
                    page_or_index=page_num,
                    description=f"pdf_page{page_num}_img{len(images)+1}",
                )
            )

        return images
