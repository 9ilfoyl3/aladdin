"""知识库 CRUD 接口（kb-sharing-refinement：固定角色 + 归属 + owner 实体专属）。

权限模型为 WeKnora 式「固定角色（admin/member）+ 归属轴」，本次细化：
- create 用 ``require_member()`` 盖 ``owner_user_id``，默认 visibility=private；
- list/get 读、文档/文件夹内容读写经 ``kb_authorization_decision``（admin 只读他人库、
  组织读写档成员可写）；
- **库实体操作**（改名/改配置/删库/改可见性/共享/撤销共享）一律 **owner 专属**
  （``_ensure_kb_owner``），admin/super_admin 也不可代改他人库实体；
- 共享为 user 多选；新增「查看已共享用户」端点；可见性带开放维度 org_permission。
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_authenticated, require_member
from app.api.errors import CrossTenantError, PermissionDeniedError, ValidationInputError
from app.auth.audit import add_audit
from app.auth.constants import (
    AuditActionEnum,
    GranteeTypeEnum,
    GrantPermissionEnum,
    KbVisibilityEnum,
    OrgPermissionEnum,
)
from app.auth.identity import IdentityContext
from app.auth.kb_authz import GrantView, KbAccessEnum, kb_authorization_decision
from app.auth.kb_scope import assemble_allowed_kb_ids
from app.auth.validators import validate_org_permission
from app.schema.api import PageResult
from app.schema.db import Document, KnowledgeBase, KnowledgeBaseGrant, Tenant, User
from app.storage.milvus import MilvusClient, get_milvus_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["KnowledgeBase"])


# ============================================================
# 请求/响应模型
# ============================================================


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, description="描述")
    config: dict | None = Field(default=None, description="检索参数配置")
    # 创建时可选指定可见性（private | organization，默认 private）
    visibility: str | None = Field(default=None, description="private | organization（默认 private）")
    # organization 时可选开放维度（read|write，默认 read）；private 时忽略
    org_permission: str | None = Field(default=None, description="read | write（仅 organization 有效）")


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
    # 组织公共库开放维度（read|write）；private 时无意义但仍透出当前存值。
    org_permission: str | None = None
    # 展示用：库 owner 用户名（共享给我的库用于显示「谁分享的」）
    owner_username: str | None = None
    # 展示用：库所属租户名（组织公共库用于显示来自哪个租户/组织）
    tenant_name: str | None = None
    # 展示用：该库点对点共享给的用户数（私有库显示「分享给 N 人」）
    share_count: int | None = None
    # 当前请求者对该库是否有内容写权限（owner/组织读写/write 共享）。
    # 供前端在文档页显隐上传/新建/删除等写操作入口；真正鉴权仍在后端守卫。
    can_write: bool | None = None


class ShareRequest(BaseModel):
    user_ids: list[str] = Field(..., min_length=1, description="被分享用户列表（同租户）")
    permission: str = Field(..., description="read | write")


class ShareItem(BaseModel):
    """已共享用户条目（供 owner 查看/管理共享）。"""
    user_id: str
    username: str
    avatar: str | None = None
    permission: str


class VisibilityRequest(BaseModel):
    visibility: str = Field(..., description="private | organization")
    # organization 时可选开放维度（read|write，默认 read）；private 时忽略。
    org_permission: str | None = Field(default=None, description="read | write（仅 organization 有效）")


# ============================================================
# 辅助
# ============================================================


def _get_milvus() -> MilvusClient:
    return get_milvus_client()


def _to_resp(
    kb: KnowledgeBase,
    doc_count: int | None = None,
    *,
    owner_username: str | None = None,
    tenant_name: str | None = None,
    share_count: int | None = None,
    can_write: bool | None = None,
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=kb.id, name=kb.name, description=kb.description, config=kb.config,
        doc_count=doc_count if doc_count is not None else (kb.doc_count or 0),
        created_at=kb.created_at, updated_at=kb.updated_at,
        visibility=kb.visibility, owner_user_id=kb.owner_user_id,
        org_permission=kb.org_permission,
        owner_username=owner_username, tenant_name=tenant_name,
        share_count=share_count, can_write=can_write,
    )


def _ensure_kb_owner(identity: IdentityContext, kb: KnowledgeBase) -> None:
    """库**实体操作**（改名/改配置/删库/改可见性/共享/撤销共享）owner 专属闸门。

    与内容读写（kb_authorization_decision）分离：实体操作只有创建人能做，
    admin/super_admin 也不放行他人库的实体操作（治理走「转移知识库归属」）。
    跨租户/库不存在 -> 404（不泄露存在性）；同租户非 owner -> 403。
    """
    if kb.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    if kb.owner_user_id is None or kb.owner_user_id != identity.acting_subject_id:
        raise PermissionDeniedError("仅知识库创建人可执行该操作")


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
        kb_visibility=kb.visibility, kb_org_permission=kb.org_permission,
        access=access, grants=grants,
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
    identity: IdentityContext = Depends(require_authenticated()),
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

    # 批量补充展示信息：owner 用户名（共享库显示「谁分享的」）+ 租户名（公共库显示来源组织）
    owner_ids = {kb.owner_user_id for kb in kbs if kb.owner_user_id}
    owner_name_map: dict[str, str] = {}
    if owner_ids:
        ur = await db.execute(select(User.id, User.username).where(User.id.in_(owner_ids)))
        owner_name_map = {uid: uname for uid, uname in ur.all()}
    tenant_ids = {kb.tenant_id for kb in kbs if kb.tenant_id}
    tenant_name_map: dict[str, str] = {}
    if tenant_ids:
        tr = await db.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids)))
        tenant_name_map = {tid: tname for tid, tname in tr.all()}

    # 仅为「我的」库统计点对点共享人数（私有库显示「分享给 N 人」）；他人库不暴露其共享名单
    subject = identity.acting_subject_id
    own_kb_ids = [kb.id for kb in kbs if subject is not None and kb.owner_user_id == subject]
    share_count_map: dict[str, int] = {}
    if own_kb_ids:
        sc = await db.execute(
            select(KnowledgeBaseGrant.kb_id, func.count(KnowledgeBaseGrant.id))
            .where(
                KnowledgeBaseGrant.kb_id.in_(own_kb_ids),
                KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value,
            )
            .group_by(KnowledgeBaseGrant.kb_id)
        )
        share_count_map = {kid: cnt for kid, cnt in sc.all()}

    items = [
        _to_resp(
            kb, count_map.get(kb.id, 0),
            owner_username=owner_name_map.get(kb.owner_user_id) if kb.owner_user_id else None,
            tenant_name=tenant_name_map.get(kb.tenant_id) if kb.tenant_id else None,
            share_count=(
                share_count_map.get(kb.id, 0)
                if subject is not None and kb.owner_user_id == subject
                else None
            ),
        )
        for kb in kbs
    ]
    return PageResult[KnowledgeBaseResponse](
        items=items, total=total, page=page, page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """创建知识库：盖章 tenant_id + owner_user_id，可选指定可见性（默认 private）。"""
    if identity.tenant_id is None:
        raise PermissionDeniedError("请在具体租户上下文内创建知识库")
    # 可见性：缺省 private；指定时校验。organization 落开放维度（缺省 read）。
    visibility = body.visibility or KbVisibilityEnum.PRIVATE.value
    if visibility not in (KbVisibilityEnum.PRIVATE.value, KbVisibilityEnum.ORGANIZATION.value):
        raise ValidationInputError("visibility 仅支持 private | organization")
    org_permission = OrgPermissionEnum.READ.value
    if visibility == KbVisibilityEnum.ORGANIZATION.value and body.org_permission is not None:
        org_permission = validate_org_permission(body.org_permission)
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        config=body.config,
        doc_count=0,
        tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id,
        visibility=visibility,
        org_permission=org_permission,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    await db.commit()
    return _to_resp(kb, 0)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    await _authorize_kb(db, identity, kb, KbAccessEnum.READ)
    # 计算写权限，供前端显隐写操作入口（owner/组织读写/write 共享 -> True）
    grants = await _load_grants(db, kb.id)
    write_decision = kb_authorization_decision(
        identity,
        kb_id=kb.id, kb_tenant_id=kb.tenant_id, kb_owner_user_id=kb.owner_user_id,
        kb_visibility=kb.visibility, kb_org_permission=kb.org_permission,
        access=KbAccessEnum.WRITE, grants=grants,
    )
    return _to_resp(kb, can_write=write_decision.allow)


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 改名/改配置属实体操作：owner 专属
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(kb, field, value)
    kb.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(kb)
    return _to_resp(kb)


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """删除知识库（需写权限）。批量 SQL 删除后台清理 Milvus/文件。"""
    from sqlalchemy import delete as sql_delete, update as sql_update

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 删库属实体操作：owner 专属

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


@router.put("/{kb_id}/share")
async def share_knowledge_base(
    kb_id: str,
    body: ShareRequest,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """共享知识库给同租户用户（user 多选）：仅 owner 可发起。

    逐用户校验同租户（跨租户用户 404），为每个用户 upsert 一条 user-grant；
    permission 仅支持 read | write。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 共享属实体操作：owner 专属（admin 不可代管他人库共享）
    if body.permission not in (GrantPermissionEnum.READ.value, GrantPermissionEnum.WRITE.value):
        raise ValidationInputError("permission 仅支持 read | write")

    for uid in body.user_ids:
        # 被分享用户须属于同租户（防跨租户共享）
        u = await db.get(User, uid)
        if u is None or u.tenant_id != kb.tenant_id:
            raise CrossTenantError()
        # upsert（同 kb + user 唯一）
        existing = (await db.execute(
            select(KnowledgeBaseGrant).where(
                KnowledgeBaseGrant.kb_id == kb_id,
                KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value,
                KnowledgeBaseGrant.grantee_id == uid,
            )
        )).scalar_one_or_none()
        if existing is not None:
            existing.permission = body.permission  # 调整即时生效
        else:
            db.add(KnowledgeBaseGrant(
                id=str(uuid.uuid4()), kb_id=kb_id,
                grantee_type=GranteeTypeEnum.USER.value, grantee_id=uid,
                permission=body.permission, granted_by=identity.acting_subject_id or "",
            ))
    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_SHARE,
        target_type="kb", target_id=kb_id, target_name=kb.name,
        detail={"user_ids": body.user_ids, "permission": body.permission}, request=request,
    )
    await db.commit()
    return {"detail": "已共享", "kb_id": kb_id,
            "user_ids": body.user_ids, "permission": body.permission}


