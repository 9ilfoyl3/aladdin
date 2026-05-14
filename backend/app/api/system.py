"""系统配置与健康检查接口"""

import asyncio
import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import get_settings
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["System"])


# ============================================================
# 响应模型
# ============================================================


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    version: str = "0.1.0"
    services: dict = Field(default_factory=dict)


class SystemConfigResponse(BaseModel):
    """系统配置响应"""
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    embed_model: str
    embed_device: str
    rerank_model: str
    rerank_device: str
    agent_max_iterations: int
    agent_timeout: float
    parent_chunk_size: int
    child_chunk_size: int
    chunk_overlap: int
    milvus_host: str
    milvus_port: int


class SystemConfigUpdate(BaseModel):
    """系统配置更新请求（仅允许更新部分字段）"""
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    embed_device: str | None = None
    rerank_device: str | None = None
    agent_max_iterations: int | None = None
    agent_timeout: float | None = None
    parent_chunk_size: int | None = None
    child_chunk_size: int | None = None
    chunk_overlap: int | None = None


# ============================================================
# 健康检查辅助函数
# ============================================================


async def _check_milvus(settings) -> str:
    """检测 Milvus 连接状态"""
    try:
        client = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)
        # 尝试连接并列出 collections
        await asyncio.to_thread(client._connect)
        return "ok"
    except Exception as e:
        logger.warning("Milvus 健康检查失败: %s", e)
        return f"unavailable: {e}"


async def _check_llm(settings) -> str:
    """检测 LLM 服务连接状态"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if settings.llm_provider == "ollama":
                resp = await client.get(f"{settings.llm_base_url}/api/tags")
            else:
                # vLLM / OpenAI 兼容接口
                resp = await client.get(f"{settings.llm_base_url}/v1/models")
            if resp.status_code == 200:
                return "ok"
            return f"unhealthy (status={resp.status_code})"
    except Exception as e:
        logger.warning("LLM 健康检查失败: %s", e)
        return f"unavailable: {e}"


# ============================================================
# 接口实现
# ============================================================


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查 - 实际检测各依赖服务连接状态"""
    settings = get_settings()

    # 并行检测各服务
    milvus_status, llm_status = await asyncio.gather(
        _check_milvus(settings),
        _check_llm(settings),
    )

    services = {
        "database": "ok",
        "milvus": milvus_status,
        "llm": llm_status,
    }

    # 整体状态：任一服务不可用则标记为 degraded
    overall = "ok"
    if any(v != "ok" for v in services.values()):
        overall = "degraded"

    return HealthResponse(status=overall, services=services)


@router.get("/config", response_model=SystemConfigResponse)
async def get_config():
    """获取系统配置"""
    settings = get_settings()
    # API Key 脱敏：只显示前8位 + ***
    api_key_display = ""
    if settings.llm_api_key:
        api_key_display = settings.llm_api_key[:8] + "***" if len(settings.llm_api_key) > 8 else "***"
    return SystemConfigResponse(
        llm_provider=settings.llm_provider,
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_api_key=api_key_display,
        embed_model=settings.embed_model,
        embed_device=settings.embed_device,
        rerank_model=settings.rerank_model,
        rerank_device=settings.rerank_device,
        agent_max_iterations=settings.agent_max_iterations,
        agent_timeout=settings.agent_timeout,
        parent_chunk_size=settings.parent_chunk_size,
        child_chunk_size=settings.child_chunk_size,
        chunk_overlap=settings.chunk_overlap,
        milvus_host=settings.milvus_host,
        milvus_port=settings.milvus_port,
    )


@router.put("/config", response_model=SystemConfigResponse)
async def update_config(body: SystemConfigUpdate):
    """更新系统配置

    注意：运行时修改配置，重启后会恢复 .env 中的值。
    如需持久化，请修改 .env 文件。
    """
    settings = get_settings()

    # 更新非 None 字段
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if hasattr(settings, field):
            # 如果 api_key 传入的是脱敏值（含***），跳过不更新
            if field == "llm_api_key" and "***" in (value or ""):
                continue
            object.__setattr__(settings, field, value)

    # API Key 脱敏返回
    api_key_display = ""
    if settings.llm_api_key:
        api_key_display = settings.llm_api_key[:8] + "***" if len(settings.llm_api_key) > 8 else "***"

    return SystemConfigResponse(
        llm_provider=settings.llm_provider,
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_api_key=api_key_display,
        embed_model=settings.embed_model,
        embed_device=settings.embed_device,
        rerank_model=settings.rerank_model,
        rerank_device=settings.rerank_device,
        agent_max_iterations=settings.agent_max_iterations,
        agent_timeout=settings.agent_timeout,
        parent_chunk_size=settings.parent_chunk_size,
        child_chunk_size=settings.child_chunk_size,
        chunk_overlap=settings.chunk_overlap,
        milvus_host=settings.milvus_host,
        milvus_port=settings.milvus_port,
    )
