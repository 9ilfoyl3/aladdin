"""RetrievalConfigStore（检索配置读取/写入层，全平台一份）测试

覆盖 tasks 子任务：
- 2.3 进程内单例 get_retrieval_config_store()。
- 2.4 属性测试 P3：恢复默认产出全 Safe_Default（fake/in-memory store）。
- 2.5 属性测试 P4：更新后读取的写读往返（fake/in-memory store）。
- 2.6 单元测试：store 读 DB 而非 get_settings；DB 读失败降级返回全默认且记 WARNING；
      update 正常写入路径。
- 2.7 集成测试：update 写入的行可从 DB 读回（sqlite+aiosqlite 内存库）。

capability-config-to-platform：检索/分块参数已上收为平台底座，**全平台一份**。Store
内部把任意传入 tenant_id（含 None / kb.tenant_id / contextvar 租户）规范化为固定平台键
``PLATFORM_RETRIEVAL_KEY``，写读同一行。本文件入参仍传 ``_TENANT`` 等任意值以保持既有
覆盖，但断言围绕「全平台一份」语义（读写落到平台单行）。全平台共享语义的属性测试见
test_retrieval_config_store_tenant.py；平台配置（P11）见 test_platform_config.py。

Feature: capability-config-to-platform
"""

import asyncio
import logging
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

# get_settings() 启动期 fail-fast 需要 JWT_SECRET；构造 store 单例会 import
# app.storage.database（其 import 期会 get_settings()）。前置好环境变量。
os.environ.setdefault("JWT_SECRET", "retrieval-store-test-secret-0123456789abcdef")

# Mock 重型依赖模块，避免 pymilvus 导入依赖问题（沿用现有测试模式）。
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
    get_retrieval_config_store,
)
from app.schema.db import Base, RetrievalConfigRow  # noqa: E402

# 测试用租户 ID 入参（全平台一份：任意入参都落到平台单行）。
_TENANT = "tenant-A"


# ============================================================
# Fake/in-memory store：用内存 dict 模拟某租户单行，忠实镜像 RetrievalConfigStore 语义
# （缓存 + 写后失效 + 复用真实 effective_from_raw），供 2.4/2.5 属性测试不连真 DB。
# ============================================================


class FakeRetrievalConfigStore:
    """以内存 dict 模拟单租户行的 RetrievalConfigStore（单租户视角）。

    行为与真实 store 一致：``get_effective`` 走 ``effective_from_raw``；``update``
    合并 patch 后失效缓存；``reset_defaults`` 写全 Safe_Default。仅把 DB 行换成
    内存 dict（None 表示尚无持久化行）。
    """

    _CACHE_TTL_SECONDS = 5

    def __init__(self, initial_row: dict | None = None):
        # None 表示尚无持久化行；dict（含空 dict）表示已有行。
        self._row: dict | None = dict(initial_row) if initial_row is not None else None
        self._cached: RetrievalConfig | None = None
        self._cached_at: float = 0.0

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def get_effective(self) -> RetrievalConfig:
        if self._cached is not None and (time.monotonic() - self._cached_at) < self._CACHE_TTL_SECONDS:
            return self._cached
        config = RetrievalConfig.effective_from_raw(self._row)
        self._cached = config
        self._cached_at = time.monotonic()
        return config

    async def update(self, patch: dict) -> RetrievalConfig:
        clean = {k: v for k, v in patch.items() if k in RETRIEVAL_FIELD_SPECS}
        if self._row is None:
            self._row = {}
        self._row.update(clean)
        self.invalidate()
        return await self.get_effective()

    async def reset_defaults(self) -> RetrievalConfig:
        defaults = {name: spec.default for name, spec in RETRIEVAL_FIELD_SPECS.items()}
        return await self.update(defaults)


# ============================================================
# 生成器
# ============================================================


@st.composite
def _arbitrary_row(draw) -> dict:
    """生成任意「先前持久化配置」dict：每字段随机决定是否出现，出现时取任意（合法或非法）值。"""
    row: dict = {}
    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        if not draw(st.booleans()):
            continue
        if spec.kind == KIND_BOOL:
            row[name] = draw(st.sampled_from([True, False, 1, 0, "yes"]))
        elif spec.kind == KIND_INT:
            row[name] = draw(
                st.one_of(
                    st.integers(min_value=spec.lo, max_value=spec.hi),
                    st.integers(),
                    st.sampled_from([None, "x", 1.5]),
                )
            )
        else:  # KIND_FLOAT
            row[name] = draw(
                st.one_of(
                    st.floats(min_value=spec.lo, max_value=spec.hi, allow_nan=False, allow_infinity=False),
                    st.floats(allow_nan=False, allow_infinity=False),
                    st.sampled_from([None, "x", True]),
                )
            )
    return row


