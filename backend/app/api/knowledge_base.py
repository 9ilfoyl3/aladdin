"""知识库 CRUD 接口（tenant-auth：Guard + 盖章 + 授权判定 + 共享/可见性）。"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authorization_guard, get_db_session
from app.api.errors import CrossTenantError, InvalidGranteeTypeError, PermissionDeniedError
from app.auth.constants import (
    GRANTEE_TYPES_ENABLED,
    GrantPermissionEnum,
    KbVisibilityEnum,
    PermissionEnum,
)
from app.auth.identity import IdentityContext
from app.auth.kb_authz import GrantView, KbAccessEnum, kb_authorization_decision
from app.auth.kb_scope import assemble_allowed_kb_ids
from app.config import get_settings
from app.schema.api import PageResult
from app.schema.db import Document, KnowledgeBase, KnowledgeBaseGrant
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["KnowledgeBase"])


# ============================================================
# 请求/响应模型
# ============================================================


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, description="描述")
    config: dict | None = Field(default=None, description="检索参数配置")


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    config: dict | None = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应。tenant-auth 追加可选 visibility/owner_user_id，不删除既有字段。"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    description: str | None
    config: dict | None
    doc_count: int
    created_at: datetime
    updated_at: datetime
    # 追加字段（向后兼容）
    visibility: str | None = None
    owner_user_id: str | None = None


class ShareRequest(BaseModel):
    grantee_type: str = Field(..., description="user | role（v1 仅此两种）")
    grantee_id: str = Field(..., min_length=1)
    permission: str = Field(..., description="read | write")


class VisibilityRequest(BaseModel):
    visibility: str = Field(..., description="private | organization")


# ============================================================
# 辅助
# ============================================================


def _get_milvus() -> MilvusClient:
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


def _to_resp(kb: KnowledgeBase, doc_count: int | None = None) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=kb.id, name=kb.name, description=kb.description, config=kb.config,
        doc_count=doc_count if doc_count is not None else (kb.doc_count or 0),
        created_at=kb.created_at, updated_at=kb.updated_at,
        visibility=kb.visibility, owner_user_id=kb.owner_user_id,
    )


async def _load_grants(db: AsyncSession, kb_id: str) -> list[GrantView]:
    rows = await db.execute(
        select(
            KnowledgeBaseGrant.grantee_type,
            KnowledgeBaseGrant.grantee_id,
            KnowledgeBaseGrant.permission,
        ).where(KnowledgeBaseGrant.kb_id == kb_id)
    )
    return [GrantView(gt, gid, perm) for gt, gid, perm in rows.all()]


async def _authorize_kb(
    db: AsyncSession, identity: IdentityContext, kb: KnowledgeBase, access: KbAccessEnum
) -> None:
    """对已加载的 KB 做唯一授权判定；拒绝则按 http_status 抛 404/403。"""
    grants = await _load_grants(db, kb.id)
    decision = kb_authorization_decision(
        identity,
        kb_id=kb.id, kb_tenant_id=kb.tenant_id, kb_owner_user_id=kb.owner_user_id,
        kb_visibility=kb.visibility, access=access, grants=grants,
    )
    if not decision.allow:
        if decision.http_status == 403:
            raise PermissionDeniedError()
        raise CrossTenantError()


# ============================================================
# 接口实现
# ============================================================


@router.get("", response_model=PageResult[KnowledgeBaseResponse])
async def list_knowledge_bases(
    page: int = 1,
    page_size: int = 20,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    """列出当前身份可读范围内的知识库（自有私有库 ∪ 同租户公共库 ∪ 被共享库）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    allowed_ids = await assemble_allowed_kb_ids(db, identity)
    if not allowed_ids:
        return PageResult[KnowledgeBaseResponse](items=[], total=0, page=page, page_size=page_size, has_more=False)

    allowed_list = list(allowed_ids)
    total = await db.scalar(
        select(func.count(KnowledgeBase.id)).where(KnowledgeBase.id.in_(allowed_list))
    ) or 0
    result = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id.in_(allowed_list))
        .order_by(KnowledgeBase.created_at.desc())
        .offset(offset).limit(page_size)
    )
    kbs = result.scalars().all()

    kb_ids = [kb.id for kb in kbs]
    count_map: dict[str, int] = {}
    if kb_ids:
        cr = await db.execute(
            select(Document.kb_id, func.count(Document.id))
            .where(Document.kb_id.in_(kb_ids)).group_by(Document.kb_id)
        )
        count_map = {row[0]: row[1] for row in cr.all()}

    items = [_to_resp(kb, count_map.get(kb.id, 0)) for kb in kbs]
    return PageResult[KnowledgeBaseResponse](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.KB_CREATE.value})
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """创建知识库：盖章 tenant_id + owner_user_id，默认 visibility=private。"""
    if identity.tenant_id is None:
        raise PermissionDeniedError("请在具体租户上下文内创建知识库")
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        config=body.config,
        doc_count=0,
        tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id,
        visibility=KbVisibilityEnum.PRIVATE.value,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    await db.commit()
    return _to_resp(kb, 0)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    await _authorize_kb(db, identity, kb, KbAccessEnum.READ)
    return _to_resp(kb)


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    await _authorize_kb(db, identity, kb, KbAccessEnum.WRITE)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(kb, field, value)
    kb.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(kb)
    return _to_resp(kb)


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: str,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    """删除知识库（需写权限）。批量 SQL 删除后台清理 Milvus/文件。"""
    from sqlalchemy import delete as sql_delete, update as sql_update

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    await _authorize_kb(db, identity, kb, KbAccessEnum.WRITE)

    doc_result = await db.execute(
        select(Document.id, Document.file_type).where(Document.kb_id == kb_id)
    )
    doc_info_list = [{"id": row[0], "file_type": row[1]} for row in doc_result.all()]

    await db.execute(
        sql_update(Document).where(Document.kb_id == kb_id)
        .where(Document.status.in_(("pending", "processing"))).values(status="cancelled")
    )
    from app.schema.db import Chunk, Folder
    await db.execute(sql_delete(KnowledgeBaseGrant).where(KnowledgeBaseGrant.kb_id == kb_id))
    await db.execute(sql_delete(Chunk).where(Chunk.kb_id == kb_id))
    await db.execute(sql_delete(Document).where(Document.kb_id == kb_id))
    await db.execute(sql_delete(Folder).where(Folder.kb_id == kb_id))
    await db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    await db.commit()

    import asyncio
    asyncio.create_task(_kb_cleanup_background(kb_id, doc_info_list))


