"""Embedding/Rerank 模型配置管理接口"""

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schema.db import EmbedConfig
from app.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/embed-configs", tags=["Embed Config"])


# ============================================================
# 请求/响应模型
# ============================================================


class EmbedConfigCreate(BaseModel):
    name: str
    config_type: str  # embedding | rerank
    provider: str  # local | remote
    # local 字段
    local_provider: Optional[str] = None  # sentence-transformers | flag-embedding
    model_name: str = "BAAI/bge-m3"
    device: str = "cpu"
    # remote 字段
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: float = 60.0
    # sparse 向量支持（仅 embedding 类型有效）
    sparse_enabled: bool = True
    # 状态
    is_active: bool = False


class EmbedConfigUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    local_provider: Optional[str] = None
    model_name: Optional[str] = None
    device: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = None
    sparse_enabled: Optional[bool] = None
    is_active: Optional[bool] = None


class EmbedConfigResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    name: str
    config_type: str
    provider: str
    local_provider: Optional[str] = None
    model_name: str
    device: str
    base_url: Optional[str] = None
    api_key_set: bool
    timeout: float
    sparse_enabled: bool
    is_active: bool
    created_at: str
    updated_at: str


class EmbedTestRequest(BaseModel):
    """测试连通性请求"""
    provider: str  # local | remote
    local_provider: Optional[str] = None
    model_name: str = "BAAI/bge-m3"
    device: str = "cpu"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: float = 60.0
    config_type: str = "embedding"  # embedding | rerank
    config_id: Optional[str] = None  # 编辑已有配置时传入，用于在 api_key 为空时回退到已保存的密钥
    sparse_enabled: bool = True  # 是否测试 sparse 端点


class EmbedTestResponse(BaseModel):
    success: bool
    message: str


# ============================================================
# 辅助函数
# ============================================================


