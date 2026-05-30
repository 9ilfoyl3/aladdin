"""实时权限解析（tenant-auth）。

每次请求按当前持久化的 user_roles → role_permissions → permissions 实时聚合
有效权限点（含 api/menu/btn 三类），绝不依据 JWT 快照。任何角色/权限点/授权
变更在下一次请求即时生效。

v1 不引入解析缓存（正确性优先；如后续加缓存，缓存键须含数据版本戳以保持
"下一次请求即时生效"的可观察行为不变）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schema.db import Permission, Role, RolePermission, UserRole


async def resolve_role_ids(session: AsyncSession, user_id: str) -> frozenset[str]:
    """解析用户当前持有的全部角色 id。"""
    rows = await session.execute(
        select(UserRole.role_id).where(UserRole.user_id == user_id)
    )
    return frozenset(r[0] for r in rows.all())


async def resolve_effective_permissions(
    session: AsyncSession, user_id: str
) -> frozenset[str]:
    """解析用户的有效权限点集合（其全部角色的权限点并集）。"""
    role_ids = await resolve_role_ids(session, user_id)
    if not role_ids:
        return frozenset()
    rows = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(role_ids))
    )
    return frozenset(r[0] for r in rows.all())


async def resolve_permissions_with_types(
    session: AsyncSession, user_id: str
) -> list[dict]:
    """供 GET /api/me/permissions 使用：返回带 type 的权限点列表。"""
    role_ids = await resolve_role_ids(session, user_id)
    if not role_ids:
        return []
    rows = await session.execute(
        select(Permission.code, Permission.type, Permission.description)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(role_ids))
        .distinct()
    )
    return [
        {"code": code, "type": ptype, "description": desc}
        for code, ptype, desc in rows.all()
    ]
