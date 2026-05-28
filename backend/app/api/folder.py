"""文件夹 CRUD 接口"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.validators import NameValidationError, validate_folder_name
from app.schema.db import Document, Folder, KnowledgeBase
from app.storage.database import get_db
from app.storage.milvus import MilvusClient
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Folder"])

# 上传文件存储目录
_UPLOAD_DIR = Path("data/uploads")


# ============================================================
# 请求/响应模型
# ============================================================


class FolderCreate(BaseModel):
    """创建文件夹请求"""
    name: str = Field(..., min_length=1, max_length=200, description="文件夹名称")
    parent_id: str | None = Field(default=None, description="父文件夹 ID，为空表示根目录")


class FolderUpdate(BaseModel):
    """更新文件夹请求"""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: str | None = None


class FolderResponse(BaseModel):
    """文件夹响应"""
    model_config = {"from_attributes": True}

    id: str
    kb_id: str
    parent_id: str | None
    name: str
    doc_count: int = 0
    subfolder_count: int = 0
    created_at: datetime
    updated_at: datetime


class FolderMoveRequest(BaseModel):
    """移动文件/文件夹请求"""
    item_ids: list[str] = Field(..., description="要移动的项目 ID 列表")
    item_type: str = Field(..., description="项目类型: folder | document")
    target_folder_id: str | None = Field(default=None, description="目标文件夹 ID，为空表示根目录")


class BreadcrumbItem(BaseModel):
    """面包屑项"""
    id: str | None
    name: str


# ============================================================
# 接口实现
# ============================================================


@router.get("/api/knowledge-bases/{kb_id}/folders", response_model=list[FolderResponse])
async def list_folders(
    kb_id: str,
    parent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """获取指定目录下的文件夹列表"""
    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if kb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 查询文件夹
    if parent_id:
        query = select(Folder).where(Folder.kb_id == kb_id, Folder.parent_id == parent_id)
    else:
        query = select(Folder).where(Folder.kb_id == kb_id, Folder.parent_id.is_(None))

    result = await db.execute(query.order_by(Folder.name))
    folders = result.scalars().all()

    # 统计每个文件夹的子文件夹数和文档数
    responses = []
    for folder in folders:
        # 子文件夹数
        sub_result = await db.execute(
            select(Folder).where(Folder.parent_id == folder.id)
        )
        subfolder_count = len(sub_result.scalars().all())

        # 文档数
        doc_result = await db.execute(
            select(Document).where(Document.folder_id == folder.id)
        )
        doc_count = len(doc_result.scalars().all())

        responses.append(FolderResponse(
            id=folder.id,
            kb_id=folder.kb_id,
            parent_id=folder.parent_id,
            name=folder.name,
            doc_count=doc_count,
            subfolder_count=subfolder_count,
            created_at=folder.created_at,
            updated_at=folder.updated_at,
        ))

    return responses


@router.post("/api/knowledge-bases/{kb_id}/folders", response_model=FolderResponse, status_code=201)
async def create_folder(
    kb_id: str,
    body: FolderCreate,
    db: AsyncSession = Depends(get_db),
):
    """创建文件夹"""
    # 校验文件夹名称
    try:
        cleaned_name = validate_folder_name(body.name)
    except NameValidationError as e:
        raise HTTPException(status_code=422, detail=e.message)

    # 验证知识库存在
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if kb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 验证父文件夹存在（如果指定了）
    if body.parent_id:
        parent_result = await db.execute(
            select(Folder).where(Folder.id == body.parent_id, Folder.kb_id == kb_id)
        )
        if parent_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="父文件夹不存在")

    folder = Folder(
        id=str(uuid.uuid4()),
        kb_id=kb_id,
        parent_id=body.parent_id,
        name=cleaned_name,
    )
    db.add(folder)
    await db.flush()
    await db.refresh(folder)

    return FolderResponse(
        id=folder.id,
        kb_id=folder.kb_id,
        parent_id=folder.parent_id,
        name=folder.name,
        doc_count=0,
        subfolder_count=0,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.put("/api/folders/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: str,
    body: FolderUpdate,
    db: AsyncSession = Depends(get_db),
):
    """更新文件夹（重命名/移动）"""
    result = await db.execute(select(Folder).where(Folder.id == folder_id))
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    if body.name is not None:
        try:
            cleaned_name = validate_folder_name(body.name)
        except NameValidationError as e:
            raise HTTPException(status_code=422, detail=e.message)
        folder.name = cleaned_name
    if body.parent_id is not None:
        # 防止循环引用：不能移动到自己或自己的子文件夹下
        if body.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="不能将文件夹移动到自身")
        # 检查是否移动到子文件夹
        if body.parent_id:
            if await _is_descendant(db, body.parent_id, folder_id):
                raise HTTPException(status_code=400, detail="不能将文件夹移动到其子文件夹中")
        folder.parent_id = body.parent_id if body.parent_id else None

    folder.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(folder)

    return FolderResponse(
        id=folder.id,
        kb_id=folder.kb_id,
        parent_id=folder.parent_id,
        name=folder.name,
        doc_count=0,
        subfolder_count=0,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.delete("/api/folders/{folder_id}", status_code=204)
async def delete_folder(folder_id: str, db: AsyncSession = Depends(get_db)):
    """删除文件夹（快速响应版：立即删除 DB 记录并返回，后台异步清理 Milvus 和文件）"""
    result = await db.execute(select(Folder).where(Folder.id == folder_id))
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    kb_id = folder.kb_id

    # 递归收集该文件夹及所有子文件夹下的文档信息（用于后台清理）
    all_doc_ids: list[str] = []
    all_doc_info: list[dict] = []  # {"id": ..., "file_type": ...}
    folder_ids_to_check = [folder_id]

    while folder_ids_to_check:
        current_folder_id = folder_ids_to_check.pop()
        # 收集当前文件夹下的文档
        doc_result = await db.execute(
            select(Document.id, Document.file_type).where(Document.folder_id == current_folder_id)
        )
        for doc_id, file_type in doc_result.all():
            all_doc_ids.append(doc_id)
            all_doc_info.append({"id": doc_id, "file_type": file_type})

        # 收集子文件夹
        sub_result = await db.execute(
            select(Folder.id).where(Folder.parent_id == current_folder_id)
        )
        for (sub_id,) in sub_result.all():
            folder_ids_to_check.append(sub_id)

    # 更新知识库文档计数
    if all_doc_ids:
        kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = kb_result.scalar_one_or_none()
        if kb:
            kb.doc_count = max(0, (kb.doc_count or 0) - len(all_doc_ids))

    # 标记正在处理的文档为 cancelled
    if all_doc_ids:
        from sqlalchemy import update as sql_update
        await db.execute(
            sql_update(Document)
            .where(Document.id.in_(all_doc_ids))
            .where(Document.status.in_(("pending", "processing")))
            .values(status="cancelled")
        )

    # 删除文件夹（ORM cascade 会自动删除子文件夹、文档和 chunks）
    await db.delete(folder)
    await db.flush()

    # 后台异步清理 Milvus 向量和物理文件（不阻塞 API 响应）
    if all_doc_ids or all_doc_info:
        asyncio.create_task(
            _folder_cleanup_background(kb_id, all_doc_ids, all_doc_info)
        )


@router.post("/api/knowledge-bases/{kb_id}/move", status_code=200)
async def move_items(
    kb_id: str,
    body: FolderMoveRequest,
    db: AsyncSession = Depends(get_db),
):
    """移动文件或文件夹到目标目录"""
    # 验证目标文件夹存在
    if body.target_folder_id:
        target_result = await db.execute(
            select(Folder).where(Folder.id == body.target_folder_id, Folder.kb_id == kb_id)
        )
        if target_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="目标文件夹不存在")

    if body.item_type == "folder":
        for item_id in body.item_ids:
            result = await db.execute(select(Folder).where(Folder.id == item_id))
            folder = result.scalar_one_or_none()
            if folder:
                folder.parent_id = body.target_folder_id
    elif body.item_type == "document":
        for item_id in body.item_ids:
            result = await db.execute(select(Document).where(Document.id == item_id))
            doc = result.scalar_one_or_none()
            if doc:
                doc.folder_id = body.target_folder_id

    await db.flush()
    return {"message": "移动成功"}


@router.get("/api/knowledge-bases/{kb_id}/folders/{folder_id}/breadcrumb", response_model=list[BreadcrumbItem])
async def get_breadcrumb(
    kb_id: str,
    folder_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取文件夹的面包屑路径"""
    breadcrumb = []
    current_id: str | None = folder_id

    while current_id:
        result = await db.execute(select(Folder).where(Folder.id == current_id))
        folder = result.scalar_one_or_none()
        if folder is None:
            break
        breadcrumb.append(BreadcrumbItem(id=folder.id, name=folder.name))
        current_id = folder.parent_id

    breadcrumb.reverse()
    return breadcrumb


