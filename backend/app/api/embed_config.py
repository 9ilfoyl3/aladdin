"""Embedding/Rerank 模型配置管理接口

统一使用远程服务，通过此接口管理服务地址和参数。
"""

import uuid
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_tenant_admin
from app.schema.db import EmbedConfig
from app.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/embed-configs",
    tags=["Embed Config"],
    dependencies=[Depends(require_tenant_admin())],
)


# ============================================================
# 请求/响应模型
# ============================================================


class EmbedConfigCreate(BaseModel):
    name: str
    config_type: str  # embedding | rerank
    model_name: str = "BAAI/bge-m3"
    # 远程服务字段
    base_url: str
    api_key: Optional[str] = None
    timeout: float = 60.0
    # sparse 向量支持（仅 embedding 类型有效）
    sparse_enabled: bool = True
    # 状态
    is_active: bool = False


class EmbedConfigUpdate(BaseModel):
    name: Optional[str] = None
    model_name: Optional[str] = None
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
    model_name: str
    base_url: Optional[str] = None
    api_key_set: bool
    timeout: float
    sparse_enabled: bool
    is_active: bool
    created_at: str
    updated_at: str


class EmbedTestRequest(BaseModel):
    """测试连通性请求"""
    model_name: str = "BAAI/bge-m3"
    base_url: str
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
        model_name=config.model_name,
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
            "model_name": config.model_name,
            "base_url": config.base_url or "",
            "api_key": config.api_key or "",
            "timeout": config.timeout,
            "sparse_enabled": config.sparse_enabled,
        }
        if config.config_type == "embedding":
            manager.reload_embedder(**kwargs)
        else:
            manager.reload_reranker(**kwargs)
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
    embed_model: str
    embed_base_url: str
    embed_sparse_enabled: bool
    rerank_model: str
    rerank_base_url: str


