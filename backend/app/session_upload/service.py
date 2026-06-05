"""会话级文件上传核心服务（design C4 / Task 7）

职责：
- ``upload``: 同步建会话文件索引（文件大小校验 → 累计文件数校验 → pipeline 解耦单元
  Load/OCR/Clean/Chunk → Pre_Embed_Gate → Embed → 写共享 collection ``kb_session_files``
  + ``session_files`` / ``session_chunks`` 关系表）。失败清理已部分写入向量与回滚 DB
  事务，不留孤儿（Req 1.10）。
- ``list_files`` / ``has_files`` / ``used_chunks`` / ``used_files``: 会话维度查询
  （配额聚合 / 检索接入判断 / 文件列表展示）。
- ``remove_file``: 单文件移除——按 ``doc_id == file_id`` 删向量 + 删 ``session_files``
  / ``session_chunks`` 行，释放配额（Req 1.8 / 6.7）。
- ``cleanup_session_files``: 删会话级联清理——按 ``session_id`` 删共享库向量 +
  ``session_chunks`` 行（``session_files`` 由 FK CASCADE 删）。失败仅记 WARNING、
  不阻塞会话删除主流程（Req 1.6）。

向量入共享 collection ``kb_session_files``，每条带 ``session_id`` 标量字段做会话级隔离
（参考 WeKnora 的"共享 collection + 标量过滤"模式）；``file_id`` 复用为 Milvus
``doc_id``，使 ``delete_by_doc_id`` 能精准移除单文件向量。删向量后 publish
``InvalidationBus`` 失效 ``kb_session_files`` 的 ``_loaded_at``（复用 bugfix
``retrieval-pipeline-hardening`` 的失效广播基础设施），避免其他 API 进程在
``load_cache_ttl`` 内仍读到已删向量。

embedding 走 ``DocumentPipeline`` 的全局信号量；上传文件落盘到
``data/uploads/sessions/`` 仅供同步处理使用，处理完毕（成功 / 失败）后删除。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete as sa_delete, func, select

from app.api.errors import FileTooLargeError
from app.pipeline.factory import create_pipeline
from app.schema.db import SessionChunk, SessionFile
from app.storage.database import async_session
from app.storage.invalidation import get_invalidation_bus
from app.storage.milvus import SESSION_FILES_KB_ID, get_milvus_client

if TYPE_CHECKING:
    from app.session_upload.limits import UploadLimits

logger = logging.getLogger(__name__)


# ============================================================
# 模块常量（避免魔法值）
# ============================================================

# 会话上传文件落盘目录：与 KB 上传 ``data/uploads`` 同根、子目录隔离。文件仅在同步建索引
# 期间需要（向量与 chunk 文本已落库），处理完毕（成功 / 失败）即删除。
_SESSION_UPLOAD_DIR = Path("data/uploads/sessions")

# Milvus 单批写入上限：与正式 KB 路径（pipeline.py）保持一致，避免一次性 RPC 过大。
_MILVUS_INSERT_BATCH_SIZE = 1000


# ============================================================
# 视图对象
# ============================================================


@dataclass(frozen=True)
class SessionFileVO:
    """会话文件视图对象（VO，对外暴露的会话文件元数据快照）。"""

    id: str
    session_id: str
    filename: str
    file_type: str | None
    file_size: int | None
    chunk_count: int
    status: str
    created_at: datetime


def _row_to_vo(row: SessionFile) -> SessionFileVO:
    """ORM ``SessionFile`` -> ``SessionFileVO`` 的轻量映射（避免散落字段拷贝）。"""
    return SessionFileVO(
        id=row.id,
        session_id=row.session_id,
        filename=row.filename,
        file_type=row.file_type,
        file_size=row.file_size,
        chunk_count=row.chunk_count,
        status=row.status,
        created_at=row.created_at,
    )


# ============================================================
# 服务核心
# ============================================================


class SessionUploadService:
    """会话级文件上传：建索引 / 列出 / 移除 / 删会话级联清理。

    无状态（不持额外缓存），依赖项均通过构造器注入便于测试；默认依赖经默认参数解析为
    工程内单例（``async_session`` / ``get_milvus_client`` / ``create_pipeline``）。
    """

    def __init__(
        self,
        *,
        milvus_client=None,
        db_session_factory=None,
        pipeline_factory=None,
    ):
        # 延迟解析 Milvus 单例：测试场景未配置 Milvus 时不阻塞导入。
        self._milvus = milvus_client
        self._db_session_factory = db_session_factory or async_session
        self._pipeline_factory = pipeline_factory or create_pipeline

    @property
    def milvus(self):
        """惰性获取 Milvus 客户端（避免模块导入期触达 Milvus 配置）。"""
        if self._milvus is None:
            self._milvus = get_milvus_client()
        return self._milvus

    # ------------------------------------------------------------------
    # 查询接口（轻量 SELECT，供配额校验 / 检索接入 / 文件列表展示）
    # ------------------------------------------------------------------

    async def has_files(self, session_id: str) -> bool:
        """该会话是否已上传文件（供检索决定是否追加会话源 cfg，design C7）。"""
        async with self._db_session_factory() as session:
            count = await session.scalar(
                select(func.count(SessionFile.id)).where(
                    SessionFile.session_id == session_id
                )
            )
            return bool(count or 0)

    async def used_chunks(self, session_id: str) -> int:
        """该会话累计 child chunk 数 = ``session_files.chunk_count`` 之和（Req 6.4）。

        移除文件即删 ``session_files`` 行，配额自动释放（Req 6.7）。
        """
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(SessionFile.chunk_count), 0)).where(
                    SessionFile.session_id == session_id
                )
            )
            return int(result.scalar_one() or 0)

    async def used_files(self, session_id: str) -> int:
        """该会话已用文件数（查询接口，供文件列表展示/调试用）。"""
        async with self._db_session_factory() as session:
            count = await session.scalar(
                select(func.count(SessionFile.id)).where(
                    SessionFile.session_id == session_id
                )
            )
            return int(count or 0)

    async def list_files(self, session_id: str) -> list[SessionFileVO]:
        """列出会话已上传文件（Req 1.8），按上传时间倒序（最新在前）。"""
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(SessionFile)
                .where(SessionFile.session_id == session_id)
                .order_by(SessionFile.created_at.desc())
            )
            return [_row_to_vo(row) for row in result.scalars().all()]

    # ------------------------------------------------------------------
    # 写路径
    # ------------------------------------------------------------------

    async def upload(
        self,
        *,
        session_id: str,
        tenant_id: str | None,
        owner_user_id: str | None,
        filename: str,
        content: bytes,
        limits: "UploadLimits",
    ) -> SessionFileVO:
        """同步建会话文件索引。

        流程：
        1. 文件大小校验：零解析成本拒绝超限文件。
        2. 落盘到临时区。
        3. pipeline.process_to_vectors(source_kind="session")：内部 Pre_Embed_Gate
           按 kb_chunk_cap 在 Embed 之前精确判定（临时文件 = 会话级 KB，统一 chunk 闸门）。
        4. ensure_session_files_collection（幂等）。
        5. 写 Milvus（每条带 session_id 标量、doc_id == file_id）。
        6. 写 session_files + session_chunks（单事务）。
        7. publish 失效广播。

        Raises:
            FileTooLargeError: 文件大小超 upload_max_file_bytes。
            UploadCapExceeded: Chunk 数超 kb_chunk_cap（Pre_Embed_Gate）。
            ValueError: 无可提取文本。
        """
        # 1) 文件大小校验
        file_size = len(content)
        if file_size > limits.upload_max_file_bytes:
            raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

        # 2) 落盘到临时区（处理完毕 finally 中删除）
        ext = Path(filename).suffix.lstrip(".").lower()
        file_id = str(uuid.uuid4())
        _SESSION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        save_path = _SESSION_UPLOAD_DIR / f"{file_id}.{ext}"
        save_path.write_bytes(content)

        try:
            # 4) Pipeline 不依赖 Document 表的同步处理单元（Load→OCR→Clean→Chunk→
            #    Pre_Embed_Gate→Embed）。Pre_Embed_Gate 内部用精确 child chunk 数
            #    + 该会话已用 chunk 累计判定，超限抛 UploadCapExceeded（不写向量）。
            pipeline = await self._pipeline_factory()
            processed = await pipeline.process_to_vectors(
                str(save_path),
                source_kind="session",
                source_id=session_id,
                tenant_id=tenant_id,
                limits=limits,
            )

            child_count = len(processed.enriched_children)
            if child_count == 0:
                # 防御性兜底：pipeline 通常已对空内容 raise ValueError；走到此处说明
                # 切分后零 child chunk（极端边界），无可建索引内容，统一报错告知用户。
                raise ValueError("文档无可提取内容，无法建立索引")

            # 5) 共享 collection 幂等建表（首个会话上传时创建）
            await self.milvus.ensure_session_files_collection()

            # 6) 组装 Milvus 数据 + 父/子 SessionChunk 行
            #    parent SessionChunk 行（parent_id=None）供检索后父块扩展取内容；
            #    child SessionChunk 行（parent_id=parent_uuid）与 Milvus 子块一一对应。
            chunk_result = processed.chunk_result
            metadata_list = processed.metadata_list
            embed_result = processed.embed_result
            child_to_parent = processed.child_to_parent

            doc_title = Path(filename).stem  # BM25 content 前缀（与 KB 路径对齐）
            parent_ids: list[str] = [
                str(uuid.uuid4()) for _ in chunk_result.parent_chunks
            ]

            parent_chunk_records: list[SessionChunk] = [
                SessionChunk(
                    id=pid,
                    file_id=file_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    parent_id=None,
                    content=ptext,
                    chunk_index=idx,
                )
                for idx, (pid, ptext) in enumerate(
                    zip(parent_ids, chunk_result.parent_chunks)
                )
            ]

            child_chunk_records: list[SessionChunk] = []
            milvus_data: list[dict] = []
            for child_idx, child_text in enumerate(processed.enriched_children):
                child_id = str(uuid.uuid4())
                parent_idx = child_to_parent.get(child_idx)
                parent_id = parent_ids[parent_idx] if parent_idx is not None else None

                meta = metadata_list[child_idx]
                child_chunk_records.append(
                    SessionChunk(
                        id=child_id,
                        file_id=file_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        parent_id=parent_id,
                        content=child_text,
                        chunk_index=child_idx,
                    )
                )

                # BM25 content 增强：加文件名前缀（与 pipeline.py KB 路径对齐，帮助
                # BM25 / Rerank 区分同结构文档）。Dense embedding 不受影响（已用原文）。
                # 字节截断由 ``MilvusClient._insert_sync`` 内部统一处理，不在此重复。
                enhanced_content = (
                    f"[{doc_title}] {child_text}" if doc_title else child_text
                )
                milvus_data.append({
                    "chunk_id": child_id,
                    # file_id 复用为 doc_id：使 delete_by_doc_id 能精准移除单文件向量（Req 1.8）
                    "doc_id": file_id,
                    "content": enhanced_content,
                    "dense_vector": embed_result.dense_vectors[child_idx],
                    "sparse_vector": embed_result.sparse_vectors[child_idx],
                    "parent_id": parent_id or "",
                    "chunk_index": child_idx,
                    "file_type": meta.file_type,
                    "element_type": meta.element_type,
                    # 共享 collection 隔离的核心字段：检索时 expr 按 session_id 过滤（Req 1.11）
                    "session_id": session_id,
                })

            # 7a) 写 Milvus（批量；任一阶段失败清理 + 传播）
            try:
                if len(milvus_data) <= _MILVUS_INSERT_BATCH_SIZE:
                    await self.milvus.insert(SESSION_FILES_KB_ID, milvus_data)
                else:
                    for offset in range(0, len(milvus_data), _MILVUS_INSERT_BATCH_SIZE):
                        await self.milvus.insert(
                            SESSION_FILES_KB_ID,
                            milvus_data[offset:offset + _MILVUS_INSERT_BATCH_SIZE],
                        )
            except Exception:
                # 部分批次可能已写入；按 doc_id == file_id 清理（幂等）。
                await self._safe_cleanup_vectors(file_id)
                raise

            # 7b) 写 SessionFile + SessionChunk（单事务，commit 失败 with 自动回滚）
            #     顺序关键：SessionChunk.file_id 外键指向 session_files.id，PostgreSQL 强制
            #     外键约束，必须先插入父行 SessionFile 再插入子行 SessionChunk，否则触发
            #     IntegrityError（外键违反）。SessionChunk 用裸 file_id 列（无 ORM
            #     relationship），SQLAlchemy 工作单元不会自动为其排序，故显式 add 父行 +
            #     flush 后再 add 子行。（SQLite 默认不强制外键，故单测未暴露此问题。）
            try:
                async with self._db_session_factory() as session:
                    session.add(
                        SessionFile(
                            id=file_id,
                            session_id=session_id,
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            filename=filename,
                            file_type=ext,
                            file_size=file_size,
                            chunk_count=child_count,
                            status="completed",
                        )
                    )
                    # 先 flush 父行，确保 session_files 行已存在，满足子行外键约束。
                    await session.flush()
                    for row in parent_chunk_records:
                        session.add(row)
                    for row in child_chunk_records:
                        session.add(row)
                    await session.commit()
            except Exception:
                # DB 写失败 → Milvus 已写入需清理（with 已回滚 DB 事务）。
                await self._safe_cleanup_vectors(file_id)
                raise

            # 8) 失效广播：让其他 API 进程清 ``kb_session_files`` 的 ``_loaded_at``
            #    （main.py 的 _handle_kb_data 已绑定该 collection 的失效逻辑）。
            await self._publish_invalidation()

            # 回读最新 SessionFile（取 server_default 时间字段填充 VO）
            return await self._fetch_one(file_id)

        finally:
            # 临时文件无论成败均清理（向量与 chunk 文本已落库，原文件不再需要）
            self._cleanup_temp_file(save_path)

    async def remove_file(
        self, *, session_id: str, file_id: str
    ) -> None:
        """移除单个会话文件（Req 1.8 / 6.7）。

        - 按 ``doc_id == file_id`` 删 Milvus 向量（仅清此文件，其余文件 / 会话不受影响）。
        - 删 ``session_chunks``（``file_id`` FK CASCADE 兜底，但显式删一次确保
          SQLite 等不一定开 FK 的环境也生效）+ 删 ``session_files`` 行（释放配额）。
        - publish ``InvalidationBus`` 失效 ``kb_session_files`` 的 ``_loaded_at``，避免
          其他 API 进程在 ``load_cache_ttl`` 内仍命中已删向量的旧加载快照（design C7.1）。

        Args:
            session_id: 校验文件归属的会话（防止跨会话误删）。
            file_id: ``SessionFile.id`` == Milvus ``doc_id``。

        Raises:
            ValueError: 文件不存在或不属于该会话。
        """
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None or row.session_id != session_id:
                raise ValueError("文件不存在或不属于该会话")

            # 先删向量：DB 行尚未删除时即使删向量失败，session_id expr 过滤仍能让用户
            # 看不到这些向量（隔离前提下安全），失败仅记 WARNING 不阻塞 DB 主流程。
            try:
                await self.milvus.delete_by_doc_id(SESSION_FILES_KB_ID, file_id)
            except Exception as e:
                logger.warning(
                    "会话文件 %s 向量删除失败（继续删 DB 行）: %s", file_id, e
                )

            # 显式删 session_chunks（FK CASCADE 兜底）+ 删 session_files
            await session.execute(
                sa_delete(SessionChunk).where(SessionChunk.file_id == file_id)
            )
            await session.delete(row)
            await session.commit()

        # 失效广播（与上传路径同款 kb_data 信号，main.py 的 _handle_kb_data 处理）
        await self._publish_invalidation()

    async def cleanup_session_files(self, session_id: str) -> None:
        """删会话级联清理（Req 1.6）。

        - ``delete_session(sid)`` 删共享库中该会话全部向量（按 ``session_id`` 标量 expr）。
        - 显式删 ``session_chunks``（``session_files`` 由 ChatSession FK CASCADE 删，
          ``session_chunks`` 由 ``session_files`` FK CASCADE 兜底，但显式删一次确保
          SQLite 等环境也生效）。
        - publish 失效广播。

        失败仅记 WARNING、不阻塞会话删除主流程（Req 1.6）：会话删除是更上层的语义，
        清理失败可由后台对账兜底（无对应 ChatSession 的孤儿向量）。
        """
        try:
            await self.milvus.delete_session(session_id)
        except Exception as e:
            logger.warning("会话 %s 向量清理失败（非致命）: %s", session_id, e)

        try:
            async with self._db_session_factory() as session:
                await session.execute(
                    sa_delete(SessionChunk).where(SessionChunk.session_id == session_id)
                )
                await session.commit()
        except Exception as e:
            logger.warning(
                "会话 %s session_chunks 清理失败（非致命）: %s", session_id, e
            )

        try:
            await self._publish_invalidation()
        except Exception as e:
            logger.warning("会话 %s 失效广播失败（非致命）: %s", session_id, e)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _safe_cleanup_vectors(self, file_id: str) -> None:
        """部分写入失败时清理 Milvus 向量（幂等，失败仅记 WARNING）。"""
        try:
            await self.milvus.delete_by_doc_id(SESSION_FILES_KB_ID, file_id)
        except Exception as e:
            logger.warning(
                "会话文件 %s 失败清理向量异常（可能本无残留）: %s", file_id, e
            )

    async def _publish_invalidation(self) -> None:
        """publish ``kb_session_files`` 的 ``_loaded_at`` 失效信号。

        复用 bugfix ``retrieval-pipeline-hardening`` 的 ``InvalidationBus``，让其他
        API 进程在 ``load_cache_ttl`` 内不再读到旧加载快照（main.py 已绑定 ``kb_data``
        handler 调用 ``milvus._loaded_at.pop`` + ``cache.invalidate_kb``）。Redis 不可用时
        ``publish`` no-op 降级（不影响主流程）。
        """
        bus = get_invalidation_bus()
        if bus is not None:
            await bus.publish("kb_data", SESSION_FILES_KB_ID)

    async def _fetch_one(self, file_id: str) -> SessionFileVO:
        """取最新持久化的 ``SessionFile``（含 ``server_default`` 时间字段）。"""
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None:
                # 不应发生（刚 commit 完）；防御性兜底。
                raise RuntimeError(f"持久化失败：未找到 SessionFile {file_id}")
            return _row_to_vo(row)

    @staticmethod
    def _cleanup_temp_file(path: Path) -> None:
        """删除临时文件，失败仅记 WARNING（不影响主流程）。"""
        try:
            if path.exists():
                os.remove(path)
        except OSError as e:
            logger.warning("会话上传临时文件清理失败 %s: %s", path, e)


# ============================================================
# 进程内单例
# ============================================================

_service: SessionUploadService | None = None


def get_session_upload_service() -> SessionUploadService:
    """进程内 ``SessionUploadService`` 单例（无状态，单例仅为依赖注入便利）。

    与 ``get_upload_limit_resolver`` / ``get_milvus_client`` / ``get_retrieval_config_store``
    风格一致。
    """
    global _service
    if _service is None:
        _service = SessionUploadService()
    return _service
