"""ASR（语音识别）服务配置管理接口"""

import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_platform
from app.schema.db import ASRConfig
from app.storage.database import get_db

# 能力配置（ASR 服务）属平台底座，全平台一份，仅超级管理员维护
# （capability-config-to-platform）。ASRConfig 表无 tenant_id（全局单份），
# 守卫收紧为 require_platform：超管可管，租户管理员不可见不可改。
router = APIRouter(
    prefix="/api/asr-configs",
    tags=["ASR Config"],
    dependencies=[Depends(require_platform())],
)

_VALID_PROVIDER_TYPES = ("openai", "external_api")


class ASRConfigCreate(BaseModel):
    """创建 ASR 服务配置请求"""
    name: str
    provider_type: str = "openai"  # openai（OpenAI 兼容接口）
    vendor: Optional[str] = None  # 服务商（仅 UI 记忆，自动回填 base_url）
    api_url: str
    api_key: Optional[str] = None
    model_name: str
    language: Optional[str] = None
    timeout: float = 300.0
    is_default: bool = False
    is_fallback: bool = False
    extra_config: Optional[dict] = None


class ASRConfigUpdate(BaseModel):
    """更新 ASR 服务配置请求（所有字段 Optional）"""
    name: Optional[str] = None
    provider_type: Optional[str] = None
    vendor: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    language: Optional[str] = None
    timeout: Optional[float] = None
    is_default: Optional[bool] = None
    is_fallback: Optional[bool] = None
    extra_config: Optional[dict] = None


