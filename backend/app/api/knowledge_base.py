"""知识库 CRUD 接口"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.schema.db import Document, KnowledgeBase
from app.storage.database import get_db
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["KnowledgeBase"])


# ============================================================
# 请求/响应模型
# ============================================================


class KnowledgeBaseCreate(BaseModel):
    """创建知识库请求"""
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, description="描述")
    retrieval_mode: str = Field(default="hybrid", description="检索模式: direct / hybrid / agent")
    config: dict | None = Field(default=None, description="检索参数配置")


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库请求"""
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    retrieval_mode: str | None = None
    config: dict | None = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    description: str | None
    retrieval_mode: str
    config: dict | None
    doc_count: int
    created_at: datetime
    updated_at: datetime


# ============================================================
# 接口实现
# ============================================================


def _get_milvus() -> MilvusClient:
    """获取 Milvus 客户端"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(db: AsyncSession = Depends(get_db)):
    """获取知识库列表，doc_count 实时统计"""
    result = await db.execute(
        select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    )
    kbs = result.scalars().all()

    # 实时统计每个知识库的文档数
    kb_ids = [kb.id for kb in kbs]
    if kb_ids:
        count_result = await db.execute(
            select(Document.kb_id, func.count(Document.id))
            .where(Document.kb_id.in_(kb_ids))
            .group_by(Document.kb_id)
        )
        count_map = {row[0]: row[1] for row in count_result.all()}
    else:
        count_map = {}

    return [
        KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            retrieval_mode=kb.retrieval_mode,
            config=kb.config,
            doc_count=count_map.get(kb.id, 0),
            created_at=kb.created_at,
            updated_at=kb.updated_at,
        )
        for kb in kbs
    ]


@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
):
    """创建知识库"""
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        retrieval_mode=body.retrieval_mode,
        config=body.config,
        doc_count=0,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    return kb


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    """获取知识库详情"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
):
    """更新知识库"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 仅更新非 None 字段
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(kb, field, value)
    kb.updated_at = datetime.utcnow()

    await db.flush()
    await db.refresh(kb)
    return kb


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    """删除知识库（批量 SQL 快速删除，后台异步清理 Milvus 和文件）"""
    from sqlalchemy import delete as sql_delete, update as sql_update

    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 收集所有文档信息（用于后续清理物理文件）
    doc_result = await db.execute(
        select(Document.id, Document.file_type).where(Document.kb_id == kb_id)
    )
    doc_info_list = [{"id": row[0], "file_type": row[1]} for row in doc_result.all()]

    # 标记正在处理的文档为 cancelled（阻止 pipeline 继续处理）
    await db.execute(
        sql_update(Document)
        .where(Document.kb_id == kb_id)
        .where(Document.status.in_(("pending", "processing")))
        .values(status="cancelled")
    )

    # 批量 SQL 删除（比 ORM cascade 快 10-100x）
    from app.schema.db import Chunk, Folder
    await db.execute(sql_delete(Chunk).where(Chunk.kb_id == kb_id))
    await db.execute(sql_delete(Document).where(Document.kb_id == kb_id))
    await db.execute(sql_delete(Folder).where(Folder.kb_id == kb_id))
    await db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    await db.commit()

    # 后台异步清理 Milvus collection + 物理文件 + 缓存（不阻塞 API 响应）
    import asyncio
    asyncio.create_task(_kb_cleanup_background(kb_id, doc_info_list))


async def _kb_cleanup_background(kb_id: str, doc_info_list: list[dict]) -> None:
    """后台清理 Milvus collection + 物理文件 + 缓存（不阻塞 API 响应）"""
    import os
    from pathlib import Path

    upload_dir = Path("data/uploads")

    # 删除 Milvus collection（耗时操作，放后台）
    try:
        milvus = _get_milvus()
        await milvus.drop_collection(kb_id)
    except Exception as e:
        logger.warning("知识库删除 - 删除 Milvus collection 失败（可忽略）: %s", e)

    # 删除物理文件
    for info in doc_info_list:
        file_path = upload_dir / f"{info['id']}.{info['file_type']}"
        if file_path.exists():
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning("知识库删除 - 删除文件失败 %s: %s", file_path, e)

    logger.info("知识库 %s 删除 - 后台清理完成，共 %d 个文件", kb_id, len(doc_info_list))

    # 清除检索缓存
    try:
        from app.retrieval.cache import get_retrieval_cache
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
    except Exception as e:
        logger.warning("知识库删除 - 清除缓存失败: %s", e)
