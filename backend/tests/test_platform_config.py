"""PlatformConfig / PlatformConfigStore 测试（任务 16.5 / 16.6）

覆盖：
- 16.5 属性测试 P11：平台 TTL 兜底与校验
  （effective_from_raw 在 [0,3600] 内保留否则回退 30；validate_platform_patch
   当且仅当越界/类型错时返回违规项，含 allowed_range）。
- 16.6 单元测试（平台侧）：写读往返 + DB 读失败降级（TTL=30）+ 进程单例。

Feature: kb-retrieval-optimization
"""

import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("JWT_SECRET", "platform-config-test-0123456789abcdef")
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
    PLATFORM_FIELD_SPECS,
    PlatformConfig,
    PlatformConfigStore,
    get_platform_config_store,
    validate_platform_patch,
)
from app.schema.db import Base, PlatformConfigRow  # noqa: E402

_SPEC = PLATFORM_FIELD_SPECS["load_cache_ttl"]
_DEFAULT_TTL = _SPEC.default  # 30
_LO, _HI = _SPEC.lo, _SPEC.hi  # 0, 3600


# ============================================================
# 字段规格单元测试
# ============================================================


def test_platform_field_spec_values():
    """PLATFORM_FIELD_SPECS.load_cache_ttl 的 default/lo/hi 等于规定值（30/[0,3600]）。"""
    assert _DEFAULT_TTL == 30
    assert _LO == 0
    assert _HI == 3600


def test_default_platform_config_matches_spec():
    """PlatformConfig 默认实例 load_cache_ttl 等于 Safe_Default。"""
    assert PlatformConfig().load_cache_ttl == _DEFAULT_TTL


# ============================================================
# 16.5 属性测试 P11：平台 TTL 兜底与校验
# ============================================================


@st.composite
def _ttl_value(draw):
    """生成 load_cache_ttl 多样取值，返回 (value_or_missing, is_valid)。

    取值类别：缺失 / None / 区间内 [0,3600] / 越界（负/超上界）/ 错类型。
    is_valid 表示「应被原样保留的合法值」。
    """
    _MISSING = "__missing__"
    choice = draw(st.sampled_from(["missing", "none", "in_range", "below", "above", "wrong_type"]))
    if choice == "missing":
        return _MISSING, False
    if choice == "none":
        return None, False
    if choice == "in_range":
        return draw(st.integers(min_value=_LO, max_value=_HI)), True
    if choice == "below":
        return draw(st.integers(max_value=_LO - 1)), False
    if choice == "above":
        return draw(st.integers(min_value=_HI + 1)), False
    # wrong_type：int 字段填 float / bool / str
    return draw(st.sampled_from([1.5, True, False, "30", "abc"])), False


@settings(max_examples=100, deadline=None)
@given(data=_ttl_value())
def test_property_platform_effective_and_validate(data):
    """Feature: kb-retrieval-optimization, Property 11: Load_Cache_TTL 平台配置兜底与校验

    For any load_cache_ttl 原始值（缺失/None/越界/合法/错类型）：
    - PlatformConfig.effective_from_raw 在 [0,3600] 内原样保留、否则回退 30；
    - validate_platform_patch 当且仅当越界/类型错时返回违规项（含 allowed_range）。

    Validates: Requirements 17.1, 17.4, 17.5
    """
    value, is_valid = data
    _MISSING = "__missing__"

    # ---- effective_from_raw 兜底 ----
    raw = None if value is _MISSING else {"load_cache_ttl": value}
    eff = PlatformConfig.effective_from_raw(raw)
    if is_valid:
        assert eff.load_cache_ttl == value
    else:
        assert eff.load_cache_ttl == _DEFAULT_TTL
    # 结果恒落在 Valid_Range 内
    assert _LO <= eff.load_cache_ttl <= _HI

    # ---- validate_platform_patch 校验（缺失不进 patch，不校验）----
    if value is not _MISSING:
        patch = {"load_cache_ttl": value}
        errors = validate_platform_patch(patch)
        if is_valid:
            assert errors == []
        else:
            # 非法值（None / 越界 / 错类型）当且仅当各返回一条违规项，含 allowed_range
            assert len(errors) == 1
            assert errors[0].field == "load_cache_ttl"
            assert errors[0].allowed_range == "[0, 3600]"


