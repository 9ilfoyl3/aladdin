"""UploadLimitResolver 生效限制求解的属性测试（任务 2.1）

被测对象：``app/session_upload/limits.py`` 的 :class:`UploadLimitResolver`。

求解规则（design C2 / requirements Req 6.3 / 6.10 / 6.11 / 9.1）：

- ``upload_max_file_bytes`` = 租户 ``upload_max_file_mb`` × 1024 × 1024。
- ``session_max_files``     = 租户 ``session_max_files``（无平台天花板）。
- ``session_chunk_cap``     = min(租户 ``session_chunk_cap``, 平台 ``session_chunk_ceiling``)。
- ``kb_chunk_cap``          = 平台 ``kb_chunk_cap``。
- ``tenant_id`` 为 None → 租户侧全部取安全默认（底层 Store 对 None 直接返回默认配置）。

底层 ``RetrievalConfigStore`` / ``PlatformConfigStore`` 的 ``get_effective`` 已通过
``effective_from_raw`` 保证返回值恒落在各自 Valid_Range 内（缺失 / None / 越界 / 错类型 →
Safe_Default）。本测试 mock 两个 Store 的 ``get_effective``，喂入「由 raw 经 effective_from_raw
得到的有效配置」（忠实镜像真实 Store 的产出），从而既覆盖租户/平台各项「缺失 / 合法 / 越界」
组合下的兜底，又验证 resolver 自身的「取 min / 字节换算 / 选租户 vs 平台」组合逻辑与
Valid_Range 约束。

Property 1（生效限制的取下界与兜底）：
*For any* 租户配置值（缺失 / 合法 / 越界）与平台配置值（缺失 / 合法 / 越界），
``UploadLimitResolver.resolve`` 产出的 ``UploadLimits`` SHALL 满足：文件大小 / 会话文件数 =
租户 Effective 值（越界 / 缺失回退安全默认）；``session_chunk_cap`` = min(租户 Effective,
平台 Effective)；``kb_chunk_cap`` = 平台 Effective；``tenant_id`` 为 None 时全部为安全默认。
每项恒落在其 Valid_Range 内。

Feature: session-file-upload
Validates: Requirements 6.3, 6.10, 6.11, 9.1
"""

import asyncio
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app.retrieval.config import (
    PLATFORM_FIELD_SPECS,
    RETRIEVAL_FIELD_SPECS,
    PlatformConfig,
    RetrievalConfig,
)
from app.session_upload.limits import UploadLimitResolver, UploadLimits

# 字节换算（与被测模块 _BYTES_PER_MB 一致）。
_BYTES_PER_MB = 1024 * 1024

# 本特性涉及的租户级 / 平台级字段（resolver 求解所依赖的）。
_TENANT_FIELDS = ("upload_max_file_mb", "session_max_files", "session_chunk_cap")
_PLATFORM_FIELDS = ("kb_chunk_cap", "session_chunk_ceiling")

# 哨兵：表示该字段在 raw 中「缺失」（不放入 dict）。
_MISSING = object()


# ============================================================
# 忠实镜像真实 Store 语义的 fake：仅把 DB 行换成预先构造好的有效配置
# ============================================================


class _FakeRetrievalStore:
    """镜像 RetrievalConfigStore.get_effective 语义（tenant_id 为 None → 全默认）。"""

    def __init__(self, effective_cfg: RetrievalConfig):
        self._cfg = effective_cfg

    async def get_effective(self, tenant_id):
        # 真实 Store：tenant_id 为 None 时直接返回全 Safe_Default，不读 DB。
        if tenant_id is None:
            return RetrievalConfig()
        return self._cfg


class _FakePlatformStore:
    """镜像 PlatformConfigStore.get_effective 语义（与 tenant_id 无关，全局单行）。"""

    def __init__(self, effective_cfg: PlatformConfig):
        self._cfg = effective_cfg

    async def get_effective(self):
        return self._cfg


def _resolve_with(retrieval_cfg, platform_cfg, tenant_id):
    """在 mock 两个 Store 单例后调用 resolver.resolve，返回 UploadLimits。"""

    async def _run():
        with (
            patch(
                "app.session_upload.limits.get_retrieval_config_store",
                return_value=_FakeRetrievalStore(retrieval_cfg),
            ),
            patch(
                "app.session_upload.limits.get_platform_config_store",
                return_value=_FakePlatformStore(platform_cfg),
            ),
        ):
            return await UploadLimitResolver().resolve(tenant_id)

    return asyncio.run(_run())


# ============================================================
# 生成器：为单个 int 字段生成 {缺失, None, 区间内, 区间外, 错类型} 取值
# ============================================================


