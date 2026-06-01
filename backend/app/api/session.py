"""Chat Session API - 会话管理接口（tenant-auth：Guard + 盖章 + 租户隔离 + 内容边界）。

会话/消息为受隔离资源（TenantScopedMixin）：Guard 设置的 contextvar 三态会让本模块
自开 async_session 的 SELECT 自动按租户过滤；创建时盖章 tenant_id；消息正文受
Content_View_Boundary 约束（Super_Admin 默认不可读）。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete

from app.api.deps import require_authenticated
from app.api.errors import CrossTenantError, PermissionDeniedError
from app.auth.identity import IdentityContext
from app.config import get_settings
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


def _ensure_not_super_admin_content(identity: IdentityContext) -> None:
    """Content_View_Boundary：Super_Admin 默认不可读 ChatMessage 正文（R34）。"""
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")


async def _get_owned_session(session, session_id: str) -> ChatSession:
    """取会话（contextvar 兜底确保仅本租户可见 -> 跨租户/不存在统一 404）。"""
    result = await session.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    cs = result.scalar_one_or_none()
    if cs is None:
        raise CrossTenantError()
    return cs


# ============================================================
# 接口实现
# ============================================================


@router.get("")
async def list_sessions(
    identity: IdentityContext = Depends(require_authenticated()),
) -> list[SessionItem]:
    """获取会话列表（按更新时间倒序，仅本租户；空会话过滤）"""
    async with async_session() as session:
        msg_count_subq = (
            select(
                ChatMessageRecord.session_id,
                func.count(ChatMessageRecord.id).label("msg_count"),
            )
            .group_by(ChatMessageRecord.session_id)
            .subquery()
        )
        # inner join 仅返回至少有一条消息的会话（过滤空会话）。
        # ChatSession 经 contextvar 兜底已限定本租户。
        result = await session.execute(
            select(ChatSession, msg_count_subq.c.msg_count)
            .join(msg_count_subq, ChatSession.id == msg_count_subq.c.session_id)
            .order_by(ChatSession.updated_at.desc())
        )
        items = []
        for row in result.all():
            s = row[0]
            count = row[1] or 0
            items.append(SessionItem(
                id=s.id, title=s.title, kb_id=s.kb_id,
                model_config_id=s.model_config_id, message_count=count,
                created_at=s.created_at, updated_at=s.updated_at,
            ))
        return items


@router.post("")
async def create_session(
    req: SessionCreate,
    identity: IdentityContext = Depends(require_authenticated()),
) -> SessionItem:
    """创建新会话（盖章 tenant_id）"""
    new_session = ChatSession(
        id=str(uuid.uuid4()),
        title=req.title,
        kb_id=req.kb_id,
        model_config_id=req.model_config_id,
        tenant_id=identity.tenant_id,
    )
    async with async_session() as session:
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)
        return SessionItem(
            id=new_session.id, title=new_session.title, kb_id=new_session.kb_id,
            model_config_id=new_session.model_config_id, message_count=0,
            created_at=new_session.created_at, updated_at=new_session.updated_at,
        )


@router.put("/{session_id}")
async def update_session(
    session_id: str,
    req: SessionUpdate,
    identity: IdentityContext = Depends(require_authenticated()),
) -> SessionItem:
    """更新会话（重命名）"""
    async with async_session() as session:
        chat_session = await _get_owned_session(session, session_id)
        if req.title is not None:
            chat_session.title = req.title
        await session.commit()
        await session.refresh(chat_session)

        count_result = await session.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session_id
            )
        )
        msg_count = count_result.scalar() or 0
        return SessionItem(
            id=chat_session.id, title=chat_session.title, kb_id=chat_session.kb_id,
            model_config_id=chat_session.model_config_id, message_count=msg_count,
            created_at=chat_session.created_at, updated_at=chat_session.updated_at,
        )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """删除会话及其所有消息（仅本租户）"""
    async with async_session() as session:
        chat_session = await _get_owned_session(session, session_id)
        await session.delete(chat_session)
        await session.commit()
    return {"detail": "已删除"}


@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> list[MessageItem]:
    """获取会话的所有消息（消息正文受内容边界约束）"""
    _ensure_not_super_admin_content(identity)
    async with async_session() as session:
        # 验证会话存在且本租户可见
        await _get_owned_session(session, session_id)
        result = await session.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.created_at)
        )
        messages = result.scalars().all()
        return [
            MessageItem(
                id=m.id, role=m.role, content=m.content,
                references=m.references, agent_steps=m.agent_steps,
                kb_id=m.kb_id, kb_ids=m.kb_ids, created_at=m.created_at,
            )
            for m in messages
        ]


@router.delete("/{session_id}/messages")
async def clear_session_messages(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """清空会话消息（保留会话本身，仅本租户）"""
    async with async_session() as session:
        # 先确认会话本租户可见，再按 session_id 删除其消息
        await _get_owned_session(session, session_id)
        await session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
        )
        await session.commit()
    return {"detail": "已清空"}