def _to_response(config: EmbedConfig) -> EmbedConfigResponse:
    return EmbedConfigResponse(
        id=config.id,
        name=config.name,
        config_type=config.config_type,
        provider=config.provider,
        local_provider=config.local_provider,
        model_name=config.model_name,
        device=config.device,
        base_url=config.base_url,
        api_key_set=bool(config.api_key),
        timeout=config.timeout,
        sparse_enabled=config.sparse_enabled,
        is_active=config.is_active,
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


def _reload_provider(config: EmbedConfig) -> None:
    """当配置被激活时，重新加载对应的 ModelManager Provider"""
    from app.models.manager import get_model_manager

    try:
        manager = get_model_manager()
        kwargs = {
            "local_provider": config.local_provider,
            "model_name": config.model_name,
            "device": config.device,
            "base_url": config.base_url or "",
            "api_key": config.api_key or "",
            "timeout": config.timeout,
            "sparse_enabled": config.sparse_enabled,
        }
        if config.config_type == "embedding":
            manager.reload_embedder(provider=config.provider, **kwargs)
        else:
            manager.reload_reranker(provider=config.provider, **kwargs)
    except Exception as e:
        logger.error("重载 Provider 失败: %s", e)


# ============================================================
# 接口实现
# ============================================================


@router.get("", response_model=list[EmbedConfigResponse])
async def list_embed_configs(
    config_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """获取所有 Embedding/Rerank 配置"""
    query = select(EmbedConfig).order_by(EmbedConfig.created_at.desc())
    if config_type:
        query = query.where(EmbedConfig.config_type == config_type)
    result = await db.execute(query)
    configs = result.scalars().all()
    return [_to_response(c) for c in configs]


class EmbedCurrentResponse(BaseModel):
    """当前生效的 Embedding/Rerank 配置"""
    embed_provider: str
    embed_model: str
    embed_device: str
    embed_base_url: str
    embed_sparse_enabled: bool
    rerank_provider: str
    rerank_model: str
    rerank_device: str
    rerank_base_url: str


@router.get("/current", response_model=EmbedCurrentResponse)
async def get_current_embed_config():
    """获取当前生效的 Embedding/Rerank 配置（来自环境变量）"""
    from app.config import get_settings
    settings = get_settings()
    return EmbedCurrentResponse(
        embed_provider=settings.embed_provider,
        embed_model=settings.embed_model,
        embed_device=settings.embed_device,
        embed_base_url=settings.embed_base_url,
        embed_sparse_enabled=settings.embed_sparse_enabled,
        rerank_provider=settings.rerank_provider,
        rerank_model=settings.rerank_model,
        rerank_device=settings.rerank_device,
        rerank_base_url=settings.rerank_base_url,
    )


@router.post("", response_model=EmbedConfigResponse, status_code=201)
async def create_embed_config(body: EmbedConfigCreate, db: AsyncSession = Depends(get_db)):
    """创建 Embedding/Rerank 配置"""
    config_id = str(uuid.uuid4())

    # 如果设为启用，取消同类型的其他启用配置
    if body.is_active:
        result = await db.execute(
            select(EmbedConfig).where(
                EmbedConfig.config_type == body.config_type,
                EmbedConfig.is_active == True,
            )
        )
        for c in result.scalars().all():
            c.is_active = False

    config = EmbedConfig(
        id=config_id,
        name=body.name,
        config_type=body.config_type,
        provider=body.provider,
        local_provider=body.local_provider,
        model_name=body.model_name,
        device=body.device,
        base_url=body.base_url,
        api_key=body.api_key or None,
        timeout=body.timeout,
        sparse_enabled=body.sparse_enabled,
        is_active=body.is_active,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)

    # 如果设为启用，立即重载对应 Provider
    if body.is_active:
        _reload_provider(config)

    return _to_response(config)


@router.put("/{config_id}", response_model=EmbedConfigResponse)
async def update_embed_config(config_id: str, body: EmbedConfigUpdate, db: AsyncSession = Depends(get_db)):
    """更新 Embedding/Rerank 配置"""
    result = await db.execute(select(EmbedConfig).where(EmbedConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="配置不存在")

    update_data = body.model_dump(exclude_unset=True)

    # 如果设为启用，取消同类型的其他启用配置
    if update_data.get("is_active"):
        others = await db.execute(
            select(EmbedConfig).where(
                EmbedConfig.config_type == config.config_type,
                EmbedConfig.is_active == True,
                EmbedConfig.id != config_id,
            )
        )
        for c in others.scalars().all():
            c.is_active = False

    for field, value in update_data.items():
        setattr(config, field, value)

    await db.flush()
    await db.refresh(config)

    # 如果当前配置是启用状态，重载对应 Provider
    if config.is_active:
        _reload_provider(config)

    return _to_response(config)


@router.delete("/{config_id}", status_code=204)
async def delete_embed_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除 Embedding/Rerank 配置"""
    result = await db.execute(select(EmbedConfig).where(EmbedConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="配置不存在")
    await db.delete(config)
    await db.flush()


@router.post("/test", response_model=EmbedTestResponse)
async def test_embed_connection(body: EmbedTestRequest, db: AsyncSession = Depends(get_db)):
    """测试 Embedding/Rerank 服务连通性（使用表单传入的值）"""
    # 如果 api_key 为空且传了 config_id，从数据库回退获取已保存的密钥
    if not body.api_key and body.config_id:
        result = await db.execute(select(EmbedConfig).where(EmbedConfig.id == body.config_id))
        saved = result.scalar_one_or_none()
        if saved and saved.api_key:
            body.api_key = saved.api_key

    try:
        if body.provider == "remote":
            if not body.base_url:
                return EmbedTestResponse(success=False, message="远程服务地址不能为空")

            if body.config_type == "embedding":
                from app.models.embedding.remote import RemoteEmbedder
                embedder = RemoteEmbedder(
                    base_url=body.base_url,
                    model=body.model_name,
                    api_key=body.api_key or "",
                    timeout=body.timeout,
                    sparse_enabled=body.sparse_enabled,
                )
                result = await embedder.embed(["测试文本"])
                if not result or len(result[0]) == 0:
                    return EmbedTestResponse(success=False, message="Dense 返回结果为空")

                msg = f"Dense 连接成功，向量维度: {len(result[0])}"

                # 测试 sparse 端点
                if body.sparse_enabled:
                    sparse_ok = await embedder.check_sparse_support()
                    if sparse_ok:
                        msg += "；Sparse 端点可用 ✓"
                    else:
                        msg += "；Sparse 端点不可用（将降级为 BM25 兜底）"

                return EmbedTestResponse(success=True, message=msg)
            else:
                from app.models.rerank.remote import RemoteReranker
                reranker = RemoteReranker(
                    base_url=body.base_url,
                    model=body.model_name,
                    api_key=body.api_key or "",
                    timeout=body.timeout,
                )
                result = await reranker.rerank("测试查询", ["文档A", "文档B"], top_k=2)
                if result:
                    return EmbedTestResponse(
                        success=True,
                        message=f"连接成功，返回 {len(result)} 个结果"
                    )
                return EmbedTestResponse(success=False, message="返回结果为空")
        else:
            # 本地模型测试：尝试加载模型
            if body.config_type == "embedding":
                local_prov = body.local_provider or "sentence-transformers"
                if local_prov == "flag-embedding":
                    from app.models.embedding.bge_m3 import BgeM3Embedder
                    embedder = BgeM3Embedder(model_name=body.model_name, device=body.device)
                else:
                    from app.models.embedding.sentence_transformer import SentenceTransformerEmbedder
                    embedder = SentenceTransformerEmbedder(model_name=body.model_name, device=body.device)
                result = await embedder.embed(["测试文本"])
                return EmbedTestResponse(
                    success=True,
                    message=f"本地模型加载成功，向量维度: {len(result[0])}"
                )
            else:
                local_prov = body.local_provider or "sentence-transformers"
                if local_prov == "flag-embedding":
                    from app.models.rerank.bge_reranker import BgeReranker
                    reranker = BgeReranker(model_name=body.model_name, device=body.device)
                else:
                    from app.models.rerank.cross_encoder_reranker import CrossEncoderReranker
                    reranker = CrossEncoderReranker(model_name=body.model_name, device=body.device)
                result = await reranker.rerank("测试", ["文档A", "文档B"], top_k=2)
                return EmbedTestResponse(
                    success=True,
                    message=f"本地模型加载成功，返回 {len(result)} 个结果"
                )
    except Exception as e:
        logger.exception("Embed/Rerank 测试失败")
        return EmbedTestResponse(success=False, message=f"测试失败: {str(e)}")


@router.post("/{config_id}/test", response_model=EmbedTestResponse)
async def test_saved_embed_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """测试已保存的配置连通性"""
    result = await db.execute(select(EmbedConfig).where(EmbedConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="配置不存在")

    return await test_embed_connection(EmbedTestRequest(
        provider=config.provider,
        local_provider=config.local_provider,
        model_name=config.model_name,
        device=config.device,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
        config_type=config.config_type,
        sparse_enabled=config.sparse_enabled,
    ), db=db)
