"""H3 降级状态透传：属性测试(Property 4) + 单元测试（任务 3.1）

对应 spec retrieval-pipeline-hardening 的 Property 4（降级状态单调透传）与
Fix 3 单元测试：

- 属性测试(Property 4)：任意检索源集合与各源成功/失败状态组合下，
  ``MultiKBSearchResult.degraded`` SHALL 为 True 当且仅当存在失败源；
  ``failed_kb_ids`` SHALL 恰为失败源集合；全成功时 degraded=False。
- 单元测试：
  - mock 某源失败，断言 degraded 透传至 ``_retrieve_multi_kb`` 返回值（三元组）。
  - ``sanitize_for_log`` 基本用例：含 CR/LF/Tab 的字符串 → 替换为空格。

**Validates: Requirements 1.2**

Feature: retrieval-pipeline-hardening
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

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
# 公共 helper
# ============================================================


def _make_result(chunk_id: str, score: float = 0.8) -> RetrievalResult:
    """辅助方法：创建 RetrievalResult"""
    return RetrievalResult(
        chunk_id=chunk_id,
        content=f"内容-{chunk_id}",
        score=score,
        doc_id="d1",
        metadata={},
    )


def _rerank_passthrough(query, results, top_k, tenant_id=None):
    """rerank_and_expand 的 mock side_effect：透传前 top_k 条。"""
    return results[:top_k]


def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# 属性测试 Property 4：降级状态单调透传
# ============================================================

# 生成检索源集合：1~8 个源，每个源 kb_id 唯一
_kb_ids_st = st.lists(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
        min_size=3,
        max_size=12,
    ),
    min_size=1,
    max_size=8,
    unique=True,
)

# 对每个源生成是否失败的布尔标志
_failure_flags_st = st.lists(st.booleans(), min_size=1, max_size=8)


@settings(max_examples=200, deadline=None)
@given(kb_ids=_kb_ids_st, failure_pattern=st.data())
def test_property_degraded_iff_any_failed(kb_ids, failure_pattern):
    """Feature: retrieval-pipeline-hardening, Property 4: 降级状态单调透传

    For any 检索源集合与各源成功/失败状态，向上返回的 degraded
    SHALL 为 True 当且仅当存在失败源；failed_kb_ids SHALL 恰为失败源集合；
    全成功时 degraded=False。

    **Validates: Requirements 1.2**
    """
    # 为每个 kb_id 生成失败标志
    failures = failure_pattern.draw(
        st.lists(st.booleans(), min_size=len(kb_ids), max_size=len(kb_ids))
    )

    # 确定预期结果
    expected_failed_set = {
        kb_id for kb_id, failed in zip(kb_ids, failures) if failed
    }
    expected_degraded = len(expected_failed_set) > 0

    async def scenario():
        mock_hybrid = AsyncMock()

        # 为每个源构建 search side_effect：失败则抛异常，成功则返回结果
        side_effects = []
        for i, (kb_id, should_fail) in enumerate(zip(kb_ids, failures)):
            if should_fail:
                side_effects.append(RuntimeError(f"模拟 {kb_id} 失败"))
            else:
                side_effects.append([_make_result(f"c-{kb_id}-{i}")])

        mock_hybrid.search = AsyncMock(side_effect=side_effects)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id=kb_id, priority=1.0 if i == 0 else 0.8)
            for i, kb_id in enumerate(kb_ids)
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)
        return result

    result: MultiKBSearchResult = _run_async(scenario())

    # 断言 Property 4
    assert result.degraded == expected_degraded, (
        f"degraded={result.degraded}, expected={expected_degraded}, "
        f"failed_kb_ids={result.failed_kb_ids}, expected_failed={expected_failed_set}"
    )
    assert set(result.failed_kb_ids) == expected_failed_set, (
        f"failed_kb_ids={set(result.failed_kb_ids)}, expected={expected_failed_set}"
    )


@settings(max_examples=200, deadline=None)
@given(kb_ids=_kb_ids_st)
def test_property_all_success_not_degraded(kb_ids):
    """Feature: retrieval-pipeline-hardening, Property 4: 全成功时 degraded=False

    For any 检索源集合且全部成功时，degraded SHALL 为 False 且 failed_kb_ids 为空。

    **Validates: Requirements 1.2**
    """

    async def scenario():
        mock_hybrid = AsyncMock()
        # 所有源成功返回结果
        side_effects = [
            [_make_result(f"c-{kb_id}-{i}")]
            for i, kb_id in enumerate(kb_ids)
        ]
        mock_hybrid.search = AsyncMock(side_effect=side_effects)
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id=kb_id, priority=1.0 if i == 0 else 0.8)
            for i, kb_id in enumerate(kb_ids)
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)
        return result

    result: MultiKBSearchResult = _run_async(scenario())

    assert result.degraded is False
    assert result.failed_kb_ids == []


# ============================================================
# 单元测试：降级透传至 _retrieve_multi_kb 返回值
# ============================================================


class TestDegradedPropagation:
    """测试降级状态透传到 MultiKBRetriever.search 返回结构"""

    @pytest.mark.asyncio
    async def test_single_source_failure_sets_degraded_and_failed_ids(self):
        """某源失败时，MultiKBSearchResult.degraded=True 且 failed_kb_ids 含该源。"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1", score=0.9)],
                RuntimeError("Milvus connection timeout"),
                [_make_result("c3", score=0.7)],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-broken", priority=0.8),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.7),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is True
        assert result.failed_kb_ids == ["kb-broken"]
        # 成功源的结果仍然返回
        chunk_ids = [r.chunk_id for r in result.results]
        assert "c1" in chunk_ids
        assert "c3" in chunk_ids

    @pytest.mark.asyncio
    async def test_all_sources_success_no_degradation(self):
        """全部源成功时，degraded=False 且 failed_kb_ids 为空。"""
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
            KBRetrievalConfig(kb_id="kb-main", priority=1.0),
            KBRetrievalConfig(kb_id="kb-aux", priority=0.8),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is False
        assert result.failed_kb_ids == []

    @pytest.mark.asyncio
    async def test_multiple_sources_failure_accumulates_failed_ids(self):
        """多个源失败时，failed_kb_ids 包含所有失败源。"""
        mock_hybrid = AsyncMock()
        mock_hybrid.search = AsyncMock(
            side_effect=[
                [_make_result("c1")],
                RuntimeError("timeout"),
                TimeoutError("cancelled"),
                [_make_result("c4")],
            ]
        )
        mock_hybrid.rerank_and_expand = AsyncMock(side_effect=_rerank_passthrough)

        retriever = MultiKBRetriever(hybrid_retriever=mock_hybrid)
        kb_configs = [
            KBRetrievalConfig(kb_id="kb-ok1", priority=1.0),
            KBRetrievalConfig(kb_id="kb-fail1", priority=0.8),
            KBRetrievalConfig(kb_id="kb-fail2", priority=0.7),
            KBRetrievalConfig(kb_id="kb-ok2", priority=0.6),
        ]

        result = await retriever.search("查询", kb_configs, top_k=10)

        assert result.degraded is True
        assert set(result.failed_kb_ids) == {"kb-fail1", "kb-fail2"}