@router.get("/{kb_id}/shares", response_model=list[ShareItem])
async def list_kb_shares(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """列出某库已共享的用户（仅 owner）：供共享管理界面查看与按人撤销。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 查看共享名单属实体管理：owner 专属
    rows = await db.execute(
        select(
            KnowledgeBaseGrant.grantee_id,
            KnowledgeBaseGrant.permission,
            User.username,
            User.avatar,
        )
        .join(User, User.id == KnowledgeBaseGrant.grantee_id)
        .where(
            KnowledgeBaseGrant.kb_id == kb_id,
            KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value,
        )
        .order_by(User.username)
    )
    return [
        ShareItem(user_id=gid, username=uname, avatar=avatar, permission=perm)
        for gid, perm, uname, avatar in rows.all()
    ]


@router.delete("/{kb_id}/share/user/{user_id}", status_code=204)
async def revoke_share(
    kb_id: str, user_id: str,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """撤销对某用户的共享（仅 owner）。"""
    from sqlalchemy import delete as sql_delete

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 撤销共享属实体操作：owner 专属
    await db.execute(
        sql_delete(KnowledgeBaseGrant).where(
            KnowledgeBaseGrant.kb_id == kb_id,
            KnowledgeBaseGrant.grantee_type == GranteeTypeEnum.USER.value,
            KnowledgeBaseGrant.grantee_id == user_id,
        )
    )
    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_REVOKE_SHARE,
        target_type="kb", target_id=kb_id, target_name=kb.name,
        detail={"user_id": user_id}, request=request,
    )
    await db.commit()


@router.put("/{kb_id}/visibility", response_model=KnowledgeBaseResponse)
async def set_visibility(
    kb_id: str,
    body: VisibilityRequest,
    request: Request,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """可见性调整 + 开放维度（owner 专属实体操作）。

    organization 时可同时指定 org_permission（read|write，默认 read）；private 时忽略。
    仅 owner 可改（admin/super_admin 不代改他人库可见性）。
    """
    if body.visibility not in (KbVisibilityEnum.PRIVATE.value, KbVisibilityEnum.ORGANIZATION.value):
        raise ValidationInputError("visibility 仅支持 private | organization")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    _ensure_kb_owner(identity, kb)  # 改可见性属实体操作：owner 专属

    kb.visibility = body.visibility  # 仅改可见性，owner/tenant 不变
    if body.visibility == KbVisibilityEnum.ORGANIZATION.value:
        # organization 落开放维度（缺省 read，经校验）
        kb.org_permission = (
            validate_org_permission(body.org_permission)
            if body.org_permission is not None
            else OrgPermissionEnum.READ.value
        )
    kb.updated_at = datetime.utcnow()
    add_audit(
        db, actor=identity, action=AuditActionEnum.KB_SET_VISIBILITY,
        target_type="kb", target_id=kb.id, target_name=kb.name,
        detail={"visibility": body.visibility, "org_permission": kb.org_permission}, request=request,
    )
    await db.commit()
    await db.refresh(kb)
    return _to_resp(kb)


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
