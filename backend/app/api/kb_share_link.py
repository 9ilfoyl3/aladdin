"""跨租户知识库分享链接路由（cross-tenant-kb-share）。

场景：A 租户用户把自己拥有的知识库，通过链接点对点只读分享给任意租户的某个用户。
交互为「分享链接领取制」——不主动选他租户的人（无法、也不应翻对方通讯录），而是
生成链接发给对方；对方**登录后**凭 token 领取，领取者即被授权主体。

与 Invitation（建租户/建用户，免登录建号）区分：本链接面向「已存在用户领取已存在 KB
的访问权」，必须登录领取。领取动作向 KnowledgeBaseGrant upsert 一条
grantee_type=user、grantee_id=领取者 user_id 的记录，与租户内点对点分享共用同一套
授权数据——故授权跟人走、换租户不影响。

权限：
- 签发 / 列表 / 吊销：仅 KB owner（与租户内分享同款 owner 专属闸门）。
- 领取：任意已认证自然人用户（含其他租户用户）。不允许 KB owner 领取自己的库；
  也不允许同租户用户走此链接（同租户用走既有分享）。

安全：token 只存哈希，token_plain 供 owner 复制重发；受 expires_at + max_uses 约束；
吊销即 is_active=False。第一版仅 read。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_authenticated
from app.api.errors import (
    CrossTenantError,
    PermissionDeniedError,
)
from app.auth.audit import add_audit
from app.auth.constants import (
    AuditActionEnum,
    GranteeTypeEnum,
    GrantPermissionEnum,
)
from app.auth.identity import IdentityContext
from app.schema.db import (
    KbShareLink,
    KnowledgeBase,
    KnowledgeBaseGrant,
    Tenant,
    User,
)

router = APIRouter(prefix="/api/kb-share-links", tags=["KbShareLink"])

# 分享链接 token 前缀：与 API Key(sk-) / 邀请(inv-) 区分，避免误用。
_SHARE_PREFIX = "kbshare-"
_TOKEN_BYTES = 32


def _generate_token() -> str:
    return f"{_SHARE_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    # 与 DB 列 TIMESTAMP WITHOUT TIME ZONE 一致：naive UTC（对齐 invitation_routes）。
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _link_active(link: KbShareLink) -> bool:
    """有效性：is_active + 未过期 + 未用满。"""
    if not link.is_active:
        return False
    exp = link.expires_at
    if exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    if exp < _now():
        return False
    if link.max_uses is not None and link.used_count >= link.max_uses:
        return False
    return True


# ============================================================
# 请求/响应模型
# ============================================================


class CreateShareLinkRequest(BaseModel):
    kb_id: str = Field(..., description="要分享的知识库 id（须为本人拥有的库）")
    expires_in_hours: int = Field(..., ge=1, le=24 * 30, description="有效期（小时），1h–30d")
    max_uses: int | None = Field(default=None, ge=1, description="可领取次数；留空=有效期内不限次")


class ShareLinkCreateResponse(BaseModel):
    id: str
    token: str = Field(description="完整分享 token，凭此拼接领取链接")
    kb_id: str
    permission: str
    expires_at: str
    max_uses: int | None


class ShareLinkItem(BaseModel):
    id: str
    token: str | None = None  # 明文 token，供列表随时复制
    kb_id: str
    permission: str
    max_uses: int | None
    used_count: int
    expires_at: str
    is_active: bool
    created_at: str


class ShareLinkInfo(BaseModel):
    """领取页展示用。"""
    kb_name: str
    owner_username: str | None
    owner_avatar: str | None = None
    owner_tenant_name: str | None
    permission: str
    valid: bool
    # 当前登录者是否可领取（同租户 / 自己的库 / 已领取等场景下为 False 并给出原因）
    can_accept: bool
    reason: str | None = None


# ============================================================
# 辅助
# ============================================================


async def _load_owned_kb(db: AsyncSession, identity: IdentityContext, kb_id: str) -> KnowledgeBase:
    """加载并校验为当前身份拥有的 KB（owner 专属，对齐 _ensure_kb_owner 语义）。

    跨租户 / 不存在 -> 404（存在性非泄露）；同租户非 owner -> 403。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    if kb.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    if kb.owner_user_id is None or kb.owner_user_id != identity.acting_subject_id:
        raise PermissionDeniedError("仅知识库创建人可分享该库")
    return kb


