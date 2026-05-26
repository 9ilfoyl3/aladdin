"""文档上传与管理接口"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.api.validators import NameValidationError, validate_filename, validate_folder_name
from app.models.manager import get_model_manager
from app.pipeline.ocr.manager import OCRManager
from app.pipeline.pipeline import DocumentPipeline
from app.pipeline.queue import TaskMessage, TaskQueue
from app.schema.db import Chunk, Document, Folder, KnowledgeBase, OCRConfig
from app.storage.database import async_session, get_db
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Document"])

# 支持的文件类型
_ALLOWED_EXTENSIONS = {"pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "jpg", "jpeg", "png"}

# 上传文件存储目录
_UPLOAD_DIR = Path("data/uploads")

# 缩略图缓存目录
_THUMBNAIL_DIR = _UPLOAD_DIR / "thumbnails"


def _generate_thumbnail(doc_id: str, file_type: str) -> None:
    """为文档生成缩略图（同步，适合在后台任务中调用）。
    支持 PDF（渲染首页）和图片（直接复制/缩放）。
    """
    if file_type not in ("pdf", "jpg", "jpeg", "png"):
        return

    _THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = _THUMBNAIL_DIR / f"{doc_id}.png"
    if thumb_path.exists():
        return

    file_path = _UPLOAD_DIR / f"{doc_id}.{file_type}"
    if not file_path.exists():
        return

    try:
        if file_type == "pdf":
            import fitz
            pdf_doc = fitz.open(str(file_path))
            page = pdf_doc[0]
            zoom = 200.0 / page.rect.width
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            pix.save(str(thumb_path))
            pdf_doc.close()
        # 图片类型不需要生成缩略图，preview 接口直接返回原文件
    except Exception as e:
        logger.warning("生成缩略图失败 doc_id=%s: %s", doc_id, e)


def _delete_thumbnail(doc_id: str) -> None:
    """删除文档对应的缩略图缓存"""
    thumb_path = _THUMBNAIL_DIR / f"{doc_id}.png"
    if thumb_path.exists():
        try:
            os.remove(thumb_path)
        except OSError:
            pass


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
    progress: float = 0
    progress_message: str | None = None
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
        print(f"[Pipeline] 文档 {doc_id} 开始处理，文件: {file_path}")
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
        print(f"[Pipeline] 文档 {doc_id} 处理完成")
    except Exception as e:
        import traceback
        print(f"[Pipeline] 文档 {doc_id} 管道处理失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        logger.error("文档 %s 管道处理失败: %s", doc_id, e)


async def _run_pipeline_safe(file_path: str, doc_id: str, kb_id: str) -> None:
    """安全包装，捕获异常避免 task 崩溃。处理成功后清除该知识库的检索缓存。"""
    try:
        await _run_pipeline(file_path, doc_id, kb_id)
        # 文档处理成功，清除该知识库的检索缓存
        from app.retrieval.cache import get_retrieval_cache
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
    except Exception as e:
        import traceback
        print(f"[Pipeline] 文档 {doc_id} 管道处理异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        logger.error("文档 %s 管道处理异常: %s", doc_id, e)


def _get_task_queue(request: Request) -> TaskQueue | None:
    """从 app.state 获取 TaskQueue 实例，不存在或为 None 时返回 None"""
    return getattr(request.app.state, "task_queue", None)


async def _enqueue_or_fallback(
    request: Request, file_path: str, doc_id: str, kb_id: str
) -> None:
    """尝试将任务入队 Redis Stream，失败时降级为 asyncio.create_task"""
    queue = _get_task_queue(request)
    if queue is not None:
        try:
            msg = TaskMessage(doc_id=doc_id, kb_id=kb_id, file_path=file_path)
            msg_id = await queue.enqueue(msg)
            print(f"[Queue] 文档 {doc_id} 已入队 Redis Stream (msg_id={msg_id})")
            return
        except Exception as e:
            print(f"[Queue] ⚠️ Redis 入队失败，降级为 create_task: {e}")
            logger.warning(
                "Redis unavailable, falling back to in-process task: %s", e
            )
    else:
        print(f"[Queue] ⚠️ Redis 不可用，降级为 create_task (doc_id={doc_id})")
        logger.warning("Redis unavailable, falling back to in-process task")
    # 降级：使用 asyncio.create_task
    asyncio.create_task(_run_pipeline_safe(file_path, doc_id, kb_id))


# ============================================================
# 接口实现
# ============================================================


@router.get("/api/knowledge-bases/{kb_id}/documents", response_model=list[DocumentResponse])
async def list_documents(kb_id: str, folder_id: str | None = None, db: AsyncSession = Depends(get_db)):
    """获取知识库下的文档列表（支持按文件夹过滤）"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if kb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 按文件夹过滤
    if folder_id:
        query = select(Document).where(Document.kb_id == kb_id, Document.folder_id == folder_id)
    else:
        query = select(Document).where(Document.kb_id == kb_id, Document.folder_id.is_(None))

    # 排序：completed > failed > processing > pending，同状态按创建时间倒序
    from sqlalchemy import case
    status_order = case(
        (Document.status == "completed", 0),
        (Document.status == "failed", 1),
        (Document.status == "processing", 2),
        (Document.status == "pending", 3),
        else_=4,
    )
    result = await db.execute(query.order_by(status_order, Document.created_at.desc()))
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
            progress=d.progress or 0,
            progress_message=d.progress_message,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in docs
    ]


