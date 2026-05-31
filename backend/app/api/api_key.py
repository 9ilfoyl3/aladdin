"""API Key 管理接口（tenant-auth：三模型 + scope 编辑 + 超管代理 Key）。

- 租户级 Key（apikey:manage，管理员）：机器凭据，记录 authorized_scope，仅返回一次明文
- 用户级 Key（apikey:self，普通用户为自己创建）：绑定本人，继承实时权限
- 超管级代理 Key（External_Agent，platform/Super_Admin）：签发/撤销仅 Super_Admin
所有端点 allow_api_key=False（API Key 管理属 Administrative/Platform 操作，禁 api_key 通道）。
列表/撤销按租户隔离；撤销为软删除。
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import generate_api_key, get_key_prefix, hash_key
from app.api.deps import authorization_guard, get_db_session
from app.api.errors import CrossTenantError, PermissionDeniedError
from app.auth.audit import add_audit
from app.auth.constants import (
    AuditActionEnum,
    EXTERNAL_USER_TENANT_ID,
    ApiKeyTypeEnum,
    KbVisibilityEnum,
    PermissionEnum,
)
from app.auth.identity import IdentityContext, OperationLevelEnum
from app.schema.db import ApiKey, KnowledgeBase

router = APIRouter(prefix="/api/api-keys", tags=["API Keys"])


# ============================================================
# 请求/响应模型
# ============================================================


class ApiKeyScope(BaseModel):
    """租户级 Key 授权范围"""
    all_public_kbs: bool = Field(default=False, description="动态规则：本租户全部公共库")
    explicit_kb_ids: list[str] = Field(default_factory=list, description="显式授权的知识库 ID")


class CreateTenantKeyRequest(BaseModel):
    """创建租户级 Key 请求"""
    name: Optional[str] = Field(default=None)
    scope: ApiKeyScope = Field(default_factory=ApiKeyScope)


class CreateUserKeyRequest(BaseModel):
    """创建用户级 Key 请求（绑定当前用户）"""
    name: Optional[str] = Field(default=None)


class CreateProxyKeyRequest(BaseModel):
    """创建超管级代理 Key 请求（仅 Super_Admin）"""
    name: Optional[str] = Field(default=None)


class UpdateScopeRequest(BaseModel):
    """编辑租户级 Key 授权范围（不重签）"""
    scope: ApiKeyScope


class CreateApiKeyResponse(BaseModel):
    """创建响应（含明文，仅此一次）"""
    id: str
    key: str = Field(description="完整 API Key，仅在创建时返回")
    prefix: str
    name: Optional[str] = None
    key_type: str
    created_at: datetime


class ApiKeyItem(BaseModel):
    """列表项（不含明文）"""
    id: str
    prefix: str
    name: Optional[str] = None
    key_type: str
    is_active: bool
    call_count: int
    last_used_at: Optional[datetime] = None
    created_at: datetime


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyItem]
    total: int


# ============================================================
# 辅助
# ============================================================


async def _validate_scope_same_tenant(
    db: AsyncSession, scope: ApiKeyScope, tenant_id: str
) -> None:
    """校验显式 KB 列表均属于该租户（跨租户 404）。"""
    for kb_id in scope.explicit_kb_ids:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None or kb.tenant_id != tenant_id:
            raise CrossTenantError()


# ============================================================
# 租户级 Key（apikey:manage）
# ============================================================


@router.post("", response_model=CreateApiKeyResponse)
async def create_tenant_key(
    request: Request,
    body: CreateTenantKeyRequest = CreateTenantKeyRequest(),
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """创建租户级 API Key（机器凭据），盖 tenant_id + 记录 scope，仅返回一次明文。"""
    if identity.tenant_id is None:
        raise PermissionDeniedError("请在具体租户上下文内创建 API Key")
    await _validate_scope_same_tenant(db, body.scope, identity.tenant_id)

    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    api_key = ApiKey(
        id=key_id,
        key_hash=hash_key(raw_key),
        prefix=get_key_prefix(raw_key),
        name=body.name,
        is_active=True,
        call_count=0,
        tenant_id=identity.tenant_id,
        key_type=ApiKeyTypeEnum.TENANT_LEVEL.value,
        authorized_scope={
            "all_public_kbs": body.scope.all_public_kbs,
            "explicit_kb_ids": body.scope.explicit_kb_ids,
        },
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)
    add_audit(
        db, actor=identity, action=AuditActionEnum.APIKEY_CREATE,
        target_type="api_key", target_id=key_id, target_name=body.name,
        detail={"key_type": "tenant_level", "prefix": api_key.prefix}, request=request,
    )
    await db.commit()
    return CreateApiKeyResponse(
        id=key_id, key=raw_key, prefix=api_key.prefix, name=api_key.name,
        key_type=api_key.key_type, created_at=api_key.created_at,
    )


@router.put("/{key_id}/scope")
async def update_key_scope(
    key_id: str,
    body: UpdateScopeRequest,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """编辑租户级 Key 的授权范围（校验 KB 同租户；不改明文/不重签；即时生效）。"""
    api_key = await db.get(ApiKey, key_id)
    if api_key is None or api_key.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    if api_key.key_type != ApiKeyTypeEnum.TENANT_LEVEL.value:
        raise PermissionDeniedError("仅租户级 Key 可编辑授权范围")
    await _validate_scope_same_tenant(db, body.scope, identity.tenant_id)
    api_key.authorized_scope = {
        "all_public_kbs": body.scope.all_public_kbs,
        "explicit_kb_ids": body.scope.explicit_kb_ids,
    }
    add_audit(
        db, actor=identity, action=AuditActionEnum.APIKEY_UPDATE_SCOPE,
        target_type="api_key", target_id=key_id, target_name=api_key.name,
        detail={"all_public_kbs": body.scope.all_public_kbs,
                "explicit_kb_ids": body.scope.explicit_kb_ids}, request=request,
    )
    await db.commit()
    return {"detail": "授权范围已更新", "id": key_id}


# ============================================================
# 用户级 Key（apikey:self，普通用户为自己创建）
# ============================================================


@router.post("/me", response_model=CreateApiKeyResponse)
async def create_user_key(
    request: CreateUserKeyRequest = CreateUserKeyRequest(),
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_SELF.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """普通用户为自己创建用户级 Key（绑定本人，继承实时权限）。"""
    if identity.user_id is None or identity.tenant_id is None:
        raise PermissionDeniedError("仅注册用户可创建用户级 Key")
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    api_key = ApiKey(
        id=key_id,
        key_hash=hash_key(raw_key),
        prefix=get_key_prefix(raw_key),
        name=request.name,
        is_active=True,
        call_count=0,
        tenant_id=identity.tenant_id,
        key_type=ApiKeyTypeEnum.USER_LEVEL.value,
        bound_user_id=identity.user_id,
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)
    await db.commit()
    return CreateApiKeyResponse(
        id=key_id, key=raw_key, prefix=api_key.prefix, name=api_key.name,
        key_type=api_key.key_type, created_at=api_key.created_at,
    )


# ============================================================
# 超管级代理 Key（External_Agent，仅 Super_Admin/platform）
# ============================================================


@router.post("/external-agent", response_model=CreateApiKeyResponse)
async def create_proxy_key(
    request: Request,
    body: CreateProxyKeyRequest = CreateProxyKeyRequest(),
    identity: IdentityContext = Depends(
        authorization_guard(op_level=OperationLevelEnum.PLATFORM, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """签发超管级代理 Key（仅 Super_Admin）。tenant 锁定 External_User_Tenant。"""
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    api_key = ApiKey(
        id=key_id,
        key_hash=hash_key(raw_key),
        prefix=get_key_prefix(raw_key),
        name=body.name,
        is_active=True,
        call_count=0,
        tenant_id=EXTERNAL_USER_TENANT_ID,
        key_type=ApiKeyTypeEnum.EXTERNAL_AGENT.value,
        key_source=key_id,  # 命名空间前缀 = 自身 id
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)
    add_audit(
        db, actor=identity, action=AuditActionEnum.PROXY_KEY_CREATE,
        target_type="api_key", target_id=key_id, target_name=body.name,
        detail={"key_type": "external_agent", "prefix": api_key.prefix}, request=request,
    )
    await db.commit()
    return CreateApiKeyResponse(
        id=key_id, key=raw_key, prefix=api_key.prefix, name=api_key.name,
        key_type=api_key.key_type, created_at=api_key.created_at,
    )


# ============================================================
# 列表与撤销
# ============================================================


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """列出本租户 API Key（仅前缀）。Super_Admin 可列全部（platform 不过滤）。"""
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
    if not identity.is_super_admin:
        stmt = stmt.where(ApiKey.tenant_id == identity.tenant_id)
    result = await db.execute(stmt)
    keys = result.scalars().all()
    items = [
        ApiKeyItem(
            id=k.id, prefix=k.prefix, name=k.name, key_type=k.key_type,
            is_active=k.is_active, call_count=k.call_count,
            last_used_at=k.last_used_at, created_at=k.created_at,
        )
        for k in keys
    ]
    return ApiKeyListResponse(items=items, total=len(items))


@router.get("/me", response_model=ApiKeyListResponse)
async def list_my_keys(
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_SELF.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """普通用户列出自己创建的用户级 Key（仅前缀）。"""
    stmt = select(ApiKey).where(
        ApiKey.bound_user_id == identity.user_id,
        ApiKey.key_type == ApiKeyTypeEnum.USER_LEVEL.value,
    ).order_by(ApiKey.created_at.desc())
    result = await db.execute(stmt)
    keys = result.scalars().all()
    items = [
        ApiKeyItem(
            id=k.id, prefix=k.prefix, name=k.name, key_type=k.key_type,
            is_active=k.is_active, call_count=k.call_count,
            last_used_at=k.last_used_at, created_at=k.created_at,
        )
        for k in keys
    ]
    return ApiKeyListResponse(items=items, total=len(items))


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    request: Request,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.APIKEY_MANAGE.value}, allow_api_key=False)
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """撤销 API Key（软删除，校验归属本租户）。

    代理 Key（External_Agent）属 Platform 操作，仅 Super_Admin 可撤销。
    """
    api_key = await db.get(ApiKey, key_id)
    if api_key is None:
        raise CrossTenantError()
    is_proxy = api_key.key_type == ApiKeyTypeEnum.EXTERNAL_AGENT.value
    if is_proxy:
        if not identity.is_super_admin:
            raise PermissionDeniedError("仅平台超级管理员可撤销代理 Key")
    elif not identity.is_super_admin and api_key.tenant_id != identity.tenant_id:
        raise CrossTenantError()
    api_key.is_active = False
    add_audit(
        db, actor=identity,
        action=AuditActionEnum.PROXY_KEY_REVOKE if is_proxy else AuditActionEnum.APIKEY_REVOKE,
        target_type="api_key", target_id=key_id, target_name=api_key.name, request=request,
    )
    await db.commit()
    return {"message": "API Key 已撤销", "id": key_id}
