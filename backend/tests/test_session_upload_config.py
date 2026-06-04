"""会话文件上传：新增上传限制配置字段的兜底与校验属性测试（任务 1.1）

复用 kb-retrieval-optimization 的 PBT 框架（hypothesis + FieldSpec 单一事实源），
专门覆盖 session-file-upload 新增的 5 个配置字段：

租户级（RETRIEVAL_FIELD_SPECS / RetrievalConfig）：
- ``upload_max_file_mb``     默认 10，范围 [1, 100]
- ``session_max_files``      默认 5，范围 [1, 20]
- ``session_chunk_cap``      默认 6000，范围 [500, 20000]

平台级（PLATFORM_FIELD_SPECS / PlatformConfig）：
- ``kb_chunk_cap``           默认 1000000，范围 [10000, 10000000]
- ``session_chunk_ceiling``  默认 20000，范围 [500, 100000]

Property 2（新增配置字段的逐字段兜底）：
*For any* 持久化原始配置 dict（新增 5 字段任意缺失 / None / 合法 / 越界 / 错类型），
``RetrievalConfig.effective_from_raw`` 与 ``PlatformConfig.effective_from_raw`` 产出值
SHALL 满足：区间内原样保留，缺失 / 越界 / 错类型替换为 Safe_Default，单字段兜底互不影响。

Property 3（新增配置字段的范围校验）：
*For any* 提交 patch（新增字段值取自区间内外），``validate_patch`` / ``validate_platform_patch``
返回的违规字段集合 SHALL 恰好等于越界字段集合，每项含字段名与允许范围；全合法返回空。

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
# 本特性新增字段（单一事实源即 *_FIELD_SPECS，此处仅声明"属于本期"的子集）
# ============================================================

# 租户级新增 3 字段（写入 RETRIEVAL_FIELD_SPECS / RetrievalConfig）
NEW_TENANT_FIELDS = ("upload_max_file_mb", "session_max_files", "session_chunk_cap")
# 平台级新增 2 字段（写入 PLATFORM_FIELD_SPECS / PlatformConfig）
NEW_PLATFORM_FIELDS = ("kb_chunk_cap", "session_chunk_ceiling")

# 期望规格：字段名 -> (default, lo, hi)。严格对照 requirements 配置汇总表 / design C1。
EXPECTED_NEW_TENANT_SPECS = {
    "upload_max_file_mb": (10, 1, 100),
    "session_max_files": (5, 1, 20),
    "session_chunk_cap": (6000, 500, 20000),
}
EXPECTED_NEW_PLATFORM_SPECS = {
    "kb_chunk_cap": (1000000, 10000, 10000000),
    "session_chunk_ceiling": (20000, 500, 100000),
}

# 哨兵：表示该字段在 raw / patch 中"缺失"（不放入 dict）。
_MISSING = object()


# ============================================================
# 字段规格单元测试（确认 5 字段已正确登记到单一事实源）
# ============================================================


@pytest.mark.parametrize("field_name", list(EXPECTED_NEW_TENANT_SPECS.keys()))
def test_new_tenant_field_spec(field_name):
    """租户级新增字段的 default / (lo, hi) / kind 等于规定值。"""
    expected_default, expected_lo, expected_hi = EXPECTED_NEW_TENANT_SPECS[field_name]
    spec = RETRIEVAL_FIELD_SPECS[field_name]
    assert spec.default == expected_default
    assert spec.lo == expected_lo
    assert spec.hi == expected_hi
    assert spec.kind == KIND_INT


@pytest.mark.parametrize("field_name", list(EXPECTED_NEW_PLATFORM_SPECS.keys()))
def test_new_platform_field_spec(field_name):
    """平台级新增字段的 default / (lo, hi) / kind 等于规定值。"""
    expected_default, expected_lo, expected_hi = EXPECTED_NEW_PLATFORM_SPECS[field_name]
    spec = PLATFORM_FIELD_SPECS[field_name]
    assert spec.default == expected_default
    assert spec.lo == expected_lo
    assert spec.hi == expected_hi
    assert spec.kind == KIND_INT


# ============================================================
# 生成器：为单个 int 字段生成 {缺失, None, 区间内, 区间外, 错类型} 的取值
# ============================================================


@st.composite
def _int_field_value(draw, spec):
    """为单个 int 字段生成多样化取值，返回 (value_or_sentinel, is_valid)。

    is_valid 表示该取值是否为「应被原样保留的合法值」（落在 [lo, hi] 内的 int）。
    """
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
    """对给定字段子集逐字段抽样，返回 (raw_dict, validity)。

    validity[name] = (raw_value_or_missing, is_valid)。缺失字段不进入 raw_dict。
    """
    raw: dict = {}
    validity: dict = {}
    for name in field_names:
        value, is_valid = draw(_int_field_value(specs[name]))
        validity[name] = (value, is_valid)
        if value is not _MISSING:
            raw[name] = value
    return raw, validity


@st.composite
def _new_tenant_raw(draw):
    return _build_raw(draw, RETRIEVAL_FIELD_SPECS, NEW_TENANT_FIELDS)


@st.composite
def _new_platform_raw(draw):
    return _build_raw(draw, PLATFORM_FIELD_SPECS, NEW_PLATFORM_FIELDS)


# ============================================================
# Property 2：新增配置字段的逐字段兜底
# ============================================================


@settings(max_examples=100)
@given(data=_new_tenant_raw())
def test_property_new_tenant_fields_per_field_fallback(data):
    """Feature: session-file-upload, Property 2: 新增租户字段逐字段兜底

    For any 含新增 3 租户字段（缺失/None/合法/越界/错类型）的原始 dict：
    - 落在 Valid_Range 内的值原样保留；
    - 缺失/越界/错类型/None → 回退该字段 Safe_Default；
    - 单字段兜底不影响其余新增字段；
    - 产出值恒落在 Valid_Range 内。

    Validates: Requirements 3.1, 3.5, 8.1
    """
    raw, validity = data
    config = RetrievalConfig.effective_from_raw(raw)

    for name in NEW_TENANT_FIELDS:
        spec = RETRIEVAL_FIELD_SPECS[name]
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)

        if is_valid:
            assert effective_value == raw_value, f"{name} 合法值未被保留"
        else:
            assert effective_value == spec.default, f"{name} 未回退到 Safe_Default"

        # 结果恒落在 Valid_Range 内
        assert spec.lo <= effective_value <= spec.hi, f"{name} 结果越界"


@settings(max_examples=100)
@given(data=_new_platform_raw())
def test_property_new_platform_fields_per_field_fallback(data):
    """Feature: session-file-upload, Property 2: 新增平台字段逐字段兜底

    For any 含新增 2 平台字段（缺失/None/合法/越界/错类型）的原始 dict：
    - 落在 Valid_Range 内的值原样保留；
    - 缺失/越界/错类型/None → 回退该字段 Safe_Default；
    - 单字段兜底不影响其余新增字段；
    - 产出值恒落在 Valid_Range 内。

    Validates: Requirements 4.1, 4.5, 8.1
    """
    raw, validity = data
    config = PlatformConfig.effective_from_raw(raw)

    for name in NEW_PLATFORM_FIELDS:
        spec = PLATFORM_FIELD_SPECS[name]
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)

        if is_valid:
            assert effective_value == raw_value, f"{name} 合法值未被保留"
        else:
            assert effective_value == spec.default, f"{name} 未回退到 Safe_Default"

        assert spec.lo <= effective_value <= spec.hi, f"{name} 结果越界"


@settings(max_examples=100)
@given(
    target=st.sampled_from(NEW_TENANT_FIELDS),
    others=_new_tenant_raw(),
)
def test_property_new_tenant_field_independence(target, others):
    """Feature: session-file-upload, Property 2（独立性切片）

    把某个新增租户字段强制设为非法值，不影响其余新增字段读取各自取值（互不影响）。

    Validates: Requirements 3.1, 9.1
    """
    raw, validity = others
    raw = dict(raw)
    raw[target] = "definitely_wrong"  # 强制错类型

    config = RetrievalConfig.effective_from_raw(raw)

    # 目标字段必回退默认
    assert getattr(config, target) == RETRIEVAL_FIELD_SPECS[target].default

    # 其余新增字段仍按各自 validity 取值
    for name in NEW_TENANT_FIELDS:
        if name == target:
            continue
        spec = RETRIEVAL_FIELD_SPECS[name]
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)
        if is_valid:
            assert effective_value == raw_value
        else:
            assert effective_value == spec.default


# ============================================================
# Property 3：新增配置字段的范围校验
# ============================================================


def _build_patch(draw, specs, field_names):
    """对给定字段子集生成 (patch, expected_violations)。

    每字段独立选择是否纳入 patch；纳入时取区间内（合法）或区间外/错类型（违规）。
    """
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
                    st.sampled_from([1.5, True, "abc"]),  # 错类型
                )
            )
            violations.add(name)

    return patch, violations


@st.composite
def _new_tenant_patch(draw):
    return _build_patch(draw, RETRIEVAL_FIELD_SPECS, NEW_TENANT_FIELDS)


@st.composite
def _new_platform_patch(draw):
    return _build_patch(draw, PLATFORM_FIELD_SPECS, NEW_PLATFORM_FIELDS)


@settings(max_examples=100)
@given(data=_new_tenant_patch())
def test_property_validate_patch_new_tenant_fields(data):
    """Feature: session-file-upload, Property 3: 新增租户字段范围校验

    For any 含新增 3 租户字段（合法区间内外）的 patch：
    - validate_patch 返回的违规字段集合恰好等于越界/错类型字段集合；
    - 每个违规项含 field 与 allowed_range；
    - 当且仅当全部合法时返回空。

    Validates: Requirements 3.3, 8.1
    """
    patch, expected_violations = data
    errors = validate_patch(patch)

    error_fields = {e.field for e in errors}
    # 只关注本期新增字段（patch 仅含这些字段，故全集即新增字段）
    assert error_fields == expected_violations

    for err in errors:
        assert isinstance(err, FieldError)
        assert err.field in RETRIEVAL_FIELD_SPECS
        spec = RETRIEVAL_FIELD_SPECS[err.field]
        assert err.allowed_range == f"[{spec.lo}, {spec.hi}]"

    if not expected_violations:
        assert errors == []


@settings(max_examples=100)
@given(data=_new_platform_patch())
def test_property_validate_platform_patch_new_fields(data):
    """Feature: session-file-upload, Property 3: 新增平台字段范围校验

    For any 含新增 2 平台字段（合法区间内外）的 patch：
    - validate_platform_patch 返回的违规字段集合恰好等于越界/错类型字段集合；
    - 每个违规项含 field 与 allowed_range；
    - 当且仅当全部合法时返回空。

    Validates: Requirements 4.5, 6.8, 8.1
    """
    patch, expected_violations = data
    errors = validate_platform_patch(patch)

    error_fields = {e.field for e in errors}
    assert error_fields == expected_violations

    for err in errors:
        assert isinstance(err, FieldError)
        assert err.field in PLATFORM_FIELD_SPECS
        spec = PLATFORM_FIELD_SPECS[err.field]
        assert err.allowed_range == f"[{spec.lo}, {spec.hi}]"

    if not expected_violations:
        assert errors == []


# ============================================================
# 边界示例单元测试（补充属性测试，锚定关键端点）
# ============================================================


@pytest.mark.parametrize(
    "field_name,low,high",
    [
        ("upload_max_file_mb", 1, 100),
        ("session_max_files", 1, 20),
        ("session_chunk_cap", 500, 20000),
    ],
)
def test_tenant_field_boundaries_kept(field_name, low, high):
    """租户字段端点值（lo / hi）为合法值，effective_from_raw 原样保留。"""
    assert getattr(RetrievalConfig.effective_from_raw({field_name: low}), field_name) == low
    assert getattr(RetrievalConfig.effective_from_raw({field_name: high}), field_name) == high
    # 越界一格回退默认
    default = RETRIEVAL_FIELD_SPECS[field_name].default
    assert getattr(RetrievalConfig.effective_from_raw({field_name: low - 1}), field_name) == default
    assert getattr(RetrievalConfig.effective_from_raw({field_name: high + 1}), field_name) == default


@pytest.mark.parametrize(
    "field_name,low,high",
    [
        ("kb_chunk_cap", 10000, 10000000),
        ("session_chunk_ceiling", 500, 100000),
    ],
)
def test_platform_field_boundaries_kept(field_name, low, high):
    """平台字段端点值（lo / hi）为合法值，effective_from_raw 原样保留。"""
    assert getattr(PlatformConfig.effective_from_raw({field_name: low}), field_name) == low
    assert getattr(PlatformConfig.effective_from_raw({field_name: high}), field_name) == high
    default = PLATFORM_FIELD_SPECS[field_name].default
    assert getattr(PlatformConfig.effective_from_raw({field_name: low - 1}), field_name) == default
    assert getattr(PlatformConfig.effective_from_raw({field_name: high + 1}), field_name) == default


def test_validate_patch_new_field_allowed_range_format():
    """越界新增字段的 allowed_range 格式为 '[lo, hi]'。"""
    errors = validate_patch({"upload_max_file_mb": 999})
    assert len(errors) == 1
    assert errors[0].field == "upload_max_file_mb"
    assert errors[0].allowed_range == "[1, 100]"

    p_errors = validate_platform_patch({"kb_chunk_cap": 1})
    assert len(p_errors) == 1
    assert p_errors[0].field == "kb_chunk_cap"
    assert p_errors[0].allowed_range == "[10000, 10000000]"
