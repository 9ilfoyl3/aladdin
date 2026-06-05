"""检索参数配置（B1）单元测试 + 属性测试

覆盖 tasks 子任务：
- 1.4 字段规格单元测试：RETRIEVAL_FIELD_SPECS 每字段 default 与 (lo, hi) 等于规定值。
- 1.5 属性测试 P1：effective_from_raw 逐字段独立兜底。
- 1.6 属性测试 P2：validate_patch 精确拒绝越界字段。
- 1.7 兜底日志单元测试：越界/缺失字段触发含 field/原值/回退值的 WARNING。

Feature: kb-retrieval-optimization
"""

import logging

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.retrieval.config import (
    KIND_BOOL,
    KIND_FLOAT,
    KIND_INT,
    RETRIEVAL_FIELD_SPECS,
    FieldError,
    RetrievalConfig,
    validate_patch,
)


# ============================================================
# 1.4 字段规格单元测试
# ============================================================

# 期望规格表：字段名 -> (default, lo, hi, kind)。严格对照 requirements/design C1 字段表。
EXPECTED_SPECS = {
    # 分块档 Chunk_Tier
    "parent_chunk_size": (2500, 100, 8000, KIND_INT),
    "child_chunk_size": (450, 50, 4000, KIND_INT),
    "chunk_overlap": (70, 0, 1000, KIND_INT),
    # 召回档 Recall_Tier
    "recall_k": (128, 1, 1000, KIND_INT),
    "rerank_candidate_k": (50, 1, 200, KIND_INT),
    "rrf_k": (60, 1, 1000, KIND_INT),
    "composite_rerank_weight": (0.6, 0.0, 1.0, KIND_FLOAT),
    "composite_base_weight": (0.3, 0.0, 1.0, KIND_FLOAT),
    "composite_source_weight": (0.1, 0.0, 1.0, KIND_FLOAT),
    "rerank_threshold": (0.2, 0.0, 1.0, KIND_FLOAT),
    "rerank_top_k": (10, 1, 100, KIND_INT),
    "threshold_degradation_enabled": (True, None, None, KIND_BOOL),
    "mmr_lambda": (0.7, 0.0, 1.0, KIND_FLOAT),
    "mmr_threshold": (0.7, 0.0, 1.0, KIND_FLOAT),
    "hnsw_ef": (128, 1, 2048, KIND_INT),
    "hnsw_ef_construction": (128, 8, 512, KIND_INT),
    "hnsw_m": (16, 4, 64, KIND_INT),
    # 上传限制档 Upload_Tier（session-file-upload 新增，租户级）
    "upload_max_file_mb": (10, 1, 100, KIND_INT),
    "session_max_files": (5, 1, 20, KIND_INT),
    "session_chunk_cap": (6000, 500, 20000, KIND_INT),
}


def test_field_specs_complete_set():
    """RETRIEVAL_FIELD_SPECS 字段集合恰为规定的字段集合（含分块档），无遗漏无多余。"""
    assert set(RETRIEVAL_FIELD_SPECS.keys()) == set(EXPECTED_SPECS.keys())


@pytest.mark.parametrize("field_name", list(EXPECTED_SPECS.keys()))
def test_field_spec_default_and_range(field_name):
    """每个字段的 default 与 (lo, hi, kind) 等于规定值。"""
    expected_default, expected_lo, expected_hi, expected_kind = EXPECTED_SPECS[field_name]
    spec = RETRIEVAL_FIELD_SPECS[field_name]

    assert spec.default == expected_default
    assert spec.lo == expected_lo
    assert spec.hi == expected_hi
    assert spec.kind == expected_kind


def test_default_config_matches_specs():
    """RetrievalConfig 的默认实例每个字段都等于其 Safe_Default。"""
    config = RetrievalConfig()
    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        assert getattr(config, name) == spec.default


# ============================================================
# 生成器：为每个字段独立生成 {缺失, None, 区间内, 区间外, 错类型} 的取值
# ============================================================

# 哨兵：表示该字段在 raw 中「缺失」（不放入 dict）。
_MISSING = object()