@router.post("/{kb_id}/share", status_code=201)
async def share_knowledge_base(
    kb_id: str,
    body: ShareRequest,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.KB_SHARE.value})
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """点对点共享：仅 owner 可发起；grantee_type 仅 user/role（预留值 400）；grantee 须同租户。"""
    if body.grantee_type not in GRANTEE_TYPES_ENABLED:
        raise InvalidGranteeTypeError()
    if body.permission not in (GrantPermissionEnum.READ.value, GrantPermissionEnum.WRITE.value):
        raise InvalidGranteeTypeError("permission 仅支持 read | write")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    if kb.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    # 仅 owner 可共享自己的库
    if kb.owner_user_id != identity.acting_subject_id and not identity.is_super_admin:
        raise PermissionDeniedError("仅知识库所有者可共享")

    # grantee 须属于同租户（防跨租户共享）
    await _validate_grantee_same_tenant(db, body.grantee_type, body.grantee_id, identity.tenant_id)

    # upsert（同 kb+grantee 唯一）
    existing = (await db.execute(
        select(KnowledgeBaseGrant).where(
            KnowledgeBaseGrant.kb_id == kb_id,
            KnowledgeBaseGrant.grantee_type == body.grantee_type,
            KnowledgeBaseGrant.grantee_id == body.grantee_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        existing.permission = body.permission  # 调整即时生效
    else:
        db.add(KnowledgeBaseGrant(
            id=str(uuid.uuid4()), kb_id=kb_id,
            grantee_type=body.grantee_type, grantee_id=body.grantee_id,
            permission=body.permission, granted_by=identity.acting_subject_id or "",
        ))
    await db.commit()
    return {"detail": "已共享", "kb_id": kb_id, "grantee_type": body.grantee_type,
            "grantee_id": body.grantee_id, "permission": body.permission}


@router.delete("/{kb_id}/share/{grantee_type}/{grantee_id}", status_code=204)
async def revoke_share(
    kb_id: str, grantee_type: str, grantee_id: str,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.KB_SHARE.value})
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """撤销共享（仅 owner）。"""
    from sqlalchemy import delete as sql_delete

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    if kb.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    if kb.owner_user_id != identity.acting_subject_id and not identity.is_super_admin:
        raise PermissionDeniedError("仅知识库所有者可撤销共享")
    await db.execute(
        sql_delete(KnowledgeBaseGrant).where(
            KnowledgeBaseGrant.kb_id == kb_id,
            KnowledgeBaseGrant.grantee_type == grantee_type,
            KnowledgeBaseGrant.grantee_id == grantee_id,
        )
    )
    await db.commit()


@router.put("/{kb_id}/visibility", response_model=KnowledgeBaseResponse)
async def set_visibility(
    kb_id: str,
    body: VisibilityRequest,
    identity: IdentityContext = Depends(authorization_guard()),
    db: AsyncSession = Depends(get_db_session),
):
    """可见性提升/调整：owner 自助 或 具备 kb:manage_visibility 的管理员代为收编。

    先 tenant_guard（不一致 404），再判主体；提升仅改 visibility，owner/tenant 不变。
    """
    if body.visibility not in (KbVisibilityEnum.PRIVATE.value, KbVisibilityEnum.ORGANIZATION.value):
        raise InvalidGranteeTypeError("visibility 仅支持 private | organization")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    if kb.tenant_id != identity.tenant_id:
        raise CrossTenantError()

    is_owner = kb.owner_user_id == identity.acting_subject_id
    can_manage = identity.has_permission(PermissionEnum.KB_MANAGE_VISIBILITY.value) or identity.is_super_admin
    if not (is_owner or can_manage):
        raise PermissionDeniedError("无权变更知识库可见性")

    kb.visibility = body.visibility  # 仅改可见性，owner/tenant 不变
    kb.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(kb)
    return _to_resp(kb)


async def _validate_grantee_same_tenant(
    db: AsyncSession, grantee_type: str, grantee_id: str, tenant_id: str | None
) -> None:
    """校验被授予的 user/role 属于同一租户（不一致 404，防跨租户共享）。"""
    from app.schema.db import Role, User

    if grantee_type == "user":
        u = await db.get(User, grantee_id)
        if u is None or u.tenant_id != tenant_id:
            raise CrossTenantError()
    elif grantee_type == "role":
        r = await db.get(Role, grantee_id)
        if r is None or r.tenant_id != tenant_id:
            raise CrossTenantError()


async def _kb_cleanup_background(kb_id: str, doc_info_list: list[dict]) -> None:
    """后台清理 Milvus collection + 物理文件 + 缓存（按 kb_id，不写受隔离资源）。"""
    import os
    from pathlib import Path

    upload_dir = Path("data/uploads")
    try:
        milvus = _get_milvus()
        await milvus.drop_collection(kb_id)
    except Exception as e:
        logger.warning("知识库删除 - 删除 Milvus collection 失败（可忽略）: %s", e)

    for info in doc_info_list:
        file_path = upload_dir / f"{info['id']}.{info['file_type']}"
        if file_path.exists():
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning("知识库删除 - 删除文件失败 %s: %s", file_path, e)

    try:
        from app.retrieval.cache import get_retrieval_cache
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
    except Exception as e:
        logger.warning("知识库删除 - 清除缓存失败: %s", e)