# ============================================================
# 单元测试：sanitize_for_log 基本用例
# ============================================================


class TestSanitizeForLog:
    """测试 sanitize_for_log 日志脱敏函数"""

    def test_replaces_newline(self):
        """换行符 \\n 替换为空格"""
        assert sanitize_for_log("line1\nline2") == "line1 line2"

    def test_replaces_carriage_return(self):
        """回车符 \\r 替换为空格"""
        assert sanitize_for_log("line1\rline2") == "line1 line2"

    def test_replaces_tab(self):
        """制表符 \\t 替换为空格"""
        assert sanitize_for_log("col1\tcol2") == "col1 col2"

    def test_replaces_crlf_combination(self):
        """CRLF 组合替换为两个空格"""
        assert sanitize_for_log("line1\r\nline2") == "line1  line2"

    def test_mixed_control_chars(self):
        """混合包含 CR/LF/Tab 全部替换"""
        assert sanitize_for_log("a\rb\nc\td") == "a b c d"

    def test_no_control_chars_unchanged(self):
        """无控制字符的普通字符串不变"""
        assert sanitize_for_log("正常文本 hello") == "正常文本 hello"

    def test_empty_string(self):
        """空字符串返回空"""
        assert sanitize_for_log("") == ""

    def test_non_string_input_coerced(self):
        """非字符串输入（如异常对象）先 str() 转换再脱敏"""
        err = RuntimeError("error\nmessage")
        result = sanitize_for_log(err)
        assert "\n" not in result
        assert "error message" in result

    def test_only_control_chars(self):
        """仅含控制字符时全部变为空格"""
        assert sanitize_for_log("\r\n\t") == "   "