@st.composite
def _field_value(draw, spec):
    """为单个字段生成多样化取值，返回 (value_or_sentinel, is_valid)。

    is_valid 表示该取值是否为「应被原样保留的合法值」。
    """
    if spec.kind == KIND_BOOL:
        choice = draw(st.sampled_from(["missing", "none", "valid", "wrong_type"]))
        if choice == "missing":
            return _MISSING, False
        if choice == "none":
            return None, False
        if choice == "valid":
            return draw(st.booleans()), True
        # 错类型：bool 字段填非 bool
        return draw(st.sampled_from([1, 0, "true", 1.5])), False

    lo, hi = spec.lo, spec.hi
    choice = draw(
        st.sampled_from(
            ["missing", "none", "in_range", "below", "above", "wrong_type"]
        )
    )
    if choice == "missing":
        return _MISSING, False
    if choice == "none":
        return None, False

    if spec.kind == KIND_INT:
        if choice == "in_range":
            return draw(st.integers(min_value=lo, max_value=hi)), True
        if choice == "below":
            return draw(st.integers(max_value=lo - 1)), False
        if choice == "above":
            return draw(st.integers(min_value=hi + 1)), False
        # wrong_type：int 字段填 float / bool / str
        return draw(st.sampled_from([1.5, True, False, "abc", 3.0])), False

    # KIND_FLOAT
    if choice == "in_range":
        # 含端点；float 字段也接受 int 形式的合法值
        val = draw(
            st.one_of(
                st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False),
                st.sampled_from([lo, hi]),
            )
        )
        return val, True
    if choice == "below":
        return draw(st.floats(max_value=lo - 0.0001, allow_nan=False, allow_infinity=False)), False
    if choice == "above":
        return draw(st.floats(min_value=hi + 0.0001, allow_nan=False, allow_infinity=False)), False
    # wrong_type：float 字段填 bool / str
    return draw(st.sampled_from([True, False, "abc", "1.5"])), False


@st.composite
def _raw_config(draw):
    """生成 (raw_dict, expected_validity) ：

    raw_dict 为构造给 effective_from_raw 的原始 dict；
    expected_validity[field] = (raw_value_or_missing, is_valid)。
    """
    raw: dict = {}
    validity: dict = {}
    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        value, is_valid = draw(_field_value(spec))
        validity[name] = (value, is_valid)
        if value is not _MISSING:
            raw[name] = value
    return raw, validity


# ============================================================
# 1.5 属性测试 P1：Effective_Value 逐字段独立兜底
# ============================================================


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(data=_raw_config())
def test_property_effective_from_raw_per_field_fallback(data):
    """Feature: kb-retrieval-optimization, Property 1: Effective_Value 逐字段独立兜底

    For any 持久化原始配置 dict（每字段可缺失/None/合法/越界/错类型）：
    - 落在 Valid_Range 内的持久化值被原样保留；
    - 缺失/越界/错类型字段被替换为其 Safe_Default；
    - 单字段兜底不改变其余字段取值；
    - 产出的有效配置每个字段都落在其 Valid_Range 内。

    Validates: Requirements 2.2, 2.3, 2.5
    """
    raw, validity = data
    config = RetrievalConfig.effective_from_raw(raw)

    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)

        if is_valid:
            # 合法值原样保留
            assert effective_value == raw_value, f"{name} 合法值未被保留"
        else:
            # 非法/缺失值回退到 Safe_Default
            assert effective_value == spec.default, f"{name} 未回退到 Safe_Default"

        # 结果恒落在 Valid_Range 内（bool 字段类型恒为 bool）
        if spec.kind == KIND_BOOL:
            assert isinstance(effective_value, bool)
        else:
            assert spec.lo <= effective_value <= spec.hi, f"{name} 结果越界"


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    field_name=st.sampled_from(list(RETRIEVAL_FIELD_SPECS.keys())),
    others_raw=_raw_config(),
)
def test_property_field_independence(field_name, others_raw):
    """Feature: kb-retrieval-optimization, Property 1（独立性切片）

    单个字段填入非法值，不影响其余字段读取各自的合法持久化值。

    Validates: Requirements 2.5
    """
    raw, validity = others_raw
    spec = RETRIEVAL_FIELD_SPECS[field_name]

    # 强制把目标字段设为越界/错类型非法值
    illegal_value = "definitely_wrong"
    raw = dict(raw)
    raw[field_name] = illegal_value

    config = RetrievalConfig.effective_from_raw(raw)

    # 目标字段必回退默认
    assert getattr(config, field_name) == spec.default

    # 其余字段仍按各自 validity 取值（合法保留、非法回退）
    for name, other_spec in RETRIEVAL_FIELD_SPECS.items():
        if name == field_name:
            continue
        _raw_value, is_valid = validity[name]
        effective_value = getattr(config, name)
        if is_valid:
            assert effective_value == _raw_value
        else:
            assert effective_value == other_spec.default


# ============================================================
# 1.6 属性测试 P2：范围校验精确拒绝越界字段
# ============================================================


