"""HybridRetriever 租户化取配置 + Load_Cache_TTL 透传 wiring 单元测试（任务 19.3）

验证：
- search/search_with_trace 按 Current_Tenant 读取该租户配置：进入 tenant_scope(T)
  时调 get_effective("T")；无租户上下文时调 get_effective(None)（Req 1.6/1.9）。
- 单次检索取一次 get_load_cache_ttl()，并把该 TTL 透传给 dense/sparse/bm25 三路
  子检索器（dense 还收到 ef）（Req 15.1/17.3）。

参考 test_hybrid_config_injection.py 的 mock 构造方式。
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.auth.identity import TenantScopeModeEnum  # noqa: E402
from app.repositories.tenant_repo import TenantScope, tenant_scope  # noqa: E402
from app.retrieval.config import RetrievalConfig  # noqa: E402
from app.retrieval.hybrid import HybridRetriever  # noqa: E402


_TTL = 10


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


def _build_hybrid():
    """构造一个注入 mock config_store / platform_store / 三路子检索器的 HybridRetriever。

    三路子检索器均为 AsyncMock，search 返回 []（fused 为空时 search 提前返回，
    本测试只关心三路召回的入参透传，不关心后续 rerank/扩展）。
    """
    config = RetrievalConfig(recall_k=7, hnsw_ef=99)

    config_store = MagicMock()
    config_store.get_effective = AsyncMock(return_value=config)

    platform_store = MagicMock()
    platform_store.get_load_cache_ttl = AsyncMock(return_value=_TTL)

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
    return hybrid, config, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever


def _assert_ttl_passthrough(config, vector_retriever, sparse_retriever, bm25_retriever):
    """断言三路子检索器都收到 load_cache_ttl=_TTL，dense 还收到 ef=config.hnsw_ef。"""
    dense_kwargs = vector_retriever.search.call_args.kwargs
    assert dense_kwargs["load_cache_ttl"] == _TTL
    assert dense_kwargs["ef"] == config.hnsw_ef

    sparse_kwargs = sparse_retriever.search.call_args.kwargs
    assert sparse_kwargs["load_cache_ttl"] == _TTL

    bm25_kwargs = bm25_retriever.search.call_args.kwargs
    assert bm25_kwargs["load_cache_ttl"] == _TTL


@pytest.mark.asyncio
async def test_search_uses_current_tenant_config_and_ttl():
    """进入 tenant_scope(T) 时 search 按租户 T 读取配置，并把 TTL 透传三路。"""
    hybrid, config, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever = _build_hybrid()

    scope = TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id="T")
    with tenant_scope(scope):
        await hybrid.search("查询", kb_id="kb_001", top_k=4)

    # 按当前租户读配置
    config_store.get_effective.assert_awaited_once_with("T")
    # 单次检索取一次 TTL
    assert platform_store.get_load_cache_ttl.await_count == 1
    # 三路透传 load_cache_ttl（dense 还透传 ef）
    _assert_ttl_passthrough(config, vector_retriever, sparse_retriever, bm25_retriever)


@pytest.mark.asyncio
async def test_search_with_trace_uses_current_tenant_config_and_ttl():
    """search_with_trace 同样按租户取配置 + 透传 TTL，与线上 search 一致。"""
    hybrid, config, config_store, platform_store, vector_retriever, sparse_retriever, bm25_retriever = _build_hybrid()

    scope = TenantScope(mode=TenantScopeModeEnum.TENANT, tenant_id="T")
    with tenant_scope(scope):
        await hybrid.search_with_trace("查询", kb_id="kb_001", top_k=4)

    config_store.get_effective.assert_awaited_once_with("T")
    assert platform_store.get_load_cache_ttl.await_count == 1
    _assert_ttl_passthrough(config, vector_retriever, sparse_retriever, bm25_retriever)


@pytest.mark.asyncio
async def test_search_without_tenant_context_reads_none():
    """无租户上下文（不进入 tenant_scope）时按 None 读取配置，回落全默认（Req 1.9）。"""
    hybrid, _, config_store, platform_store, _, _, _ = _build_hybrid()

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    config_store.get_effective.assert_awaited_once_with(None)
    assert platform_store.get_load_cache_ttl.await_count == 1


@pytest.mark.asyncio
async def test_search_platform_scope_reads_none():
    """platform 态（超管跨租户）scope.tenant_id 为 None → 按 None 读取全默认。"""
    hybrid, _, config_store, _, _, _, _ = _build_hybrid()

    scope = TenantScope(mode=TenantScopeModeEnum.PLATFORM, tenant_id=None)
    with tenant_scope(scope):
        await hybrid.search("查询", kb_id="kb_001", top_k=4)

    config_store.get_effective.assert_awaited_once_with(None)
