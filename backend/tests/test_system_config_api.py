"""System_Config_API 租户化 + Platform_Config_API 测试（Task 20）

覆盖：
- 检索/分块配置按调用方租户隔离读写（GET/PUT/reset）：
  - 普通租户管理员（tenant_id=T）读写落到 T 行；另一租户不受影响（Req 6.6）。
  - 分块字段（parent_chunk_size 等）经 retrieval 分档读写，并在顶层兼容平铺（Req 6.1/6.4）。
  - 超级管理员经 X-Tenant-ID 指定租户读写生效（Req 6.7）；未指定 → 400（Req 6.8）。
  - 越界字段返回 422 且 store 未写（Req 3.2/3.3/3.4）。
  - api_key 通道被拒（沿用 require_tenant_admin，Req 6.5）。
- Platform_Config_API（超管 Load_Cache_TTL）：
  - GET/PUT load_cache_ttl 即时反映；越界 → 422 不写库（Req 17.1/17.3/17.4）。
  - 非超管（普通租户管理员）/ api_key 通道被拒（require_platform，Req 18.1/18.2）。
  - GET /config 响应不含 load_cache_ttl（Req 18.3）。
- 模型层：RetrievalConfigSection/Update 含分块三字段。

注：沿用进程隔离旁路鉴权模式（把 deps._resolve_identity 替换为可切换身份），并把
RetrievalConfigStore / PlatformConfigStore 单例临时指向 sqlite 内存库。

Feature: kb-retrieval-optimization
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

# get_settings() 启动期 fail-fast 需要 JWT_SECRET；导入 app.main 前置好。
os.environ.setdefault("JWT_SECRET", "system-config-test-secret-0123456789abcdef")

# Mock 重型依赖模块，避免 pymilvus 导入依赖问题（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.retrieval.config import (  # noqa: E402
    PLATFORM_FIELD_SPECS,
    RETRIEVAL_FIELD_SPECS,
    PlatformConfigStore,
    RetrievalConfigStore,
)
from app.schema.db import Base  # noqa: E402

# 17 个检索/分块字段名（六档单一事实源，含分块档三字段）。
_FIELD_NAMES = tuple(RETRIEVAL_FIELD_SPECS.keys())

# 两个租户 id（隔离测试用）。
_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


def _tenant_admin_identity(tenant_id: str = _TENANT_A):
    """普通租户管理员（带 tenant_id，role=admin，非超管）。"""
    from app.auth.constants import TenantRoleEnum
    from app.auth.identity import (
        IdentityContext,
        IdentitySourceEnum,
        OperationLevelEnum,
    )

    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.TENANT,
        tenant_id=tenant_id,
        user_id="u-admin",
        username="admin",
        is_super_admin=False,
        role=TenantRoleEnum.ADMIN,
    )


def _super_admin_identity():
    """超级管理员（platform/JWT，tenant_id=None）。"""
    from app.auth.identity import (
        IdentityContext,
        IdentitySourceEnum,
        OperationLevelEnum,
    )

    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.PLATFORM,
        tenant_id=None,
        is_super_admin=True,
        role=None,
    )


def _api_key_identity():
    """api_key 通道身份（应被 require_tenant_admin / require_platform 拒绝）。"""
    from app.auth.identity import (
        IdentityContext,
        IdentitySourceEnum,
        OperationLevelEnum,
    )

    return IdentityContext(
        source=IdentitySourceEnum.API_KEY,
        op_level=OperationLevelEnum.TENANT,
        tenant_id="tenant-x",
        api_key_id="key-1",
        is_super_admin=False,
        role=None,
    )


@pytest_asyncio.fixture
async def ctx():
    """httpx AsyncClient + 指向 sqlite 内存库的两个 Store 单例 + 可切换身份。

    - 建全部表（含 retrieval_configs / platform_configs）。
    - 进程隔离地把 RetrievalConfigStore / PlatformConfigStore 单例替换为绑定测试库的实例。
    - 进程隔离地把 deps._resolve_identity 替换为返回 holder["identity"]，默认普通租户管理员
      （tenant_id=tenant-a）；测试可改 holder["identity"] 切换为超管 / api_key。
    结束即还原，不污染其它测试模块。

    yield (client, retrieval_store, platform_store, holder)。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import app.retrieval.config as config_module

    retrieval_store = RetrievalConfigStore(factory)
    platform_store = PlatformConfigStore(factory)
    _orig_store = config_module._store
    _orig_platform = config_module._platform_store
    config_module._store = retrieval_store
    config_module._platform_store = platform_store

    # 配置变更审计经注入的 get_db_session 写入（其在调用时从 app.storage.database 取
    # async_session）。把进程级 async_session 指向测试库，使审计落到同一内存库可查。
    import app.storage.database as dbmod

    _orig_async_session = dbmod.async_session
    dbmod.async_session = factory

    from app.main import app
    import app.api.deps as deps

    _orig_resolve = deps._resolve_identity
    holder = {"identity": _tenant_admin_identity(_TENANT_A), "factory": factory}

    async def _fake_resolve(request, session):
        return holder["identity"], False

    deps._resolve_identity = _fake_resolve

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, retrieval_store, platform_store, holder

    deps._resolve_identity = _orig_resolve
    config_module._store = _orig_store
    config_module._platform_store = _orig_platform
    dbmod.async_session = _orig_async_session
    await engine.dispose()


