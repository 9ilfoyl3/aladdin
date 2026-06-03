"""Rerank_Filter（B2 软阈值多重兜底）属性测试与单元测试（任务 5.3-5.7）

直接测纯函数 ``HybridRetriever._apply_rerank_filter``：
- 构造按 rerank 原始分数降序的 RetrievalResult 列表（score 为 rerank 原始分）。
- 用一个轻量 mock config 对象承载 rerank_threshold / threshold_degradation_enabled 两字段。

对照 design.md Components C3（三层算法 + 不劣化论证）与 Correctness Properties P5-P8。

属性测试每条 ≥100 次迭代（Hypothesis @settings(max_examples=100)）。
"""

import sys
from dataclasses import dataclass
from unittest.mock import MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题（沿用现有测试模式）
sys.modules.setdefault("pymilvus", MagicMock())

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.hybrid import (  # noqa: E402
    _DEGRADE_FACTOR,
    _DEGRADE_FLOOR,
    _DEGRADE_TRIGGER_THRESHOLD,
    _TOP1_FALLBACK_MIN,
    HybridRetriever,
)

PBT_ITERATIONS = 200


# ============================================================
# 测试辅助
# ============================================================


@dataclass
class FakeFilterConfig:
    """承载 _apply_rerank_filter 所需的两个字段的轻量配置对象。"""

    rerank_threshold: float
    threshold_degradation_enabled: bool = True


def _make_filter() -> HybridRetriever:
    """构造一个最小化 HybridRetriever，仅用于调用纯函数 _apply_rerank_filter。

    _apply_rerank_filter 不依赖任何检索器/会话状态，传 None 占位即可。
    """
    return HybridRetriever.__new__(HybridRetriever)


def _results_from_scores(scores: list[float]) -> list[RetrievalResult]:
    """按给定分数构造结果列表（调用方负责保证降序）。"""
    return [
        RetrievalResult(
            chunk_id=f"c{i}",
            content=f"内容{i}",
            score=s,
            doc_id=f"d{i}",
            metadata={},
        )
        for i, s in enumerate(scores)
    ]


def _descending(scores: list[float]) -> list[float]:
    """转为降序列表（rerank 原始分数降序的前置约束）。"""
    return sorted(scores, reverse=True)


def _degraded_threshold(threshold: float) -> float:
    """与实现一致的降级阈值计算。"""
    return max(_DEGRADE_FLOOR, threshold * _DEGRADE_FACTOR)


# 分数生成器：范围适当超出 [0,1]（reranker 实际可能输出负分或 >1），验证健壮性。
_score_strategy = st.floats(
    min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False
)
_threshold_strategy = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


# ============================================================
# 5.3 P5：rerank 软阈值过滤
# ============================================================


@settings(max_examples=PBT_ITERATIONS, suppress_health_check=[HealthCheck.too_slow])
@given(
    scores=st.lists(_score_strategy, min_size=1, max_size=20),
    threshold=_threshold_strategy,
)
def test_property_5_soft_threshold_filter(scores, threshold):
    """Feature: kb-retrieval-optimization, Property 5: rerank 软阈值过滤

    对已按 rerank 原始分数降序的列表与任意阈值 t∈[0,1]，当存在分数 ≥ t 的结果时，
    第一层过滤后的结果恰为所有分数 ≥ t 的结果（不漏留、不错删）；t=0.0 时输出等于输入。

    Validates: Requirements 7.1, 7.4
    """
    rf = _make_filter()
    desc = _descending(scores)
    reranked = _results_from_scores(desc)
    # 关闭降级，隔离第一层逻辑（避免第一层为空时进入降级/兜底，污染断言）。
    config = FakeFilterConfig(rerank_threshold=threshold, threshold_degradation_enabled=False)

    expected_first_layer = [r for r in reranked if r.score >= threshold]
    result = rf._apply_rerank_filter(reranked, config)

    if expected_first_layer:
        # 存在 ≥ t 的结果：输出恰为第一层过滤结果（顺序、内容、分数完全一致）。
        assert result == expected_first_layer
        assert all(r.score >= threshold for r in result)
        # 不漏留：所有 ≥ t 的都在结果里
        assert len(result) == sum(1 for r in reranked if r.score >= threshold)

    # t=0.0 边界：全部 score（>=0 或负数？）—— 0.0 时 score>=0.0 才留。
    # 按 Req 7.4，threshold==0.0 等价不过滤，保留全部 rerank 结果。
    if threshold == 0.0:
        # 分数可能含负数；Req 7.4 语义是"等价不过滤"。reranker 原始分约定非负，
        # 但生成器含负数，此处验证 >=0.0 的恒等保留行为：当全部 score>=0 时输出==输入。
        if all(r.score >= 0.0 for r in reranked):
            assert result == reranked


