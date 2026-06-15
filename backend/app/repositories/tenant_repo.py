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
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import with_loader_criteria

from app.api.errors import CrossTenantError
from app.auth.identity import IdentityContext, TenantScopeModeEnum
from app.schema.db import Chunk, Document, Folder, KnowledgeBase, TenantScopedMixin

# cross-tenant-kb-share：「KB 内容类」白名单 —— 仅这些类在跨租户分享授权下被额外放行。
# 每项为 (模型, 取该模型上「KB id」的列名)。KnowledgeBase 用主键 id，其余用 kb_id 外键。
# 其余受隔离类（ApiKey/ChatSession/ChatMessageRecord/SessionFile/SessionChunk/CustomSkill）
# 不在此列，过滤条件维持纯 tenant_id == tid，一行不松。
_KB_CONTENT_MODELS: tuple[tuple[type, str], ...] = (
    (KnowledgeBase, "id"),
    (Document, "kb_id"),
    (Folder, "kb_id"),
    (Chunk, "kb_id"),
)


@dataclass(frozen=True)
class TenantScope:
    """存入 contextvar 的当前请求租户范围。"""

    mode: TenantScopeModeEnum
    tenant_id: str | None  # tenant/external 模式下为具体租户；platform 为 None
    # cross-tenant-kb-share：当前身份经跨租户分享被授予 read 的 KB id 集合（默认空）。
    # 仅对「KB 内容类」(KnowledgeBase/Document/Folder/Chunk) 的 SELECT 兜底过滤额外放行
    # 这批 kb；空集时等价旧行为（IN () 恒 false）。其余受隔离类不受其影响。
    cross_tenant_kb_ids: frozenset[str] = field(default_factory=frozenset)


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


def scope_from_identity(
    identity: IdentityContext,
    cross_tenant_kb_ids: frozenset[str] | None = None,
) -> TenantScope:
    """由 IdentityContext 推导仓储兜底所需的三态范围。

    ``cross_tenant_kb_ids``（cross-tenant-kb-share）：当前身份经跨租户分享被授予 read
    的 KB id 集合。由守卫在请求入口查好后注入；默认 None（空集）时行为与改造前等价。
    """
    return TenantScope(
        mode=identity.tenant_scope_mode(),
        tenant_id=identity.tenant_id,
        cross_tenant_kb_ids=cross_tenant_kb_ids or frozenset(),
    )


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
        # 显式豁免：携带 skip_tenant_filter=True 的语句不注入任何租户过滤。
        # 用于跨租户分享「领取前」按主键读取目标 KB 元数据等受控、自带边界校验的场景。
        if orm_execute_state.execution_options.get("skip_tenant_filter"):
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
        extra_kb_ids = scope.cross_tenant_kb_ids

        if not extra_kb_ids:
            # 快路径：无跨租户授权（绝大多数请求）。行为与改造前**逐字节等价**——
            # 对所有 TenantScopedMixin 模型统一注入 tenant_id == tid。
            orm_execute_state.statement = orm_execute_state.statement.options(
                with_loader_criteria(
                    TenantScopedMixin,
                    lambda cls: cls.tenant_id == tid,
                    include_aliases=True,
                )
            )
            return

        # 慢路径：当前身份持有跨租户被授予的 KB（已领取分享）。
        # 「KB 内容类」额外放行这批 kb（仅读路径生效，写仍由各端点 owner/grant 闸门把关）；
        # 其余受隔离类维持纯 tenant_id 过滤，不受影响。
        # 注意：此处刻意**不**施加 Mixin 级统一 criteria——with_loader_criteria 的多条
        # criteria 是 AND 叠加，若叠加 Mixin 级 tenant_id==tid 会把 OR 放行重新收紧为
        # 仅本租户。故慢路径改为逐类施加：KB 内容类用 OR，其余类各自用纯 tenant 过滤。
        # 慢路径罕见（仅已领取跨租户分享者命中），用直接表达式而非 lambda，规避
        # lambda 缓存键碰撞；代价是该语句缓存性下降，可接受。
        kb_id_list = list(extra_kb_ids)
        stmt = orm_execute_state.statement
        for model, kb_col_name in _KB_CONTENT_MODELS:
            kb_col = getattr(model, kb_col_name)
            stmt = stmt.options(
                with_loader_criteria(
                    model,
                    or_(model.tenant_id == tid, kb_col.in_(kb_id_list)),
                    include_aliases=True,
                )
            )
        # 非 KB 内容类的其余受隔离模型：保持纯 tenant 过滤（与旧行为一致）。
        kb_content_types = {m for m, _ in _KB_CONTENT_MODELS}
        for other_model in _iter_other_scoped_models(kb_content_types):
            stmt = stmt.options(
                with_loader_criteria(
                    other_model,
                    other_model.tenant_id == tid,
                    include_aliases=True,
                )
            )
        orm_execute_state.statement = stmt

    _loader_criteria_installed = True


def _iter_other_scoped_models(exclude: set[type]):
    """枚举所有继承 TenantScopedMixin、但不在 KB 内容类白名单内的映射模型。

    用于慢路径：对这些类施加纯 ``tenant_id == tid`` 过滤（与旧行为一致），
    确保跨租户放行严格限定在 KB 内容类，绝不波及 ApiKey/Chat/Session/Skill 等。
    """
    from app.schema.db import Base

    seen: set[type] = set()
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if not issubclass(model, TenantScopedMixin):
            continue
        if model in exclude or model in seen:
            continue
        seen.add(model)
        yield model


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