@st.composite
def _int_field_value(draw, spec):
    """返回 (value_or_sentinel, is_valid)。is_valid 表示应被原样保留的合法区间内 int。"""
    lo, hi = spec.lo, spec.hi
    choice = draw(
        st.sampled_from(["missing", "none", "in_range", "below", "above", "wrong_type"])
    )
    if choice == "missing":
        return _MISSING, False
    if choice == "none":
        return None, False
    if choice == "in_range":
        return draw(st.integers(min_value=lo, max_value=hi)), True
    if choice == "below":
        return draw(st.integers(max_value=lo - 1)), False
    if choice == "above":
        return draw(st.integers(min_value=hi + 1)), False
    # wrong_type：int 字段填 float / bool / str（bool 是 int 子类，必须判非法）
    return draw(st.sampled_from([1.5, True, False, "abc", 3.0])), False


def _build_raw(draw, specs, field_names):
    """逐字段抽样，返回 (raw_dict, validity)；validity[name] = (raw_value_or_missing, is_valid)。"""
    raw: dict = {}
    validity: dict = {}
    for name in field_names:
        value, is_valid = draw(_int_field_value(specs[name]))
        validity[name] = (value, is_valid)
        if value is not _MISSING:
            raw[name] = value
    return raw, validity


@st.composite
def _configs(draw):
    """生成 (retrieval_cfg, platform_cfg, tenant_validity, platform_validity)。

    cfg 经 effective_from_raw 构造（忠实镜像真实 Store 产出，恒落在 Valid_Range 内）。
    validity 记录每个字段是否为「合法原样保留值」，供断言兜底用。
    """
    tenant_raw, tenant_validity = _build_raw(draw, RETRIEVAL_FIELD_SPECS, _TENANT_FIELDS)
    platform_raw, platform_validity = _build_raw(draw, PLATFORM_FIELD_SPECS, _PLATFORM_FIELDS)
    retrieval_cfg = RetrievalConfig.effective_from_raw(tenant_raw)
    platform_cfg = PlatformConfig.effective_from_raw(platform_raw)
    return retrieval_cfg, platform_cfg, tenant_validity, platform_validity


def _assert_in_valid_ranges(limits: UploadLimits) -> None:
    """断言 UploadLimits 各项恒落在各自 Valid_Range 内（Req 9.1）。"""
    mb_spec = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"]
    files_spec = RETRIEVAL_FIELD_SPECS["session_max_files"]
    sess_spec = RETRIEVAL_FIELD_SPECS["session_chunk_cap"]
    kb_spec = PLATFORM_FIELD_SPECS["kb_chunk_cap"]

    assert mb_spec.lo * _BYTES_PER_MB <= limits.upload_max_file_bytes <= mb_spec.hi * _BYTES_PER_MB
    assert files_spec.lo <= limits.session_max_files <= files_spec.hi
    # session_chunk_cap = min(租户[500,20000], 平台[500,100000]) → 恒落在租户区间内。
    assert sess_spec.lo <= limits.session_chunk_cap <= sess_spec.hi
    assert kb_spec.lo <= limits.kb_chunk_cap <= kb_spec.hi


# ============================================================
# Property 1：生效限制的取下界与兜底
# ============================================================


@settings(max_examples=100)
@given(data=_configs())
def test_property_resolve_min_and_fallback(data):
    """Feature: session-file-upload, Property 1: 生效限制的取下界与兜底

    For any 租户配置值（缺失/合法/越界）与平台配置值（缺失/合法/越界）：
    - upload_max_file_bytes = 租户 upload_max_file_mb × 1024 × 1024；
    - session_max_files     = 租户 session_max_files（无平台天花板）；
    - session_chunk_cap     = min(租户 session_chunk_cap, 平台 session_chunk_ceiling)；
    - kb_chunk_cap          = 平台 kb_chunk_cap；
    - 越界/缺失字段在底层已回退 Safe_Default，故 resolver 产出亦为兜底值；
    - 每项恒落在其 Valid_Range 内。

    Validates: Requirements 6.3, 6.10, 6.11, 9.1
    """
    retrieval_cfg, platform_cfg, tenant_validity, _platform_validity = data

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id="tenant-A")

    # 文件大小：租户值 × MB（字节换算）
    assert limits.upload_max_file_bytes == retrieval_cfg.upload_max_file_mb * _BYTES_PER_MB
    # 会话文件数：租户值（无平台天花板）
    assert limits.session_max_files == retrieval_cfg.session_max_files
    # 会话 chunk：取下界（租户 vs 平台天花板）
    assert limits.session_chunk_cap == min(
        retrieval_cfg.session_chunk_cap, platform_cfg.session_chunk_ceiling
    )
    assert limits.session_chunk_cap <= retrieval_cfg.session_chunk_cap
    assert limits.session_chunk_cap <= platform_cfg.session_chunk_ceiling
    # 单库 chunk：平台值
    assert limits.kb_chunk_cap == platform_cfg.kb_chunk_cap

    # 兜底：越界/缺失的租户字段，resolver 产出应等于其 Safe_Default
    mb_default = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default
    files_default = RETRIEVAL_FIELD_SPECS["session_max_files"].default
    _raw_mb, mb_valid = tenant_validity["upload_max_file_mb"]
    _raw_files, files_valid = tenant_validity["session_max_files"]
    if not mb_valid:
        assert limits.upload_max_file_bytes == mb_default * _BYTES_PER_MB
    if not files_valid:
        assert limits.session_max_files == files_default

    # Valid_Range 约束（Req 9.1）
    _assert_in_valid_ranges(limits)