@router.post("/api/knowledge-bases/{kb_id}/documents/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    kb_id: str,
    request: Request,
    file: UploadFile = File(...),
    folder_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """上传文档（multipart/form-data），支持指定文件夹"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 校验文件名
    filename = file.filename or "unknown"
    try:
        filename = validate_filename(filename)
    except NameValidationError as e:
        raise HTTPException(status_code=422, detail=e.message)

    # 验证文件类型
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    # 保存文件到 data/uploads 目录
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    doc_id = str(uuid.uuid4())
    save_filename = f"{doc_id}.{ext}"
    file_path = _UPLOAD_DIR / save_filename

    content = await file.read()
    file_size = len(content)

    # 计算文件哈希，检测重复
    import hashlib
    file_hash = hashlib.sha256(content).hexdigest()
    existing = await db.execute(
        select(Document).where(
            Document.kb_id == kb_id,
            Document.file_hash == file_hash,
        )
    )
    existing_doc = existing.scalar_one_or_none()
    if existing_doc is not None:
        return DocumentResponse(
            id=existing_doc.id,
            kb_id=existing_doc.kb_id,
            filename=existing_doc.filename,
            file_type=existing_doc.file_type,
            file_size=existing_doc.file_size,
            status="duplicate",
            error_message=f"文件已存在（与 {existing_doc.filename} 内容相同）",
            chunk_count=existing_doc.chunk_count,
            created_at=existing_doc.created_at.isoformat() if existing_doc.created_at else "",
        )

    with open(file_path, "wb") as f:
        f.write(content)

    # 创建文档记录
    doc = Document(
        id=doc_id,
        kb_id=kb_id,
        folder_id=folder_id,
        filename=filename,
        file_type=ext,
        file_size=file_size,
        file_hash=file_hash,
        status="pending",
    )
    db.add(doc)

    # 更新知识库文档计数
    kb.doc_count = (kb.doc_count or 0) + 1

    await db.flush()
    await db.refresh(doc)
    await db.commit()

    # 生成缩略图（PDF 首页渲染）
    _generate_thumbnail(doc_id, ext)

    # 后台触发管道处理（优先入队 Redis Stream，降级为 asyncio.create_task）
    await _enqueue_or_fallback(request, str(file_path), doc_id, kb_id)

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
        progress=doc.progress or 0,
        progress_message=doc.progress_message,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.post("/api/documents/{doc_id}/retry", response_model=DocumentResponse)
async def retry_document(doc_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """重新识别文档（清除旧数据后重新处理）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status == "processing":
        raise HTTPException(status_code=400, detail="文档正在处理中")

    # 清除旧的 chunk 数据
    chunk_result = await db.execute(select(Chunk.id).where(Chunk.doc_id == doc_id))
    chunk_ids = [row[0] for row in chunk_result.all()]
    if chunk_ids:
        try:
            milvus = _get_milvus()
            await milvus.delete(doc.kb_id, chunk_ids)
        except Exception as e:
            logger.warning("清除旧向量失败（可忽略）: %s", e)
        # 删除 SQLite 中的 chunks
        from sqlalchemy import delete as sql_delete
        await db.execute(sql_delete(Chunk).where(Chunk.doc_id == doc_id))

    # 重置状态
    doc.status = "pending"
    doc.error_message = None
    doc.chunk_count = 0
    await db.flush()

    # 重新触发管道（优先入队 Redis Stream）
    file_path = _UPLOAD_DIR / f"{doc_id}.{doc.file_type}"
    if not file_path.exists():
        doc.status = "failed"
        doc.error_message = "原始文件已丢失，无法重新识别"
        await db.flush()
        raise HTTPException(status_code=400, detail="原始文件已丢失")

    # 尝试入队 Redis Stream，降级为 create_task
    queue = _get_task_queue(request)
    if queue is not None:
        try:
            msg = TaskMessage(doc_id=doc_id, kb_id=doc.kb_id, file_path=str(file_path))
            await queue.enqueue(msg)
        except Exception as e:
            logger.warning("Redis 入队失败，降级为 create_task: %s", e)
            asyncio.create_task(_run_pipeline_safe(str(file_path), doc_id, doc.kb_id))
    else:
        asyncio.create_task(_run_pipeline_safe(str(file_path), doc_id, doc.kb_id))

    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status="pending",
        error_message=None,
        chunk_count=0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """删除文档（快速响应版：立即删除 DB 记录并返回，后台异步清理 Milvus 和文件）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 收集清理所需信息（在删除 DB 记录前）
    kb_id = doc.kb_id
    file_type = doc.file_type

    # 如果文档正在处理中，先标记为 cancelled（Pipeline 各阶段会检查此状态并终止）
    if doc.status in ("pending", "processing"):
        doc.status = "cancelled"
        await db.flush()

    # 更新知识库文档计数
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb and kb.doc_count > 0:
        kb.doc_count -= 1

    # 删除文档（ORM cascade 会自动删除关联 chunks）
    await db.delete(doc)
    await db.flush()

    # 后台异步清理 Milvus 向量 + 本地文件 + 缓存（不阻塞 API 响应）
    asyncio.create_task(_doc_cleanup_background(doc_id, kb_id, file_type))


async def _doc_cleanup_background(doc_id: str, kb_id: str, file_type: str) -> None:
    """单文档删除后台清理：Milvus 向量、物理文件、缩略图、缓存"""
    # 删除 Milvus 中的向量
    try:
        milvus = _get_milvus()
        if await milvus.has_collection(kb_id):
            await milvus.delete_by_doc_id(kb_id, doc_id)
    except Exception as e:
        logger.warning("删除 Milvus 向量失败（可忽略）: %s", e)

    # 删除本地文件
    file_path = _UPLOAD_DIR / f"{doc_id}.{file_type}"
    if file_path.exists():
        try:
            os.remove(file_path)
        except OSError:
            pass

    # 删除缩略图缓存
    _delete_thumbnail(doc_id)

    # 清除该知识库的检索缓存
    from app.retrieval.cache import get_retrieval_cache
    cache = await get_retrieval_cache()
    if cache:
        await cache.invalidate_kb(kb_id)


# ============================================================
# 批量删除文档
# ============================================================


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    doc_ids: list[str]


@router.post("/api/documents/batch-delete", status_code=200)
async def batch_delete_documents(body: BatchDeleteRequest, db: AsyncSession = Depends(get_db)):
    """批量删除文档（快速响应版：立即删除 DB 记录并返回，后台异步清理 Milvus 和文件）"""
    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="doc_ids 不能为空")

    # ─── 第一步：批量标记 cancelled，让 pipeline 立即停止处理 ───
    from sqlalchemy import update as sql_update, delete as sql_delete
    await db.execute(
        sql_update(Document)
        .where(Document.id.in_(body.doc_ids))
        .where(Document.status.in_(("pending", "processing")))
        .values(status="cancelled")
    )
    await db.flush()

    # ─── 第二步：批量查询所有待删除文档（收集清理所需信息） ───
    result = await db.execute(
        select(Document).where(Document.id.in_(body.doc_ids))
    )
    docs = result.scalars().all()
    if not docs:
        return {"deleted_count": 0, "total_requested": len(body.doc_ids)}

    # 收集清理信息
    kb_ids_affected: set[str] = set()
    doc_ids_found: list[str] = []
    cleanup_info: list[dict] = []  # [{id, kb_id, file_type}]

    for doc in docs:
        kb_ids_affected.add(doc.kb_id)
        doc_ids_found.append(doc.id)
        cleanup_info.append({"id": doc.id, "kb_id": doc.kb_id, "file_type": doc.file_type})

    # ─── 第三步：批量查询所有 chunk_ids（在删除前收集） ───
    kb_chunk_map: dict[str, list[str]] = {}
    if doc_ids_found:
        chunk_result = await db.execute(
            select(Chunk.id, Chunk.doc_id).where(Chunk.doc_id.in_(doc_ids_found))
        )
        for chunk_id, doc_id in chunk_result.all():
            doc_obj = next((d for d in docs if d.id == doc_id), None)
            if doc_obj:
                kb_chunk_map.setdefault(doc_obj.kb_id, []).append(chunk_id)

    # ─── 第四步：批量更新知识库文档计数 ───
    for kb_id in kb_ids_affected:
        doc_count_in_batch = sum(1 for d in docs if d.kb_id == kb_id)
        kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = kb_result.scalar_one_or_none()
        if kb:
            kb.doc_count = max(0, (kb.doc_count or 0) - doc_count_in_batch)

    # ─── 第五步：批量删除 DB 记录（立即生效，前端刷新后看不到这些文档） ───
    await db.execute(sql_delete(Chunk).where(Chunk.doc_id.in_(doc_ids_found)))
    await db.execute(sql_delete(Document).where(Document.id.in_(doc_ids_found)))
    await db.commit()

    # ─── 第六步：后台异步清理 Milvus 向量 + 本地文件 + 缓存 ───
    asyncio.create_task(_batch_cleanup_background(kb_chunk_map, cleanup_info, kb_ids_affected))

    return {"deleted_count": len(doc_ids_found), "total_requested": len(body.doc_ids)}


async def _batch_cleanup_background(
    kb_chunk_map: dict[str, list[str]],
    cleanup_info: list[dict],
    kb_ids_affected: set[str],
) -> None:
    """后台清理 Milvus 向量、本地文件和缓存（不阻塞 API 响应）"""
    # 删除 Milvus 向量（使用 doc_id 表达式删除，覆盖孤儿向量）
    # 按 kb_id 分组，对每个文档用 delete_by_doc_id 确保清理干净
    kb_doc_map: dict[str, list[str]] = {}
    for info in cleanup_info:
        kb_doc_map.setdefault(info["kb_id"], []).append(info["id"])

    for kb_id, doc_ids in kb_doc_map.items():
        try:
            milvus = _get_milvus()
            if await milvus.has_collection(kb_id):
                for doc_id in doc_ids:
                    await milvus.delete_by_doc_id(kb_id, doc_id)
        except Exception as e:
            logger.warning("批量删除后台清理 - 删除 Milvus 向量失败: %s", e)

    # 删除本地文件
    for info in cleanup_info:
        file_path = _UPLOAD_DIR / f"{info['id']}.{info['file_type']}"
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError:
                pass
        # 删除缩略图缓存
        _delete_thumbnail(info['id'])

    # 清除检索缓存
    from app.retrieval.cache import get_retrieval_cache
    cache = await get_retrieval_cache()
    if cache:
        for kb_id in kb_ids_affected:
            await cache.invalidate_kb(kb_id)


# ============================================================
# 文件夹批量上传
# ============================================================


class FolderUploadFileInfo(BaseModel):
    """文件夹上传中单个文件的信息"""
    relative_path: str
    filename: str
    file_type: str
    supported: bool
    reason: str | None = None


class FolderUploadValidateRequest(BaseModel):
    """文件夹上传校验请求"""
    paths: list[str]  # 文件相对路径列表（含目录结构）


class FolderUploadValidateResponse(BaseModel):
    """文件夹上传校验响应"""
    supported_files: list[FolderUploadFileInfo]
    unsupported_files: list[FolderUploadFileInfo]
    folder_structure: list[str]  # 需要创建的文件夹路径列表


class FolderUploadResultItem(BaseModel):
    """文件夹上传结果中的单个文件"""
    relative_path: str
    filename: str
    doc_id: str | None = None
    folder_id: str | None = None
    status: str  # uploaded | skipped | error
    message: str | None = None


class FolderUploadResponse(BaseModel):
    """文件夹批量上传响应"""
    total_files: int
    uploaded_count: int
    skipped_count: int
    created_folders: list[str]
    results: list[FolderUploadResultItem]


@router.post("/api/knowledge-bases/{kb_id}/documents/validate-folder", response_model=FolderUploadValidateResponse)
async def validate_folder_upload(
    kb_id: str,
    body: FolderUploadValidateRequest,
    db: AsyncSession = Depends(get_db),
):
    """校验文件夹上传：解析目录结构，区分支持和不支持的文件"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if kb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    supported_files: list[FolderUploadFileInfo] = []
    unsupported_files: list[FolderUploadFileInfo] = []
    folder_paths: set[str] = set()

    for rel_path in body.paths:
        # 提取目录部分
        parts = rel_path.replace("\\", "/").split("/")
        filename = parts[-1]

        # 收集所有中间目录
        for i in range(1, len(parts)):
            folder_path = "/".join(parts[:i])
            folder_paths.add(folder_path)

        # 校验文件名
        try:
            validate_filename(filename)
        except NameValidationError as e:
            unsupported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type="",
                supported=False,
                reason=f"文件名校验失败: {e.message}",
            ))
            continue

        # 检查文件扩展名
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _ALLOWED_EXTENSIONS:
            supported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type=ext,
                supported=True,
            ))
        else:
            unsupported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type=ext or "(无扩展名)",
                supported=False,
                reason=f"不支持的文件类型: {ext or '无扩展名'}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
            ))

    # 排序文件夹路径（确保父目录在前）
    sorted_folders = sorted(folder_paths)

    return FolderUploadValidateResponse(
        supported_files=supported_files,
        unsupported_files=unsupported_files,
        folder_structure=sorted_folders,
    )


