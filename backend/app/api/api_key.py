"""API Key 管理接口

提供 API Key 的创建、列表查询和撤销功能。
- POST /api/api-keys: 创建新 Key（仅返回一次明文）
- GET /api/api-keys: 列表（仅展示前缀）
- DELETE /api/api-keys/{id}: 撤销（设置 is_active=False）
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import generate_api_key, get_key_prefix, hash_key
from app.schema.db import ApiKey
from app.storage.database import get_db

router = APIRouter(prefix="/api/api-keys", tags=["API Keys"])


# ============================================================
# 请求/响应模型
# ============================================================


class CreateApiKeyRequest(BaseModel):
    """创建 API Key 请求"""
    name: Optional[str] = Field(default=None, description="Key 名称/备注")


class CreateApiKeyResponse(BaseModel):
    """创建 API Key 响应（包含明文，仅此一次）"""
    id: str
    key: str = Field(description="完整 API Key，仅在创建时返回")
    prefix: str
    name: Optional[str] = None
    created_at: datetime


class ApiKeyItem(BaseModel):
    """API Key 列表项（不含明文）"""
    id: str
    prefix: str
    name: Optional[str] = None
    is_active: bool
    call_count: int
    last_used_at: Optional[datetime] = None
    created_at: datetime


class ApiKeyListResponse(BaseModel):
    """API Key 列表响应"""
    items: list[ApiKeyItem]
    total: int


# ============================================================
# 接口实现
# ============================================================


@router.post("", response_model=CreateApiKeyResponse)
async def create_api_key(
    request: CreateApiKeyRequest = CreateApiKeyRequest(),
    db: AsyncSession = Depends(get_db),
):
    """创建新的 API Key

    生成随机 Key，数据库中仅存储哈希值。
    明文 Key 仅在此响应中返回一次，请妥善保存。
    """
    # 生成 Key
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())

    # 创建数据库记录
    api_key = ApiKey(
        id=key_id,
        key_hash=hash_key(raw_key),
        prefix=get_key_prefix(raw_key),
        name=request.name,
        is_active=True,
        call_count=0,
    )
    db.add(api_key)
    await db.flush()

    return CreateApiKeyResponse(
        id=key_id,
        key=raw_key,
        prefix=api_key.prefix,
        name=api_key.name,
        created_at=api_key.created_at,
    )


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    """获取 API Key 列表

    仅返回前缀，不返回完整 Key。
    """
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
    result = await db.execute(stmt)
    keys = result.scalars().all()

    items = [
        ApiKeyItem(
            id=k.id,
            prefix=k.prefix,
            name=k.name,
            is_active=k.is_active,
            call_count=k.call_count,
            last_used_at=k.last_used_at,
            created_at=k.created_at,
        )
        for k in keys
    ]

    return ApiKeyListResponse(items=items, total=len(items))


@router.delete("/{key_id}")
async def revoke_api_key(key_id: str, db: AsyncSession = Depends(get_db)):
    """撤销 API Key（软删除，设置 is_active=False）"""
    stmt = select(ApiKey).where(ApiKey.id == key_id)
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()

    if api_key is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    api_key.is_active = False
    await db.flush()

    return {"message": "API Key 已撤销", "id": key_id}
