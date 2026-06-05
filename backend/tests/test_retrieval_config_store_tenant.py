"""RetrievalConfigStore 全平台一份语义测试（capability-config-to-platform）

历史上检索/分块参数按租户分键（每租户一行）。现已上收为平台底座：**全平台一份**，
仅超级管理员维护、对全平台生效。Store 内部把任意传入 tenant_id（含 None / kb.tenant_id /
contextvar 租户）规范化为固定平台键 ``PLATFORM_RETRIEVAL_KEY``，保证「写哪行 = 读哪行」。

本文件覆盖：
- 全平台一份：不同 tenant_id 入参读写的是同一份配置（写 A 后读 B 能看到 A 的值）。
- get_effective(None) 与显式 tenant_id 等价（都读平台单行）。
- update→get_effective 往返（含分块档字段）持久化到平台单行。
- DB 读失败时降级全默认且记 WARNING。

用 sqlite+aiosqlite 内存库建表后构造真实 RetrievalConfigStore（不连 PostgreSQL）。

Feature: capability-config-to-platform
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
    PLATFORM_RETRIEVAL_KEY,
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
def _two_tenants_and_patch(draw):
    """生成 (tenant_a, tenant_b, patch_a)，保证 A != B。"""
    a = draw(_tenant_id())
    b = draw(_tenant_id())
    if a == b:
        b = b + "x"  # 强制不同
    return a, b, draw(_legal_patch())


# ============================================================
# 全平台一份：不同 tenant_id 入参共享同一份配置
# ============================================================


@settings(max_examples=100, deadline=None)
@given(data=_two_tenants_and_patch())
def test_property_all_tenants_share_one_config(data):
    """Feature: capability-config-to-platform — 全平台一份共享配置

    For any 两个不同 tenant_id 入参 A、B 与任意合法 patch：
    - update(A, patch) 后，get_effective(B) 与 get_effective(None) 都能看到该 patch
      （因为内部都落到同一平台单行 PLATFORM_RETRIEVAL_KEY）；
    - reset_defaults(任意入参) 后，所有入参读出的都是全默认。

    Validates: 能力配置上收平台、全平台一份。
    """
    tenant_a, tenant_b, patch = data

    async def scenario():
        store, engine = await _make_store()
        try:
            # 写 A 入参
            eff_a = await store.update(tenant_a, patch)
            for name, value in patch.items():
                assert getattr(eff_a, name) == value, f"{name} 未反映 patch"

            # 用 B 入参 / None 入参读，都应看到 A 写入的值（全平台一份）
            eff_b = await store.get_effective(tenant_b)
            eff_none = await store.get_effective(None)
            for name, value in patch.items():
                assert getattr(eff_b, name) == value, f"B 入参未读到平台共享值（{name}）"
                assert getattr(eff_none, name) == value, f"None 入参未读到平台共享值（{name}）"

            # reset（任意入参）后所有入参读出全默认
            await store.reset_defaults(tenant_b)
            assert await store.get_effective(tenant_a) == RetrievalConfig()
            assert await store.get_effective(None) == RetrievalConfig()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_store_reads_platform_key():
    """get_effective(任意入参) 读 DB 平台单行（主键 = PLATFORM_RETRIEVAL_KEY）。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    config = asyncio.run(store.get_effective("any-tenant"))

    assert config == RetrievalConfig()
    assert session.get.await_count == 1
    args, _ = session.get.call_args
    assert args[0] is RetrievalConfigRow
    assert args[1] == PLATFORM_RETRIEVAL_KEY


# ============================================================
# 写读往返（含分块档） + DB 失败降级
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
    """update→get_effective 往返，含分块档字段（parent/child/overlap），落到平台单行。"""
    store, factory = sqlite_store

    eff = await store.update(
        None,
        {"parent_chunk_size": 3000, "child_chunk_size": 600, "chunk_overlap": 120, "recall_k": 200},
    )
    assert eff.parent_chunk_size == 3000
    assert eff.child_chunk_size == 600
    assert eff.chunk_overlap == 120
    assert eff.recall_k == 200

    # 直接读 DB 验证持久化到平台单行
    async with factory() as session:
        row = await session.get(RetrievalConfigRow, PLATFORM_RETRIEVAL_KEY)
    assert row is not None
    assert row.parent_chunk_size == 3000
    assert row.child_chunk_size == 600
    assert row.chunk_overlap == 120


@pytest.mark.asyncio
async def test_get_effective_degrades_on_db_failure(caplog):
    """DB 读失败时 get_effective 降级全默认且记 WARNING（Req 5.3）。"""
    session = MagicMock()
    session.get = AsyncMock(side_effect=RuntimeError("db down"))
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = await store.get_effective("tenant-x")

    assert config == RetrievalConfig()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("检索配置" in w.getMessage() or "降级" in w.getMessage() for w in warnings)