@router.post("/api/knowledge-bases/{kb_id}/documents/upload-folder", response_model=FolderUploadResponse, status_code=201)
async def upload_folder(
    kb_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    paths: Annotated[str, Form(...)] = "",
    parent_folder_id: Annotated[str | None, Form()] = None,
    db: AsyncSession = Depends(get_db),
):
    """批量上传文件夹

    - files: 多个文件（multipart）
    - paths: JSON 字符串，文件相对路径列表，与 files 一一对应
    - parent_folder_id: 上传到的父文件夹 ID（可选，为空表示当前目录）
    """
    import json

    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 解析路径列表
    try:
        path_list: list[str] = json.loads(paths)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="paths 参数格式错误，需要 JSON 数组字符串")

    if len(path_list) != len(files):
        raise HTTPException(status_code=400, detail="文件数量与路径数量不匹配")

    # 1. 收集需要创建的文件夹结构
    folder_paths: set[str] = set()
    for rel_path in path_list:
        parts = rel_path.replace("\\", "/").split("/")
        for i in range(1, len(parts)):
            folder_path = "/".join(parts[:i])
            folder_paths.add(folder_path)

    # 2. 按层级排序并创建文件夹
    sorted_folders = sorted(folder_paths)
    folder_id_map: dict[str, str] = {}  # path -> folder_id
    created_folders: list[str] = []

    for folder_path in sorted_folders:
        parts = folder_path.split("/")
        folder_name = parts[-1]

        # 校验文件夹名称
        try:
            folder_name = validate_folder_name(folder_name)
        except NameValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=f"文件夹 '{folder_path}' 名称校验失败: {e.message}",
            )

        # 确定父文件夹 ID
        if len(parts) == 1:
            parent_id = parent_folder_id
        else:
            parent_path = "/".join(parts[:-1])
            parent_id = folder_id_map.get(parent_path, parent_folder_id)

        # 检查同名文件夹是否已存在
        existing_query = select(Folder).where(
            Folder.kb_id == kb_id,
            Folder.name == folder_name,
            Folder.parent_id == parent_id if parent_id else Folder.parent_id.is_(None),
        )
        existing_result = await db.execute(existing_query)
        existing_folder = existing_result.scalar_one_or_none()

        if existing_folder:
            folder_id_map[folder_path] = existing_folder.id
        else:
            folder_id = str(uuid.uuid4())
            new_folder = Folder(
                id=folder_id,
                kb_id=kb_id,
                parent_id=parent_id,
                name=folder_name,
            )
            db.add(new_folder)
            folder_id_map[folder_path] = folder_id
            created_folders.append(folder_path)

    await db.flush()

    # 3. 逐个处理文件
    results: list[FolderUploadResultItem] = []
    uploaded_count = 0
    skipped_count = 0

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for file, rel_path in zip(files, path_list):
        parts = rel_path.replace("\\", "/").split("/")
        filename = parts[-1]
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # 跳过不支持的文件
        if ext not in _ALLOWED_EXTENSIONS:
            skipped_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="skipped",
                message=f"不支持的文件类型: {ext}",
            ))
            continue

        # 校验文件名
        try:
            filename = validate_filename(filename)
        except NameValidationError as e:
            skipped_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="skipped",
                message=f"文件名校验失败: {e.message}",
            ))
            continue

        # 确定文件所属文件夹
        if len(parts) > 1:
            folder_path = "/".join(parts[:-1])
            target_folder_id = folder_id_map.get(folder_path, parent_folder_id)
        else:
            target_folder_id = parent_folder_id

        try:
            # 保存文件
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
                folder_id=target_folder_id,
                filename=filename,
                file_type=ext,
                file_size=file_size,
                status="pending",
            )
            db.add(doc)

            # 更新知识库文档计数
            kb.doc_count = (kb.doc_count or 0) + 1
            await db.flush()
            await db.commit()

            # 生成缩略图（PDF 首页渲染）
            _generate_thumbnail(doc_id, ext)

            # 后台触发管道处理（优先入队 Redis Stream，降级为 asyncio.create_task）
            await _enqueue_or_fallback(request, str(file_path), doc_id, kb_id)

            uploaded_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                doc_id=doc_id,
                folder_id=target_folder_id,
                status="uploaded",
            ))
        except Exception as e:
            logger.error("文件 %s 上传失败: %s", rel_path, e)
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="error",
                message=str(e),
            ))

    await db.flush()

    return FolderUploadResponse(
        total_files=len(files),
        uploaded_count=uploaded_count,
        skipped_count=skipped_count,
        created_folders=created_folders,
        results=results,
    )


@router.get("/api/documents/{doc_id}/preview")
async def preview_document_file(doc_id: str, db: AsyncSession = Depends(get_db)):
    """预览文档文件缩略图（支持图片和 PDF 首页）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 图片类型：直接返回原文件
    if doc.file_type in ("jpg", "jpeg", "png"):
        file_path = _UPLOAD_DIR / f"{doc_id}.{doc.file_type}"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        media_type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
        return FileResponse(
            path=str(file_path),
            media_type=media_type_map.get(doc.file_type, "application/octet-stream"),
            filename=doc.filename,
        )

    # PDF 类型：返回缩略图（上传时已预生成，此处兜底）
    if doc.file_type == "pdf":
        thumb_path = _THUMBNAIL_DIR / f"{doc_id}.png"

        # 兜底：如果缩略图不存在则现场生成
        if not thumb_path.exists():
            _generate_thumbnail(doc_id, "pdf")

        if not thumb_path.exists():
            raise HTTPException(status_code=404, detail="缩略图不可用")

        return FileResponse(
            path=str(thumb_path),
            media_type="image/png",
            filename=f"{doc_id}_thumb.png",
        )

    raise HTTPException(status_code=400, detail="该文件类型不支持预览")


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
