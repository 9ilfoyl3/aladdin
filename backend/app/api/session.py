"""Chat Session API - 会话管理接口

提供对话会话的 CRUD 操作，支持会话历史消息查询。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete

from app.schema.db import ChatSession, ChatMessageRecord
from app.storage.database import async_session

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


# ============================================================
# 请求/响应模型
# ============================================================


class SessionCreate(BaseModel):
    """创建会话请求"""
    title: str = Field(default="新对话", max_length=200)
    kb_id: str | None = None
    model_config_id: str | None = None


class SessionUpdate(BaseModel):
    """更新会话请求"""
    title: str | None = Field(default=None, max_length=200)


class SessionItem(BaseModel):
    """会话列表项"""
    id: str
    title: str
    kb_id: str | None
    model_config_id: str | None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class MessageItem(BaseModel):
    """消息列表项"""
    id: str
    role: str
    content: str
    references: dict | list | None = None
    agent_steps: list | None = None
    kb_id: str | None = None
    kb_ids: list | None = None
    created_at: datetime


# ============================================================
# 接口实现
# ============================================================


@router.get("")
async def list_sessions() -> list[SessionItem]:
    """获取会话列表（按更新时间倒序）"""
    async with async_session() as session:
        # 子查询：统计每个会话的消息数
        msg_count_subq = (
            select(
                ChatMessageRecord.session_id,
                func.count(ChatMessageRecord.id).label("msg_count"),
            )
            .group_by(ChatMessageRecord.session_id)
            .subquery()
        )

        result = await session.execute(
            select(ChatSession, msg_count_subq.c.msg_count)
            .outerjoin(msg_count_subq, ChatSession.id == msg_count_subq.c.session_id)
            .order_by(ChatSession.updated_at.desc())
        )

        items = []
        for row in result.all():
            s = row[0]
            count = row[1] or 0
            items.append(SessionItem(
                id=s.id,
                title=s.title,
                kb_id=s.kb_id,
                model_config_id=s.model_config_id,
                message_count=count,
                created_at=s.created_at,
                updated_at=s.updated_at,
            ))
        return items


@router.post("")
async def create_session(req: SessionCreate) -> SessionItem:
    """创建新会话"""
    new_session = ChatSession(
        id=str(uuid.uuid4()),
        title=req.title,
        kb_id=req.kb_id,
        model_config_id=req.model_config_id,
    )
    async with async_session() as session:
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)
        return SessionItem(
            id=new_session.id,
            title=new_session.title,
            kb_id=new_session.kb_id,
            model_config_id=new_session.model_config_id,
            message_count=0,
            created_at=new_session.created_at,
            updated_at=new_session.updated_at,
        )


@router.put("/{session_id}")
async def update_session(session_id: str, req: SessionUpdate) -> SessionItem:
    """更新会话（重命名）"""
    async with async_session() as session:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        chat_session = result.scalar_one_or_none()
        if not chat_session:
            raise HTTPException(status_code=404, detail="会话不存在")

        if req.title is not None:
            chat_session.title = req.title
        await session.commit()
        await session.refresh(chat_session)

        # 查询消息数
        count_result = await session.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session_id
            )
        )
        msg_count = count_result.scalar() or 0

        return SessionItem(
            id=chat_session.id,
            title=chat_session.title,
            kb_id=chat_session.kb_id,
            model_config_id=chat_session.model_config_id,
            message_count=msg_count,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
        )


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """删除会话及其所有消息"""
    async with async_session() as session:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        chat_session = result.scalar_one_or_none()
        if not chat_session:
            raise HTTPException(status_code=404, detail="会话不存在")

        await session.delete(chat_session)
        await session.commit()
    return {"detail": "已删除"}


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str) -> list[MessageItem]:
    """获取会话的所有消息"""
    async with async_session() as session:
        # 验证会话存在
        sess_result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        if not sess_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="会话不存在")

        result = await session.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.created_at)
        )
        messages = result.scalars().all()
        return [
            MessageItem(
                id=m.id,
                role=m.role,
                content=m.content,
                references=m.references,
                agent_steps=m.agent_steps,
                kb_id=m.kb_id,
                kb_ids=m.kb_ids,
                created_at=m.created_at,
            )
            for m in messages
        ]


@router.delete("/{session_id}/messages")
async def clear_session_messages(session_id: str):
    """清空会话消息（保留会话本身）"""
    async with async_session() as session:
        sess_result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        if not sess_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="会话不存在")

        await session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
        )
        await session.commit()
    return {"detail": "已清空"}