# ============================================================
# GET /config —— 含 retrieval 分区且等于 get_effective(tenant)
# ============================================================


class TestGetConfigRetrievalSection:
    @pytest.mark.asyncio
    async def test_get_includes_retrieval_section_with_all_fields(self, ctx):
        """GET 响应含 retrieval 分区，17 个六档字段齐全（含分块档）。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "retrieval" in data
        for name in _FIELD_NAMES:
            assert name in data["retrieval"], f"缺少检索字段 {name}"

    @pytest.mark.asyncio
    async def test_get_retrieval_equals_effective_for_caller_tenant(self, ctx):
        """retrieval 分区每个字段等于调用方租户的 store.get_effective(tenant)。"""
        client, store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        data = resp.json()
        eff = await store.get_effective(_TENANT_A)
        for name in _FIELD_NAMES:
            assert data["retrieval"][name] == getattr(eff, name), f"{name} 与有效配置不一致"

    @pytest.mark.asyncio
    async def test_get_defaults_when_no_row(self, ctx):
        """无持久化行时，retrieval 分区为各字段 Safe_Default。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        data = resp.json()
        for name, spec in RETRIEVAL_FIELD_SPECS.items():
            assert data["retrieval"][name] == spec.default

    @pytest.mark.asyncio
    async def test_get_top_level_chunk_fields_from_retrieval(self, ctx):
        """顶层分块字段（兼容平铺）取自该租户 retrieval 有效值。"""
        client, store, _pstore, _holder = ctx
        await store.update(_TENANT_A, {"parent_chunk_size": 1300})
        resp = await client.get("/api/system/config")
        data = resp.json()
        assert data["parent_chunk_size"] == 1300
        assert data["retrieval"]["parent_chunk_size"] == 1300

    @pytest.mark.asyncio
    async def test_get_response_excludes_load_cache_ttl(self, ctx):
        """GET /config 响应不含 load_cache_ttl（顶层与 retrieval 分档都无，Req 18.3）。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        data = resp.json()
        assert "load_cache_ttl" not in data
        assert "load_cache_ttl" not in data["retrieval"]


# ============================================================
# PUT /config —— 嵌套 retrieval patch 即时反映（含分块字段）
# ============================================================


class TestPutConfigRetrieval:
    @pytest.mark.asyncio
    async def test_put_nested_retrieval_patch_reflected(self, ctx):
        """PUT 嵌套 retrieval patch 后，响应与后续 GET 均反映新值（落到调用方租户）。"""
        client, store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": 256, "rerank_threshold": 0.5}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["retrieval"]["recall_k"] == 256
        assert data["retrieval"]["rerank_threshold"] == 0.5
        # 未提交字段保持默认
        assert data["retrieval"]["rrf_k"] == RETRIEVAL_FIELD_SPECS["rrf_k"].default

        # store 即时反映（无需重启）
        eff = await store.get_effective(_TENANT_A)
        assert eff.recall_k == 256
        assert eff.rerank_threshold == 0.5

        # 再次 GET 仍是新值
        resp2 = await client.get("/api/system/config")
        assert resp2.json()["retrieval"]["recall_k"] == 256

    @pytest.mark.asyncio
    async def test_put_chunk_field_via_retrieval_tier(self, ctx):
        """分块字段经 retrieval 分档读写：retrieval 与顶层平铺均反映新值。"""
        client, store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"parent_chunk_size": 1200}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["retrieval"]["parent_chunk_size"] == 1200
        assert data["parent_chunk_size"] == 1200  # 顶层兼容平铺

        eff = await store.get_effective(_TENANT_A)
        assert eff.parent_chunk_size == 1200

    @pytest.mark.asyncio
    async def test_put_partial_patch_only_updates_submitted_fields(self, ctx):
        """仅提交的字段被更新，其余检索字段保持默认。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"hnsw_ef": 512}},
        )
        data = resp.json()
        assert data["retrieval"]["hnsw_ef"] == 512
        for name, spec in RETRIEVAL_FIELD_SPECS.items():
            if name == "hnsw_ef":
                continue
            assert data["retrieval"][name] == spec.default

    @pytest.mark.asyncio
    async def test_put_bool_field_update(self, ctx):
        """bool 字段 threshold_degradation_enabled 可被更新为 False。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"threshold_degradation_enabled": False}},
        )
        assert resp.status_code == 200
        assert resp.json()["retrieval"]["threshold_degradation_enabled"] is False

    @pytest.mark.asyncio
    async def test_put_llm_and_retrieval_in_same_response(self, ctx):
        """LLM 参数（内存 Settings）与检索参数（store/DB）可在同一 PUT 处理且同响应返回。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={
                "llm_model": "qwen2.5:14b",
                "retrieval": {"recall_k": 200},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_model"] == "qwen2.5:14b"
        assert data["retrieval"]["recall_k"] == 200

    @pytest.mark.asyncio
    async def test_put_without_retrieval_leaves_retrieval_untouched(self, ctx):
        """body.retrieval 为 None 时不动检索参数，保持现有行为。"""
        client, store, _pstore, _holder = ctx
        await store.update(_TENANT_A, {"recall_k": 300})
        resp = await client.put("/api/system/config", json={"llm_model": "m"})
        assert resp.status_code == 200
        assert resp.json()["retrieval"]["recall_k"] == 300


# ============================================================
# 租户隔离 —— 不同租户读写互不影响（Req 6.6）
# ============================================================


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_writes_isolated_between_tenants(self, ctx):
        """租户 A 写入不影响租户 B（B 仍为默认）。"""
        client, store, _pstore, holder = ctx
        # A 写
        holder["identity"] = _tenant_admin_identity(_TENANT_A)
        await client.put("/api/system/config", json={"retrieval": {"recall_k": 256}})
        # 切到 B 读
        holder["identity"] = _tenant_admin_identity(_TENANT_B)
        resp = await client.get("/api/system/config")
        assert resp.json()["retrieval"]["recall_k"] == RETRIEVAL_FIELD_SPECS["recall_k"].default
        # store 校验
        eff_a = await store.get_effective(_TENANT_A)
        eff_b = await store.get_effective(_TENANT_B)
        assert eff_a.recall_k == 256
        assert eff_b.recall_k == RETRIEVAL_FIELD_SPECS["recall_k"].default

    @pytest.mark.asyncio
    async def test_reset_only_affects_target_tenant(self, ctx):
        """reset 只重置目标租户，另一租户配置不受影响。"""
        client, store, _pstore, holder = ctx
        await store.update(_TENANT_A, {"recall_k": 256})
        await store.update(_TENANT_B, {"recall_k": 300})
        # A 触发 reset
        holder["identity"] = _tenant_admin_identity(_TENANT_A)
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 200
        # A 恢复默认，B 保持
        eff_a = await store.get_effective(_TENANT_A)
        eff_b = await store.get_effective(_TENANT_B)
        assert eff_a.recall_k == RETRIEVAL_FIELD_SPECS["recall_k"].default
        assert eff_b.recall_k == 300


# ============================================================
# 超管经 X-Tenant-ID 指定租户（Req 6.7/6.8）
# ============================================================


class TestSuperAdminTargetTenant:
    @pytest.mark.asyncio
    async def test_super_admin_with_header_reads_writes_target(self, ctx):
        """超管带 X-Tenant-ID 指定租户读写生效。"""
        client, store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": 222}},
            headers={"X-Tenant-ID": _TENANT_B},
        )
        assert resp.status_code == 200
        assert resp.json()["retrieval"]["recall_k"] == 222
        eff_b = await store.get_effective(_TENANT_B)
        assert eff_b.recall_k == 222

    @pytest.mark.asyncio
    async def test_super_admin_without_header_get_400(self, ctx):
        """超管未指定 X-Tenant-ID → GET 400。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.get("/api/system/config")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_super_admin_without_header_put_400(self, ctx):
        """超管未指定 X-Tenant-ID → PUT 400。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.put("/api/system/config", json={"retrieval": {"recall_k": 200}})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_super_admin_without_header_reset_400(self, ctx):
        """超管未指定 X-Tenant-ID → reset 400。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 400


# ============================================================
# PUT /config —— 越界字段 422 且不写库
# ============================================================


class TestPutConfigValidation:
    @pytest.mark.asyncio
    async def test_put_out_of_range_returns_422_with_field_info(self, ctx):
        """越界 retrieval 字段返回 422，body 含 field/value/allowed_range。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": 99999}},  # 超出 [1, 1000]
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list) and len(detail) >= 1
        item = detail[0]
        assert item["field"] == "recall_k"
        assert item["value"] == 99999
        assert "allowed_range" in item

    @pytest.mark.asyncio
    async def test_put_chunk_out_of_range_returns_422(self, ctx):
        """分块字段越界（parent_chunk_size=999999）返回 422 且 store 未写。"""
        client, store, _pstore, _holder = ctx
        spy = AsyncMock()
        store.update = spy  # type: ignore[method-assign]
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"parent_chunk_size": 999999}},  # 超出 [100, 8000]
        )
        assert resp.status_code == 422
        item = resp.json()["detail"][0]
        assert item["field"] == "parent_chunk_size"
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_put_out_of_range_does_not_call_store_update(self, ctx):
        """越界时 store.update 未被调用（不写库，Req 3.4）。"""
        client, store, _pstore, _holder = ctx
        spy = AsyncMock()
        store.update = spy  # type: ignore[method-assign]

        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"rerank_threshold": 5.0}},  # 超出 [0.0, 1.0]
        )
        assert resp.status_code == 422
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_put_out_of_range_keeps_previous_value(self, ctx):
        """越界更新被拒后，持久化值保持更新前不变。"""
        client, store, _pstore, _holder = ctx
        await store.update(_TENANT_A, {"recall_k": 500})

        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": -1}},  # 越界
        )
        assert resp.status_code == 422
        eff = await store.get_effective(_TENANT_A)
        assert eff.recall_k == 500  # 未被改动


