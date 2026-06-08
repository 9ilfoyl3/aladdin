"""知识库 CRUD 接口（kb-sharing-refinement：固定角色 + 归属 + owner 实体专属）。

权限模型为「固定角色（admin/member）+ 归属轴」，本次细化：
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
from app.retrieval.config import (
    RETRIEVAL_FIELD_SPECS,
    get_platform_config_store,
    get_retrieval_config_store,
)
from app.schema.api import PageResult
from app.schema.db import Document, KnowledgeBase, KnowledgeBaseGrant, Tenant, User
from app.storage.milvus import MilvusClient, get_milvus_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["KnowledgeBase"])

# 单 MB 文件保守估算的 child chunk 密度（chunk/MB），仅用于「约可容纳 N 份文档」的辅助翻译展示。
# 真实入库判定始终以精确 child chunk 数（KB_Chunk_Cap，Req 4）为准，而非该近似文件数（Req 7.5）。
CHUNK_DENSITY = 300


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


class KBCapacityVO(BaseModel):
    """知识库容量进度条（用户侧可视化，Req 7）。

    真实度量单位为 child chunk：分母 = 平台 Effective KB_Chunk_Cap，分子 = 该库当前精确
    child chunk 数（聚合 ``Document.chunk_count``）。文件数（``approx_*_files``）仅作辅助
    翻译，标「约」，真实入库判定以精确 chunk 数为准（Req 7.5）。
    """

    # 该库当前精确 child chunk 数（聚合 Document.chunk_count）
    used_chunks: int = Field(..., description="该库当前精确 child chunk 数")
    # 总容量 = 平台 Effective KB_Chunk_Cap（Req 7.6）
    total_chunks: int = Field(..., description="总容量（平台 KB_Chunk_Cap）")
    # 已用百分比 = used / total，封顶 1.0（Req 7.3）
    percent: float = Field(..., description="已用百分比（0~1，封顶 1.0）")
    # 约可容纳文档数 = total_chunks // 单文件估算 chunk，向下取整（Req 7.4，标「约」）
    approx_total_files: int = Field(..., description="约可容纳文档数（向下取整，近似）")
    # 已传文档数（精确，Document 计数）
    approx_used_files: int = Field(..., description="已传文档数（精确）")
    # 约还可上传文档数 = 剩余 chunk // 单文件估算 chunk，向下取整（用户侧最直观的「还能传多少」）
    approx_remaining_files: int = Field(..., description="约还可上传文档数（向下取整，近似）")


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
    # 容量进度条（Req 7）。列表/详情按需填充；未计算时为 None（向后兼容）。
    capacity: KBCapacityVO | None = None


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


def _compute_capacity(
    used_chunks: int,
    used_files: int,
    kb_chunk_cap: int,
    upload_max_file_mb: int,
) -> KBCapacityVO:
    """纯函数：依据精确已用 chunk / 已传文档数 + 平台上限 + 租户单文件上限计算容量进度条。

    - ``total_chunks`` = 平台 Effective KB_Chunk_Cap（分母，Req 7.2/7.6）。
    - ``percent`` = used / total，封顶 1.0；total<=0 时记 0（防除零，Req 7.3）。
    - ``approx_total_files`` = total_chunks // (upload_max_file_mb × CHUNK_DENSITY)，向下取整
      （单文件估算 chunk = 租户上传上限 × 保守密度，Req 7.4）；估算分母<=0 时记 0。
    - ``approx_used_files`` = 已传文档数（精确）。
    - ``approx_remaining_files`` = 剩余 chunk // 单文件估算 chunk，向下取整（用户最关心的「还能传多少」）。

    入参均按非负兜底处理，保证输出稳定（不抛错）。
    """
    used = max(0, used_chunks)
    total = max(0, kb_chunk_cap)
    used_docs = max(0, used_files)

    percent = min(1.0, used / total) if total > 0 else 0.0

    per_file_chunks = max(0, upload_max_file_mb) * CHUNK_DENSITY
    approx_total_files = total // per_file_chunks if per_file_chunks > 0 else 0
    # 剩余 chunk（不为负）→ 约还可上传份数（向下取整，保守）
    remaining_chunks = max(0, total - used)
    approx_remaining_files = remaining_chunks // per_file_chunks if per_file_chunks > 0 else 0

    return KBCapacityVO(
        used_chunks=used,
        total_chunks=total,
        percent=percent,
        approx_total_files=approx_total_files,
        approx_used_files=used_docs,
        approx_remaining_files=approx_remaining_files,
    )


async def _build_capacity(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    used_files: int | None = None,
) -> KBCapacityVO:
    """读平台 KB_Chunk_Cap + 该库租户的 Upload_File_Size_Limit，聚合该库精确 chunk/文档数，
    组装 ``KBCapacityVO``。

    - 已用 chunk = 聚合该库 ``Document.chunk_count`` 之和（精确，Req 7.2）。
    - 已传文档数：若调用方已批量算出则复用，否则单独 count（Req 7.4 的 approx_used_files）。
    - 配置读失败由 Store 自身降级安全默认（不抛错）。
    """
    platform_cfg = await get_platform_config_store().get_effective()
    retrieval_cfg = await get_retrieval_config_store().get_effective(kb.tenant_id)

    used_chunks = (
        await db.execute(
            select(func.coalesce(func.sum(Document.chunk_count), 0)).where(Document.kb_id == kb.id)
        )
    ).scalar_one()

    if used_files is None:
        used_files = (
            await db.execute(
                select(func.count(Document.id)).where(Document.kb_id == kb.id)
            )
        ).scalar_one()

    return _compute_capacity(
        used_chunks=int(used_chunks or 0),
        used_files=int(used_files or 0),
        kb_chunk_cap=platform_cfg.kb_chunk_cap,
        upload_max_file_mb=retrieval_cfg.upload_max_file_mb,
    )


def _to_resp(
    kb: KnowledgeBase,
    doc_count: int | None = None,
    *,
    owner_username: str | None = None,
    tenant_name: str | None = None,
    share_count: int | None = None,
    can_write: bool | None = None,
    capacity: KBCapacityVO | None = None,
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=kb.id, name=kb.name, description=kb.description, config=kb.config,
        doc_count=doc_count if doc_count is not None else (kb.doc_count or 0),
        created_at=kb.created_at, updated_at=kb.updated_at,
        visibility=kb.visibility, owner_user_id=kb.owner_user_id,
        org_permission=kb.org_permission,
        owner_username=owner_username, tenant_name=tenant_name,
        share_count=share_count, can_write=can_write,
        capacity=capacity,
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


def _kb_relation(
    kb: KnowledgeBase, subject_id: str | None, is_admin: bool
) -> str:
    """计算当前身份与某 KB 的关系（用于筛选/排序展现优先级）。

    与前端 relationBadge 的判定优先级保持一致：
    mine（我的）> org（组织公共）> others（他人私有·管理员只读）/ shared（共享给我）。
    """
    if subject_id is not None and kb.owner_user_id == subject_id:
        return "mine"
    if kb.visibility == KbVisibilityEnum.ORGANIZATION.value:
        return "org"
    if is_admin:
        return "others"
    return "shared"


# 默认「推荐」排序的关系优先级：我的 > 共享给我 > 组织公共 > 他人私有
_RELATION_RANK = {"mine": 0, "shared": 1, "org": 2, "others": 3}
_VALID_RELATIONS = {"mine", "shared", "org", "others"}
_VALID_SORTS = {"recommended", "updated", "created", "name", "docs"}


@router.get("", response_model=PageResult[KnowledgeBaseResponse])
async def list_knowledge_bases(
    page: int = 1,
    page_size: int = 20,
    relation: str | None = None,
    sort: str = "recommended",
    q: str | None = None,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """列出当前身份可读范围内的知识库（自有私有库 ∪ 同租户公共库 ∪ 被共享库）。

    支持按关系筛选（relation）、排序（sort）与名称搜索（q）。可见范围本就全量装配进内存，
    在其上做筛选/排序/分页可保证跨页结果正确（纯前端排序只能作用于已加载页，故收口到后端）。
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    sort = sort if sort in _VALID_SORTS else "recommended"
    relation_filter = relation if relation in _VALID_RELATIONS else None

    allowed_ids = await assemble_allowed_kb_ids(db, identity)
    if not allowed_ids:
        return PageResult[KnowledgeBaseResponse](items=[], total=0, page=page, page_size=page_size, has_more=False)

    # 全量装载可见范围内的 KB 实体：关系判定 + 排序 + 名称搜索都需要逐行信息
    rows = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id.in_(list(allowed_ids)))
    )
    all_kbs = list(rows.scalars().all())

    subject = identity.acting_subject_id
    is_admin = identity.is_tenant_admin or identity.is_super_admin

    # 名称模糊搜索
    if q and q.strip():
        kw = q.strip().lower()
        all_kbs = [kb for kb in all_kbs if kw in (kb.name or "").lower()]

    # 关系筛选
    if relation_filter is not None:
        all_kbs = [kb for kb in all_kbs if _kb_relation(kb, subject, is_admin) == relation_filter]

    # 文档数：一次分组查询覆盖全部已筛选 KB，供排序与展示复用（跨页排序需全量计数）
    filtered_ids = [kb.id for kb in all_kbs]
    count_map: dict[str, int] = {}
    if filtered_ids:
        cr = await db.execute(
            select(Document.kb_id, func.count(Document.id))
            .where(Document.kb_id.in_(filtered_ids)).group_by(Document.kb_id)
        )
        count_map = {row[0]: row[1] for row in cr.all()}

    # 排序
    if sort == "name":
        all_kbs.sort(key=lambda kb: (kb.name or "").lower())
    elif sort == "created":
        all_kbs.sort(key=lambda kb: kb.created_at, reverse=True)
    elif sort == "updated":
        all_kbs.sort(key=lambda kb: kb.updated_at or kb.created_at, reverse=True)
    elif sort == "docs":
        all_kbs.sort(key=lambda kb: count_map.get(kb.id, 0), reverse=True)
    else:  # recommended：关系优先级 → 最近更新
        all_kbs.sort(
            key=lambda kb: (
                _RELATION_RANK.get(_kb_relation(kb, subject, is_admin), 99),
                -(kb.updated_at or kb.created_at).timestamp(),
            )
        )

    total = len(all_kbs)
    kbs = all_kbs[offset:offset + page_size]

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

    # 容量进度条（Req 7）：聚合本页各库精确 child chunk 数（Document.chunk_count 之和）；
    # 平台 KB_Chunk_Cap 全局只读一次，租户级 Upload_File_Size_Limit 按页内租户去重读取。
    page_kb_ids = [kb.id for kb in kbs]
    used_chunk_map: dict[str, int] = {}
    if page_kb_ids:
        ucr = await db.execute(
            select(Document.kb_id, func.coalesce(func.sum(Document.chunk_count), 0))
            .where(Document.kb_id.in_(page_kb_ids)).group_by(Document.kb_id)
        )
        used_chunk_map = {row[0]: int(row[1] or 0) for row in ucr.all()}
    platform_cfg = await get_platform_config_store().get_effective()
    upload_mb_by_tenant: dict[str | None, int] = {}
    for tid in {kb.tenant_id for kb in kbs}:
        cfg = await get_retrieval_config_store().get_effective(tid)
        upload_mb_by_tenant[tid] = cfg.upload_max_file_mb

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
            capacity=_compute_capacity(
                used_chunks=used_chunk_map.get(kb.id, 0),
                used_files=count_map.get(kb.id, 0),
                kb_chunk_cap=platform_cfg.kb_chunk_cap,
                upload_max_file_mb=upload_mb_by_tenant.get(
                    kb.tenant_id, RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default
                ),
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
    return _to_resp(kb, can_write=write_decision.allow, capacity=await _build_capacity(db, kb))


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
