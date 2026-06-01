"""Agent 预设配置 API

管理 Agent 运行预设（max_iterations、temperature、thinking_enabled 等），
支持 CRUD 操作和内置预设自动创建。
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import require_authenticated, require_tenant_admin
from app.auth.identity import IdentityContext
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


class PromptRewriteRequest(BaseModel):
    """AI 改写系统提示词请求"""
    instruction: str = Field(..., max_length=2000, description="用户描述的角色与特性")
    current_prompt: str | None = Field(default=None, max_length=20000, description="当前已有的提示词（可选，作为改写基础）")


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

@router.get("/placeholders")
async def list_placeholders(
    _identity: IdentityContext = Depends(require_authenticated()),
):
    """返回 system_prompt 支持的占位符变量及默认模板

    供前端在编辑预设时渲染"可插入变量"标签，以及"插入默认模板"功能。
    """
    from app.agent.prompts.progressive_rag import (
        PROGRESSIVE_RAG_PROMPT,
        SYSTEM_PROMPT_PLACEHOLDERS,
    )

    return {
        "placeholders": [
            {"name": name, "description": desc}
            for name, desc in SYSTEM_PROMPT_PLACEHOLDERS.items()
        ],
        "default_prompt": PROGRESSIVE_RAG_PROMPT,
    }


@router.post("/rewrite-prompt")
async def rewrite_prompt(
    data: PromptRewriteRequest,
    _identity: IdentityContext = Depends(require_tenant_admin()),
):
    """基于 Progressive RAG 结构，用默认模型把用户的角色/特性描述改写为完整系统提示词

    用户只需用自然语言描述想要的角色与特性，AI 会产出一份结构完整、保留
    Evidence-First 检索纪律的系统提示词，并保留可用占位符。
    """
    from app.api.chat import _get_llm_for_request
    from app.agent.prompts.progressive_rag import (
        PROGRESSIVE_RAG_PROMPT,
        SYSTEM_PROMPT_PLACEHOLDERS,
    )

    instruction = (data.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="请描述你想要的角色与特性")

    # 使用默认模型（model_config_id=None → 走数据库默认配置 → 全局配置兜底）
    llm, _, _, _ = await _get_llm_for_request(None)

    placeholder_lines = "\n".join(
        f"- {{{name}}}: {desc}" for name, desc in SYSTEM_PROMPT_PLACEHOLDERS.items()
    )

    meta_system = (
        "你是一名资深的提示词工程师，专门为「知识库问答 Agent」编写系统提示词。"
        "你将参考一份成熟的 Progressive RAG 系统提示词作为结构与风格基准，"
        "并依据用户对角色和特性的描述，产出一份全新的、可直接使用的完整系统提示词。\n\n"
        "硬性要求：\n"
        "1. 必须保留 Evidence-First（基于检索证据回答、不臆造）的核心检索纪律。\n"
        "2. 必须保留「先检索知识库、深读 chunk、再作答」的 ReAct 工作流要点。\n"
        "3. 融入用户描述的角色定位、语气、专长与行为偏好。\n"
        "4. 可在合适位置使用以下占位符变量（保持花括号原样，运行时会被替换）：\n"
        f"{placeholder_lines}\n"
        "5. 结构清晰（用 ### 分节），语言与基准提示词一致（以英文为主、可中英混排）。\n"
        "6. 只输出最终的系统提示词正文本身，不要任何前言、解释、代码块包裹或额外说明。"
    )

    base_prompt = (data.current_prompt or "").strip() or PROGRESSIVE_RAG_PROMPT
    meta_user = (
        "【基准 Progressive RAG 系统提示词】\n"
        f"{base_prompt}\n\n"
        "【用户希望的角色与特性】\n"
        f"{instruction}\n\n"
        "请基于以上基准，结合用户描述，产出改写后的完整系统提示词。"
    )

    try:
        rewritten = await llm.generate(
            [
                {"role": "system", "content": meta_system},
                {"role": "user", "content": meta_user},
            ],
            temperature=0.7,
        )
    except Exception as e:
        logger.exception("AI 改写提示词失败")
        raise HTTPException(status_code=502, detail=f"AI 改写失败：{e}")

    rewritten = (rewritten or "").strip()
    if not rewritten:
        raise HTTPException(status_code=502, detail="AI 返回为空，请重试")

    # 去除模型可能包裹的 ``` 代码块
    if rewritten.startswith("```"):
        lines = rewritten.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        rewritten = "\n".join(lines).strip()

    return {"prompt": rewritten}


@router.get("", response_model=list[AgentPresetResponse])
async def list_presets(
    _identity: IdentityContext = Depends(require_authenticated()),
):
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
async def create_preset(
    data: AgentPresetCreate,
    _identity: IdentityContext = Depends(require_tenant_admin()),
):
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
async def update_preset(
    preset_id: str,
    data: AgentPresetUpdate,
    _identity: IdentityContext = Depends(require_tenant_admin()),
):
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
async def delete_preset(
    preset_id: str,
    _identity: IdentityContext = Depends(require_tenant_admin()),
):
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