# ============================================================
# POST /config/retrieval/reset —— 恢复默认（含分块档）
# ============================================================


class TestResetRetrievalConfig:
    @pytest.mark.asyncio
    async def test_reset_restores_all_defaults(self, ctx):
        """reset 后所有检索/分块字段恢复为各自 Safe_Default。"""
        client, store, _pstore, _holder = ctx
        await store.update(_TENANT_A, {"recall_k": 777, "mmr_lambda": 0.1, "parent_chunk_size": 1234})

        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 200
        data = resp.json()
        for name, spec in RETRIEVAL_FIELD_SPECS.items():
            assert data["retrieval"][name] == spec.default, f"{name} 未恢复默认"

        eff = await store.get_effective(_TENANT_A)
        for name, spec in RETRIEVAL_FIELD_SPECS.items():
            assert getattr(eff, name) == spec.default

    @pytest.mark.asyncio
    async def test_reset_returns_full_system_config(self, ctx):
        """reset 响应为完整 SystemConfigResponse（含 settings 字段 + retrieval 分区）。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.post("/api/system/config/retrieval/reset")
        data = resp.json()
        assert "llm_provider" in data
        assert "parent_chunk_size" in data
        assert "retrieval" in data


# ============================================================
# 鉴权 —— api_key 通道被拒（Req 6.5）
# ============================================================


class TestRetrievalConfigAuth:
    @pytest.mark.asyncio
    async def test_api_key_channel_rejected_on_get(self, ctx):
        """api_key 通道访问 GET /config 被拒（403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _api_key_identity()
        resp = await client.get("/api/system/config")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_channel_rejected_on_reset(self, ctx):
        """api_key 通道访问 reset 端点被拒（403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _api_key_identity()
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 403


# ============================================================
# Platform_Config_API —— 超管 Load_Cache_TTL（Req 17/18）
# ============================================================


class TestPlatformConfigApi:
    @pytest.mark.asyncio
    async def test_get_platform_config_default(self, ctx):
        """超管 GET 平台配置：无持久化行时返回 Safe_Default（30）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.get("/api/system/platform-config")
        assert resp.status_code == 200
        assert resp.json()["load_cache_ttl"] == PLATFORM_FIELD_SPECS["load_cache_ttl"].default

    @pytest.mark.asyncio
    async def test_put_platform_config_reflected(self, ctx):
        """超管 PUT load_cache_ttl 即时反映（写后回读 + store 校验）。"""
        client, _store, pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.put("/api/system/platform-config", json={"load_cache_ttl": 10})
        assert resp.status_code == 200
        assert resp.json()["load_cache_ttl"] == 10
        # store 即时反映
        eff = await pstore.get_effective()
        assert eff.load_cache_ttl == 10
        # 再次 GET 仍是新值
        resp2 = await client.get("/api/system/platform-config")
        assert resp2.json()["load_cache_ttl"] == 10

    @pytest.mark.asyncio
    async def test_put_platform_out_of_range_422_not_written(self, ctx):
        """越界 load_cache_ttl（5000 超出 [0,3600]）→ 422 且 store 未写。"""
        client, _store, pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        spy = AsyncMock()
        pstore.update = spy  # type: ignore[method-assign]
        resp = await client.put("/api/system/platform-config", json={"load_cache_ttl": 5000})
        assert resp.status_code == 422
        item = resp.json()["detail"][0]
        assert item["field"] == "load_cache_ttl"
        assert "allowed_range" in item
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tenant_admin_rejected_on_get(self, ctx):
        """普通租户管理员访问平台配置 GET 被拒（require_platform，403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _tenant_admin_identity(_TENANT_A)
        resp = await client.get("/api/system/platform-config")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_admin_rejected_on_put(self, ctx):
        """普通租户管理员访问平台配置 PUT 被拒（require_platform，403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _tenant_admin_identity(_TENANT_A)
        resp = await client.put("/api/system/platform-config", json={"load_cache_ttl": 10})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_rejected_on_platform_config(self, ctx):
        """api_key 通道访问平台配置被拒（403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _api_key_identity()
        resp = await client.get("/api/system/platform-config")
        assert resp.status_code == 403


# ============================================================
# 模型层单元测试
# ============================================================


class TestRetrievalConfigModels:
    def test_section_from_config_roundtrip(self):
        """RetrievalConfigSection.from_config 透传 RetrievalConfig 全部字段（含分块档）。"""
        from app.api.system import RetrievalConfigSection
        from app.retrieval.config import RetrievalConfig

        config = RetrievalConfig(recall_k=200, rerank_threshold=0.4, parent_chunk_size=1500)
        section = RetrievalConfigSection.from_config(config)
        for name in _FIELD_NAMES:
            assert getattr(section, name) == getattr(config, name)

    def test_section_has_chunk_fields(self):
        """RetrievalConfigSection 含分块档三字段。"""
        from app.api.system import RetrievalConfigSection

        for name in ("parent_chunk_size", "child_chunk_size", "chunk_overlap"):
            assert name in RetrievalConfigSection.model_fields

    def test_update_all_fields_optional_default_none(self):
        """RetrievalConfigUpdate 可全空构造，各字段默认 None（含分块档）。"""
        from app.api.system import RetrievalConfigUpdate

        upd = RetrievalConfigUpdate()
        for name in _FIELD_NAMES:
            assert getattr(upd, name) is None

    def test_update_has_chunk_fields(self):
        """RetrievalConfigUpdate 含分块档三字段。"""
        from app.api.system import RetrievalConfigUpdate

        for name in ("parent_chunk_size", "child_chunk_size", "chunk_overlap"):
            assert name in RetrievalConfigUpdate.model_fields

    def test_update_exclude_unset_only_keeps_submitted(self):
        """model_dump(exclude_unset, exclude_none) 仅保留提交字段。"""
        from app.api.system import RetrievalConfigUpdate

        upd = RetrievalConfigUpdate(parent_chunk_size=1200)
        patch = upd.model_dump(exclude_unset=True, exclude_none=True)
        assert patch == {"parent_chunk_size": 1200}


# ============================================================
# _diff_changes 纯函数单元测试
# ============================================================


class TestDiffChanges:
    def test_only_returns_changed_fields_sorted(self):
        """仅返回 patch 中与 before 不同的字段，按字段名排序稳定输出。"""
        from app.api.system import _diff_changes

        before = {"recall_k": 128, "rrf_k": 60, "hnsw_ef": 128}
        patch = {"rrf_k": 80, "recall_k": 128, "hnsw_ef": 256}
        changes = _diff_changes(before, patch)
        # recall_k 未变（128→128）应被剔除；其余按字段名排序
        assert changes == [
            {"field": "hnsw_ef", "old": 128, "new": 256},
            {"field": "rrf_k", "old": 60, "new": 80},
        ]

    def test_empty_when_all_equal(self):
        """patch 全部与现状相同 → 空列表（无变更）。"""
        from app.api.system import _diff_changes

        before = {"recall_k": 128, "rrf_k": 60}
        patch = {"recall_k": 128, "rrf_k": 60}
        assert _diff_changes(before, patch) == []

    def test_old_is_none_for_missing_before_key(self):
        """before 缺失该字段时 old 为 None。"""
        from app.api.system import _diff_changes

        assert _diff_changes({}, {"recall_k": 256}) == [
            {"field": "recall_k", "old": None, "new": 256}
        ]


# ============================================================
# 变更审计 + 变更明细回显（kb-retrieval-optimization 审计扩展）
# ============================================================


async def _count_audit(factory, action: str | None = None) -> int:
    """统计 audit_logs 行数（可按 action 过滤）。"""
    from sqlalchemy import func, select

    from app.schema.db import AuditLog

    stmt = select(func.count(AuditLog.id))
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    async with factory() as session:
        return (await session.scalar(stmt)) or 0


async def _latest_audit(factory, action: str):
    """取指定 action 的最近一条审计记录。"""
    from sqlalchemy import select

    from app.schema.db import AuditLog

    async with factory() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == action)
            )
        ).scalars().all()
        return rows[-1] if rows else None


class TestConfigChangeAudit:
    @pytest.mark.asyncio
    async def test_put_with_change_returns_changes_and_writes_audit(self, ctx):
        """PUT 有变更 → 响应 changes 含 {field,old,new}，且 audit_logs 新增一条 system.config_update。"""
        client, store, _pstore, holder = ctx
        factory = holder["factory"]
        # 现状 recall_k 默认 128 → 改为 256
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": 256}},
        )
        assert resp.status_code == 200
        changes = resp.json()["changes"]
        assert changes == [
            {"field": "recall_k", "old": RETRIEVAL_FIELD_SPECS["recall_k"].default, "new": 256}
        ]
        # 审计落库一条，detail.changes 正确
        assert await _count_audit(factory, "system.config_update") == 1
        row = await _latest_audit(factory, "system.config_update")
        assert row.target_type == "system_config"
        assert row.target_id == _TENANT_A
        assert row.detail["tenant_id"] == _TENANT_A
        assert row.detail["changes"] == [
            {"field": "recall_k", "old": RETRIEVAL_FIELD_SPECS["recall_k"].default, "new": 256}
        ]

    @pytest.mark.asyncio
    async def test_put_no_change_skips_store_and_audit(self, ctx):
        """PUT 提交值与现值相同 → changes 空、store.update 未被调用、无审计。"""
        client, store, _pstore, holder = ctx
        factory = holder["factory"]
        spy = AsyncMock()
        store.update = spy  # type: ignore[method-assign]
        # 提交默认值（与现状一致）
        default_recall = RETRIEVAL_FIELD_SPECS["recall_k"].default
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": default_recall}},
        )
        assert resp.status_code == 200
        assert resp.json()["changes"] == []
        spy.assert_not_awaited()
        assert await _count_audit(factory, "system.config_update") == 0

    @pytest.mark.asyncio
    async def test_put_out_of_range_writes_no_audit(self, ctx):
        """越界 422 时不写审计。"""
        client, _store, _pstore, holder = ctx
        factory = holder["factory"]
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"recall_k": 99999}},
        )
        assert resp.status_code == 422
        assert await _count_audit(factory, "system.config_update") == 0

    @pytest.mark.asyncio
    async def test_reset_with_change_writes_audit(self, ctx):
        """reset 有实际变更 → 写一条 system.config_reset，changes 反映回默认。"""
        client, store, _pstore, holder = ctx
        factory = holder["factory"]
        await store.update(_TENANT_A, {"recall_k": 256})
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 200
        changes = resp.json()["changes"]
        # recall_k 由 256 回到默认
        recall_change = [c for c in changes if c["field"] == "recall_k"]
        assert recall_change == [
            {"field": "recall_k", "old": 256, "new": RETRIEVAL_FIELD_SPECS["recall_k"].default}
        ]
        assert await _count_audit(factory, "system.config_reset") == 1
        row = await _latest_audit(factory, "system.config_reset")
        assert row.detail["tenant_id"] == _TENANT_A

    @pytest.mark.asyncio
    async def test_reset_no_change_skips_audit(self, ctx):
        """reset 时本就是全默认（无变更）→ changes 空且无审计。"""
        client, _store, _pstore, holder = ctx
        factory = holder["factory"]
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 200
        assert resp.json()["changes"] == []
        assert await _count_audit(factory, "system.config_reset") == 0

    @pytest.mark.asyncio
    async def test_get_config_has_empty_changes(self, ctx):
        """GET /config 响应 changes 为空列表（附加字段不破坏现有行为）。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        assert resp.json()["changes"] == []

    @pytest.mark.asyncio
    async def test_platform_put_with_change_writes_audit(self, ctx):
        """平台 TTL 改动 → 响应 changes 含 {field,old,new}，audit_logs 新增 platform.config_update。"""
        client, _store, _pstore, holder = ctx
        factory = holder["factory"]
        holder["identity"] = _super_admin_identity()
        default_ttl = PLATFORM_FIELD_SPECS["load_cache_ttl"].default
        resp = await client.put("/api/system/platform-config", json={"load_cache_ttl": 10})
        assert resp.status_code == 200
        assert resp.json()["changes"] == [
            {"field": "load_cache_ttl", "old": default_ttl, "new": 10}
        ]
        assert await _count_audit(factory, "platform.config_update") == 1
        row = await _latest_audit(factory, "platform.config_update")
        assert row.target_type == "platform_config"
        assert row.target_id == "global"
        assert row.detail["changes"] == [
            {"field": "load_cache_ttl", "old": default_ttl, "new": 10}
        ]

    @pytest.mark.asyncio
    async def test_platform_put_no_change_skips_audit(self, ctx):
        """平台 TTL 提交与现值相同 → changes 空、store.update 未调用、无审计。"""
        client, _store, pstore, holder = ctx
        factory = holder["factory"]
        holder["identity"] = _super_admin_identity()
        spy = AsyncMock()
        pstore.update = spy  # type: ignore[method-assign]
        default_ttl = PLATFORM_FIELD_SPECS["load_cache_ttl"].default
        resp = await client.put(
            "/api/system/platform-config", json={"load_cache_ttl": default_ttl}
        )
        assert resp.status_code == 200
        assert resp.json()["changes"] == []
        spy.assert_not_awaited()
        assert await _count_audit(factory, "platform.config_update") == 0


