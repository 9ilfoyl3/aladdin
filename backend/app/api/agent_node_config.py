"""Agent 节点模型配置接口"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schema.db import AgentNodeConfig, LLMConfig
from app.storage.database import get_db

router = APIRouter(prefix="/api/agent-node-configs", tags=["Agent Node Config"])

# 有效节点名称
VALID_NODE_NAMES = ("router", "planner", "reflector")


class AgentNodeConfigUpdate(BaseModel):
    router_model_id: Optional[str] = None
    planner_model_id: Optional[str] = None
    reflector_model_id: Optional[str] = None


class AgentNodeConfigResponse(BaseModel):
    router_model_id: Optional[str] = None
    router_model_name: Optional[str] = None
    planner_model_id: Optional[str] = None
    planner_model_name: Optional[str] = None
    reflector_model_id: Optional[str] = None
    reflector_model_name: Optional[str] = None


@router.get("", response_model=AgentNodeConfigResponse)
async def get_agent_node_configs(db: AsyncSession = Depends(get_db)):
    """获取所有 Agent 节点的模型配置"""
    result = await db.execute(select(AgentNodeConfig))
    configs = {c.node_name: c for c in result.scalars().all()}

    response_data: dict = {}
    for node_name in VALID_NODE_NAMES:
        node_config = configs.get(node_name)
        model_id = node_config.model_config_id if node_config else None
        model_name = None

        if model_id:
            llm_result = await db.execute(
                select(LLMConfig.name).where(LLMConfig.id == model_id)
            )
            model_name = llm_result.scalar_one_or_none()

        response_data[f"{node_name}_model_id"] = model_id
        response_data[f"{node_name}_model_name"] = model_name

    return AgentNodeConfigResponse(**response_data)


@router.put("", response_model=AgentNodeConfigResponse)
async def update_agent_node_configs(
    body: AgentNodeConfigUpdate, db: AsyncSession = Depends(get_db)
):
    """批量更新 Agent 节点模型配置"""
    update_data = body.model_dump(exclude_unset=True)

    for node_name in VALID_NODE_NAMES:
        field_key = f"{node_name}_model_id"
        if field_key not in update_data:
            continue

        value = update_data[field_key]

        # 查询现有节点配置
        result = await db.execute(
            select(AgentNodeConfig).where(AgentNodeConfig.node_name == node_name)
        )
        node_config = result.scalar_one_or_none()

        if value == "":
            # 空字符串：清除绑定
            if node_config:
                node_config.model_config_id = None
        elif value is not None:
            # 非空字符串：验证 model_config_id 存在后 upsert
            llm_result = await db.execute(
                select(LLMConfig).where(LLMConfig.id == value)
            )
            if llm_result.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"模型不存在: {value}",
                )

            if node_config:
                node_config.model_config_id = value
            else:
                node_config = AgentNodeConfig(
                    node_name=node_name, model_config_id=value
                )
                db.add(node_config)
        # value is None（字段传了但值为 None）：不操作

    await db.flush()

    # 返回更新后的配置
    return await get_agent_node_configs(db)
