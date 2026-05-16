"""完整文档处理管道编排

流程：load → chunk → enrich → embed → index（写入 Milvus + SQLite）
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.manager import ModelManager
from app.pipeline.chunker import HierarchicalChunker
from app.pipeline.embedder import PipelineEmbedder
from app.pipeline.enricher import Enricher
from app.pipeline.loader import get_loader
from app.schema.db import Chunk, Document
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)


class DocumentPipeline:
    """文档处理管道，编排 load → chunk → enrich → embed → index 全流程"""

    def __init__(
        self,
        model_manager: ModelManager,
        milvus_client: MilvusClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ):
        self.milvus = milvus_client
        self.db_session_factory = db_session_factory
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

                # 检查提取的文本是否为空（常见于扫描件 PDF）
                # TODO: 后续集成 OCR（PaddleOCR / pytesseract）支持扫描件文档
                stripped_content = load_result.content.strip()
                if not stripped_content or len(stripped_content) < 10:
                    raise ValueError(
                        "文档提取文本为空，可能是扫描件或图片型文档，当前暂不支持 OCR 识别"
                    )

                # 2. Chunk：父子 chunk 切分
                chunk_result = self.chunker.chunk(load_result.content, load_result.metadata)

                # 3. Enrich：富化（当前为 pass-through）
                enriched_children = await self.enricher.enrich(chunk_result.child_chunks)

                # 4. Embed：对子 chunk 生成稠密+稀疏向量
                embed_result = await self.embedder.embed(enriched_children)

                # 5. Index：写入 Milvus + SQLite
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

                # 6. 更新文档状态为 completed
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