@settings(max_examples=PBT_ITERATIONS, suppress_health_check=[HealthCheck.too_slow])
@given(scores=st.lists(_score_strategy, min_size=1, max_size=20))
def test_property_5_threshold_zero_keeps_all_nonneg(scores):
    """Feature: kb-retrieval-optimization, Property 5: rerank 软阈值过滤（t=0.0 等价不过滤）

    阈值 0.0 时对非负分数列表保留全部（等价不过滤）。

    Validates: Requirements 7.4
    """
    rf = _make_filter()
    # reranker 原始分约定非负，取绝对值确保 >=0
    desc = _descending([abs(s) for s in scores])
    reranked = _results_from_scores(desc)
    config = FakeFilterConfig(rerank_threshold=0.0, threshold_degradation_enabled=True)

    result = rf._apply_rerank_filter(reranked, config)
    assert result == reranked


# ============================================================
# 5.4 P6：rerank 阈值降级
# ============================================================


@settings(max_examples=PBT_ITERATIONS, suppress_health_check=[HealthCheck.too_slow])
@given(
    threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    degradation_enabled=st.booleans(),
    n=st.integers(min_value=1, max_value=15),
    gap=st.floats(min_value=0.001, max_value=0.5, allow_nan=False, allow_infinity=False),
)
def test_property_6_threshold_degradation(threshold, degradation_enabled, n, gap):
    """Feature: kb-retrieval-optimization, Property 6: rerank 阈值降级

    构造"全 < t"列表（保证第一层为空）：
    - 若降级开关开启且 t>0.3，降级层以 max(0.3, t*0.7) 重过滤一次，
      产出恰为所有分数 ≥ 降级阈值的结果。
    - 若 t≤0.3 或降级开关关闭，跳过降级（不进行第二次过滤）。
    一次检索内降级最多发生一次。

    Validates: Requirements 8.1, 8.2, 8.4
    """
    rf = _make_filter()
    # 构造严格小于 t 的分数列表，保证第一层过滤为空。
    # 分数取 [t - gap*(i+1)] 形式并 clamp 到 >=0，再降序。
    raw = [max(0.0, threshold - gap * (i + 1)) for i in range(n)]
    # 保证确实全部 < t（当 threshold 很小时 max(0,...) 可能等于 0 但 t>0 仍满足 <）
    desc = _descending(raw)
    # 若 threshold==0.0，则不可能"全 < t"，跳过该退化场景
    if threshold == 0.0:
        return
    reranked = _results_from_scores(desc)
    # 确认前置：第一层确实为空
    assert all(r.score < threshold for r in reranked)

    config = FakeFilterConfig(
        rerank_threshold=threshold, threshold_degradation_enabled=degradation_enabled
    )
    result = rf._apply_rerank_filter(reranked, config)

    should_degrade = degradation_enabled and threshold > _DEGRADE_TRIGGER_THRESHOLD

    if should_degrade:
        degraded = _degraded_threshold(threshold)
        expected_after_degrade = [r for r in reranked if r.score >= degraded]
        if expected_after_degrade:
            # 降级过滤产出恰为所有 ≥ 降级阈值的结果
            assert result == expected_after_degrade
            assert all(r.score >= degraded for r in result)
        else:
            # 降级后仍空 → 进入 top-1 兜底；此处仅断言降级阈值语义：
            # 结果要么是 [] 要么是单条 top-1（兜底），不会是按原阈值的结果。
            assert len(result) <= 1
    else:
        # 跳过降级：未发生第二次过滤。第一层已空，直接进入 top-1 兜底。
        # 结果要么 [] 要么单条 top-1，绝不会包含按降级阈值过滤的多条结果。
        assert len(result) <= 1


