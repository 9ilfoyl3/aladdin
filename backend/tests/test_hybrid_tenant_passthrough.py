"""H5 租户配置显式传参：属性测试(Property 2) + 单元测试（任务 1.1）

对应 spec retrieval-pipeline-hardening 的 Property 2（租户传参优先级）与
Fix 1 单元测试：

- 属性测试(Property 2)：任意「显式 tenant_id（None 或具体值）/ contextvar 状态
  （未设置 / tenant 态 / platform 态）」组合下，检索采用的有效租户
  SHALL = 显式 tenant_id（非 None 时）否则 contextvar 值。
  通过 mock ``config_store.get_effective`` 捕获 ``search`` 传入的 ``effective_tenant`` 验证。
- 单元测试：
  - contextvar 已 reset（不进入 tenant_scope，``current_tenant_scope()`` 返回 None）时，
    显式传 tenant_id 仍取到正确租户（get_effective 收到的是显式值）；显式值优先于 contextvar。
  - ``RetrievalConfigStore.get_effective(None)`` 产生 WARNING（caplog 断言），仍返回全默认。

参考 test_hybrid_tenant_ttl.py / test_retrieval_config_store.py 的 mock 与属性测试风格。

Feature: retrieval-pipeline-hardening
"""

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# get_settings() 启动期 fail-fast 需要 JWT_SECRET（构造 RetrievalConfigStore 单例 /
# import app.storage.database 会触发）。前置好环境变量。
os.environ.setdefault("JWT_SECRET", "h5-tenant-passthrough-test-secret-0123456789abcdef")

# 模拟 pymilvus 模块以避免导入依赖问题（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.auth.identity import TenantScopeModeEnum  # noqa: E402
from app.repositories.tenant_repo import TenantScope, tenant_scope  # noqa: E402
from app.retrieval.config import RetrievalConfig, RetrievalConfigStore  # noqa: E402
from app.retrieval.hybrid import HybridRetriever  # noqa: E402


# ============================================================
# 公共 fake / mock 构件
# ============================================================


class FakeAsyncSession:
    """模拟异步数据库会话（父块扩展查询返回空）。"""

    async def execute(self, stmt):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSessionFactory:
    def __call__(self):
        return FakeAsyncSession()


def _build_capturing_hybrid():
    """构造一个 HybridRetriever，其 config_store.get_effective 为 AsyncMock，捕获每次入参。

    三路子检索器均返回 []，使 ``search`` 在 RRF 融合为空时提前返回，
    本测试只关心 ``get_effective`` 收到的有效租户参数，不关心后续 rerank/扩展。

    Returns:
        (hybrid, config_store)
    """
    config = RetrievalConfig()

    config_store = MagicMock()
    config_store.get_effective = AsyncMock(return_value=config)

    platform_store = MagicMock()
    platform_store.get_load_cache_ttl = AsyncMock(return_value=0)

    vector_retriever = MagicMock()
    vector_retriever.search = AsyncMock(return_value=[])
    sparse_retriever = MagicMock()
    sparse_retriever.search = AsyncMock(return_value=[])
    bm25_retriever = MagicMock()
    bm25_retriever.search = AsyncMock(return_value=[])

    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[])

    hybrid = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=reranker,
        db_session_factory=FakeSessionFactory(),
        bm25_retriever=bm25_retriever,
        config_store=config_store,
        platform_store=platform_store,
    )
    return hybrid, config_store


# ============================================================
# 属性测试 Property 2：租户传参优先级
# ============================================================


# contextvar 三种状态：
#   ("none",)            -> 不进入 tenant_scope，current_tenant_scope() 返回 None
#   ("tenant", <tid>)    -> TenantScope(TENANT, tid)，contextvar 有效租户 = tid
#   ("platform",)        -> TenantScope(PLATFORM, None)，contextvar 有效租户 = None
_ctx_state = st.one_of(
    st.just(("none",)),
    st.builds(lambda tid: ("tenant", tid), st.text(min_size=1, max_size=12)),
    st.just(("platform",)),
)

# 显式 tenant_id：None（触发回退）或任意字符串（含空串，验证「is not None」边界）。
_explicit_tenant = st.one_of(st.none(), st.text(min_size=0, max_size=12))


def _ctx_to_scope_and_value(ctx_state):
    """把生成的 contextvar 状态映射为 (scope_or_None, contextvar 有效租户值)。"""
    kind = ctx_state[0]
    if kind == "none":
        return None, None
    if kind == "tenant":
        tid = ctx_state[1]
        return TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id=tid), tid
    # platform 态
    return TenantScope(mode=TenantScopeModeEnum.PLATFORM, tenant_id=None), None