@st.composite
def _patch_with_expected_violations(draw):
    """生成 (patch, expected_violation_fields)。

    每字段独立选择是否纳入 patch，纳入时取区间内或区间外的值。
    bool 字段：合法 = bool，非法 = 错类型。
    """
    patch: dict = {}
    violations: set[str] = set()

    for name, spec in RETRIEVAL_FIELD_SPECS.items():
        include = draw(st.booleans())
        if not include:
            continue

        if spec.kind == KIND_BOOL:
            legal = draw(st.booleans())
            if legal:
                patch[name] = draw(st.booleans())
            else:
                patch[name] = draw(st.sampled_from([1, "yes", 0.0]))
                violations.add(name)
            continue

        lo, hi = spec.lo, spec.hi
        legal = draw(st.booleans())
        if spec.kind == KIND_INT:
            if legal:
                patch[name] = draw(st.integers(min_value=lo, max_value=hi))
            else:
                patch[name] = draw(
                    st.one_of(
                        st.integers(max_value=lo - 1),
                        st.integers(min_value=hi + 1),
                    )
                )
                violations.add(name)
        else:  # KIND_FLOAT
            if legal:
                patch[name] = draw(
                    st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False)
                )
            else:
                patch[name] = draw(
                    st.one_of(
                        st.floats(max_value=lo - 0.0001, allow_nan=False, allow_infinity=False),
                        st.floats(min_value=hi + 0.0001, allow_nan=False, allow_infinity=False),
                    )
                )
                violations.add(name)

    return patch, violations


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(data=_patch_with_expected_violations())
def test_property_validate_patch_rejects_exact_violations(data):
    """Feature: kb-retrieval-optimization, Property 2: 范围校验精确拒绝越界字段

    For any 提交 patch（字段值随机取自合法区间内外）：
    - validate_patch 返回的违规字段集合恰好等于 patch 中越界字段集合；
    - 每个违规项携带 field 与 allowed_range；
    - 当且仅当全部合法时返回空列表。

    Validates: Requirements 3.2, 3.3
    """
    patch, expected_violations = data
    errors = validate_patch(patch)

    error_fields = {e.field for e in errors}
    assert error_fields == expected_violations

    # 每个违规项含 field / value / allowed_range
    for err in errors:
        assert isinstance(err, FieldError)
        assert err.field in RETRIEVAL_FIELD_SPECS
        assert err.allowed_range  # 非空字符串
        assert err.to_dict()["allowed_range"] == err.allowed_range

    # 当且仅当无越界时返回空
    if not expected_violations:
        assert errors == []


def test_validate_patch_ignores_unknown_fields():
    """patch 中的未知字段被忽略，不计入错误。"""
    errors = validate_patch({"unknown_field": 999, "recall_k": 128})
    assert errors == []


def test_validate_patch_allowed_range_format():
    """越界数值字段的 allowed_range 格式为 '[lo, hi]'。"""
    errors = validate_patch({"recall_k": 99999})
    assert len(errors) == 1
    assert errors[0].field == "recall_k"
    assert errors[0].allowed_range == "[1, 1000]"


# ============================================================
# 1.7 兜底日志单元测试
# ============================================================


def test_fallback_log_on_out_of_range(caplog):
    """越界字段触发含 field/原值/回退值的 WARNING 日志。"""
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = RetrievalConfig.effective_from_raw({"recall_k": 99999})

    assert config.recall_k == RETRIEVAL_FIELD_SPECS["recall_k"].default

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    # recall_k 越界回退的那条日志（其余字段缺失也会各记一条，故按字段匹配而非取首条）
    messages = [w.getMessage() for w in warnings]
    assert any("recall_k" in m and "99999" in m and "128" in m for m in messages)


def test_fallback_log_on_missing_field(caplog):
    """提供了行但字段缺失时，触发含 field/回退值的 WARNING 日志。"""
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        # 提供一个非空行（含合法 recall_k），但缺失 rerank_top_k
        RetrievalConfig.effective_from_raw({"recall_k": 200})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    messages = [w.getMessage() for w in warnings]
    assert any("rerank_top_k" in m for m in messages)
    assert any("10" in m for m in messages)  # rerank_top_k 回退值


def test_no_fallback_log_when_raw_is_none(caplog):
    """raw 为 None（未配置态）时不刷兜底日志。"""
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = RetrievalConfig.effective_from_raw(None)

    assert config == RetrievalConfig()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_fallback_log_on_wrong_type(caplog):
    """类型错误字段触发含 field/原值/回退值的 WARNING 日志。"""
    with caplog.at_level(logging.WARNING, logger="app.retrieval.config"):
        config = RetrievalConfig.effective_from_raw({"rrf_k": "not_an_int"})

    assert config.rrf_k == RETRIEVAL_FIELD_SPECS["rrf_k"].default
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    messages = [w.getMessage() for w in warnings]
    assert any("rrf_k" in m and "not_an_int" in m and "60" in m for m in messages)