async def _load_valid_link(db: AsyncSession, token: str) -> KbShareLink:
    link = (await db.execute(
        select(KbShareLink).where(KbShareLink.token_hash == _hash_token(token))
    )).scalar_one_or_none()
    if link is None or not _link_active(link):
        raise CrossTenantError("分享链接无效或已失效")
    return link


# ============================================================
# 签发 / 列表 / 吊销（owner 专属）
# ============================================================


@router.post("", response_model=ShareLinkCreateResponse, status_code=201)
async def create_share_link(
    body: CreateShareLinkRequest,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """为本人拥有的知识库签发一个跨租户只读分享链接。"""
    kb = await _load_owned_kb(db, identity, body.kb_id)

    raw_token = _generate_token()
    link_id = str(uuid.uuid4())
    expires_at = _now() + timedelta(hours=body.expires_in_hours)
    db.add(KbShareLink(
        id=link_id,
        token_hash=_hash_token(raw_token),
        token_plain=raw_token,
        kb_id=kb.id,
        owner_tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id or "",
        permission=GrantPermissionEnum.READ.value,  # 第一版仅 read
        max_uses=body.max_uses,
        used_count=0,
        expires_at=expires_at,
        is_active=True,
    ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_SHARE_LINK_CREATE,
        target_type="kb", target_id=kb.id, target_name=kb.name,
        detail={"link_id": link_id, "max_uses": body.max_uses,
                "expires_at": expires_at.isoformat()},
        request=request,
    )
    await db.commit()
    return ShareLinkCreateResponse(
        id=link_id, token=raw_token, kb_id=kb.id,
        permission=GrantPermissionEnum.READ.value,
        expires_at=expires_at.isoformat(), max_uses=body.max_uses,
    )


@router.get("", response_model=list[ShareLinkItem])
async def list_share_links(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """列出某库的分享链接（仅 owner）。"""
    await _load_owned_kb(db, identity, kb_id)
    rows = (await db.execute(
        select(KbShareLink).where(KbShareLink.kb_id == kb_id)
        .order_by(KbShareLink.created_at.desc())
    )).scalars().all()
    return [
        ShareLinkItem(
            id=r.id, token=r.token_plain, kb_id=r.kb_id, permission=r.permission,
            max_uses=r.max_uses, used_count=r.used_count,
            expires_at=r.expires_at.isoformat() if r.expires_at else "",
            is_active=_link_active(r),
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.delete("/{link_id}", status_code=204)
async def revoke_share_link(
    link_id: str,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """吊销分享链接（软删除：is_active=False）。仅 owner。

    吊销仅使链接不可再被领取；已领取者的 grant 不随之撤销（撤销走 KB 共享管理的撤销接口）。
    """
    link = await db.get(KbShareLink, link_id)
    if link is None:
        raise CrossTenantError()
    # owner 校验：经库归属再校验一次（防止越权吊销他人链接）
    await _load_owned_kb(db, identity, link.kb_id)
    link.is_active = False
    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_SHARE_LINK_REVOKE,
        target_type="kb", target_id=link.kb_id,
        detail={"link_id": link.id}, request=request,
    )
    await db.commit()


# ============================================================
# 领取（任意已认证用户）
# ============================================================


@router.get("/{token}/info", response_model=ShareLinkInfo)
async def get_share_link_info(
    token: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """领取页展示：校验链接有效性，返回库名/分享者/是否可领取。

    需登录（领取者即被授权主体）。注意：此处对 KnowledgeBase / Tenant / User 的查询不会被
    租户兜底过滤误伤——KbShareLink 本身不受隔离，KnowledgeBase 经 db.get 主键读取，
    但兜底过滤仅作用于 ORM SELECT 且当前身份可能跨租户，故统一用 db.get（按主键，
    仍受 loader criteria 影响）。为稳妥，这里显式不依赖兜底，按主键读后自行判定。
    """
    link = await _load_valid_link(db, token)
    # 用主键读 KB 元数据用于展示；跨租户读取（领取场景）属正常，绕过兜底用原始查询。
    kb = await _get_kb_bypass_scope(db, link.kb_id)
    if kb is None:
        raise CrossTenantError("分享链接无效或已失效")

    owner_username = None
    owner_avatar = None
    if link.owner_user_id:
        u = await db.get(User, link.owner_user_id)
        owner_username = u.username if u else None
        owner_avatar = u.avatar if u else None
    owner_tenant_name = None
    if link.owner_tenant_id:
        t = await db.get(Tenant, link.owner_tenant_id)
        owner_tenant_name = t.name if t else None

    can_accept, reason = _accept_eligibility(identity, link, kb)
    return ShareLinkInfo(
        kb_name=kb.name, owner_username=owner_username, owner_avatar=owner_avatar,
        owner_tenant_name=owner_tenant_name, permission=link.permission,
        valid=True, can_accept=can_accept, reason=reason,
    )


@router.post("/{token}/accept")
async def accept_share_link(
    token: str,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """领取分享：为当前登录用户 upsert 一条指向该 KB 的 read user-grant。"""
    link = await _load_valid_link(db, token)
    kb = await _get_kb_bypass_scope(db, link.kb_id)
    if kb is None:
        raise CrossTenantError("分享链接无效或已失效")

    can_accept, reason = _accept_eligibility(identity, link, kb)
    if not can_accept:
        raise PermissionDeniedError(reason or "无法领取该分享")

    subject_id = identity.acting_subject_id
    # upsert grant（与租户内分享共用同一套授权数据；唯一约束 kb_id+grantee_type+grantee_id）
    existing = (await db.execute(
        select(KnowledgeBaseGrant).where(
            KnowledgeBaseGrant.kb_id == kb.id,
            KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value,
            KnowledgeBaseGrant.grantee_id == subject_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        # 已有授权（含同租户既有共享）：不降级既有权限，仅保持/确保至少 read。
        if existing.permission not in (
            GrantPermissionEnum.READ.value, GrantPermissionEnum.WRITE.value
        ):
            existing.permission = GrantPermissionEnum.READ.value
    else:
        db.add(KnowledgeBaseGrant(
            id=str(uuid.uuid4()), kb_id=kb.id,
            grantee_type=GranteeTypeEnum.USER.value, grantee_id=subject_id or "",
            permission=link.permission,  # read
            granted_by=link.owner_user_id,
        ))

    link.used_count = link.used_count + 1
    if link.max_uses is not None and link.used_count >= link.max_uses:
        link.is_active = False

    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_SHARE_LINK_ACCEPT,
        target_type="kb", target_id=kb.id, target_name=kb.name,
        detail={"link_id": link.id, "grantee_id": subject_id,
                "permission": link.permission},
        request=request,
    )
    await db.commit()
    return {"detail": "已领取分享", "kb_id": kb.id, "permission": link.permission}


# ============================================================
# 内部辅助
# ============================================================


async def _get_kb_bypass_scope(db: AsyncSession, kb_id: str) -> KnowledgeBase | None:
    """按主键读取 KB，绕过租户兜底过滤（领取场景为合法跨租户读元数据）。

    领取者尚未持有该 KB 的 grant，故仓储兜底（按类过滤）此刻不会放行该 KB；为读取
    展示元数据与校验，使用 execution_options(skip_tenant_filter) 等价手段——这里直接用
    Core 级查询并显式按主键过滤，规避 ORM loader criteria。
    """
    from sqlalchemy import select as _select

    row = await db.execute(
        _select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        .execution_options(skip_tenant_filter=True)
    )
    return row.scalar_one_or_none()


def _accept_eligibility(
    identity: IdentityContext, link: KbShareLink, kb: KnowledgeBase
) -> tuple[bool, str | None]:
    """领取资格判定（纯逻辑）。

    不可领取的情形：
    - 未携带自然人主体（机器级 Key）；
    - 领取者即 KB owner（分享给自己无意义）；
    - 领取者与 KB 同租户（同租户走既有分享，不应走跨租户链接）。
    """
    subject_id = identity.acting_subject_id
    if subject_id is None:
        return False, "请以用户身份登录后领取"
    if kb.owner_user_id is not None and kb.owner_user_id == subject_id:
        return False, "不能领取自己分享的知识库"
    if kb.tenant_id == identity.tenant_id:
        return False, "同团队成员请通过站内共享获取该知识库"
    return True, None
