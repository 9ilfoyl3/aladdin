"""审计日志记录（tenant-auth 管理扩展）。

设计要点：
- 与业务写**同一事务**提交（调用方负责 commit），可靠不丢、失败一起回滚。
- 仅记录元数据（id / 用户名 / 动作 / 计数等），**绝不记录业务内容正文**
  （对齐 Content_View_Boundary：审计不得成为内容泄露旁路）。
- actor 信息取自 IdentityContext；请求 ip/ua 由调用方从 Request 传入（可选）。

只追加：本模块只提供写入与查询，不提供更新/删除审计记录的能力。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import AuditActionEnum, AuditResultEnum
from app.auth.identity import IdentityContext
from app.schema.db import AuditLog


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # 优先 X-Forwarded-For（反代场景），回退直连地址
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def add_audit(
    session: AsyncSession,
    *,
    actor: IdentityContext | None,
    action: AuditActionEnum,
    target_type: str | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    detail: dict[str, Any] | None = None,
    result: AuditResultEnum = AuditResultEnum.SUCCESS,
    request: Request | None = None,
    actor_username: str | None = None,
) -> None:
    """把一条审计记录加入当前会话（不在此 commit；随调用方业务事务一起提交）。

    actor 为 None 时（如登录失败尚无身份）允许只带 actor_username。
    """
    entry = AuditLog(
        id=str(uuid.uuid4()),
        actor_user_id=(actor.user_id if actor else None),
        # 操作者用户名：显式传入优先（如登录失败场景），否则取身份对象自带的 username
        actor_username=actor_username or (actor.username if actor else None),
        actor_tenant_id=(actor.tenant_id if actor else None),
        actor_is_super_admin=bool(actor.is_super_admin) if actor else False,
        # 操作者写入时刻的固定角色快照（admin/member/None）。审计是不可变事实，
        # 故落快照而非展示时 join 当前用户。
        actor_role=(actor.role.value if actor and actor.role is not None else None),
        action=action.value,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        detail=detail or {},
        result=result.value,
        ip=_client_ip(request),
        user_agent=(request.headers.get("User-Agent") if request else None),
    )
    session.add(entry)