# ============================================================
# 5.5 P7：top-1 兜底
# ============================================================


@settings(max_examples=PBT_ITERATIONS, suppress_health_check=[HealthCheck.too_slow])
@given(
    top1=st.floats(min_value=0.0, max_value=0.3, allow_nan=False, allow_infinity=False),
    n_rest=st.integers(min_value=0, max_value=10),
)
def test_property_7_top1_fallback(top1, n_rest):
    """Feature: kb-retrieval-optimization, Property 7: top-1 兜底

    构造"降级后仍空"列表（最高分 top1 ≤ 0.3 且不会触发降级保留）：
    - 若 top1 ≥ 0.15，返回 [top1]。
    - 若 top1 < 0.15，返回 []。

    构造方式：阈值取 0.3（≤ 0.3 → 跳过降级），列表最高分为 top1（≤0.3，故第一层为空），
    其余分数 ≤ top1（降序），保证第一层空且降级被跳过，直接进入 top-1 兜底。

    Validates: Requirements 9.1, 9.2
    """
    rf = _make_filter()
    # 阈值 0.3：top1 ≤ 0.3 时第一层 score>=0.3 仅当 top1==0.3 才可能留；
    # 为确保第一层为空，取阈值略高于 top1。但阈值需 ≤ 0.3 以跳过降级。
    # 选择阈值 = 0.3，并令所有分数严格 < 0.3 ⇒ 第一层空、降级跳过（t≤0.3）。
    # 因此把 top1 限制在 [0.0, 0.3)，再单独覆盖 0.15 两侧。
    if top1 >= _DEGRADE_TRIGGER_THRESHOLD:
        top1 = _DEGRADE_TRIGGER_THRESHOLD - 1e-6
    threshold = _DEGRADE_TRIGGER_THRESHOLD  # 0.3，确保跳过降级
    rest = [max(0.0, top1 - 0.001 * (i + 1)) for i in range(n_rest)]
    scores = _descending([top1] + rest)
    reranked = _results_from_scores(scores)

    # 前置：第一层为空（全 < 0.3），降级被跳过（t==0.3 不 > 0.3）
    assert all(r.score < threshold for r in reranked)

    config = FakeFilterConfig(rerank_threshold=threshold, threshold_degradation_enabled=True)
    result = rf._apply_rerank_filter(reranked, config)

    if top1 >= _TOP1_FALLBACK_MIN:
        assert len(result) == 1
        assert result[0] is reranked[0]
        assert result[0].score == reranked[0].score
    else:
        assert result == []


# ============================================================
# 5.6 P8：软阈值不劣化保证
# ============================================================


@settings(max_examples=PBT_ITERATIONS, suppress_health_check=[HealthCheck.too_slow])
@given(
    scores=st.lists(_score_strategy, min_size=1, max_size=20),
    threshold=_threshold_strategy,
    degradation_enabled=st.booleans(),
)
def test_property_8_no_degradation_guarantee(scores, threshold, degradation_enabled):
    """Feature: kb-retrieval-optimization, Property 8: 软阈值不劣化保证

    对任意 rerank 原始分数列表与任意"启用"配置（需求规定的两个方向，互为逆否）：
    - Req 10.2：只要存在分数 ≥ 0.15 的结果，启用配置至少返回一条（不劣化）。
    - Req 10.1：启用配置返回空，则列表中最高分必 < 0.15。

    Validates: Requirements 10.1, 10.2
    """
    rf = _make_filter()
    desc = _descending(scores)
    reranked = _results_from_scores(desc)
    config = FakeFilterConfig(
        rerank_threshold=threshold, threshold_degradation_enabled=degradation_enabled
    )

    result = rf._apply_rerank_filter(reranked, config)
    max_score = desc[0]  # 已降序，首个为最高分

    # 需求只规定单一方向（Req 10.1 与 10.2 互为逆否）：
    #   - Req 10.2: 最高分 ≥ 0.15 ⟹ 启用配置至少返回一条（不劣化）。
    #   - Req 10.1（逆否等价）: 启用配置返回空 ⟹ 最高分 < 0.15。
    # 注意：反方向（最高分 < 0.15 ⟹ 必返回空）并非需求，且不成立——
    # 低阈值（含 threshold=0.0 不过滤基线）仍会保留 < 0.15 的结果，这是允许的。
    if max_score >= _TOP1_FALLBACK_MIN:
        # 存在 ≥ 0.15 ⇒ 启用配置至少返回一条（Req 10.2）
        assert len(result) >= 1
    if not result:
        # 启用配置返回空 ⇒ 最高分必 < 0.15（Req 10.1）
        assert max_score < _TOP1_FALLBACK_MIN