@settings(max_examples=100)
@given(data=_configs())
def test_property_resolve_tenant_none_all_defaults(data):
    """Feature: session-file-upload, Property 1（tenant_id=None 切片）

    For any 租户配置（任意取值），当 tenant_id 为 None 时，租户侧三项 SHALL 全部取安全默认
    （底层 Store 对 None 直接返回默认配置，不读持久化值）；平台侧仍取平台 Effective。
    每项恒落在其 Valid_Range 内。

    Validates: Requirements 9.1
    """
    retrieval_cfg, platform_cfg, _tenant_validity, _platform_validity = data

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id=None)

    mb_default = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default
    files_default = RETRIEVAL_FIELD_SPECS["session_max_files"].default
    sess_default = RETRIEVAL_FIELD_SPECS["session_chunk_cap"].default

    # 租户侧全部取安全默认（与生成的 retrieval_cfg 无关）
    assert limits.upload_max_file_bytes == mb_default * _BYTES_PER_MB
    assert limits.session_max_files == files_default
    # 会话 chunk = min(租户默认, 平台 Effective)
    assert limits.session_chunk_cap == min(sess_default, platform_cfg.session_chunk_ceiling)
    # 平台侧仍取平台 Effective
    assert limits.kb_chunk_cap == platform_cfg.kb_chunk_cap

    _assert_in_valid_ranges(limits)


# ============================================================
# 边界示例单元测试（锚定关键端点，补充属性测试）
# ============================================================


def test_resolve_all_defaults_snapshot():
    """两侧均为默认配置时，UploadLimits 等于各项 Safe_Default 组合。"""
    limits = _resolve_with(RetrievalConfig(), PlatformConfig(), tenant_id="tenant-A")

    assert limits.upload_max_file_bytes == 10 * _BYTES_PER_MB
    assert limits.session_max_files == 5
    assert limits.session_chunk_cap == min(6000, 20000)  # = 6000
    assert limits.kb_chunk_cap == 1000000


def test_resolve_session_chunk_cap_takes_platform_ceiling_when_lower():
    """租户会话 chunk 大于平台天花板时，生效取平台天花板（取下界，Req 6.11）。"""
    retrieval_cfg = RetrievalConfig.effective_from_raw({"session_chunk_cap": 20000})
    platform_cfg = PlatformConfig.effective_from_raw({"session_chunk_ceiling": 500})

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id="tenant-A")

    assert limits.session_chunk_cap == 500


def test_resolve_session_chunk_cap_takes_tenant_when_lower():
    """租户会话 chunk 小于平台天花板时，生效取租户值（取下界）。"""
    retrieval_cfg = RetrievalConfig.effective_from_raw({"session_chunk_cap": 500})
    platform_cfg = PlatformConfig.effective_from_raw({"session_chunk_ceiling": 100000})

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id="tenant-A")

    assert limits.session_chunk_cap == 500


def test_resolve_degrades_to_defaults_on_store_failure():
    """任一 Store 读取异常时降级安全默认（不放行无限制，Req 9.2）。"""

    class _BoomRetrievalStore:
        async def get_effective(self, tenant_id):
            raise RuntimeError("DB down")

    class _BoomPlatformStore:
        async def get_effective(self):
            raise RuntimeError("DB down")

    async def _run():
        with (
            patch(
                "app.session_upload.limits.get_retrieval_config_store",
                return_value=_BoomRetrievalStore(),
            ),
            patch(
                "app.session_upload.limits.get_platform_config_store",
                return_value=_BoomPlatformStore(),
            ),
        ):
            return await UploadLimitResolver().resolve("tenant-A")

    limits = asyncio.run(_run())

    # 降级为全 Safe_Default 组合
    assert limits.upload_max_file_bytes == 10 * _BYTES_PER_MB
    assert limits.session_max_files == 5
    assert limits.session_chunk_cap == min(6000, 20000)
    assert limits.kb_chunk_cap == 1000000