@st.composite
def _legal_patch(draw) -> dict:
    """生成一个全部字段合法（落在 Valid_Range 内）的 patch（至少一个字段）。"""
    names = list(RETRIEVAL_FIELD_SPECS.keys())
    chosen = draw(st.lists(st.sampled_from(names), min_size=1, max_size=len(names), unique=True))
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


# ============================================================
# 2.4 属性测试 P3：恢复默认产出全 Safe_Default
# ============================================================


@settings(max_examples=100, deadline=None)
@given(prior=_arbitrary_row())
def test_property_reset_defaults_produces_all_safe_default(prior):
    """Feature: kb-retrieval-optimization, Property 3: 恢复默认产出全 Safe_Default

    For any 先前持久化的检索配置（任意取值），执行 reset_defaults 之后读取的有效配置
    SHALL 对每个字段都等于其 Safe_Default。

    Validates: Requirements 4.1
    """

    async def scenario() -> RetrievalConfig:
        store = FakeRetrievalConfigStore(initial_row=prior)
        await store.reset_defaults()
        return await store.get_effective()

    effective = asyncio.run(scenario())

    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        assert getattr(effective, name) == spec.default, f"{name} 未恢复为 Safe_Default"


# ============================================================
# 2.5 属性测试 P4：更新后读取的写读往返
# ============================================================


@settings(max_examples=100, deadline=None)
@given(prior=_arbitrary_row(), patch=_legal_patch())
def test_property_update_then_get_effective_roundtrip(prior, patch):
    """Feature: kb-retrieval-optimization, Property 4: 更新后读取的写读往返

    For any 通过范围校验的合法更新 patch，update(patch) 之后在同进程内调用
    get_effective() SHALL 反映该 patch 中每个被更新字段的新值（写后缓存已失效，无需重启）。

    Validates: Requirements 5.2
    """

    async def scenario() -> RetrievalConfig:
        store = FakeRetrievalConfigStore(initial_row=prior)
        # 先读一次，建立缓存，验证 update 后缓存确实失效
        await store.get_effective()
        await store.update(patch)
        return await store.get_effective()

    effective = asyncio.run(scenario())

    for name, value in patch.items():
        assert getattr(effective, name) == value, f"{name} 更新后未反映新值"


# ============================================================
# 真实 store 的 fake 异步会话上下文工具
# ============================================================