async def _is_descendant(db: AsyncSession, folder_id: str, ancestor_id: str) -> bool:
    """检查 folder_id 是否是 ancestor_id 的后代"""
    current_id: str | None = folder_id
    while current_id:
        if current_id == ancestor_id:
            return True
        result = await db.execute(select(Folder).where(Folder.id == current_id))
        folder = result.scalar_one_or_none()
        if folder is None:
            break
        current_id = folder.parent_id
    return False


async def _folder_cleanup_background(
    kb_id: str, doc_ids: list[str], doc_info_list: list[dict]
) -> None:
    """后台清理 Milvus 向量和物理文件（不阻塞 API 响应）"""
    # 使用批量 doc_id 表达式删除 Milvus 向量（合并为 IN 表达式，只 load/flush 一次）
    if doc_ids:
        try:
            settings = get_settings()
            milvus = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)
            if await milvus.has_collection(kb_id):
                await milvus.delete_by_doc_ids(kb_id, doc_ids)
            logger.info("文件夹删除 - Milvus 向量清理完成，共 %d 个文档", len(doc_ids))
        except Exception as e:
            logger.warning("文件夹删除 - Milvus 向量清理失败: %s", e)

    # 删除物理文件
    for info in doc_info_list:
        file_path = _UPLOAD_DIR / f"{info['id']}.{info['file_type']}"
        if file_path.exists():
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning("文件夹删除 - 删除文件失败 %s: %s", file_path, e)

    # 清除检索缓存
    try:
        from app.retrieval.cache import get_retrieval_cache
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
    except Exception as e:
        logger.warning("文件夹删除 - 清除缓存失败: %s", e)
