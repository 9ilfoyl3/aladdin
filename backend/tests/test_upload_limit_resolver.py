"""UploadLimitResolver 生效限制求解的属性测试

被测对象：``app/session_upload/limits.py`` 的 :class:`UploadLimitResolver`。

求解规则（会话专属限额已废弃，临时文件 = 会话级 KB，与正式 KB 共用 chunk 上限）：

- ``upload_max_file_bytes`` = 租户 ``upload_max_file_mb`` × 1024 × 1024（会话与 KB 共用）。
- ``kb_chunk_cap``          = 平台 ``kb_chunk_cap``（会话与 KB 共用）。
- ``tenant_id`` 为 None → 租户侧取安全默认。
- 任一 Store 异常 → 降级安全默认（不放行无限制）。

Feature: session-file-upload
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

_BYTES_PER_MB = 1024 * 1024

_TENANT_FIELDS = ("upload_max_file_mb",)
_PLATFORM_FIELDS = ("kb_chunk_cap",)

_MISSING = object()


# ============================================================
# 忠实镜像真实 Store 语义的 fake
# ============================================================


class _FakeRetrievalStore:
    """镜像 RetrievalConfigStore.get_effective 语义（tenant_id 为 None → 全默认）。"""

    def __init__(self, effective_cfg: RetrievalConfig):
        self._cfg = effective_cfg

    async def get_effective(self, tenant_id):
        if tenant_id is None:
            return RetrievalConfig()
        return self._cfg


class _FakePlatformStore:
    """镜像 PlatformConfigStore.get_effective 语义（全局单行）。"""

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
# 生成器
# ============================================================


@st.composite
def _int_field_value(draw, spec):
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
    return draw(st.sampled_from([1.5, True, False, "abc", 3.0])), False


def _build_raw(draw, specs, field_names):
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
    tenant_raw, tenant_validity = _build_raw(draw, RETRIEVAL_FIELD_SPECS, _TENANT_FIELDS)
    platform_raw, platform_validity = _build_raw(draw, PLATFORM_FIELD_SPECS, _PLATFORM_FIELDS)
    retrieval_cfg = RetrievalConfig.effective_from_raw(tenant_raw)
    platform_cfg = PlatformConfig.effective_from_raw(platform_raw)
    return retrieval_cfg, platform_cfg, tenant_validity, platform_validity


def _assert_in_valid_ranges(limits: UploadLimits) -> None:
    mb_spec = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"]
    kb_spec = PLATFORM_FIELD_SPECS["kb_chunk_cap"]
    assert mb_spec.lo * _BYTES_PER_MB <= limits.upload_max_file_bytes <= mb_spec.hi * _BYTES_PER_MB
    assert kb_spec.lo <= limits.kb_chunk_cap <= kb_spec.hi


# ============================================================
# Property 1：生效限制求解与兜底
# ============================================================


@settings(max_examples=100)
@given(data=_configs())
def test_property_resolve_and_fallback(data):
    """Feature: session-file-upload, Property 1: 生效限制求解与兜底

    For any 租户/平台配置（缺失/合法/越界）：
    - upload_max_file_bytes = 租户 upload_max_file_mb × 1024 × 1024；
    - kb_chunk_cap          = 平台 kb_chunk_cap；
    - 越界/缺失字段在底层已回退 Safe_Default；
    - 每项恒落在其 Valid_Range 内。
    """
    retrieval_cfg, platform_cfg, tenant_validity, _platform_validity = data

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id="tenant-A")

    assert limits.upload_max_file_bytes == retrieval_cfg.upload_max_file_mb * _BYTES_PER_MB
    assert limits.kb_chunk_cap == platform_cfg.kb_chunk_cap

    mb_default = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default
    _raw_mb, mb_valid = tenant_validity["upload_max_file_mb"]
    if not mb_valid:
        assert limits.upload_max_file_bytes == mb_default * _BYTES_PER_MB

    _assert_in_valid_ranges(limits)


@settings(max_examples=100)
@given(data=_configs())
def test_property_resolve_tenant_none_defaults(data):
    """Feature: session-file-upload, Property 1（tenant_id=None 切片）

    tenant_id 为 None 时租户侧取安全默认；平台侧仍取平台 Effective。
    """
    retrieval_cfg, platform_cfg, _tenant_validity, _platform_validity = data

    limits = _resolve_with(retrieval_cfg, platform_cfg, tenant_id=None)

    mb_default = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default
    assert limits.upload_max_file_bytes == mb_default * _BYTES_PER_MB
    assert limits.kb_chunk_cap == platform_cfg.kb_chunk_cap

    _assert_in_valid_ranges(limits)


# ============================================================
# 边界示例单元测试
# ============================================================


def test_resolve_all_defaults_snapshot():
    """两侧均为默认配置时，UploadLimits 等于各项 Safe_Default 组合。"""
    limits = _resolve_with(RetrievalConfig(), PlatformConfig(), tenant_id="tenant-A")

    assert limits.upload_max_file_bytes == 10 * _BYTES_PER_MB
    assert limits.kb_chunk_cap == 1000000


def test_resolve_degrades_to_defaults_on_store_failure():
    """任一 Store 读取异常时降级安全默认（不放行无限制）。"""

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

    assert limits.upload_max_file_bytes == 10 * _BYTES_PER_MB
    assert limits.kb_chunk_cap == 1000000