class _FakeAsyncSessionCtx:
    """模拟 ``async with session_factory() as session`` 的异步上下文管理器。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


# ============================================================
# 2.6 单元测试：读 DB 而非 get_settings + DB 失败降级 + update 写入路径
# ============================================================


@pytest.mark.asyncio
async def test_store_reads_db_not_get_settings(monkeypatch):
    """store.get_effective 读 DB 平台单行（session.get(RetrievalConfigRow, 平台键）），不经 get_settings。"""
    import app.config as config_module

    settings_spy = MagicMock(side_effect=config_module.get_settings)
    monkeypatch.setattr(config_module, "get_settings", settings_spy)

    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    config = await store.get_effective(_TENANT)

    # 确实读了 DB 行，主键为平台键（全平台一份）
    assert session.get.await_count == 1
    args, _ = session.get.call_args
    assert args[0] is RetrievalConfigRow
    assert args[1] == PLATFORM_RETRIEVAL_KEY
    # 行不存在 → 全 Safe_Default
    assert config == RetrievalConfig()
    # 未通过 get_settings 读取检索配置
    settings_spy.assert_not_called()


@pytest.mark.asyncio
async def test_store_get_effective_degrades_on_db_failure(caplog):
    """DB 读失败时 get_effective 不抛错，降级返回全 Safe_Default 并记 WARNING。"""
    session = MagicMock()
    session.get = AsyncMock(side_effect=RuntimeError("db connection refused"))
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)

    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = await store.get_effective(_TENANT)

    assert config == RetrievalConfig()  # 全默认
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("检索配置" in w.getMessage() or "降级" in w.getMessage() for w in warnings)


@pytest.mark.asyncio
async def test_store_get_effective_uses_cache_within_ttl():
    """TTL 内二次 get_effective(同租户) 命中缓存，不重复读 DB。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    await store.get_effective(_TENANT)
    await store.get_effective(_TENANT)

    assert session.get.await_count == 1  # 第二次命中缓存


@pytest.mark.asyncio
async def test_store_get_effective_none_tenant_reads_platform_row():
    """tenant_id 为 None（无上下文）时仍读平台单行（全平台一份），返回平台配置/全默认。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = RetrievalConfigStore(factory)
    config = await store.get_effective(None)

    assert config == RetrievalConfig()
    # 全平台一份：None 入参也读平台单行（主键为平台键），而非短路跳过 DB
    assert session.get.await_count == 1
    args, _ = session.get.call_args
    assert args[1] == PLATFORM_RETRIEVAL_KEY


# ============================================================
# 2.7 集成测试：sqlite+aiosqlite 内存库的真实持久化写读
# ============================================================


@pytest_asyncio.fixture
async def sqlite_store():
    """真实 RetrievalConfigStore，后端为 sqlite+aiosqlite 内存库（不连 PostgreSQL）。"""
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
async def test_store_update_normal_write_path(sqlite_store):
    """update 正常写入路径：写后 get_effective 反映新值（首次 UPSERT 建行）。"""
    store, _factory = sqlite_store

    # 初始无行 → 全默认
    initial = await store.get_effective(_TENANT)
    assert initial == RetrievalConfig()

    effective = await store.update(_TENANT, {"recall_k": 256, "rerank_threshold": 0.5})
    assert effective.recall_k == 256
    assert effective.rerank_threshold == 0.5
    # 未更新字段仍为默认
    assert effective.rrf_k == RETRIEVAL_FIELD_SPECS["rrf_k"].default


@pytest.mark.asyncio
async def test_store_update_persists_row_readable_from_db(sqlite_store):
    """集成：update 写入的行可从 DB 直接读回（Req 5.1）。全平台一份：行存于平台单行。"""
    store, factory = sqlite_store

    await store.update(
        _TENANT, {"recall_k": 300, "hnsw_ef": 512, "threshold_degradation_enabled": False}
    )

    # 直接从 DB 读平台单行，绕过 store 缓存，验证持久化生效
    async with factory() as session:
        row = await session.get(RetrievalConfigRow, PLATFORM_RETRIEVAL_KEY)

    assert row is not None
    assert row.tenant_id == PLATFORM_RETRIEVAL_KEY
    assert row.recall_k == 300
    assert row.hnsw_ef == 512
    assert row.threshold_degradation_enabled is False


@pytest.mark.asyncio
async def test_store_reset_defaults_persists_all_defaults(sqlite_store):
    """集成：reset_defaults 后 DB 行各字段为各自 Safe_Default。"""
    store, factory = sqlite_store

    # 先写入一些非默认值（均落在各自 Valid_Range 内）
    await store.update(_TENANT, {"recall_k": 999, "mmr_lambda": 0.1})
    await store.reset_defaults(_TENANT)

    async with factory() as session:
        row = await session.get(RetrievalConfigRow, PLATFORM_RETRIEVAL_KEY)

    assert row is not None
    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        assert getattr(row, name) == spec.default, f"{name} 未持久化为 Safe_Default"


@pytest.mark.asyncio
async def test_store_update_then_get_effective_reflects_immediately(sqlite_store):
    """集成：写后缓存失效，下一次 get_effective 即时反映新值（Req 5.2）。"""
    store, _factory = sqlite_store

    await store.get_effective(_TENANT)  # 建立缓存
    await store.update(_TENANT, {"rrf_k": 80})
    reread = await store.get_effective(_TENANT)

    assert reread.rrf_k == 80


# ============================================================
# 2.3 进程内单例
# ============================================================


def test_get_retrieval_config_store_is_singleton():
    """get_retrieval_config_store 返回进程内单例（同一实例）。"""
    import app.retrieval.config as config_module

    # 重置单例以隔离测试
    config_module._store = None
    s1 = get_retrieval_config_store()
    s2 = get_retrieval_config_store()

    assert s1 is s2
    assert isinstance(s1, RetrievalConfigStore)
