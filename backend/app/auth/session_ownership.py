"""会话归属校验（单一权威实现）

会话/消息是个人对话历史，会话内上传的临时文件也归属会话本人。任何"接收前端/第三方
传入 session_id"的入口（问答续写、会话文件召回等）在触达会话数据前，都必须先校验该
session_id 归属当前行事主体本人，否则任何同租户用户都能凭他人 session_id 读取其历史或
附件内容。

此处收敛为单一实现，供 chat 问答链路与开放检索（``/api/retrieval/search`` 的会话附件源）
共用同一套"存在性非泄露 404"安全语义，避免安全逻辑在多处复制后分叉。
"""

from __future__ import annotations

from app.api.errors import CrossTenantError
from app.auth.identity import IdentityContext
from app.schema.db import ChatSession
from app.storage.database import async_session


async def verify_session_owner(session_id: str, identity: IdentityContext) -> None:
    """校验 ``session_id`` 归属当前行事主体本人，否则抛 ``CrossTenantError``（404）。

    - 跨租户由 contextvar 兜底过滤为不可见 → get 返回 None → 404。
    - owner 不匹配（含同租户他人、无主历史会话）→ 404。
    - tenant_level 机器身份（``acting_subject_id`` 为 None）不绑定自然人 → 一律 404。

    统一以 404（存在性非泄露）表现，不区分"会话不存在"与"非本人会话"，避免探测。
    """
    subject = identity.acting_subject_id
    async with async_session() as session:
        cs = await session.get(ChatSession, session_id)
    if cs is None or subject is None or cs.owner_user_id != subject:
        raise CrossTenantError()