@settings(max_examples=200, deadline=None)
@given(explicit=_explicit_tenant, ctx_state=_ctx_state)
def test_property_tenant_priority(explicit, ctx_state):
    """Feature: retrieval-pipeline-hardening, Property 2: 租户传参优先级

    For any 显式 tenant_id 与 contextvar 状态的组合，search 解析出的有效租户
    SHALL = 显式 tenant_id（非 None 时）否则 contextvar 值。

    Validates: Requirements 1.2
    """
    scope, ctx_value = _ctx_to_scope_and_value(ctx_state)
    expected = explicit if explicit is not None else ctx_value

    async def scenario():
        hybrid, config_store = _build_capturing_hybrid()
        if scope is None:
            await hybrid.search("查询", kb_id="kb_001", top_k=4, tenant_id=explicit)
        else:
            with tenant_scope(scope):
                await hybrid.search("查询", kb_id="kb_001", top_k=4, tenant_id=explicit)
        return config_store.get_effective

    get_effective = asyncio.run(scenario())

    get_effective.assert_awaited_once_with(expected)


@settings(max_examples=200, deadline=None)
@given(explicit=_explicit_tenant, ctx_state=_ctx_state)
def test_property_tenant_priority_search_with_trace(explicit, ctx_state):
    """Feature: retrieval-pipeline-hardening, Property 2: 租户传参优先级（调参链路一致）

    search_with_trace 的租户优先级与 search 一致：显式 tenant_id 非 None 时优先，否则回退 contextvar。

    Validates: Requirements 1.2
    """
    scope, ctx_value = _ctx_to_scope_and_value(ctx_state)
    expected = explicit if explicit is not None else ctx_value

    async def scenario():
        hybrid, config_store = _build_capturing_hybrid()
        if scope is None:
            await hybrid.search_with_trace("查询", kb_id="kb_001", top_k=4, tenant_id=explicit)
        else:
            with tenant_scope(scope):
                await hybrid.search_with_trace("查询", kb_id="kb_001", top_k=4, tenant_id=explicit)
        return config_store.get_effective

    get_effective = asyncio.run(scenario())

    get_effective.assert_awaited_once_with(expected)


# ============================================================
# 单元测试：contextvar 已 reset 时显式传参仍取到正确租户
# ============================================================


@pytest.mark.asyncio
async def test_explicit_tenant_used_when_contextvar_reset():
    """contextvar 已 reset（未进入 tenant_scope）时，显式 tenant_id 仍被用于读配置。

    模拟流式响应中依赖上下文已 reset、检索仍需取到正确租户配置的场景（H5）。
    """
    hybrid, config_store = _build_capturing_hybrid()

    # 不进入 tenant_scope：current_tenant_scope() == None（contextvar 已 reset）
    await hybrid.search("查询", kb_id="kb_001", top_k=4, tenant_id="tenant-X")

    config_store.get_effective.assert_awaited_once_with("tenant-X")


@pytest.mark.asyncio
async def test_explicit_tenant_overrides_contextvar():
    """显式 tenant_id 优先级高于 contextvar：二者不同时取显式值。"""
    hybrid, config_store = _build_capturing_hybrid()

    scope = TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id="ctx-tenant")
    with tenant_scope(scope):
        await hybrid.search("查询", kb_id="kb_001", top_k=4, tenant_id="explicit-tenant")

    # 显式值胜出，contextvar 值被忽略
    config_store.get_effective.assert_awaited_once_with("explicit-tenant")


@pytest.mark.asyncio
async def test_falls_back_to_contextvar_when_no_explicit_tenant():
    """未传 tenant_id（None，向后兼容既有调用点）时回退 contextvar 当前租户。"""
    hybrid, config_store = _build_capturing_hybrid()

    scope = TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id="ctx-tenant")
    with tenant_scope(scope):
        await hybrid.search("查询", kb_id="kb_001", top_k=4)

    config_store.get_effective.assert_awaited_once_with("ctx-tenant")


@pytest.mark.asyncio
async def test_rerank_and_expand_uses_explicit_tenant_when_contextvar_reset():
    """多库路径 rerank_and_expand 同样按显式 tenant_id 读配置（contextvar 已 reset 场景）。"""
    from app.retrieval.base import RetrievalResult

    hybrid, config_store = _build_capturing_hybrid()

    results = [
        RetrievalResult(
            chunk_id="c0",
            content="独特内容片段甲",
            score=0.9,
            doc_id="d0",
            metadata={"parent_id": ""},
        )
    ]

    # 未进入 tenant_scope：contextvar 为 None；显式传 tenant 必须被采用
    await hybrid.rerank_and_expand("查询", results, top_k=5, tenant_id="tenant-Y")

    config_store.get_effective.assert_awaited_once_with("tenant-Y")


# ============================================================
# 单元测试：get_effective(None) 产生 WARNING
# ============================================================


class _FakeAsyncSessionCtx:
    """模拟 ``async with session_factory() as session`` 的异步上下文管理器。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_get_effective_none_emits_warning_and_returns_defaults(caplog):
    """get_effective(None) 记一条 WARNING（H5 可观测性），仍返回全 Safe_Default 不抛错、不打 DB。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)

    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = await store.get_effective(None)

    # 仍返回全默认配置
    assert config == RetrievalConfig()
    # 不打 DB（None 直接短路）
    assert session.get.await_count == 0
    # 产生 WARNING，且消息指向 tenant_id=None / 全默认回退
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "tenant_id=None" in w.getMessage() or "全默认" in w.getMessage()
        for w in warnings
    ), f"未捕获到 get_effective(None) 的 WARNING：{[w.getMessage() for w in warnings]}"