# ============================================================
# 上传限制配置（session-file-upload Task 13）
# ============================================================


class TestUploadLimitTenantConfig:
    """租户级上传限制三字段随 /api/system/config GET/PUT/reset 读写（Req 3.1/3.3/6.1/8.1/8.5）。"""

    @pytest.mark.asyncio
    async def test_get_includes_upload_limit_fields(self, ctx):
        """GET retrieval 分区含上传限制三字段且为 Safe_Default。"""
        client, _store, _pstore, _holder = ctx
        resp = await client.get("/api/system/config")
        assert resp.status_code == 200
        section = resp.json()["retrieval"]
        for name in ("upload_max_file_mb", "session_max_files", "session_chunk_cap"):
            assert section[name] == RETRIEVAL_FIELD_SPECS[name].default

    @pytest.mark.asyncio
    async def test_put_upload_limit_fields_reflected(self, ctx):
        """PUT 上传限制字段后响应与 store 即时反映新值。"""
        client, store, _pstore, _holder = ctx
        resp = await client.put(
            "/api/system/config",
            json={
                "retrieval": {
                    "upload_max_file_mb": 50,
                    "session_max_files": 10,
                    "session_chunk_cap": 12000,
                }
            },
        )
        assert resp.status_code == 200
        section = resp.json()["retrieval"]
        assert section["upload_max_file_mb"] == 50
        assert section["session_max_files"] == 10
        assert section["session_chunk_cap"] == 12000

        eff = await store.get_effective(_TENANT_A)
        assert eff.upload_max_file_mb == 50
        assert eff.session_max_files == 10
        assert eff.session_chunk_cap == 12000

    @pytest.mark.asyncio
    async def test_put_upload_limit_out_of_range_422_not_written(self, ctx):
        """越界上传限制字段（upload_max_file_mb=999 超出 [1,100]）→ 422 且 store 未写。"""
        client, store, _pstore, _holder = ctx
        spy = AsyncMock()
        store.update = spy  # type: ignore[method-assign]
        resp = await client.put(
            "/api/system/config",
            json={"retrieval": {"upload_max_file_mb": 999}},
        )
        assert resp.status_code == 422
        item = resp.json()["detail"][0]
        assert item["field"] == "upload_max_file_mb"
        assert "allowed_range" in item
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_restores_upload_limit_defaults(self, ctx):
        """reset 后上传限制三字段恢复 Safe_Default。"""
        client, store, _pstore, _holder = ctx
        await store.update(_TENANT_A, {"upload_max_file_mb": 80, "session_chunk_cap": 9000})
        resp = await client.post("/api/system/config/retrieval/reset")
        assert resp.status_code == 200
        section = resp.json()["retrieval"]
        for name in ("upload_max_file_mb", "session_max_files", "session_chunk_cap"):
            assert section[name] == RETRIEVAL_FIELD_SPECS[name].default


