"""完整文档处理管道编排

流程：load → OCR(嵌入图片，并发+按页插入) → 合并文本 → chunk → enrich → embed → index
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.manager import ModelManager
from app.pipeline.chunker import HierarchicalChunker
from app.pipeline.embedder import PipelineEmbedder
from app.pipeline.enricher import Enricher
from app.pipeline.loader import EmbeddedImage, LoadResult, get_loader
from app.schema.db import Chunk, Document
from app.storage.milvus import MilvusClient

if TYPE_CHECKING:
    from app.pipeline.ocr.manager import OCRManager
    from app.pipeline.ocr.provider import OCRResult

logger = logging.getLogger(__name__)

# 并发 OCR 的最大并行数
_OCR_CONCURRENCY = 4


class DocumentPipeline:
    """文档处理管道，编排 load → chunk → enrich → embed → index 全流程"""

    def __init__(
        self,
        model_manager: ModelManager,
        milvus_client: MilvusClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        ocr_manager: OCRManager | None = None,
    ):
        self.milvus = milvus_client
        self.db_session_factory = db_session_factory
        self.ocr_manager = ocr_manager
        # 初始化管道各节点
        self.chunker = HierarchicalChunker()
        self.enricher = Enricher(llm=None, enabled=False)
        self.embedder = PipelineEmbedder(embed_provider=model_manager.embedder)

    async def process(self, file_path: str, doc_id: str, kb_id: str) -> None:
        """完整文档处理流程

        Args:
            file_path: 文件路径
            doc_id: 文档 ID
            kb_id: 知识库 ID
        """
        images_to_cleanup: list[EmbeddedImage] = []
        async with self.db_session_factory() as session:
            try:
                # 更新状态为 processing
                await self._update_status(session, doc_id, "processing")

                # 1. Load：根据文件扩展名选择 loader
                ext = Path(file_path).suffix.lstrip(".")
                loader = get_loader(ext)
                print(f"[Pipeline] 文档 {doc_id} 开始加载，类型: {ext}")
                load_result = loader.load(file_path)
                images_to_cleanup = load_result.images
                print(f"[Pipeline] 文档 {doc_id} 加载完成，内容长度: {len(load_result.content)}")
                logger.info("文档 %s 加载完成，内容长度: %d", doc_id, len(load_result.content))

                # 2. 处理文本为空的情况（纯扫描件/纯图片文件）
                stripped_content = load_result.content.strip()
                if not stripped_content or len(stripped_content) < 10:
                    if self.ocr_manager:
                        print(f"[Pipeline] 文档 {doc_id} 文本为空或过短(长度={len(stripped_content)})，触发整文件 OCR")
                        ocr_result = await self.ocr_manager.recognize(file_path)
                        load_result = LoadResult(
                            content=ocr_result.full_text,
                            metadata={
                                **load_result.metadata,
                                "ocr_provider": ocr_result.provider_name,
                            },
                            images=[],  # 整文件 OCR 已处理，无需再处理图片
                        )
                        print(f"[Pipeline] 文档 {doc_id} OCR 完成, Provider: {ocr_result.provider_name}, 文本长度: {len(ocr_result.full_text)}")
                        logger.info(
                            "文档 %s 通过 OCR (%s) 获取文本，长度: %d",
                            doc_id, ocr_result.provider_name, len(ocr_result.full_text),
                        )
                    else:
                        raise ValueError("文档提取文本为空，且未配置 OCR 服务")

                # 3. 处理嵌入图片：并发 OCR + 按页位置插入文本
                final_content = load_result.content
                if load_result.images and self.ocr_manager:
                    final_content = await self._process_embedded_images(
                        load_result, doc_id
                    )
                elif load_result.images and not self.ocr_manager:
                    logger.warning(
                        "文档 %s 包含 %d 张嵌入图片，但未配置 OCR 服务，图片内容将被忽略",
                        doc_id, len(load_result.images),
                    )

                # 4. Chunk：父子 chunk 切分
                print(f"[Pipeline] 文档 {doc_id} 开始分块，文本长度: {len(final_content)}")

                # 如果 loader 已经预切分（表格类文件），直接使用，跳过 chunker
                if load_result.pre_chunked:
                    pre_chunks = load_result.pre_chunked
                    from app.pipeline.chunker import ChunkResult
                    chunk_result = ChunkResult(
                        parent_chunks=pre_chunks,
                        child_chunks=pre_chunks,
                        parent_child_map={i: [i] for i in range(len(pre_chunks))},
                    )
                    print(f"[Pipeline] 文档 {doc_id} 使用 loader 预切分，共 {len(pre_chunks)} 个 chunk")
                else:
                    chunk_result = self.chunker.chunk(final_content, load_result.metadata)
                    print(f"[Pipeline] 文档 {doc_id} 分块完成，父块: {len(chunk_result.parent_chunks)}，子块: {len(chunk_result.child_chunks)}")

                # 大文件提示：chunk 数量较多时打印警告（不截断）
                if len(chunk_result.child_chunks) > 50000:
                    print(f"[Pipeline] ⚠️ 文档 {doc_id} 子块数量较多: {len(chunk_result.child_chunks)}，处理时间可能较长")
                    logger.warning(
                        "文档 %s 子块数量 %d，处理时间可能较长",
                        doc_id, len(chunk_result.child_chunks),
                    )

                # 5. Enrich：富化（当前为 pass-through）
                enriched_children = await self.enricher.enrich(chunk_result.child_chunks)

                # 6. Embed：对子 chunk 生成稠密+稀疏向量
                print(f"[Pipeline] 文档 {doc_id} 开始 embedding，共 {len(enriched_children)} 个子块")
                embed_result = await self.embedder.embed(enriched_children)
                print(f"[Pipeline] 文档 {doc_id} embedding 完成")

                # 7. Index：写入 Milvus + SQLite
                print(f"[Pipeline] 文档 {doc_id} 开始写入索引，父块: {len(chunk_result.parent_chunks)}，子块: {len(enriched_children)}")
                parent_ids: list[str] = []
                for i in range(len(chunk_result.parent_chunks)):
                    parent_ids.append(str(uuid.uuid4()))

                # 写入父 chunk 到 SQLite
                for idx, (pid, content) in enumerate(
                    zip(parent_ids, chunk_result.parent_chunks)
                ):
                    parent_chunk = Chunk(
                        id=pid,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=None,
                        content=content,
                        chunk_index=idx,
                    )
                    session.add(parent_chunk)

                # 写入子 chunk 到 SQLite + Milvus
                milvus_data: list[dict] = []
                child_count = len(enriched_children)

                for child_idx, child_text in enumerate(enriched_children):
                    child_id = str(uuid.uuid4())

                    parent_idx = self._find_parent(chunk_result.parent_child_map, child_idx)
                    parent_id = parent_ids[parent_idx] if parent_idx is not None else None

                    child_chunk = Chunk(
                        id=child_id,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=parent_id,
                        content=child_text,
                        chunk_index=child_idx,
                    )
                    session.add(child_chunk)

                    milvus_data.append({
                        "chunk_id": child_id,
                        "doc_id": doc_id,
                        "content": child_text[:65535],
                        "dense_vector": embed_result.dense_vectors[child_idx],
                        "sparse_vector": embed_result.sparse_vectors[child_idx],
                        "parent_id": parent_id or "",
                        "chunk_index": child_idx,
                    })

                # 确保 collection 存在
                if not await self.milvus.has_collection(kb_id):
                    await self.milvus.create_collection(kb_id)

                # 批量写入 Milvus
                # TODO: [性能] 对于超大文件（chunk 数 > 10000），应分批写入（每批 1000 条），避免单次请求过大
                if milvus_data:
                    batch_size = 1000
                    total = len(milvus_data)
                    if total <= batch_size:
                        print(f"[Pipeline] 文档 {doc_id} 写入 Milvus，共 {total} 条向量")
                        await self.milvus.insert(kb_id, milvus_data)
                    else:
                        print(f"[Pipeline] 文档 {doc_id} 分批写入 Milvus，共 {total} 条向量，每批 {batch_size} 条")
                        for i in range(0, total, batch_size):
                            batch = milvus_data[i:i + batch_size]
                            await self.milvus.insert(kb_id, batch)
                            if (i // batch_size + 1) % 10 == 0:
                                print(f"[Pipeline] Milvus 写入进度: {min(i + batch_size, total)}/{total}")
                        print(f"[Pipeline] Milvus 写入完成")

                # 8. 更新文档状态为 completed
                await self._update_status(session, doc_id, "completed", chunk_count=child_count)
                await session.commit()

                print(f"[Pipeline] 文档 {doc_id} 全部处理完成 ✓ 父块: {len(chunk_result.parent_chunks)}，子块: {child_count}")
                logger.info(
                    "文档 %s 处理完成，父块: %d，子块: %d",
                    doc_id, len(chunk_result.parent_chunks), child_count,
                )

            except Exception as e:
                import traceback
                print(f"[Pipeline] 文档 {doc_id} 处理失败: {type(e).__name__}: {e}")
                traceback.print_exc()
                await session.rollback()
                async with self.db_session_factory() as err_session:
                    await self._update_status(
                        err_session, doc_id, "failed", error_message=f"{type(e).__name__}: {e}"
                    )
                    await err_session.commit()
                logger.error("文档 %s 处理失败: %s", doc_id, e)
                raise
            finally:
                # 清理图片临时目录
                self._cleanup_image_temp_dirs(images_to_cleanup)

    async def _process_embedded_images(
        self, load_result: LoadResult, doc_id: str
    ) -> str:
        """处理嵌入图片：并发 OCR 识别，按页位置将图片文本插入到对应页面文本之后

        Args:
            load_result: 文档加载结果
            doc_id: 文档 ID

        Returns:
            合并了图片 OCR 文本的最终文档文本
        """
        images = load_result.images
        logger.info("文档 %s 开始并发处理 %d 张嵌入图片 OCR", doc_id, len(images))

        # 并发 OCR 识别所有图片
        ocr_results = await self._concurrent_ocr_images(images, doc_id)

        # 按页码分组图片 OCR 文本
        page_image_texts: dict[int, list[str]] = defaultdict(list)
        for img, ocr_text in zip(images, ocr_results):
            if ocr_text:
                page_image_texts[img.page_or_index].append(ocr_text)

        if not page_image_texts:
            return load_result.content

        # 如果有按页文本，按页插入图片 OCR 文本
        if load_result.page_texts:
            merged_pages: list[str] = []
            for page_idx, page_text in enumerate(load_result.page_texts):
                merged_pages.append(page_text)
                page_num = page_idx + 1
                if page_num in page_image_texts:
                    img_section = "\n".join(
                        f"[图片内容]\n{txt}" for txt in page_image_texts[page_num]
                    )
                    merged_pages.append(img_section)

            # 处理可能超出页码范围的图片（如 docx 的序号索引）
            for page_num, texts in page_image_texts.items():
                if page_num > len(load_result.page_texts):
                    img_section = "\n".join(
                        f"[图片内容]\n{txt}" for txt in texts
                    )
                    merged_pages.append(img_section)

            final_content = "\n\n".join(merged_pages)
        else:
            # 没有按页文本（如 docx 只有段落），追加到末尾
            all_img_texts = []
            for page_num in sorted(page_image_texts.keys()):
                for txt in page_image_texts[page_num]:
                    all_img_texts.append(f"[图片内容 - 第{page_num}张]\n{txt}")
            final_content = load_result.content + "\n\n" + "\n\n".join(all_img_texts)

        logger.info(
            "文档 %s 图片 OCR 完成，成功识别 %d/%d 张，最终文本长度: %d",
            doc_id,
            sum(1 for r in ocr_results if r),
            len(images),
            len(final_content),
        )

        return final_content

    async def _concurrent_ocr_images(
        self, images: list[EmbeddedImage], doc_id: str
    ) -> list[str]:
        """并发调用 OCR 识别多张图片，使用 Semaphore 控制并发数

        Args:
            images: 嵌入图片列表
            doc_id: 文档 ID（用于日志）

        Returns:
            与 images 等长的列表，每个元素为 OCR 识别文本（失败为空字符串）
        """
        semaphore = asyncio.Semaphore(_OCR_CONCURRENCY)

        async def _ocr_single(idx: int, img: EmbeddedImage) -> str:
            async with semaphore:
                try:
                    ocr_result = await self.ocr_manager.recognize(img.file_path)
                    text = ocr_result.full_text.strip()
                    if text:
                        logger.debug(
                            "文档 %s 图片 %d/%d OCR 成功，文本长度: %d",
                            doc_id, idx + 1, len(images), len(text),
                        )
                    return text
                except Exception as e:
                    logger.warning(
                        "文档 %s 图片 %d/%d OCR 失败: %s",
                        doc_id, idx + 1, len(images), e,
                    )
                    return ""

        tasks = [_ocr_single(i, img) for i, img in enumerate(images)]
        results = await asyncio.gather(*tasks)
        return list(results)

    @staticmethod
    def _cleanup_image_temp_dirs(images: list[EmbeddedImage]) -> None:
        """清理图片临时文件和目录

        从图片路径推断临时目录并删除整个目录。

        Args:
            images: 嵌入图片列表
        """
        if not images:
            return

        # 收集所有临时目录（去重）
        tmp_dirs: set[str] = set()
        for img in images:
            if img.file_path and os.path.exists(img.file_path):
                tmp_dirs.add(os.path.dirname(img.file_path))

        for tmp_dir in tmp_dirs:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.debug("已清理图片临时目录: %s", tmp_dir)
            except Exception as e:
                logger.warning("清理临时目录失败 %s: %s", tmp_dir, e)

    @staticmethod
    def _find_parent(parent_child_map: dict[int, list[int]], child_idx: int) -> int | None:
        """根据子 chunk 索引查找对应的父 chunk 索引"""
        for parent_idx, children in parent_child_map.items():
            if child_idx in children:
                return parent_idx
        return None

    @staticmethod
    async def _update_status(
        session: AsyncSession,
        doc_id: str,
        status: str,
        chunk_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新文档状态"""
        result = await session.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            return
        doc.status = status
        if chunk_count is not None:
            doc.chunk_count = chunk_count
        if error_message is not None:
            doc.error_message = error_message
