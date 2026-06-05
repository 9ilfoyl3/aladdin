"""会话文件上传：上传限制配置字段的兜底与校验属性测试

覆盖现行的上传限制配置字段（会话专属限额已废弃，临时文件统一由 kb_chunk_cap 约束）：

租户级（RETRIEVAL_FIELD_SPECS / RetrievalConfig）：
- ``upload_max_file_mb``     默认 10，范围 [1, 100]

平台级（PLATFORM_FIELD_SPECS / PlatformConfig）：
- ``kb_chunk_cap``           默认 1000000，范围 [10000, 10000000]（单库/单会话共用）

Property 2（配置字段的逐字段兜底）：
*For any* 持久化原始配置 dict（字段任意缺失 / None / 合法 / 越界 / 错类型），
``effective_from_raw`` 产出值 SHALL 满足：区间内原样保留，缺失 / 越界 / 错类型替换为
Safe_Default。

Property 3（配置字段的范围校验）：
*For any* 提交 patch，``validate_patch`` / ``validate_platform_patch`` 返回的违规字段集合
SHALL 恰好等于越界字段集合，每项含字段名与允许范围；全合法返回空。

Feature: session-file-upload
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.retrieval.config import (
    KIND_INT,
    PLATFORM_FIELD_SPECS,
    RETRIEVAL_FIELD_SPECS,
    FieldError,
    PlatformConfig,
    RetrievalConfig,
    validate_patch,
    validate_platform_patch,
)

# ============================================================
# 上传限制相关字段（会话专属限额已废弃，仅余下列字段）
# ============================================================

# 租户级上传字段（写入 RETRIEVAL_FIELD_SPECS / RetrievalConfig）
UPLOAD_TENANT_FIELDS = ("upload_max_file_mb",)
# 平台级上传字段（写入 PLATFORM_FIELD_SPECS / PlatformConfig）
UPLOAD_PLATFORM_FIELDS = ("kb_chunk_cap",)

# 期望规格：字段名 -> (default, lo, hi)。
EXPECTED_TENANT_SPECS = {
    "upload_max_file_mb": (10, 1, 100),
}
EXPECTED_PLATFORM_SPECS = {
    "kb_chunk_cap": (1000000, 10000, 10000000),
}

# 哨兵：表示该字段在 raw / patch 中"缺失"（不放入 dict）。
_MISSING = object()


# ============================================================
# 字段规格单元测试
# ============================================================


@pytest.mark.parametrize("field_name", list(EXPECTED_TENANT_SPECS.keys()))
def test_tenant_field_spec(field_name):
    """租户级上传字段的 default / (lo, hi) / kind 等于规定值。"""
    expected_default, expected_lo, expected_hi = EXPECTED_TENANT_SPECS[field_name]
    spec = RETRIEVAL_FIELD_SPECS[field_name]
    assert spec.default == expected_default
    assert spec.lo == expected_lo
    assert spec.hi == expected_hi
    assert spec.kind == KIND_INT


@pytest.mark.parametrize("field_name", list(EXPECTED_PLATFORM_SPECS.keys()))
def test_platform_field_spec(field_name):
    """平台级上传字段的 default / (lo, hi) / kind 等于规定值。"""
    expected_default, expected_lo, expected_hi = EXPECTED_PLATFORM_SPECS[field_name]
    spec = PLATFORM_FIELD_SPECS[field_name]
    assert spec.default == expected_default
    assert spec.lo == expected_lo
    assert spec.hi == expected_hi
    assert spec.kind == KIND_INT


def test_deprecated_session_fields_removed():
    """废弃的会话专属限额字段不应再登记到任何单一事实源。"""
    for name in ("session_max_files", "session_chunk_cap"):
        assert name not in RETRIEVAL_FIELD_SPECS
        assert name not in RetrievalConfig.model_fields
    assert "session_chunk_ceiling" not in PLATFORM_FIELD_SPECS
    assert "session_chunk_ceiling" not in PlatformConfig.model_fields


# ============================================================
# 生成器：为单个 int 字段生成 {缺失, None, 区间内, 区间外, 错类型} 的取值
# ============================================================


@st.composite
def _int_field_value(draw, spec):
    """为单个 int 字段生成多样化取值，返回 (value_or_sentinel, is_valid)。"""
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
    """对给定字段子集逐字段抽样，返回 (raw_dict, validity)。"""
    raw: dict = {}
    validity: dict = {}
    for name in field_names:
        value, is_valid = draw(_int_field_value(specs[name]))
        validity[name] = (value, is_valid)
        if value is not _MISSING:
            raw[name] = value
    return raw, validity


@st.composite
def _tenant_raw(draw):
    return _build_raw(draw, RETRIEVAL_FIELD_SPECS, UPLOAD_TENANT_FIELDS)


@st.composite
def _platform_raw(draw):
    return _build_raw(draw, PLATFORM_FIELD_SPECS, UPLOAD_PLATFORM_FIELDS)


# ============================================================
# Property 2：配置字段的逐字段兜底
# ============================================================


@settings(max_examples=100)
@given(data=_tenant_raw())
def test_property_tenant_field_fallback(data):
    """Feature: session-file-upload, Property 2: 租户上传字段兜底。"""
    raw, validity = data
    config = RetrievalConfig.effective_from_raw(raw)

    for name in UPLOAD_TENANT_FIELDS:
        spec = RETRIEVAL_FIELD_SPECS[name]
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)
        if is_valid:
            assert effective_value == raw_value
        else:
            assert effective_value == spec.default
        assert spec.lo <= effective_value <= spec.hi


@settings(max_examples=100)
@given(data=_platform_raw())
def test_property_platform_field_fallback(data):
    """Feature: session-file-upload, Property 2: 平台上传字段兜底。"""
    raw, validity = data
    config = PlatformConfig.effective_from_raw(raw)

    for name in UPLOAD_PLATFORM_FIELDS:
        spec = PLATFORM_FIELD_SPECS[name]
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)
        if is_valid:
            assert effective_value == raw_value
        else:
            assert effective_value == spec.default
        assert spec.lo <= effective_value <= spec.hi


# ============================================================
# Property 3：配置字段的范围校验
# ============================================================


def _build_patch(draw, specs, field_names):
    """对给定字段子集生成 (patch, expected_violations)。"""
    patch: dict = {}
    violations: set[str] = set()

    for name in field_names:
        spec = specs[name]
        include = draw(st.booleans())
        if not include:
            continue
        lo, hi = spec.lo, spec.hi
        legal = draw(st.booleans())
        if legal:
            patch[name] = draw(st.integers(min_value=lo, max_value=hi))
        else:
            patch[name] = draw(
                st.one_of(
                    st.integers(max_value=lo - 1),
                    st.integers(min_value=hi + 1),
                    st.sampled_from([1.5, True, "abc"]),
                )
            )
            violations.add(name)
    return patch, violations


@st.composite
def _tenant_patch(draw):
    return _build_patch(draw, RETRIEVAL_FIELD_SPECS, UPLOAD_TENANT_FIELDS)


@st.composite
def _platform_patch(draw):
    return _build_patch(draw, PLATFORM_FIELD_SPECS, UPLOAD_PLATFORM_FIELDS)


@settings(max_examples=100)
@given(data=_tenant_patch())
def test_property_validate_patch_tenant_fields(data):
    """Feature: session-file-upload, Property 3: 租户上传字段范围校验。"""
    patch, expected_violations = data
    errors = validate_patch(patch)
    error_fields = {e.field for e in errors if e.field in UPLOAD_TENANT_FIELDS}
    assert error_fields == expected_violations
    for err in errors:
        if err.field not in UPLOAD_TENANT_FIELDS:
            continue
        assert isinstance(err, FieldError)
        spec = RETRIEVAL_FIELD_SPECS[err.field]
        assert err.allowed_range == f"[{spec.lo}, {spec.hi}]"


@settings(max_examples=100)
@given(data=_platform_patch())
def test_property_validate_platform_patch_fields(data):
    """Feature: session-file-upload, Property 3: 平台上传字段范围校验。"""
    patch, expected_violations = data
    errors = validate_platform_patch(patch)
    error_fields = {e.field for e in errors if e.field in UPLOAD_PLATFORM_FIELDS}
    assert error_fields == expected_violations
    for err in errors:
        if err.field not in UPLOAD_PLATFORM_FIELDS:
            continue
        assert isinstance(err, FieldError)
        spec = PLATFORM_FIELD_SPECS[err.field]
        assert err.allowed_range == f"[{spec.lo}, {spec.hi}]"


# ============================================================
# 边界示例单元测试
# ============================================================


@pytest.mark.parametrize(
    "field_name,low,high",
    [
        ("upload_max_file_mb", 1, 100),
    ],
)
def test_tenant_field_boundaries_kept(field_name, low, high):
    """租户字段端点值（lo / hi）为合法值，effective_from_raw 原样保留。"""
    assert getattr(RetrievalConfig.effective_from_raw({field_name: low}), field_name) == low
    assert getattr(RetrievalConfig.effective_from_raw({field_name: high}), field_name) == high
    default = RETRIEVAL_FIELD_SPECS[field_name].default
    assert getattr(RetrievalConfig.effective_from_raw({field_name: low - 1}), field_name) == default
    assert getattr(RetrievalConfig.effective_from_raw({field_name: high + 1}), field_name) == default


@pytest.mark.parametrize(
    "field_name,low,high",
    [
        ("kb_chunk_cap", 10000, 10000000),
    ],
)
def test_platform_field_boundaries_kept(field_name, low, high):
    """平台字段端点值（lo / hi）为合法值，effective_from_raw 原样保留。"""
    assert getattr(PlatformConfig.effective_from_raw({field_name: low}), field_name) == low
    assert getattr(PlatformConfig.effective_from_raw({field_name: high}), field_name) == high
    default = PLATFORM_FIELD_SPECS[field_name].default
    assert getattr(PlatformConfig.effective_from_raw({field_name: low - 1}), field_name) == default
    assert getattr(PlatformConfig.effective_from_raw({field_name: high + 1}), field_name) == default


def test_validate_patch_field_allowed_range_format():
    """越界字段的 allowed_range 格式为 '[lo, hi]'。"""
    errors = validate_patch({"upload_max_file_mb": 999})
    assert len(errors) == 1
    assert errors[0].field == "upload_max_file_mb"
    assert errors[0].allowed_range == "[1, 100]"

    p_errors = validate_platform_patch({"kb_chunk_cap": 1})
    assert len(p_errors) == 1
    assert p_errors[0].field == "kb_chunk_cap"
    assert p_errors[0].allowed_range == "[10000, 10000000]"
