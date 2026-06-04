"""H3 降级状态透传 — 属性测试 + 单元测试

Property 4：任意源成功/失败状态组合下：
  1. degraded=True ⟺ 至少一个源失败
  2. failed_sources 恰为失败源集合
  3. 所有源成功时 degraded=False 且 failed_sources 为空

单元测试：
  - mock 某源失败，断言 degraded 透传至 MultiKBSearchResult
  - sanitize_for_log 脱敏生效（替换 CR/LF/Tab）

**Validates: Requirements Bug 3 (H3) — Property 4**
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import asyncio  # noqa: E402

import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.log_safety import sanitize_for_log  # noqa: E402
from app.retrieval.multi_kb import (  # noqa: E402
    KBRetrievalConfig,
    MultiKBRetriever,
    MultiKBSearchResult,
)


# ============================================================
# 辅助工具
# ============================================================

_KB_ID_POOL = [f"kb-{i}" for i in range(8)]


def _make_result(chunk_id: str, score: float = 0.8) -> RetrievalResult:
    """创建一个检索结果"""
    return RetrievalResult(
        chunk_id=chunk_id,
        content=f"内容-{chunk_id}",
        score=score,
        doc_id="d1",
        metadata={},
    )


def _rerank_passthrough(query, results, top_k, tenant_id=None):
    """rerank_and_expand 的 mock side_effect：直通返回前 top_k 条"""
    return results[:top_k]


# ============================================================
# Property 4: 降级状态单调透传
# ============================================================


@st.composite
def _source_scenarios(draw):
    """生成任意源成功/失败状态组合。

    返回 (kb_configs, source_outcomes) 其中：
    - kb_configs: KBRetrievalConfig 列表（1~6 个源）
    - source_outcomes: 列表，每项为 ("success", results) 或 ("fail", exception)

    至少有一个源参与（min_size=1），允许全成功/全失败/混合。
    """
    n_sources = draw(st.integers(min_value=1, max_value=6))
    kb_ids = draw(
        st.lists(
            st.sampled_from(_KB_ID_POOL),
            min_size=n_sources,
            max_size=n_sources,
            unique=True,
        )
    )
    kb_configs = [
        KBRetrievalConfig(kb_id=kid, priority=1.0 if i == 0 else 0.8)
        for i, kid in enumerate(kb_ids)
    ]

    outcomes = []
    for i in range(n_sources):
        is_success = draw(st.booleans())
        if is_success:
            # 成功源返回 1~3 条结果
            n_results = draw(st.integers(min_value=1, max_value=3))
            results = [_make_result(f"c-{kb_ids[i]}-{j}") for j in range(n_results)]
            outcomes.append(("success", results))
        else:
            outcomes.append(("fail", RuntimeError(f"模拟 {kb_ids[i]} 检索失败")))

    return kb_configs, outcomes


@settings(max_examples=150, deadline=None)
@given(scenario=_source_scenarios())
def test_property4_degraded_iff_any_source_failed(scenario):
    """Property 4 ①②③：degraded ⟺ 存在失败源；failed_sources 恰为失败源集合；
    全成功时 degraded=False 且 failed_sources 为空。

    **Validates: Requirements Bug 3 (H3) — Property 4**
    """
    kb_configs, outcomes = scenario

    # 计算期望的 degraded 和 failed_kb_ids
    expected_failed_ids: set[str] = set()
    for cfg, outcome in zip(kb_configs, outcomes):
        if outcome[0] == "fail":
            expected_failed_ids.add(cfg.kb_id)

    expected_degraded = len(expected_failed_ids) > 0

    # 构建 mock HybridRetriever
    mock_hybrid = AsyncMock()

    # search 的 side_effect 按顺序返回成功结果或抛异常
    side_effects = []
    for outcome in outcomes:
        if outcome[0] == "success":
            side_effects.append(outcome[1])  # list[RetrievalResult]
        else:
            side_effects.append(outcome[1])  # Exception

    mock_hybrid.search = AsyncMock(side_effect=side_effects)
    mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

    retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)

    async def _run():
        return await retriever.search("查询", kb_configs, top_k=10)

    result: MultiKBSearchResult = asyncio.run(_run())

    # Property 4 ①: degraded=True ⟺ 至少一个源失败
    assert result.degraded is expected_degraded

    # Property 4 ②: failed_sources 恰为失败源集合
    assert set(result.failed_kb_ids) == expected_failed_ids

    # Property 4 ③: 全成功时 degraded=False 且 failed_sources 为空（隐含在 ① ② 中）
    if not expected_degraded:
        assert result.degraded is False
        assert result.failed_kb_ids == []


# ============================================================
# 单元测试：mock 某源失败，断言 degraded 透传
# ============================================================


class TestDegradedPropagation:
    """单元测试：降级状态透传"""

    @pytest.mark.asyncio
    async def test_single_source_failure_sets_degraded(self):
        """mock 一个源失败，断言 degraded=True 且 failed_kb_ids 正确"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1")],                   # kb-ok 成功
                RuntimeError("Milvus 超时"),             # kb-bad 失败
                [_make_result("c3")],                   # kb-ok2 成功
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-ok", priority=1.0),
            KBRetrievalConfig(kb_id="kb-bad", priority=0.8),
            KBRetrievalConfig(kb_id="kb-ok2", priority=0.7),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        # degraded 应为 True（存在失败源）
        assert result.degraded is True
        # failed_kb_ids 恰为失败源
        assert result.failed_kb_ids == ["kb-bad"]
        # 成功源的结果仍返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c3" in chunk_ids

    @pytest.mark.asyncio
    async def test_all_sources_success_no_degradation(self):
        """所有源成功时 degraded=False 且 failed_kb_ids 为空"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1")],
                [_make_result("c2")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-a", priority=1.0),
            KBRetrievalConfig(kb_id="kb-b", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is False
        assert result.failed_kb_ids == []

    @pytest.mark.asyncio
    async def test_all_sources_fail_degraded_with_empty_results(self):
        """所有源全部失败时 degraded=True，结果为空"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                RuntimeError("timeout"),
                RuntimeError("not found"),
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-x", priority=1.0),
            KBRetrievalConfig(kb_id="kb-y", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is True
        assert set(result.failed_kb_ids) == {"kb-x", "kb-y"}
        assert result.results == []


# ============================================================
# 单元测试：sanitize_for_log 脱敏
# ============================================================


class TestSanitizeForLog:
    """单元测试：日志脱敏函数"""

    def test_strips_cr(self):
        """替换 \\r 为空格"""
        assert sanitize_for_log("hello\rworld") == "hello world"

    def test_strips_lf(self):
        """替换 \\n 为空格"""
        assert sanitize_for_log("line1\nline2") == "line1 line2"

    def test_strips_tab(self):
        """替换 \\t 为空格"""
        assert sanitize_for_log("col1\tcol2") == "col1 col2"

    def test_strips_combined_crlf_tab(self):
        """同时替换 \\r\\n\\t"""
        assert sanitize_for_log("a\r\n\tb") == "a   b"

    def test_no_special_chars_unchanged(self):
        """无特殊字符时内容不变"""
        assert sanitize_for_log("normal text") == "normal text"

    def test_empty_string(self):
        """空字符串返回空"""
        assert sanitize_for_log("") == ""

    def test_exception_object(self):
        """接受异常对象，转为字符串后脱敏"""
        exc = RuntimeError("error\nwith\nnewlines")
        result = sanitize_for_log(exc)
        assert "\n" not in result
        assert "error with newlines" in result

    def test_non_string_input(self):
        """非字符串输入经 str() 转换后脱敏"""
        assert sanitize_for_log(12345) == "12345"
        assert sanitize_for_log(None) == "None"

    def test_kb_id_with_injection_attempt(self):
        """防 CR/LF 日志注入：含换行的 kb_id 被脱敏"""
        malicious_id = "kb-normal\n[FAKE LOG] Injected line"
        result = sanitize_for_log(malicious_id)
        assert "\n" not in result
        assert result == "kb-normal [FAKE LOG] Injected line"
