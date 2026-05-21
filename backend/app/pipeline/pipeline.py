"""完整文档处理管道编排

流程：load → OCR(嵌入图片) → 合并文本 → chunk → enrich → embed → index（写入 Milvus + SQLite）
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
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

logger = logging.getLogger(__name__)


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
        async with self.db_session_factory() as session:
            try:
                # 更新状态为 processing
                await self._update_status(session, doc_id, "processing")

                # 1. Load：根据文件扩展名选择 loader
                ext = Path(file_path).suffix.lstrip(".")
                loader = get_loader(ext)
                load_result = loader.load(file_path)
                logger.info("文档 %s 加载完成，内容长度: %d", doc_id, len(load_result.content))

                # 2. 处理文本为空的情况（纯扫描件/纯图片文件）
                stripped_content = load_result.content.strip()
                if not stripped_content or len(stripped_content) < 10:
                    if self.ocr_manager:
                        # OCR 可用，对整个文件调用 OCR 识别
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
                            doc_id,
                            ocr_result.provider_name,
                            len(ocr_result.full_text),
                        )
                    else:
                        raise ValueError(
                            "文档提取文本为空，且未配置 OCR 服务"
                        )

                # 3. 处理嵌入图片：对文档中的图片调用 OCR，将识别文本追加到主文本
                if load_result.images and self.ocr_manager:
                    image_text = await self._ocr_embedded_images(
                        load_result.images, doc_id
                    )
                    if image_text:
                        # 将图片 OCR 文本追加到文档文本末尾，用明确的分隔标记
                        load_result = LoadResult(
                            content=load_result.content + "\n\n" + image_text,
                            metadata={
                                **load_result.metadata,
                                "has_ocr_images": True,
                                "ocr_image_count": len(load_result.images),
                            },
                            images=[],  # 已处理完毕
                        )
                        logger.info(
                            "文档 %s 嵌入图片 OCR 完成，追加文本长度: %d",
                            doc_id, len(image_text),
                        )
                elif load_result.images and not self.ocr_manager:
                    logger.warning(
                        "文档 %s 包含 %d 张嵌入图片，但未配置 OCR 服务，图片内容将被忽略",
                        doc_id, len(load_result.images),
                    )

                # 4. Chunk：父子 chunk 切分
                chunk_result = self.chunker.chunk(load_result.content, load_result.metadata)

                # 5. Enrich：富化（当前为 pass-through）
                enriched_children = await self.enricher.enrich(chunk_result.child_chunks)

                # 6. Embed：对子 chunk 生成稠密+稀疏向量
                embed_result = await self.embedder.embed(enriched_children)

                # 7. Index：写入 Milvus + SQLite
                # 生成父 chunk ID 映射
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

                    # 找到该子 chunk 对应的父 chunk
                    parent_idx = self._find_parent(chunk_result.parent_child_map, child_idx)
                    parent_id = parent_ids[parent_idx] if parent_idx is not None else None

                    # SQLite 元数据
                    child_chunk = Chunk(
                        id=child_id,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=parent_id,
                        content=child_text,
                        chunk_index=child_idx,
                    )
                    session.add(child_chunk)

                    # Milvus 向量数据
                    milvus_data.append({
                        "chunk_id": child_id,
                        "doc_id": doc_id,
                        "content": child_text[:65535],  # Milvus VARCHAR 长度限制
                        "dense_vector": embed_result.dense_vectors[child_idx],
                        "sparse_vector": embed_result.sparse_vectors[child_idx],
                        "parent_id": parent_id or "",
                        "chunk_index": child_idx,
                    })

                # 确保 collection 存在
                if not await self.milvus.has_collection(kb_id):
                    await self.milvus.create_collection(kb_id)

                # 批量写入 Milvus
                if milvus_data:
                    await self.milvus.insert(kb_id, milvus_data)

                # 8. 更新文档状态为 completed
                await self._update_status(session, doc_id, "completed", chunk_count=child_count)
                await session.commit()

                logger.info(
                    "文档 %s 处理完成，父块: %d，子块: %d",
                    doc_id,
                    len(chunk_result.parent_chunks),
                    child_count,
                )

            except Exception as e:
                await session.rollback()
                # 标记失败状态
                async with self.db_session_factory() as err_session:
                    await self._update_status(
                        err_session, doc_id, "failed", error_message=str(e)
                    )
                    await err_session.commit()
                logger.error("文档 %s 处理失败: %s", doc_id, e)
                raise

    async def _ocr_embedded_images(
        self, images: list[EmbeddedImage], doc_id: str
    ) -> str:
        """对文档中嵌入的图片逐张调用 OCR 识别，返回合并后的文本

        将图片数据写入临时文件，调用 OCR Manager 识别，
        识别完成后清理临时文件。

        Args:
            images: 嵌入图片列表
            doc_id: 文档 ID（用于日志）

        Returns:
            所有图片 OCR 识别文本的合并结果
        """
        ocr_texts: list[str] = []

        logger.info(
            "文档 %s 开始处理 %d 张嵌入图片的 OCR",
            doc_id, len(images),
        )

        for idx, img in enumerate(images):
            tmp_path = None
            try:
                # 将图片数据写入临时文件
                suffix = f".{img.format}" if img.format else ".png"
                with tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False
                ) as tmp_file:
                    tmp_file.write(img.data)
                    tmp_path = tmp_file.name

                # 调用 OCR 识别
                ocr_result = await self.ocr_manager.recognize(tmp_path)

                if ocr_result.full_text.strip():
                    # 添加位置标记，便于后续理解上下文
                    header = f"[图片内容 - 位置: 第{img.page_or_index}页/第{img.page_or_index}张]"
                    ocr_texts.append(f"{header}\n{ocr_result.full_text.strip()}")
                    logger.debug(
                        "文档 %s 图片 %d/%d OCR 成功，文本长度: %d",
                        doc_id, idx + 1, len(images), len(ocr_result.full_text),
                    )
                else:
                    logger.debug(
                        "文档 %s 图片 %d/%d OCR 结果为空，跳过",
                        doc_id, idx + 1, len(images),
                    )

            except Exception as e:
                logger.warning(
                    "文档 %s 图片 %d/%d OCR 失败: %s",
                    doc_id, idx + 1, len(images), e,
                )
            finally:
                # 清理临时文件
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        if ocr_texts:
            logger.info(
                "文档 %s 嵌入图片 OCR 完成，成功识别 %d/%d 张",
                doc_id, len(ocr_texts), len(images),
            )

        return "\n\n".join(ocr_texts)

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
