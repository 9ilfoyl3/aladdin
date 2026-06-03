"""RetrievalConfigStore 租户隔离测试（任务 16.4 / 16.6）

覆盖：
- 16.4 属性测试 P9：租户隔离的配置读写（两租户互不影响；reset 只影响目标租户；
  get_effective(None) 恒全默认且不读 DB）。
- 16.6 单元测试（检索侧）：按租户 update→get_effective 往返（含分块档字段）；
  DB 读失败时降级全默认且记 WARNING。

用 sqlite+aiosqlite 内存库建表后构造真实 RetrievalConfigStore（不连 PostgreSQL）。

Feature: kb-retrieval-optimization
"""

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("JWT_SECRET", "retrieval-store-tenant-test-0123456789abcdef")
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.retrieval.config import (  # noqa: E402
    KIND_BOOL,
    KIND_INT,
    RETRIEVAL_FIELD_SPECS,
    RetrievalConfig,
    RetrievalConfigStore,
)
from app.schema.db import Base, RetrievalConfigRow  # noqa: E402


# ============================================================
# 工具：每次构造独立的 sqlite 内存库 + store（属性测试每次迭代隔离）
# ============================================================


async def _make_store():
    """建一个 sqlite 内存库 + 真实 store，返回 (store, engine)。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return RetrievalConfigStore(factory), engine


class _FakeAsyncSessionCtx:
    """模拟 ``async with session_factory() as session``。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


# ============================================================
# 生成器：两个不同租户 ID + 各自合法 patch
# ============================================================


@st.composite
def _tenant_id(draw) -> str:
    return draw(st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8))


@st.composite
def _legal_patch(draw, min_size: int = 1) -> dict:
    """生成全部字段合法（落在 Valid_Range 内）的 patch（至少 min_size 个字段）。"""
    names = list(RETRIEVAL_FIELD_SPECS.keys())
    chosen = draw(st.lists(st.sampled_from(names), min_size=min_size, max_size=len(names), unique=True))
    patch: dict = {}
    for name in chosen:
        spec = RETRIEVAL_FIELD_SPECS[name]
        if spec.kind == KIND_BOOL:
            patch[name] = draw(st.booleans())
        elif spec.kind == KIND_INT:
            patch[name] = draw(st.integers(min_value=spec.lo, max_value=spec.hi))
        else:  # KIND_FLOAT
            patch[name] = draw(
                st.floats(min_value=spec.lo, max_value=spec.hi, allow_nan=False, allow_infinity=False)
            )
    return patch


@st.composite
def _two_tenants_and_patches(draw):
    """生成 (tenant_a, tenant_b, patch_a, patch_b)，保证 A != B。"""
    a = draw(_tenant_id())
    b = draw(_tenant_id())
    if a == b:
        b = b + "x"  # 强制不同
    return a, b, draw(_legal_patch()), draw(_legal_patch())


# ============================================================
# 16.4 属性测试 P9：租户隔离的配置读写
# ============================================================


@settings(max_examples=100, deadline=None)
@given(data=_two_tenants_and_patches())
def test_property_tenant_isolation(data):
    """Feature: kb-retrieval-optimization, Property 9: 租户隔离的配置读写

    For any 两个不同租户 A、B 与各自合法 patch：
    - update(A, patch_A) 后，get_effective(A) 反映 patch_A；
    - get_effective(B) 不受 patch_A 影响（仍为 B 既有值或全默认）；
    - update(B, patch_B) 后 A 仍保持其 patch_A；
    - reset_defaults(A) 只影响 A（A 全默认），B 不变；
    - get_effective(None) 恒为全 Safe_Default。

    Validates: Requirements 1.8, 1.9, 4.3
    """
    tenant_a, tenant_b, patch_a, patch_b = data

    async def scenario():
        store, engine = await _make_store()
        try:
            # B 初始：尚无行 → 全默认
            b_initial = await store.get_effective(tenant_b)
            assert b_initial == RetrievalConfig()

            # 写 A
            eff_a = await store.update(tenant_a, patch_a)
            for name, value in patch_a.items():
                assert getattr(eff_a, name) == value, f"A 的 {name} 未反映 patch"

            # B 不受 A 影响（仍全默认）
            assert await store.get_effective(tenant_b) == RetrievalConfig()

            # 写 B
            eff_b = await store.update(tenant_b, patch_b)
            for name, value in patch_b.items():
                assert getattr(eff_b, name) == value

            # A 仍保持 patch_a（未被 B 的写覆盖）
            eff_a_again = await store.get_effective(tenant_a)
            for name, value in patch_a.items():
                assert getattr(eff_a_again, name) == value, f"A 的 {name} 被 B 写入污染"

            # reset A：只影响 A
            await store.reset_defaults(tenant_a)
            assert await store.get_effective(tenant_a) == RetrievalConfig()
            eff_b_after_reset_a = await store.get_effective(tenant_b)
            for name, value in patch_b.items():
                assert getattr(eff_b_after_reset_a, name) == value, "reset(A) 影响了 B"

            # get_effective(None) 恒全默认
            assert await store.get_effective(None) == RetrievalConfig()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_property_none_tenant_never_reads_db():
    """get_effective(None) 不触碰 session_factory（不读 DB），恒返回全默认（Req 1.11）。"""
    factory = MagicMock(side_effect=AssertionError("get_effective(None) 不应构造 session"))
    store = RetrievalConfigStore(factory)

    config = asyncio.run(store.get_effective(None))
    assert config == RetrievalConfig()
    factory.assert_not_called()


# ============================================================
# 16.6 单元测试（检索侧）：写读往返（含分块档） + DB 失败降级
# ============================================================


@pytest_asyncio.fixture
async def sqlite_store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = RetrievalConfigStore(factory)
    try:
        yield store, factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_roundtrip_includes_chunk_tier(sqlite_store):
    """按租户 update→get_effective 往返，含分块档字段（parent/child/overlap）。"""
    store, factory = sqlite_store
    tenant = "tenant-chunk"

    eff = await store.update(
        tenant,
        {"parent_chunk_size": 3000, "child_chunk_size": 600, "chunk_overlap": 120, "recall_k": 200},
    )
    assert eff.parent_chunk_size == 3000
    assert eff.child_chunk_size == 600
    assert eff.chunk_overlap == 120
    assert eff.recall_k == 200

    # 直接读 DB 验证持久化
    async with factory() as session:
        row = await session.get(RetrievalConfigRow, tenant)
    assert row is not None
    assert row.parent_chunk_size == 3000
    assert row.child_chunk_size == 600
    assert row.chunk_overlap == 120


@pytest.mark.asyncio
async def test_get_effective_degrades_on_db_failure(caplog):
    """DB 读失败时按租户 get_effective 降级全默认且记 WARNING（Req 5.3）。"""
    session = MagicMock()
    session.get = AsyncMock(side_effect=RuntimeError("db down"))
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = await store.get_effective("tenant-x")

    assert config == RetrievalConfig()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("检索配置" in w.getMessage() or "降级" in w.getMessage() for w in warnings)


@pytest.mark.asyncio
async def test_update_requires_non_empty_tenant(sqlite_store):
    """update 必须指定非空 tenant_id，否则抛 ValueError（写须定位目标租户）。"""
    store, _factory = sqlite_store
    with pytest.raises(ValueError):
        await store.update("", {"recall_k": 100})
