"""评测指标计算单元测试（任务 12.4）。

覆盖：
- recall@k 计算正确（给定 expected_doc_ids 与返回 doc_ids，验证比例）。
- hit@k 正确（doc_id 命中 / keyword 命中 / 不命中三种）。
- run_eval 聚合（avg_recall@k / hit_rate）与 funnel / top1_rerank_score 采集。
- compare 输出含 before/after 成对指标，且**配置被还原**（mock store 验证最后一次
  update 为原配置快照）。

用 mock hybrid（search_with_trace 返回构造好的 (results, trace)）与 mock store，
避免触达真实 Milvus / 模型。沿用现有测试的 pymilvus mock 模式。

Feature: kb-retrieval-optimization
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.scripts.evaluate_retrieval import (  # noqa: E402
    EvalQuery,
    EvalSet,
    _compute_hit_at_k,
    _compute_recall_at_k,
    compare,
    run_eval,
)


# ============================================================
# 测试替身
# ============================================================


def _result(chunk_id: str, doc_id: str, content: str = "") -> RetrievalResult:
    """构造一条 RetrievalResult。"""
    return RetrievalResult(chunk_id=chunk_id, content=content, score=0.5, doc_id=doc_id)


class FakeHybrid:
    """mock 检索器：search_with_trace 按 query 返回预设 (results, trace)。"""

    def __init__(self, responses: dict[str, tuple[list[RetrievalResult], dict]]):
        self._responses = responses
        self.calls: list[tuple[str, str, int]] = []

    async def search_with_trace(self, query: str, kb_id: str, top_k: int = 10):
        self.calls.append((query, kb_id, top_k))
        return self._responses[query]


class FakeStore:
    """mock RetrievalConfigStore：记录 update 调用顺序，get_effective 返回快照对象。"""

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.update_calls: list[dict] = []

    async def get_effective(self):
        return self._snapshot

    async def update(self, patch: dict):
        self.update_calls.append(patch)
        return self._snapshot


class FakeConfigSnapshot:
    """mock 配置快照：提供 model_dump() 供 compare 还原逻辑使用。"""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return dict(self._data)


# ============================================================
# recall@k 计算
# ============================================================


class TestRecallAtK:
    def test_full_recall(self):
        """返回覆盖全部 expected → recall=1.0"""
        assert _compute_recall_at_k(["d1", "d2", "d3"], ["d1", "d2"]) == 1.0

    def test_partial_recall(self):
        """返回覆盖一半 expected → recall=0.5"""
        assert _compute_recall_at_k(["d1", "dX"], ["d1", "d2"]) == 0.5

    def test_zero_recall(self):
        """返回完全未覆盖 expected → recall=0.0"""
        assert _compute_recall_at_k(["dX", "dY"], ["d1", "d2"]) == 0.0

    def test_no_expected_returns_none(self):
        """无 expected_doc_ids 标注 → None（不计入召回均值）"""
        assert _compute_recall_at_k(["d1"], []) is None

    def test_duplicate_returned_doc_ids_not_double_counted(self):
        """返回中重复的 doc_id 不重复计数"""
        assert _compute_recall_at_k(["d1", "d1"], ["d1", "d2"]) == 0.5


# ============================================================
# hit@k 计算（doc_id 命中 / keyword 命中 / 不命中）
# ============================================================


class TestHitAtK:
    def test_hit_by_doc_id(self):
        """任一返回 doc_id ∈ expected_doc_ids → 命中"""
        results = [_result("c1", "d9"), _result("c2", "d2")]
        assert _compute_hit_at_k(results, ["d2"], []) == 1

    def test_miss_by_doc_id(self):
        """无返回 doc_id 命中 expected_doc_ids → 不命中"""
        results = [_result("c1", "d9"), _result("c2", "d8")]
        assert _compute_hit_at_k(results, ["d2"], []) == 0

    def test_hit_by_keyword(self):
        """无 doc_id 标注时，关键词出现在任一 content → 命中"""
        results = [_result("c1", "d1", content="经济补偿按月工资计算")]
        assert _compute_hit_at_k(results, [], ["经济补偿"]) == 1

    def test_miss_by_keyword(self):
        """无 doc_id 标注且关键词不在任何 content → 不命中"""
        results = [_result("c1", "d1", content="与本主题无关的内容")]
        assert _compute_hit_at_k(results, [], ["经济补偿"]) == 0

    def test_doc_id_takes_priority_over_keyword(self):
        """有 doc_id 标注时优先用 doc_id 判定（即使关键词能命中也以 doc_id 为准）"""
        results = [_result("c1", "d9", content="包含经济补偿字样")]
        # doc_id 不命中 → 0，尽管 content 含关键词
        assert _compute_hit_at_k(results, ["d2"], ["经济补偿"]) == 0

    def test_no_annotation_returns_zero(self):
        """既无 doc_id 也无 keyword 标注 → 无法判定，返回 0"""
        results = [_result("c1", "d1", content="任意内容")]
        assert _compute_hit_at_k(results, [], []) == 0


# ============================================================
# run_eval：指标采集与聚合
# ============================================================


@pytest.mark.asyncio
async def test_run_eval_collects_and_aggregates():
    """run_eval 逐条采集指标并正确聚合 avg_recall@k / hit_rate，采集 funnel 与 top1_rerank_score。"""
    funnel_q1 = [
        {"stage": "三路召回去重", "count": 100},
        {"stage": "RRF 融合", "count": 80},
        {"stage": "Rerank 候选", "count": 50},
        {"stage": "Rerank 输出", "count": 10},
        {"stage": "MMR 去冗余", "count": 8},
    ]
    responses = {
        # q1：有 expected_doc_ids，命中 1/2，top-1 rerank_score=0.91
        "q1": (
            [_result("c1", "d1"), _result("c2", "dX")],
            {"funnel": funnel_q1, "per_result": {"c1": {"rerank_score": 0.91}}},
        ),
        # q2：无 doc_id 标注，用 keyword 命中
        "q2": (
            [_result("c3", "d3", content="包含关键词 经济补偿 的内容")],
            {"funnel": [], "per_result": {"c3": {"rerank_score": 0.42}}},
        ),
    }
    hybrid = FakeHybrid(responses)
    eval_set = EvalSet(
        name="unit-set",
        kb_id="kb-1",
        queries=[
            EvalQuery(query="q1", expected_doc_ids=["d1", "d2"]),
            EvalQuery(query="q2", expected_keywords=["经济补偿"]),
        ],
    )

    report = await run_eval(hybrid, eval_set, "default", top_k=10)

    assert report.config_label == "default"
    assert report.eval_set_name == "unit-set"
    assert len(report.per_query) == 2

    m1, m2 = report.per_query
    # q1: recall = 1/2, hit=1, returned=2, top1_rerank=0.91, funnel 透传
    assert m1.recall_at_k == 0.5
    assert m1.hit_at_k == 1
    assert m1.returned_count == 2
    assert m1.top1_rerank_score == 0.91
    assert m1.funnel == funnel_q1
    # q2: 无 doc_id 标注 → recall None；keyword 命中 → hit=1
    assert m2.recall_at_k is None
    assert m2.hit_at_k == 1
    assert m2.top1_rerank_score == 0.42

    # 聚合：avg_recall 仅对有标注的 q1 求均值 = 0.5；hit_rate = (1+1)/2 = 1.0
    assert report.avg_recall_at_k == 0.5
    assert report.hit_rate == 1.0

    # search_with_trace 按 top_k 调用
    assert hybrid.calls == [("q1", "kb-1", 10), ("q2", "kb-1", 10)]


@pytest.mark.asyncio
async def test_run_eval_empty_results_top1_none():
    """空返回结果时 returned_count=0、top1_rerank_score=None、hit=0。"""
    responses = {"q1": ([], {"funnel": [], "per_result": {}})}
    hybrid = FakeHybrid(responses)
    eval_set = EvalSet(
        name="empty-set",
        kb_id="kb-1",
        queries=[EvalQuery(query="q1", expected_doc_ids=["d1"])],
    )

    report = await run_eval(hybrid, eval_set, "default", top_k=5)

    m = report.per_query[0]
    assert m.returned_count == 0
    assert m.top1_rerank_score is None
    assert m.hit_at_k == 0
    assert m.recall_at_k == 0.0  # 有标注但全未命中
    assert report.hit_rate == 0.0


# ============================================================
# compare：成对指标 + 配置还原
# ============================================================


@pytest.mark.asyncio
async def test_compare_pairs_metrics_and_restores_config():
    """compare 输出含 before/after 成对指标，且 finally 把配置还原为原快照。"""
    responses = {
        "q1": (
            [_result("c1", "d1")],
            {"funnel": [], "per_result": {"c1": {"rerank_score": 0.8}}},
        ),
    }
    hybrid = FakeHybrid(responses)
    eval_set = EvalSet(
        name="cmp-set",
        kb_id="kb-1",
        queries=[EvalQuery(query="q1", expected_doc_ids=["d1"])],
    )

    orig_data = {"recall_k": 128, "rerank_threshold": 0.2}
    store = FakeStore(FakeConfigSnapshot(orig_data))

    before = {"rerank_threshold": 0.5}
    after = {"rerank_threshold": 0.1}

    result = await compare(hybrid, eval_set, before, after, top_k=10, store=store)

    # 输出结构含 before / after 成对报告与逐条 diff
    assert result["eval_set"] == "cmp-set"
    assert result["before"]["config_label"] == "before"
    assert result["after"]["config_label"] == "after"
    assert len(result["diff"]) == 1
    d = result["diff"][0]
    assert d["query"] == "q1"
    assert d["recall_before"] == 1.0
    assert d["recall_after"] == 1.0
    assert d["recall_delta"] == 0.0
    assert d["hit_before"] == 1
    assert d["hit_after"] == 1

    # summary 成对汇总
    assert result["summary"]["avg_recall_before"] == 1.0
    assert result["summary"]["avg_recall_after"] == 1.0

    # 配置切换顺序：before → after → 还原原快照（最后一次 update 为 orig_data）
    assert store.update_calls[0] == before
    assert store.update_calls[1] == after
    assert store.update_calls[-1] == orig_data


@pytest.mark.asyncio
async def test_compare_restores_config_on_exception():
    """run_eval 中途异常时，finally 仍把配置还原为原快照（不污染全局配置）。"""

    class BoomHybrid:
        async def search_with_trace(self, query, kb_id, top_k=10):
            raise RuntimeError("boom")

    eval_set = EvalSet(
        name="cmp-set",
        kb_id="kb-1",
        queries=[EvalQuery(query="q1", expected_doc_ids=["d1"])],
    )
    orig_data = {"recall_k": 200}
    store = FakeStore(FakeConfigSnapshot(orig_data))

    with pytest.raises(RuntimeError):
        await compare(BoomHybrid(), eval_set, {"recall_k": 1}, {"recall_k": 2}, store=store)

    # 异常路径下最后一次 update 仍是原快照还原
    assert store.update_calls[-1] == orig_data
