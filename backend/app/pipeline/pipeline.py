"""完整文档处理管道编排

流程：load → OCR(嵌入图片，并发+按页插入) → 合并文本 → chunk → enrich → embed → index

集成：
- ProgressTracker：各阶段加权进度更新
- PipelineLogger：结构化 JSON 日志 + 慢阶段检测
- trace_id：链路追踪贯穿全流程
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dataclasses import asdict

from app.config import get_settings
from app.models.manager import ModelManager
from app.pipeline.chunker_router import ChunkerFactory, ChunkerRouter
import app.pipeline.chunkers  # noqa: F401 — 确保所有 Chunker 注册到 Factory
from app.pipeline.embedder import PipelineEmbedder
from app.pipeline.enricher import Enricher
from app.pipeline.cleaner import TextCleaner
from app.pipeline.loader import EmbeddedImage, LoadResult, get_loader
from app.pipeline.logging import PipelineLogger
from app.pipeline.context_embedder import ContextualEmbedder
from app.pipeline.metadata import ChunkMetadata, MetadataExtractor
from app.pipeline.progress import PipelineStage, ProgressTracker
from app.schema.db import Chunk, Document, KnowledgeBase
from app.storage.milvus import MilvusClient

if TYPE_CHECKING:
    from app.pipeline.ocr.manager import OCRManager
    from app.pipeline.ocr.provider import OCRResult

logger = logging.getLogger(__name__)

# 并发 OCR 的最大并行数
_OCR_CONCURRENCY = 4


class CancelledError(Exception):
    """文档处理被取消（文档已删除或用户主动取消）"""
    pass


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
        self.enricher = Enricher(llm=None, enabled=False)
        self.embedder = PipelineEmbedder(embed_provider=model_manager.embedder)

    async def process(
        self, file_path: str, doc_id: str, kb_id: str, trace_id: str | None = None
    ) -> None:
        """完整文档处理流程

        Args:
            file_path: 文件路径
            doc_id: 文档 ID
            kb_id: 知识库 ID
            trace_id: 链路追踪 ID，不传则自动生成 UUID4
        """
        # 生成或使用传入的 trace_id
        if not trace_id:
            trace_id = str(uuid.uuid4())

        # 初始化进度追踪器和结构化日志器
        settings = get_settings()
        tracker = ProgressTracker(doc_id, self.db_session_factory)
        pl = PipelineLogger(
            trace_id=trace_id,
            doc_id=doc_id,
            slow_threshold_ms=settings.pipeline_slow_threshold_ms,
        )

        pipeline_start = time.monotonic()
        current_stage = PipelineStage.LOAD
        images_to_cleanup: list[EmbeddedImage] = []

        async with self.db_session_factory() as session:
            try:
                # 更新状态为 processing（立即提交，让前端轮询能看到）
                await self._update_status(session, doc_id, "processing")
                await session.commit()

                # ─── 1. Load 阶段 ───
                current_stage = PipelineStage.LOAD
                await tracker.start_stage(PipelineStage.LOAD, "正在加载文档")
                stage_start = time.monotonic()

                ext = Path(file_path).suffix.lstrip(".")
                loader = get_loader(ext)
                print(f"[Pipeline] 文档 {doc_id} 开始加载，类型: {ext}")
                load_result = loader.load(file_path)
                images_to_cleanup = load_result.images
                print(f"[Pipeline] 文档 {doc_id} 加载完成，内容长度: {len(load_result.content)}")
                logger.info("文档 %s 加载完成，内容长度: %d", doc_id, len(load_result.content))

                load_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.LOAD)
                pl.stage_complete(
                    stage="load",
                    duration_ms=load_duration_ms,
                    input_size=1,  # 1 个文件
                    output_size=len(load_result.content),
                )

                # ─── 2. OCR 阶段 ───
                await self._check_cancelled(doc_id)
                current_stage = PipelineStage.OCR
                stripped_content = load_result.content.strip()
                needs_ocr = (not stripped_content or len(stripped_content) < 10) and self.ocr_manager
                has_embedded_images = bool(load_result.images and self.ocr_manager)

                if needs_ocr or has_embedded_images:
                    await tracker.start_stage(PipelineStage.OCR, "正在进行 OCR 识别")
                    stage_start = time.monotonic()

                    if needs_ocr:
                        print(f"[Pipeline] 文档 {doc_id} 文本为空或过短(长度={len(stripped_content)})，触发整文件 OCR")
                        ocr_result = await self.ocr_manager.recognize(file_path)
                        load_result = LoadResult(
                            content=ocr_result.full_text,
                            metadata={
                                **load_result.metadata,
                                "ocr_provider": ocr_result.provider_name,
                            },
                            images=[],
                        )
                        print(f"[Pipeline] 文档 {doc_id} OCR 完成, Provider: {ocr_result.provider_name}, 文本长度: {len(ocr_result.full_text)}")
                        logger.info(
                            "文档 %s 通过 OCR (%s) 获取文本，长度: %d",
                            doc_id, ocr_result.provider_name, len(ocr_result.full_text),
                        )
                    elif not stripped_content or len(stripped_content) < 10:
                        # 文本为空且无 OCR manager
                        raise ValueError("文档提取文本为空，且未配置 OCR 服务")

                    # 处理嵌入图片
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
                    else:
                        final_content = load_result.content

                    ocr_duration_ms = int((time.monotonic() - stage_start) * 1000)
                    await tracker.complete_stage(PipelineStage.OCR)
                    pl.stage_complete(
                        stage="ocr",
                        duration_ms=ocr_duration_ms,
                        input_size=len(stripped_content),
                        output_size=len(final_content),
                    )
                else:
                    # 跳过 OCR 阶段
                    if not stripped_content or len(stripped_content) < 10:
                        if not self.ocr_manager:
                            raise ValueError("文档提取文本为空，且未配置 OCR 服务")

                    final_content = load_result.content
                    if load_result.images and not self.ocr_manager:
                        logger.warning(
                            "文档 %s 包含 %d 张嵌入图片，但未配置 OCR 服务，图片内容将被忽略",
                            doc_id, len(load_result.images),
                        )

                    await tracker.skip_stage(PipelineStage.OCR)

                # ─── 2.5 TextCleaner 去噪阶段 ───
                # 从知识库 config 读取 enable_cleaner 开关（默认 True）
                enable_cleaner = True
                result_kb = await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
                )
                kb_obj = result_kb.scalar_one_or_none()
                if kb_obj and kb_obj.config and isinstance(kb_obj.config, dict):
                    enable_cleaner = kb_obj.config.get("enable_cleaner", True)

                if enable_cleaner:
                    cleaner = TextCleaner()
                    # 如果 final_content 经过了嵌入图片 OCR 合并，page_blocks 不再准确
                    # （page_blocks 只包含 pymupdf 提取的原始文本块，不含图片 OCR 文本）
                    use_page_blocks = load_result.page_blocks if load_result.page_blocks else None
                    if final_content != load_result.content:
                        use_page_blocks = None
                    final_content = cleaner.clean(
                        content=final_content,
                        page_texts=load_result.page_texts if load_result.page_texts else None,
                        page_blocks=use_page_blocks,
                    )
                    logger.info(
                        "文档 %s TextCleaner 去噪完成，文本长度: %d",
                        doc_id, len(final_content),
                    )

                # ─── 2.6 清洗后文本为空时，尝试整文件 OCR 兜底 ───
                cleaned_stripped = final_content.strip()
                if (not cleaned_stripped or len(cleaned_stripped) < 10) and self.ocr_manager and not needs_ocr:
                    print(f"[Pipeline] 文档 {doc_id} 清洗后文本为空或过短(长度={len(cleaned_stripped)})，触发整文件 OCR 兜底")
                    logger.info(
                        "文档 %s 清洗后文本为空，触发整文件 OCR 兜底", doc_id
                    )
                    # 之前没有走过整文件 OCR，现在补做
                    await tracker.start_stage(PipelineStage.OCR, "正在进行 OCR 识别（清洗后兜底）")
                    stage_start = time.monotonic()

                    ocr_result = await self.ocr_manager.recognize(file_path)
                    final_content = ocr_result.full_text
                    load_result = LoadResult(
                        content=final_content,
                        metadata={
                            **load_result.metadata,
                            "ocr_provider": ocr_result.provider_name,
                        },
                        images=[],
                    )
                    print(f"[Pipeline] 文档 {doc_id} OCR 兜底完成, Provider: {ocr_result.provider_name}, 文本长度: {len(final_content)}")

                    ocr_duration_ms = int((time.monotonic() - stage_start) * 1000)
                    await tracker.complete_stage(PipelineStage.OCR)
                    pl.stage_complete(
                        stage="ocr",
                        duration_ms=ocr_duration_ms,
                        input_size=0,
                        output_size=len(final_content),
                    )

                # ─── 3. Chunk 阶段 ───
                await self._check_cancelled(doc_id)
                current_stage = PipelineStage.CHUNK
                await tracker.start_stage(PipelineStage.CHUNK, "正在切分文档")
                stage_start = time.monotonic()

                print(f"[Pipeline] 文档 {doc_id} 开始分块，文本长度: {len(final_content)}")

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
                    # 选择 Chunker：优先使用知识库 config 中的 chunker_type，否则自动路由
                    chunker = await self._select_chunker(session, kb_id, ext, final_content)
                    chunk_result = chunker.chunk(final_content, load_result.metadata)
                    print(f"[Pipeline] 文档 {doc_id} 分块完成，父块: {len(chunk_result.parent_chunks)}，子块: {len(chunk_result.child_chunks)}")

                if len(chunk_result.child_chunks) > 50000:
                    print(f"[Pipeline] ⚠️ 文档 {doc_id} 子块数量较多: {len(chunk_result.child_chunks)}，处理时间可能较长")
                    logger.warning(
                        "文档 %s 子块数量 %d，处理时间可能较长",
                        doc_id, len(chunk_result.child_chunks),
                    )

                # Enrich（当前为 pass-through）
                enriched_children = await self.enricher.enrich(chunk_result.child_chunks)

                chunk_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.CHUNK)
                pl.stage_complete(
                    stage="chunk",
                    duration_ms=chunk_duration_ms,
                    input_size=len(final_content),
                    output_size=len(enriched_children),
                )

                # ─── 4. Embed 阶段 ───
                await self._check_cancelled(doc_id)
                current_stage = PipelineStage.EMBED
                await tracker.start_stage(
                    PipelineStage.EMBED,
                    f"正在生成向量 (共 {len(enriched_children)} 个文本块)",
                )
                stage_start = time.monotonic()

                print(f"[Pipeline] 文档 {doc_id} 开始 embedding，共 {len(enriched_children)} 个子块")

                # 提取元数据（供上下文增强和 Index 阶段使用）
                extractor = MetadataExtractor()
                metadata_list = extractor.extract(
                    child_chunks=enriched_children,
                    parent_chunks=chunk_result.parent_chunks,
                    parent_child_map=chunk_result.parent_child_map,
                    doc_metadata=load_result.metadata,
                    page_texts=load_result.page_texts if load_result.page_texts else None,
                )

                # 使用 ContextualEmbedder 构造上下文增强的 embedding 输入
                ctx_embedder = ContextualEmbedder()
                embed_texts = []
                for child_idx, (child_text, meta) in enumerate(zip(enriched_children, metadata_list)):
                    parent_idx = self._find_parent(chunk_result.parent_child_map, child_idx)
                    parent_text = chunk_result.parent_chunks[parent_idx] if parent_idx is not None else None
                    embed_text = ctx_embedder.build_embed_text(child_text, meta, parent_text)
                    embed_texts.append(embed_text)

                # 使用增强后的文本进行 embedding
                embed_result = await self._embed_with_progress(
                    embed_texts, tracker, doc_id
                )

                embed_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.EMBED)

                # 计算 embed 阶段额外统计
                batch_size = self.embedder.batch_size
                total_chunks = len(enriched_children)
                batch_count = (total_chunks + batch_size - 1) // batch_size
                avg_batch_duration_ms = (
                    embed_duration_ms // batch_count if batch_count > 0 else 0
                )

                pl.stage_complete(
                    stage="embed",
                    duration_ms=embed_duration_ms,
                    input_size=len(enriched_children),
                    output_size=len(enriched_children),
                    batch_count=batch_count,
                    total_chunks=total_chunks,
                    avg_batch_duration_ms=avg_batch_duration_ms,
                )
                print(f"[Pipeline] 文档 {doc_id} embedding 完成")

                # ─── 5. Index 阶段 ───
                await self._check_cancelled(doc_id)
                current_stage = PipelineStage.INDEX
                await tracker.start_stage(PipelineStage.INDEX, "正在写入索引")
                stage_start = time.monotonic()

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

                    # 构造 chunk_metadata JSON
                    meta = metadata_list[child_idx]
                    chunk_metadata_dict = asdict(meta)

                    child_chunk = Chunk(
                        id=child_id,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=parent_id,
                        content=child_text,
                        chunk_index=child_idx,
                        chunk_metadata=chunk_metadata_dict,
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
                        "file_type": meta.file_type,
                        "element_type": meta.element_type,
                    })

                # 确保 collection 存在
                if not await self.milvus.has_collection(kb_id):
                    await self.milvus.create_collection(kb_id)

                # 检查 collection schema 版本，兼容旧 schema
                schema_info = await self.milvus.check_schema_version(kb_id)
                if schema_info["exists"] and not schema_info["has_new_fields"]:
                    for record in milvus_data:
                        record.pop("file_type", None)
                        record.pop("element_type", None)
                    logger.info("文档 %s: 旧 schema collection，跳过 file_type/element_type 字段", doc_id)

                # 批量写入 Milvus
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

                index_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.INDEX)
                pl.stage_complete(
                    stage="index",
                    duration_ms=index_duration_ms,
                    input_size=len(milvus_data),
                    output_size=len(milvus_data),
                )

                # ─── 完成 ───
                await self._update_status(session, doc_id, "completed", chunk_count=child_count)
                await session.commit()
                await tracker.complete()

                total_duration_ms = int((time.monotonic() - pipeline_start) * 1000)
                pl.summary(total_duration_ms)

                print(f"[Pipeline] 文档 {doc_id} 全部处理完成 ✓ 父块: {len(chunk_result.parent_chunks)}，子块: {child_count}")
                logger.info(
                    "文档 %s 处理完成，父块: %d，子块: %d",
                    doc_id, len(chunk_result.parent_chunks), child_count,
                )

            except CancelledError:
                print(f"[Pipeline] 文档 {doc_id} 处理已取消（文档被删除）")
                logger.info("文档 %s 处理已取消", doc_id)
                await session.rollback()
                # 不标记失败，不 re-raise（任务静默终止）

            except Exception as e:
                import traceback
                print(f"[Pipeline] 文档 {doc_id} 处理失败: {type(e).__name__}: {e}")
                traceback.print_exc()
                await session.rollback()

                # 进度追踪：标记失败（progress 值不变）
                await tracker.fail(current_stage, f"{type(e).__name__}: {e}")

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

    async def _select_chunker(
        self, session: AsyncSession, kb_id: str, file_type: str, content: str
    ):
        """选择 Chunker：优先使用知识库 config 中的 chunker_type，否则自动路由

        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            file_type: 文件扩展名
            content: 文档文本内容

        Returns:
            BaseChunker 实例
        """
        # 查询知识库 config 中是否指定了 chunker_type
        chunker_type = None
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
        kb = result.scalar_one_or_none()
        if kb and kb.config and isinstance(kb.config, dict):
            chunker_type = kb.config.get("chunker_type")

        if chunker_type:
            # 手动覆盖：使用知识库配置指定的 Chunker
            logger.info("知识库 %s 手动指定 chunker_type=%s", kb_id, chunker_type)
            try:
                return ChunkerFactory.create(chunker_type)
            except ValueError:
                logger.warning(
                    "知识库 %s 指定的 chunker_type=%s 未注册，回退到自动路由",
                    kb_id, chunker_type,
                )

        # 自动路由：根据文件类型和内容特征选择
        selected_type = ChunkerRouter.select(file_type, content)
        logger.info("自动路由选择 chunker_type=%s (file_type=%s)", selected_type, file_type)
        return ChunkerFactory.create(selected_type)

    async def _embed_with_progress(
        self,
        texts: list[str],
        tracker: ProgressTracker,
        doc_id: str = "",
    ):
        """带进度追踪的 embed 调用

        逐批调用 embedder，每完成一批更新子进度到数据库，
        前端轮询时能看到实时进度。

        Args:
            texts: 待向量化的文本列表
            tracker: 进度追踪器
            doc_id: 文档 ID（用于日志）

        Returns:
            EmbedResult
        """
        from app.pipeline.embedder import EmbedResult

        if not texts:
            return EmbedResult(dense_vectors=[], sparse_vectors=[])

        batch_size = self.embedder.batch_size
        total_batches = (len(texts) + batch_size - 1) // batch_size

        # 批次较少时直接一次性处理
        if total_batches <= 2:
            result = await self.embedder.embed(texts)
            await tracker.update_sub_progress(
                PipelineStage.EMBED, total_batches, total_batches,
                f"正在生成向量 ({total_batches}/{total_batches} 批)"
            )
            return result

        # 批次较多时，逐批处理并实时更新进度
        import asyncio
        import math

        # 清洗和截断文本（复用 embedder 的逻辑）
        sanitized = self.embedder._sanitize_texts(texts)
        sanitized = self.embedder._truncate_texts(sanitized)

        # 构建批次
        batches = []
        for i in range(0, len(sanitized), batch_size):
            batches.append(sanitized[i:i + batch_size])

        print(f"[Pipeline] 文档 {doc_id} embed 逐批处理: {total_batches} 批, batch_size={batch_size}, 并发={self.embedder.concurrency}")

        # 并发处理，但每完成一批就更新进度
        semaphore = asyncio.Semaphore(self.embedder.concurrency)
        results: list[tuple[list[list[float]], list[dict[int, float]]] | None] = [None] * len(batches)
        completed_count = 0
        progress_lock = asyncio.Lock()

        async def _process_batch(batch_idx: int, batch: list[str]):
            nonlocal completed_count
            # 每批开始前检查是否已取消
            async with self.db_session_factory() as check_session:
                r = await check_session.execute(
                    select(Document.status).where(Document.id == doc_id)
                )
                st = r.scalar_one_or_none()
                if st is None or st == "cancelled":
                    raise CancelledError(f"文档 {doc_id} 已被取消或删除")

            async with semaphore:
                dense = await self.embedder.provider.embed(batch)
                sparse = await self.embedder.provider.embed_sparse(batch)
                results[batch_idx] = (dense, sparse)

            # 更新进度
            async with progress_lock:
                completed_count += 1
                # 每 5 批或最后一批更新一次进度（避免过于频繁的 DB 写入）
                if completed_count % 5 == 0 or completed_count == total_batches:
                    await tracker.update_sub_progress(
                        PipelineStage.EMBED, completed_count, total_batches,
                        f"正在生成向量 ({completed_count}/{total_batches} 批)"
                    )
                # 日志：每 10% 输出一次
                if total_batches > 10 and completed_count % max(1, total_batches // 10) == 0:
                    print(f"[Pipeline] 文档 {doc_id} embed 进度: {completed_count}/{total_batches} 批 ({completed_count * 100 // total_batches}%)")

        await asyncio.gather(*[_process_batch(i, batch) for i, batch in enumerate(batches)])

        # 合并结果
        all_dense: list[list[float]] = []
        all_sparse: list[dict[int, float]] = []
        for dense, sparse in results:
            all_dense.extend(dense)
            all_sparse.extend(sparse)

        # 修复 NaN
        has_nan = False
        for idx, vec in enumerate(all_dense):
            if any(math.isnan(v) or math.isinf(v) for v in vec):
                has_nan = True
                all_dense[idx] = self.embedder._fix_nan_vector(vec)
        for idx, sp in enumerate(all_sparse):
            if not sp or any(math.isnan(v) or math.isinf(v) for v in sp.values()):
                has_nan = True
                all_sparse[idx] = self.embedder._fix_nan_sparse(sp)
        if has_nan:
            logger.warning("检测到 embedding 结果包含 NaN，已修复")

        print(f"[Pipeline] 文档 {doc_id} embed 完成，共 {len(all_dense)} 个向量")
        return EmbedResult(dense_vectors=all_dense, sparse_vectors=all_sparse)

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

    async def _check_cancelled(self, doc_id: str) -> None:
        """检查文档是否已被取消/删除，是则抛出异常终止处理"""
        async with self.db_session_factory() as session:
            result = await session.execute(
                select(Document.status).where(Document.id == doc_id)
            )
            status = result.scalar_one_or_none()
            if status is None or status == "cancelled":
                raise CancelledError(f"文档 {doc_id} 已被取消或删除")