def test_validate_platform_patch_ignores_unknown_fields():
    """patch 中未知字段被忽略，不计入错误。"""
    assert validate_platform_patch({"unknown": 999, "load_cache_ttl": 30}) == []


def test_validate_platform_patch_empty():
    """空 patch 返回空错误列表。"""
    assert validate_platform_patch({}) == []


def test_platform_effective_from_none_no_warning(caplog):
    """raw 为 None（未配置态）时不刷兜底日志，返回默认。"""
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        eff = PlatformConfig.effective_from_raw(None)
    assert eff.load_cache_ttl == _DEFAULT_TTL
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ============================================================
# fake 异步会话上下文
# ============================================================


class _FakeAsyncSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


# ============================================================
# 16.6 单元测试（平台侧）：写读往返 + DB 失败降级
# ============================================================


@pytest_asyncio.fixture
async def sqlite_platform_store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = PlatformConfigStore(factory)
    try:
        yield store, factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_platform_update_roundtrip(sqlite_platform_store):
    """update→get_effective 往返：首次 UPSERT 建行，写后即时反映新值。"""
    store, factory = sqlite_platform_store

    # 初始无行 → 默认 30
    assert (await store.get_effective()).load_cache_ttl == _DEFAULT_TTL
    assert await store.get_load_cache_ttl() == _DEFAULT_TTL

    eff = await store.update({"load_cache_ttl": 10})
    assert eff.load_cache_ttl == 10
    assert await store.get_load_cache_ttl() == 10

    # 直接读 DB 验证持久化（单行 id="global"）
    async with factory() as session:
        row = await session.get(PlatformConfigRow, "global")
    assert row is not None
    assert row.id == "global"
    assert row.load_cache_ttl == 10


@pytest.mark.asyncio
async def test_platform_get_effective_degrades_on_db_failure(caplog):
    """DB 读失败时 get_effective 不抛错，降级返回默认（TTL=30）并记 WARNING（Req 17.6）。"""
    session = MagicMock()
    session.get = AsyncMock(side_effect=RuntimeError("db down"))
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = PlatformConfigStore(factory)
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = await store.get_effective()
        ttl = await store.get_load_cache_ttl()

    assert config.load_cache_ttl == _DEFAULT_TTL
    assert ttl == _DEFAULT_TTL
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("平台配置" in w.getMessage() or "降级" in w.getMessage() for w in warnings)


@pytest.mark.asyncio
async def test_platform_get_effective_uses_cache_within_ttl():
    """TTL 内二次 get_effective 命中缓存，不重复读 DB。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock(return_value=_FakeAsyncSessionCtx(session))

    store = PlatformConfigStore(factory)
    await store.get_effective()
    await store.get_effective()

    assert session.get.await_count == 1


@pytest.mark.asyncio
async def test_platform_update_then_get_effective_reflects_immediately(sqlite_platform_store):
    """写后缓存失效，下一次 get_effective 即时反映新值（Req 17.3）。"""
    store, _factory = sqlite_platform_store

    await store.get_effective()  # 建立缓存
    await store.update({"load_cache_ttl": 120})
    assert (await store.get_effective()).load_cache_ttl == 120


def test_get_platform_config_store_is_singleton():
    """get_platform_config_store 返回进程内单例（同一实例）。"""
    import app.retrieval.config as config_module

    config_module._platform_store = None
    s1 = get_platform_config_store()
    s2 = get_platform_config_store()

    assert s1 is s2
    assert isinstance(s1, PlatformConfigStore)
