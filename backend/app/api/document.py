"""文档上传与管理接口"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.manager import get_model_manager
from app.pipeline.ocr.manager import OCRManager
from app.pipeline.pipeline import DocumentPipeline
from app.schema.db import Chunk, Document, KnowledgeBase, OCRConfig
from app.storage.database import async_session, get_db
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Document"])

# 支持的文件类型
_ALLOWED_EXTENSIONS = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "jpg", "jpeg", "png"}

# 上传文件存储目录
_UPLOAD_DIR = Path("data/uploads")


# ============================================================
# 响应模型
# ============================================================


class DocumentResponse(BaseModel):
    """文档响应"""
    model_config = {"from_attributes": True}

    id: str
    kb_id: str
    filename: str
    file_type: str
    file_size: int | None
    status: str
    error_message: str | None
    chunk_count: int
    created_at: str


class ChunkResponse(BaseModel):
    """切片响应"""
    model_config = {"from_attributes": True}

    id: str
    doc_id: str
    kb_id: str
    parent_id: str | None
    content: str
    chunk_index: int | None
    created_at: str
    children: list[str] = []  # 子块内容列表，用于前端高亮


# ============================================================
# 辅助函数
# ============================================================


def _get_milvus() -> MilvusClient:
    """获取 Milvus 客户端"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


async def _run_pipeline(file_path: str, doc_id: str, kb_id: str) -> None:
    """后台执行文档处理管道"""
    try:
        manager = get_model_manager()
        milvus = _get_milvus()

        # 从数据库加载 OCR 配置
        ocr_manager = None
        async with async_session() as session:
            result = await session.execute(select(OCRConfig))
            configs = result.scalars().all()
        if configs:
            ocr_manager = OCRManager(configs)

        pipeline = DocumentPipeline(
            model_manager=manager,
            milvus_client=milvus,
            db_session_factory=async_session,
            ocr_manager=ocr_manager,
        )
        await pipeline.process(file_path, doc_id, kb_id)
    except Exception as e:
        logger.error("文档 %s 管道处理失败: %s", doc_id, e)


async def _run_pipeline_safe(file_path: str, doc_id: str, kb_id: str) -> None:
    """安全包装，捕获异常避免 task 崩溃"""
    try:
        await _run_pipeline(file_path, doc_id, kb_id)
    except Exception as e:
        logger.error("文档 %s 管道处理异常: %s", doc_id, e)


# ============================================================
# 接口实现
# ============================================================


@router.get("/api/knowledge-bases/{kb_id}/documents", response_model=list[DocumentResponse])
async def list_documents(kb_id: str, db: AsyncSession = Depends(get_db)):
    """获取知识库下的文档列表"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if kb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    result = await db.execute(
        select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()
    return [
        DocumentResponse(
            id=d.id,
            kb_id=d.kb_id,
            filename=d.filename,
            file_type=d.file_type,
            file_size=d.file_size,
            status=d.status,
            error_message=d.error_message,
            chunk_count=d.chunk_count,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in docs
    ]


@router.post("/api/knowledge-bases/{kb_id}/documents/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """上传文档（multipart/form-data）"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 验证文件类型
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，支持: {', '.join(_ALLOWED_EXTENSIONS)}",
        )

    # 保存文件到 data/uploads 目录
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    doc_id = str(uuid.uuid4())
    save_filename = f"{doc_id}.{ext}"
    file_path = _UPLOAD_DIR / save_filename

    content = await file.read()
    file_size = len(content)
    with open(file_path, "wb") as f:
        f.write(content)

    # 创建文档记录
    doc = Document(
        id=doc_id,
        kb_id=kb_id,
        filename=filename,
        file_type=ext,
        file_size=file_size,
        status="pending",
    )
    db.add(doc)

    # 更新知识库文档计数
    kb.doc_count = (kb.doc_count or 0) + 1

    await db.flush()
    await db.refresh(doc)

    # 后台触发管道处理
    asyncio.create_task(_run_pipeline_safe(str(file_path), doc_id, kb_id))

    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.get("/api/documents/{doc_id}", response_model=DocumentResponse)
async def get_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """获取文档详情"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """删除文档（级联清理 chunks + Milvus 向量）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 获取该文档的所有 chunk_id，用于清理 Milvus
    chunk_result = await db.execute(
        select(Chunk.id).where(Chunk.doc_id == doc_id)
    )
    chunk_ids = [row[0] for row in chunk_result.all()]

    # 删除 Milvus 中的向量
    if chunk_ids:
        try:
            milvus = _get_milvus()
            await milvus.delete(doc.kb_id, chunk_ids)
        except Exception as e:
            logger.warning("删除 Milvus 向量失败（可忽略）: %s", e)

    # 更新知识库文档计数
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb and kb.doc_count > 0:
        kb.doc_count -= 1

    # 删除文档（ORM cascade 会自动删除关联 chunks）
    await db.delete(doc)
    await db.flush()

    # 删除本地文件
    file_path = _UPLOAD_DIR / f"{doc_id}.{doc.file_type}"
    if file_path.exists():
        os.remove(file_path)


@router.get("/api/documents/{doc_id}/chunks", response_model=list[ChunkResponse])
async def list_document_chunks(doc_id: str, db: AsyncSession = Depends(get_db)):
    """查看文档的切片列表（返回父块 + 子块内容用于高亮）"""
    # 验证文档存在
    doc_result = await db.execute(select(Document).where(Document.id == doc_id))
    if doc_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 查询父块
    result = await db.execute(
        select(Chunk).where(Chunk.doc_id == doc_id, Chunk.parent_id.is_(None)).order_by(Chunk.chunk_index)
    )
    parent_chunks = result.scalars().all()

    # 查询所有子块，按 parent_id 分组
    child_result = await db.execute(
        select(Chunk).where(Chunk.doc_id == doc_id, Chunk.parent_id.isnot(None)).order_by(Chunk.chunk_index)
    )
    all_children = child_result.scalars().all()
    children_by_parent: dict[str, list[str]] = {}
    for child in all_children:
        children_by_parent.setdefault(child.parent_id, []).append(child.content)

    return [
        ChunkResponse(
            id=c.id,
            doc_id=c.doc_id,
            kb_id=c.kb_id,
            parent_id=c.parent_id,
            content=c.content,
            chunk_index=c.chunk_index,
            created_at=c.created_at.isoformat() if c.created_at else "",
            children=children_by_parent.get(c.id, []),
        )
        for c in parent_chunks
    ]