class TestUploadLimitPlatformConfig:
    """平台级单库 chunk 上限 + 会话天花板随 /api/system/platform-config 读写，GET 含内存推荐。

    Req 4.1/4.4/4.6/5.1/6.2/8.4。
    """

    @pytest.mark.asyncio
    async def test_get_platform_includes_upload_caps_and_recommendation(self, ctx):
        """超管 GET 平台配置含 kb_chunk_cap / session_chunk_ceiling 默认值与内存推荐块。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.get("/api/system/platform-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kb_chunk_cap"] == PLATFORM_FIELD_SPECS["kb_chunk_cap"].default
        assert data["session_chunk_ceiling"] == PLATFORM_FIELD_SPECS["session_chunk_ceiling"].default
        rec = data["memory_recommendation"]
        assert rec is not None
        for key in (
            "detected_memory_gb",
            "recommended_kb_chunk_cap",
            "safety_factor",
            "active_kbs_assumption",
            "assumption",
        ):
            assert key in rec

    @pytest.mark.asyncio
    async def test_put_platform_upload_caps_reflected(self, ctx):
        """超管 PUT kb_chunk_cap / session_chunk_ceiling 即时反映（写后回读 + store 校验）。"""
        client, _store, pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        resp = await client.put(
            "/api/system/platform-config",
            json={"kb_chunk_cap": 2000000, "session_chunk_ceiling": 50000},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kb_chunk_cap"] == 2000000
        assert data["session_chunk_ceiling"] == 50000

        eff = await pstore.get_effective()
        assert eff.kb_chunk_cap == 2000000
        assert eff.session_chunk_ceiling == 50000

        resp2 = await client.get("/api/system/platform-config")
        assert resp2.json()["kb_chunk_cap"] == 2000000

    @pytest.mark.asyncio
    async def test_put_platform_upload_cap_out_of_range_422_not_written(self, ctx):
        """越界 kb_chunk_cap（5000 低于 [10000, 1e7]）→ 422 且 store 未写。"""
        client, _store, pstore, holder = ctx
        holder["identity"] = _super_admin_identity()
        spy = AsyncMock()
        pstore.update = spy  # type: ignore[method-assign]
        resp = await client.put(
            "/api/system/platform-config", json={"kb_chunk_cap": 5000}
        )
        assert resp.status_code == 422
        item = resp.json()["detail"][0]
        assert item["field"] == "kb_chunk_cap"
        assert "allowed_range" in item
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tenant_admin_rejected_on_platform_upload_caps(self, ctx):
        """普通租户管理员不能读平台级上传上限（require_platform，403）。"""
        client, _store, _pstore, holder = ctx
        holder["identity"] = _tenant_admin_identity(_TENANT_A)
        resp = await client.get("/api/system/platform-config")
        assert resp.status_code == 403


class TestUploadLimitModels:
    """模型层：上传限制字段在 Section/Update 与平台模型中存在。"""

    def test_section_has_upload_limit_fields(self):
        from app.api.system import RetrievalConfigSection

        for name in ("upload_max_file_mb", "session_max_files", "session_chunk_cap"):
            assert name in RetrievalConfigSection.model_fields

    def test_update_has_upload_limit_fields_optional(self):
        from app.api.system import RetrievalConfigUpdate

        upd = RetrievalConfigUpdate()
        for name in ("upload_max_file_mb", "session_max_files", "session_chunk_cap"):
            assert name in RetrievalConfigUpdate.model_fields
            assert getattr(upd, name) is None

    def test_platform_models_have_upload_cap_fields(self):
        from app.api.system import PlatformConfigResponse, PlatformConfigUpdate

        for name in ("kb_chunk_cap", "session_chunk_ceiling"):
            assert name in PlatformConfigResponse.model_fields
            assert name in PlatformConfigUpdate.model_fields
        assert "memory_recommendation" in PlatformConfigResponse.model_fields