# ============================================================
# 5.7 单元测试：空结果不报错 + 降级最多一次
# ============================================================


def test_empty_input_returns_empty_no_exception():
    """空输入返回 [] 且不抛异常（Req 9.3）。"""
    rf = _make_filter()
    config = FakeFilterConfig(rerank_threshold=0.2)
    assert rf._apply_rerank_filter([], config) == []


def test_all_low_scores_returns_empty_no_exception():
    """全低分输入（< 0.15）返回 [] 且不抛异常（Req 9.2/9.3）。"""
    rf = _make_filter()
    scores = _descending([0.14, 0.10, 0.05, 0.0])
    reranked = _results_from_scores(scores)
    config = FakeFilterConfig(rerank_threshold=0.2, threshold_degradation_enabled=True)

    result = rf._apply_rerank_filter(reranked, config)
    assert result == []


def test_degradation_applied_at_most_once():
    """降级最多发生一次：结果等价于"单次降级阈值过滤"，不会二次收缩。

    构造：阈值 0.5（>0.3，触发降级），降级阈值 = max(0.3, 0.5*0.7) = 0.35。
    列表中存在 [0.35, 0.4) 之间分数：第一层（>=0.5）为空，降级（>=0.35）应保留这些条。
    若发生二次降级（错误），会保留更多低于 0.35 的条，断言其等于"恰好一次降级"结果可捕获。
    """
    rf = _make_filter()
    threshold = 0.5
    degraded = _degraded_threshold(threshold)  # 0.35
    # 分数：两条 >= 0.35 但 < 0.5（应被单次降级保留），两条 < 0.35（不应保留）
    scores = _descending([0.45, 0.36, 0.30, 0.20])
    reranked = _results_from_scores(scores)
    config = FakeFilterConfig(rerank_threshold=threshold, threshold_degradation_enabled=True)

    result = rf._apply_rerank_filter(reranked, config)

    # 第一层为空（无 >=0.5）；单次降级（>=0.35）保留 0.45 与 0.36 两条
    expected = [r for r in reranked if r.score >= degraded]
    assert result == expected
    assert len(result) == 2
    assert all(r.score >= degraded for r in result)
    # 低于降级阈值的条目（0.30, 0.20）不应出现（证明未二次降级到更低阈值）
    assert all(r.score >= degraded for r in result)


def test_degradation_disabled_skips_to_top1_fallback():
    """降级开关关闭时跳过降级，直接进入 top-1 兜底（Req 8.4）。"""
    rf = _make_filter()
    # 阈值 0.5（本会触发降级），但开关关闭 ⇒ 跳过降级。
    # 最高分 0.45 >= 0.15 ⇒ top-1 兜底返回该条。
    scores = _descending([0.45, 0.36, 0.30])
    reranked = _results_from_scores(scores)
    config = FakeFilterConfig(rerank_threshold=0.5, threshold_degradation_enabled=False)

    result = rf._apply_rerank_filter(reranked, config)
    assert len(result) == 1
    assert result[0] is reranked[0]


def test_threshold_le_trigger_skips_degradation():
    """阈值 ≤ 0.3 时跳过降级，直接进入 top-1 兜底（Req 8.2）。"""
    rf = _make_filter()
    # 阈值 0.3（不 > 0.3）⇒ 跳过降级。最高分 0.25 < 0.3 第一层空。
    # 0.25 >= 0.15 ⇒ top-1 兜底返回该条。
    scores = _descending([0.25, 0.20, 0.10])
    reranked = _results_from_scores(scores)
    config = FakeFilterConfig(rerank_threshold=0.3, threshold_degradation_enabled=True)

    result = rf._apply_rerank_filter(reranked, config)
    assert len(result) == 1
    assert result[0].score == 0.25
