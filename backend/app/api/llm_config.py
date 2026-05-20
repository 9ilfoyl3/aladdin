"""LLM 模型配置管理接口"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM

from app.schema.db import LLMConfig
from app.storage.database import get_db

router = APIRouter(prefix="/api/llm-configs", tags=["LLM Config"])


class LLMConfigCreate(BaseModel):
    name: str
    provider: str  # ollama | vllm
    base_url: str
    model: str
    api_key: Optional[str] = None
    is_default: bool = False
    chat_visible: bool = True
    stream_enabled: bool = True
    thinking_enabled: bool = False
    max_context_tokens: Optional[int] = None

    @field_validator("max_context_tokens", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "" or v is None:
            return None
        return v


class LLMConfigUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    is_default: Optional[bool] = None
    chat_visible: Optional[bool] = None
    stream_enabled: Optional[bool] = None
    thinking_enabled: Optional[bool] = None
    max_context_tokens: Optional[int] = None

    @field_validator("max_context_tokens", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "" or v is None:
            return None
        return v


class LLMConfigResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    name: str
    provider: str
    base_url: str
    model: str
    api_key_set: bool  # 是否已设置 API Key（不返回明文）
    is_default: bool
    chat_visible: bool
    stream_enabled: bool
    thinking_enabled: bool
    max_context_tokens: Optional[int] = None
    created_at: str


@router.get("", response_model=list[LLMConfigResponse])
async def list_llm_configs(chat_visible: Optional[bool] = None, db: AsyncSession = Depends(get_db)):
    """获取所有 LLM 模型配置"""
    query = select(LLMConfig).order_by(LLMConfig.created_at.desc())
    if chat_visible is not None:
        query = query.where(LLMConfig.chat_visible == chat_visible)
    result = await db.execute(query)
    configs = result.scalars().all()
    return [
        LLMConfigResponse(
            id=c.id,
            name=c.name,
            provider=c.provider,
            base_url=c.base_url,
            model=c.model,
            api_key_set=bool(c.api_key),
            is_default=c.is_default,
            chat_visible=c.chat_visible,
            stream_enabled=c.stream_enabled,
            thinking_enabled=c.thinking_enabled,
            max_context_tokens=c.max_context_tokens,
            created_at=c.created_at.isoformat() if c.created_at else "",
        )
        for c in configs
    ]


@router.post("", response_model=LLMConfigResponse, status_code=201)
async def create_llm_config(body: LLMConfigCreate, db: AsyncSession = Depends(get_db)):
    """创建 LLM 模型配置"""
    config_id = str(uuid.uuid4())

    # 如果设为默认，取消其他默认
    if body.is_default:
        result = await db.execute(select(LLMConfig).where(LLMConfig.is_default == True))
        for c in result.scalars().all():
            c.is_default = False

    config = LLMConfig(
        id=config_id,
        name=body.name,
        provider=body.provider,
        base_url=body.base_url,
        model=body.model,
        api_key=body.api_key or None,
        is_default=body.is_default,
        chat_visible=body.chat_visible,
        stream_enabled=body.stream_enabled,
        thinking_enabled=body.thinking_enabled,
        max_context_tokens=body.max_context_tokens,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)

    return LLMConfigResponse(
        id=config.id,
        name=config.name,
        provider=config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_set=bool(config.api_key),
        is_default=config.is_default,
        chat_visible=config.chat_visible,
        stream_enabled=config.stream_enabled,
        thinking_enabled=config.thinking_enabled,
        max_context_tokens=config.max_context_tokens,
        created_at=config.created_at.isoformat() if config.created_at else "",
    )


@router.put("/{config_id}", response_model=LLMConfigResponse)
async def update_llm_config(config_id: str, body: LLMConfigUpdate, db: AsyncSession = Depends(get_db)):
    """更新 LLM 模型配置"""
    result = await db.execute(select(LLMConfig).where(LLMConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    update_data = body.model_dump(exclude_unset=True)

    # 如果设为默认，取消其他默认
    if update_data.get("is_default"):
        others = await db.execute(select(LLMConfig).where(LLMConfig.is_default == True, LLMConfig.id != config_id))
        for c in others.scalars().all():
            c.is_default = False

    for field, value in update_data.items():
        setattr(config, field, value)

    await db.flush()
    await db.refresh(config)

    return LLMConfigResponse(
        id=config.id,
        name=config.name,
        provider=config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_set=bool(config.api_key),
        is_default=config.is_default,
        chat_visible=config.chat_visible,
        stream_enabled=config.stream_enabled,
        thinking_enabled=config.thinking_enabled,
        max_context_tokens=config.max_context_tokens,
        created_at=config.created_at.isoformat() if config.created_at else "",
    )


@router.delete("/{config_id}", status_code=204)
async def delete_llm_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除 LLM 模型配置"""
    result = await db.execute(select(LLMConfig).where(LLMConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    await db.delete(config)
    await db.flush()


class LLMTestRequest(BaseModel):
    """测试模型连通性请求"""
    provider: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    config_id: Optional[str] = None  # 编辑已有配置时传入，用于补全空密钥


class LLMTestResponse(BaseModel):
    """测试模型连通性响应"""
    success: bool
    message: str
    reply: Optional[str] = None


@router.post("/test", response_model=LLMTestResponse)
async def test_llm_connection(body: LLMTestRequest, db: AsyncSession = Depends(get_db)):
    """测试 LLM 模型连通性，发送一条简单消息验证配置是否正确"""
    api_key = body.api_key or ""

    # 如果密钥为空且提供了 config_id，从数据库补全
    if not api_key and body.config_id:
        result = await db.execute(select(LLMConfig).where(LLMConfig.id == body.config_id))
        existing = result.scalar_one_or_none()
        if existing and existing.api_key:
            api_key = existing.api_key

    try:
        if body.provider == "ollama":
            llm = OllamaLLM(base_url=body.base_url, model=body.model)
        else:
            llm = VllmLLM(base_url=body.base_url, model=body.model, api_key=api_key)

        # 发送简单测试消息
        messages = [{"role": "user", "content": "你好，请回复测试成功"}]
        reply = await llm.generate(messages)

        # 关闭连接
        if hasattr(llm, "close"):
            await llm.close()

        return LLMTestResponse(success=True, message="连接成功", reply=reply[:200])
    except Exception as e:
        return LLMTestResponse(success=False, message=f"连接失败: {str(e)}")


@router.post("/{config_id}/test", response_model=LLMTestResponse)
async def test_llm_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """测试已保存的模型配置连通性"""
    result = await db.execute(select(LLMConfig).where(LLMConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    try:
        if config.provider == "ollama":
            llm = OllamaLLM(base_url=config.base_url, model=config.model)
        else:
            llm = VllmLLM(base_url=config.base_url, model=config.model, api_key=config.api_key or "")

        messages = [{"role": "user", "content": "你好，请回复测试成功"}]
        reply = await llm.generate(messages)

        if hasattr(llm, "close"):
            await llm.close()

        return LLMTestResponse(success=True, message="连接成功", reply=reply[:200])
    except Exception as e:
        return LLMTestResponse(success=False, message=f"连接失败: {str(e)}")