class ASRConfigResponse(BaseModel):
    """ASR 服务配置响应（api_key 脱敏）"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    provider_type: str
    vendor: Optional[str] = None
    api_url: str
    api_key_set: bool
    model_name: str
    language: Optional[str] = None
    timeout: float
    is_default: bool
    is_fallback: bool
    extra_config: Optional[dict] = None
    created_at: str
    updated_at: str


class ASRTestRequest(BaseModel):
    """临时 ASR 连通性测试请求"""
    provider_type: str = "openai"
    api_url: str
    api_key: Optional[str] = None
    timeout: float = 30.0


class ASRTestResponse(BaseModel):
    """ASR 连通性测试响应"""
    success: bool
    message: str
    elapsed_ms: Optional[float] = None


def _validate_asr_config(name: str, provider_type: str, api_url: str, model_name: str,
                         timeout: float, is_default: bool, is_fallback: bool) -> None:
    """校验 ASR 配置字段，不通过时抛出 HTTPException 422"""
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="名称不能为空")
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="名称过长，最大 100 字符")
    if provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(status_code=422, detail="类型无效，仅支持 openai")
    if not api_url or not api_url.strip():
        raise HTTPException(status_code=422, detail="API 地址不能为空")
    if not model_name or not model_name.strip():
        raise HTTPException(status_code=422, detail="模型名称不能为空")
    if timeout < 1 or timeout > 1800:
        raise HTTPException(status_code=422, detail="超时时间须在 1-1800 秒之间")
    if is_default and is_fallback:
        raise HTTPException(status_code=422, detail="同一服务不能同时设为默认和备用")


def _config_to_response(config: ASRConfig) -> ASRConfigResponse:
    """将 ORM 对象转换为响应模型"""
    return ASRConfigResponse(
        id=config.id,
        name=config.name,
        provider_type=config.provider_type,
        vendor=config.vendor,
        api_url=config.api_url,
        api_key_set=bool(config.api_key),
        model_name=config.model_name,
        language=config.language,
        timeout=config.timeout,
        is_default=config.is_default,
        is_fallback=config.is_fallback,
        extra_config=config.extra_config,
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.get("", response_model=list[ASRConfigResponse])
async def list_asr_configs(db: AsyncSession = Depends(get_db)):
    """获取所有 ASR 服务配置，按创建时间倒序排列"""
    result = await db.execute(select(ASRConfig).order_by(ASRConfig.created_at.desc()))
    configs = result.scalars().all()
    return [_config_to_response(c) for c in configs]


@router.post("", response_model=ASRConfigResponse, status_code=201)
async def create_asr_config(body: ASRConfigCreate, db: AsyncSession = Depends(get_db)):
    """创建 ASR 服务配置"""
    _validate_asr_config(
        name=body.name,
        provider_type=body.provider_type,
        api_url=body.api_url,
        model_name=body.model_name,
        timeout=body.timeout,
        is_default=body.is_default,
        is_fallback=body.is_fallback,
    )

    # 如果设为默认，取消其他默认
    if body.is_default:
        result = await db.execute(select(ASRConfig).where(ASRConfig.is_default == True))
        for c in result.scalars().all():
            c.is_default = False

    # 如果设为备用，取消其他备用
    if body.is_fallback:
        result = await db.execute(select(ASRConfig).where(ASRConfig.is_fallback == True))
        for c in result.scalars().all():
            c.is_fallback = False

    config = ASRConfig(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        provider_type=body.provider_type,
        vendor=(body.vendor or "").strip() or None,
        api_url=body.api_url.strip(),
        api_key=body.api_key or None,
        model_name=body.model_name.strip(),
        language=(body.language or "").strip() or None,
        timeout=body.timeout,
        is_default=body.is_default,
        is_fallback=body.is_fallback,
        extra_config=body.extra_config,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)

    return _config_to_response(config)


async def _perform_asr_test(provider_type: str, api_url: str, api_key: Optional[str], timeout: float) -> ASRTestResponse:
    """执行 ASR 连通性测试的核心逻辑（探测 base_url 可达性）"""
    start = time.time()
    try:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 先尝试 GET（健康检查），失败则尝试 HEAD
            # ASR 服务的转写端点通常只接受 POST，GET/HEAD 返回 404/405 也视为可达
            try:
                response = await client.get(api_url, headers=headers)
            except Exception:
                response = await client.head(api_url, headers=headers)
        elapsed_ms = (time.time() - start) * 1000
        status = response.status_code
        if 200 <= status < 500:
            # 4xx 表示服务可达但路径/方法不匹配（如 404/405），视为连通
            return ASRTestResponse(success=True, message=f"服务可达，状态码 {status}", elapsed_ms=elapsed_ms)
        return ASRTestResponse(success=False, message=f"服务异常，状态码 {status}", elapsed_ms=elapsed_ms)
    except httpx.TimeoutException:
        return ASRTestResponse(success=False, message="连接超时", elapsed_ms=None)
    except Exception as e:
        return ASRTestResponse(success=False, message=f"连接失败: {str(e)}", elapsed_ms=None)


@router.post("/test", response_model=ASRTestResponse)
async def test_asr_connection(body: ASRTestRequest):
    """临时配置连通性测试（无需先保存）"""
    return await _perform_asr_test(
        provider_type=body.provider_type,
        api_url=body.api_url,
        api_key=body.api_key,
        timeout=body.timeout,
    )


@router.post("/{config_id}/test", response_model=ASRTestResponse)
async def test_saved_asr_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """测试已保存的 ASR 服务配置连通性"""
    result = await db.execute(select(ASRConfig).where(ASRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="ASR 配置不存在")

    return await _perform_asr_test(
        provider_type=config.provider_type,
        api_url=config.api_url,
        api_key=config.api_key,
        timeout=min(config.timeout, 30.0),
    )


@router.put("/{config_id}", response_model=ASRConfigResponse)
async def update_asr_config(config_id: str, body: ASRConfigUpdate, db: AsyncSession = Depends(get_db)):
    """更新 ASR 服务配置（部分更新）"""
    result = await db.execute(select(ASRConfig).where(ASRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="ASR 配置不存在")

    update_data = body.model_dump(exclude_unset=True)

    # api_key 为空字符串时保持原值
    if "api_key" in update_data and update_data["api_key"] == "":
        del update_data["api_key"]

    if "name" in update_data:
        name = update_data["name"]
        if not name or not name.strip():
            raise HTTPException(status_code=422, detail="名称不能为空")
        if len(name) > 100:
            raise HTTPException(status_code=422, detail="名称过长，最大 100 字符")
        update_data["name"] = name.strip()

    if "provider_type" in update_data:
        if update_data["provider_type"] not in _VALID_PROVIDER_TYPES:
            raise HTTPException(status_code=422, detail="类型无效，仅支持 openai")

    if "api_url" in update_data:
        if not update_data["api_url"] or not update_data["api_url"].strip():
            raise HTTPException(status_code=422, detail="API 地址不能为空")
        update_data["api_url"] = update_data["api_url"].strip()

    if "model_name" in update_data:
        if not update_data["model_name"] or not update_data["model_name"].strip():
            raise HTTPException(status_code=422, detail="模型名称不能为空")
        update_data["model_name"] = update_data["model_name"].strip()

    if "language" in update_data:
        update_data["language"] = (update_data["language"] or "").strip() or None

    if "vendor" in update_data:
        update_data["vendor"] = (update_data["vendor"] or "").strip() or None

    if "timeout" in update_data:
        if update_data["timeout"] < 1 or update_data["timeout"] > 1800:
            raise HTTPException(status_code=422, detail="超时时间须在 1-1800 秒之间")

    # is_default / is_fallback 互斥校验
    new_is_default = update_data.get("is_default", config.is_default)
    new_is_fallback = update_data.get("is_fallback", config.is_fallback)
    if new_is_default and new_is_fallback:
        raise HTTPException(status_code=422, detail="同一服务不能同时设为默认和备用")

    # 如果设为默认，取消其他默认
    if update_data.get("is_default"):
        others = await db.execute(
            select(ASRConfig).where(ASRConfig.is_default == True, ASRConfig.id != config_id)
        )
        for c in others.scalars().all():
            c.is_default = False

    # 如果设为备用，取消其他备用
    if update_data.get("is_fallback"):
        others = await db.execute(
            select(ASRConfig).where(ASRConfig.is_fallback == True, ASRConfig.id != config_id)
        )
        for c in others.scalars().all():
            c.is_fallback = False

    for field, value in update_data.items():
        setattr(config, field, value)

    await db.flush()
    await db.refresh(config)

    return _config_to_response(config)


@router.delete("/{config_id}", status_code=204)
async def delete_asr_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除 ASR 服务配置"""
    result = await db.execute(select(ASRConfig).where(ASRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="ASR 配置不存在")
    await db.delete(config)
    await db.flush()
