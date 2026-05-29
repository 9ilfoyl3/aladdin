"""Agent 预设配置 API

管理 Agent 运行预设（max_iterations、temperature、thinking_enabled 等），
支持 CRUD 操作和内置预设自动创建。
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.schema.db import AgentPreset
from app.storage.database import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-presets", tags=["Agent Config"])


# ============ Pydantic Models ============

class AgentPresetCreate(BaseModel):
    """创建 Agent 预设请求"""
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=500)
    config_json: dict = Field(default_factory=dict)
    is_default: bool = False


class AgentPresetUpdate(BaseModel):
    """更新 Agent 预设请求"""
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    config_json: dict | None = None
    is_default: bool | None = None


class AgentPresetResponse(BaseModel):
    """Agent 预设响应"""
    id: str
    name: str
    description: str | None
    config_json: dict | None
    is_default: bool
    created_at: str
    updated_at: str


# ============ 内置预设 ============

_BUILTIN_PRESETS = [
    {
        "id": "preset-quick-qa",
        "name": "快速问答",
        "description": "单轮检索直接作答，适合简单问题，快速返回结果",
        "config_json": {
            "agent_mode": "hybrid",
            "max_iterations": 5,
            "thinking_enabled": False,
            "temperature": 0.3,
        },
        "is_default": False,
    },
    {
        "id": "preset-smart-reasoning",
        "name": "智能推理",
        "description": "ReAct 多步推理，深度思考和多轮检索，适合复杂问题",
        "config_json": {
            "agent_mode": "agent",
            "max_iterations": 20,
            "thinking_enabled": True,
            "temperature": 0.7,
        },
        "is_default": True,
    },
]


async def get_effective_preset_config(preset_id: str | None) -> dict:
    """获取生效的 Agent 预设配置（config_json）

    指定 preset_id 时返回该预设；否则返回默认预设（is_default=True）。
    都找不到时返回智能推理内置预设的配置作为兜底。

    Returns:
        预设的 config_json 字典，至少包含 agent_mode / max_iterations /
        temperature / thinking_enabled（取自数据库或内置兜底）。
    """
    await _ensure_builtin_presets()

    async with async_session() as session:
        preset = None
        if preset_id:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.id == preset_id)
            )
            preset = result.scalar_one_or_none()
        if preset is None:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.is_default == True)  # noqa: E712
            )
            preset = result.scalar_one_or_none()

    if preset is not None and preset.config_json:
        return dict(preset.config_json)

    # 兜底：内置智能推理预设
    return dict(_BUILTIN_PRESETS[1]["config_json"])


async def _ensure_builtin_presets() -> None:
    """确保内置预设存在（不存在时自动创建）"""
    async with async_session() as session:
        for preset_data in _BUILTIN_PRESETS:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.id == preset_data["id"])
            )
            existing = result.scalar_one_or_none()
            if not existing:
                preset = AgentPreset(**preset_data)
                session.add(preset)
                logger.info("创建内置 Agent 预设: %s", preset_data["name"])
        await session.commit()


def _to_response(preset: AgentPreset) -> AgentPresetResponse:
    """ORM 模型转响应"""
    return AgentPresetResponse(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        config_json=preset.config_json,
        is_default=preset.is_default,
        created_at=preset.created_at.isoformat() if preset.created_at else "",
        updated_at=preset.updated_at.isoformat() if preset.updated_at else "",
    )


# ============ CRUD Endpoints ============

@router.get("", response_model=list[AgentPresetResponse])
async def list_presets():
    """获取所有 Agent 预设列表"""
    # 确保内置预设存在
    await _ensure_builtin_presets()

    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).order_by(AgentPreset.created_at)
        )
        presets = result.scalars().all()
    return [_to_response(p) for p in presets]


@router.post("", response_model=AgentPresetResponse)
async def create_preset(data: AgentPresetCreate):
    """创建新的 Agent 预设"""
    preset = AgentPreset(
        id=str(uuid.uuid4()),
        name=data.name,
        description=data.description,
        config_json=data.config_json,
        is_default=data.is_default,
    )

    async with async_session() as session:
        # 如果设为默认，取消其他默认
        if data.is_default:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.is_default == True)  # noqa: E712
            )
            for existing in result.scalars().all():
                existing.is_default = False

        session.add(preset)
        await session.commit()
        await session.refresh(preset)

    return _to_response(preset)


@router.put("/{preset_id}", response_model=AgentPresetResponse)
async def update_preset(preset_id: str, data: AgentPresetUpdate):
    """更新 Agent 预设"""
    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.id == preset_id)
        )
        preset = result.scalar_one_or_none()
        if not preset:
            raise HTTPException(status_code=404, detail="预设不存在")

        if data.name is not None:
            preset.name = data.name
        if data.description is not None:
            preset.description = data.description
        if data.config_json is not None:
            preset.config_json = data.config_json
        if data.is_default is not None:
            if data.is_default:
                # 取消其他默认
                others = await session.execute(
                    select(AgentPreset).where(
                        AgentPreset.is_default == True,  # noqa: E712
                        AgentPreset.id != preset_id,
                    )
                )
                for other in others.scalars().all():
                    other.is_default = False
            preset.is_default = data.is_default

        await session.commit()
        await session.refresh(preset)

    return _to_response(preset)


@router.delete("/{preset_id}")
async def delete_preset(preset_id: str):
    """删除 Agent 预设"""
    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.id == preset_id)
        )
        preset = result.scalar_one_or_none()
        if not preset:
            raise HTTPException(status_code=404, detail="预设不存在")

        await session.delete(preset)
        await session.commit()

    return {"detail": "已删除"}
