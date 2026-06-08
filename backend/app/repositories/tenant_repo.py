"""TenantRepository + 仓储兜底（方案 A 显式过滤 + 方案 B 全局兜底）。

三处收敛点之一：所有受隔离资源的读写应经此处统一注入 tenant_id 过滤。

- 方案 A（主动正确）：`TenantRepository.scoped_select / get_or_404 / stamp / add`
  在端点显式表达过滤与盖章，可读、可测。
- 方案 B（兜底防漏）：基于 contextvar 三态 + `with_loader_criteria` 的全局
  `do_orm_execute` 事件钩子，对继承 TenantScopedMixin 的模型自动追加 tenant 过滤。
  即便某处漏用仓储（含函数内自开 async_session 的旧代码），也不致越权。

contextvar 三态（见 IdentityContext.tenant_scope_mode）：
  - tenant   -> 注入 tenant_id == <指定租户>
  - platform -> 不注入（Super_Admin 跨租户读容器/账号元数据；内容正文另由边界拦截）
  - external -> 注入 tenant_id == External_User_Tenant
未设置 contextvar 时（如启动期 init_db / Bootstrap / Worker 无身份上下文）默认
**不注入**（等价 platform），避免误伤启动任务——这些路径不处理用户请求。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import with_loader_criteria

from app.api.errors import CrossTenantError
from app.auth.identity import IdentityContext, TenantScopeModeEnum
from app.schema.db import TenantScopedMixin


@dataclass(frozen=True)
class TenantScope:
    """存入 contextvar 的当前请求租户范围。"""

    mode: TenantScopeModeEnum
    tenant_id: str | None  # tenant/external 模式下为具体租户；platform 为 None


# 当前请求的租户范围（请求入口 set，结束 reset）。未设置即 None -> 不注入过滤。
_current_scope: ContextVar[TenantScope | None] = ContextVar(
    "tenant_scope", default=None
)


def set_tenant_scope(scope: TenantScope | None) -> Any:
    """设置当前租户范围，返回 token 供 reset。"""
    return _current_scope.set(scope)


def reset_tenant_scope(token: Any) -> None:
    _current_scope.reset(token)


def current_tenant_scope() -> TenantScope | None:
    return _current_scope.get()


def scope_from_identity(identity: IdentityContext) -> TenantScope:
    """由 IdentityContext 推导仓储兜底所需的三态范围。"""
    return TenantScope(mode=identity.tenant_scope_mode(), tenant_id=identity.tenant_id)


@contextmanager
def tenant_scope(scope: TenantScope | None) -> Iterator[None]:
    """with 作用域内临时设置租户范围（测试/后台任务便捷用）。"""
    token = set_tenant_scope(scope)
    try:
        yield
    finally:
        reset_tenant_scope(token)


_loader_criteria_installed = False


def install_tenant_loader_criteria() -> None:
    """注册全局 do_orm_execute 事件，对所有 TenantScopedMixin 模型按当前 contextvar
    三态自动注入 tenant 过滤（方案 B 兜底）。在应用与 Worker 启动时各调用一次。

    - 注册在全局 `sqlalchemy.orm.Session` 类上：覆盖 `Depends(get_db)` 注入的会话与
      函数内自开的 `async_session()`（二者底层都是同一 Session 类）。
    - 幂等：重复调用只注册一次，避免同一过滤被叠加多遍。
    - 只对 SELECT（ORM 查询）注入；写入/删除的盖章与归属由方案 A 显式负责，
      避免对 bulk update/delete 产生意外行为。
    """
    global _loader_criteria_installed
    if _loader_criteria_installed:
        return

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "do_orm_execute")
    def _add_tenant_criteria(orm_execute_state):  # noqa: ANN001
        if not orm_execute_state.is_select:
            return
        scope = _current_scope.get()
        if scope is None or scope.mode == TenantScopeModeEnum.PLATFORM:
            # 未设置范围 或 platform 态：不注入（跨租户读元数据由内容边界单独管控）
            return
        if scope.tenant_id is None:
            return
        # 绑定到局部变量：with_loader_criteria 的 lambda 会把简单闭包变量作为
        # 可缓存的 SQL 绑定参数处理；直接引用 scope.tenant_id（属性访问）不可缓存。
        tid = scope.tenant_id
        orm_execute_state.statement = orm_execute_state.statement.options(
            with_loader_criteria(
                TenantScopedMixin,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )

    _loader_criteria_installed = True


class TenantRepository:
    """受隔离资源的统一数据访问层（方案 A，显式注入）。"""

    def __init__(self, session: AsyncSession, identity: IdentityContext):
        self.session = session
        self.identity = identity

    def scoped_select(self, model) -> Select:
        """构造已注入 tenant 过滤的 SELECT。

        - platform 态（Super_Admin）：不注入 tenant 过滤（跨租户读元数据）。
        - 其余：强制 where tenant_id == identity.tenant_id。
        """
        stmt = select(model)
        if self.identity.tenant_scope_mode() == TenantScopeModeEnum.PLATFORM:
            return stmt
        return stmt.where(model.tenant_id == self.identity.tenant_id)

    async def get_or_404(self, model, id_: str):
        """按主键取受隔离资源；跨租户或不存在返回**完全相同**的 404（存在性非泄露）。"""
        obj = await self.session.get(model, id_)
        if obj is None:
            raise CrossTenantError()
        # platform 态放行跨租户读（元数据）；其余强制租户一致
        if self.identity.tenant_scope_mode() != TenantScopeModeEnum.PLATFORM:
            obj_tenant = getattr(obj, "tenant_id", None)
            if obj_tenant != self.identity.tenant_id:
                raise CrossTenantError()
        return obj

    def stamp(self, entity) -> None:
        """写入前盖章 tenant_id（仅对受隔离模型；platform 态需调用方显式指定租户）。"""
        if isinstance(entity, TenantScopedMixin):
            if self.identity.tenant_id is not None:
                entity.tenant_id = self.identity.tenant_id

    async def add(self, entity) -> None:
        """盖章并加入会话。"""
        self.stamp(entity)
        self.session.add(entity)