@router.get("/current", response_model=EmbedCurrentResponse)
async def get_current_embed_config():
    """获取当前生效的 Embedding/Rerank 配置（来自环境变量）"""
    from app.config import get_settings
    settings = get_settings()
    return EmbedCurrentResponse(
        embed_model=settings.embed_model,
        embed_base_url=settings.embed_base_url,
        embed_sparse_enabled=settings.embed_sparse_enabled,
        rerank_model=settings.rerank_model,
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
        provider="remote",
        model_name=body.model_name,
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
    """测试 Embedding/Rerank 服务连通性"""
    # 如果 api_key 为空且传了 config_id，从数据库回退获取已保存的密钥
    if not body.api_key and body.config_id:
        result = await db.execute(select(EmbedConfig).where(EmbedConfig.id == body.config_id))
        saved = result.scalar_one_or_none()
        if saved and saved.api_key:
            body.api_key = saved.api_key

    if not body.base_url:
        return EmbedTestResponse(success=False, message="远程服务地址不能为空")

    # 探活策略（与 Worker 启动健康检查共用 app.models.probe）：
    # 1. 优先探 /health —— 自建服务（TEI/Infinity）即使推理队列打满也能秒回，不占队列。
    # 2. /health 不存在（404/405）—— 云端网关（百炼）只有推理端点，降级为最小推理请求验证。
    from app.models.probe import check_health

    try:
        health = await check_health(body.base_url, timeout=5.0)
    except httpx.ConnectError:
        return EmbedTestResponse(success=False, message="无法连接到服务，请检查地址")
    except httpx.TimeoutException:
        return EmbedTestResponse(success=False, message="连接超时，请检查地址和网络")
    except Exception as e:
        logger.warning("health 探测异常，降级为推理探活: %s", e)
        health = None

    if health is False:
        return EmbedTestResponse(success=False, message="服务未就绪，请检查服务状态")

    # 自建服务 /health 已通过 —— 额外探测实际推理端点的「路径 + 鉴权」是否正确。
    # 策略：发一个带 Auth 但 body 为空的 POST 到推理端点。
    # - 路径错 → 404（端点不存在）
    # - Key 错/缺 → 401（鉴权失败）
    # - 路径对 + Key 对 → 400/422（参数不全,预期行为,证明端点可达且鉴权通过）
    # 不会占用推理队列（请求在参数校验阶段就被拒绝,不入队）。
    if health is True:
        from app.models.embedding.remote import RemoteEmbedder
        probe_base = body.base_url.rstrip("/")
        # 推导实际推理端点 URL（与 RemoteEmbedder 逻辑一致）
        if body.config_type == "rerank":
            if probe_base.endswith("/v1"):
                probe_url = f"{probe_base}/reranks"
            elif "/" not in probe_base.split("://", 1)[-1].lstrip("/"):
                probe_url = f"{probe_base}/rerank"
            else:
                probe_url = probe_base
        else:
            if probe_base.endswith("/v1"):
                probe_url = f"{probe_base}/embeddings"
            elif "/" not in probe_base.split("://", 1)[-1].lstrip("/"):
                probe_url = f"{probe_base}/embeddings"
            else:
                probe_url = probe_base

        headers = {"Content-Type": "application/json"}
        if body.api_key:
            headers["Authorization"] = f"Bearer {body.api_key}"

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                # 发空 JSON body — 触发快速 400/422，不进推理队列
                resp = await client.post(probe_url, headers=headers, json={})
            if resp.status_code == 401:
                return EmbedTestResponse(success=False, message="鉴权失败 (HTTP 401)，请检查 API Key")
            if resp.status_code == 403:
                return EmbedTestResponse(success=False, message="鉴权失败 (HTTP 403)，请检查 API Key")
            if resp.status_code == 404:
                return EmbedTestResponse(
                    success=False,
                    message="推理端点不存在 (HTTP 404)，请检查服务地址格式（Infinity 模式不带 /v1）",
                )
            # 400/422/200 都说明端点可达 + 鉴权通过
        except httpx.ConnectError:
            return EmbedTestResponse(success=False, message="无法连接到推理端点，请检查地址")
        except httpx.TimeoutException:
            return EmbedTestResponse(success=False, message="推理端点超时，请检查地址")
        except Exception:
            pass  # 其他异常不阻断，继续走正常流程

        # 端点可达 + 鉴权通过，补充 sparse 检测
        if body.config_type == "embedding" and body.sparse_enabled:
            embedder = RemoteEmbedder(
                base_url=body.base_url,
                model=body.model_name,
                api_key=body.api_key or "",
                timeout=min(body.timeout, 15.0),
                sparse_enabled=body.sparse_enabled,
            )
            sparse_ok = await embedder.check_sparse_support()
            suffix = "；Sparse 端点可用 ✓" if sparse_ok else "；Sparse 端点不可用（将降级为 BM25 兜底）"
            return EmbedTestResponse(success=True, message="连接成功，服务在线" + suffix)
        return EmbedTestResponse(success=True, message="连接成功，服务在线")

    # health is None：无 /health 路由（云端网关），发最小推理请求验证
    try:
        if body.config_type == "embedding":
            from app.models.embedding.remote import RemoteEmbedder
            embedder = RemoteEmbedder(
                base_url=body.base_url,
                model=body.model_name,
                api_key=body.api_key or "",
                timeout=min(body.timeout, 15.0),
                sparse_enabled=body.sparse_enabled,
            )
            vectors = await embedder.embed(["连接测试"])
            dim = len(vectors[0]) if vectors and vectors[0] else 0
            msg = f"连接成功，向量维度 {dim}" if dim else "连接成功，服务在线"
            if body.sparse_enabled:
                sparse_ok = await embedder.check_sparse_support()
                msg += "；Sparse 端点可用 ✓" if sparse_ok else "；Sparse 端点不可用（将降级为 BM25 兜底）"
            return EmbedTestResponse(success=True, message=msg)
        else:
            from app.models.rerank.remote import RemoteReranker
            reranker = RemoteReranker(
                base_url=body.base_url,
                model=body.model_name,
                api_key=body.api_key or "",
                timeout=min(body.timeout, 15.0),
            )
            await reranker.rerank("连接测试", ["这是一段用于连通性测试的候选文本"], top_k=1)
            return EmbedTestResponse(success=True, message="连接成功，服务在线")

    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        detail = e.response.text[:200] if e.response.text else ""
        if code in (401, 403):
            return EmbedTestResponse(success=False, message=f"鉴权失败 (HTTP {code})，请检查 API Key")
        if code == 404:
            return EmbedTestResponse(success=False, message=f"端点不存在 (HTTP 404)，请检查服务地址和接口格式。{detail}")
        return EmbedTestResponse(success=False, message=f"服务返回错误 (HTTP {code})。{detail}")
    except httpx.ConnectError:
        return EmbedTestResponse(success=False, message="无法连接到服务，请检查地址")
    except httpx.TimeoutException:
        return EmbedTestResponse(success=False, message="连接超时，请检查地址和网络")
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
        model_name=config.model_name,
        base_url=config.base_url or "",
        api_key=config.api_key,
        timeout=config.timeout,
        config_type=config.config_type,
        sparse_enabled=config.sparse_enabled,
    ), db=db)
