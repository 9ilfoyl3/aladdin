"""路由决策纯函数 _resolve_retrieval_route 单元测试

agent-session-source-unification Task 8：覆盖 design「Route Resolution 真值表」全部组合，
重点 Property 2（Agent 不降级）：agent + session_has_files=True → AGENT 而非 MULTI_KB。
"""

import pytest

from app.api.chat import Route, _resolve_retrieval_route


# ---- agent 模式：恒走 AGENT（除 skip / 无源），不因会话文件降级（Property 2） ----

def test_agent_with_kb_and_files_is_agent():
    """agent + 选库 + 有会话文件 → AGENT（关键反降级用例）。"""
    assert _resolve_retrieval_route("agent", ["kbA"], True, False) is Route.AGENT


def test_agent_with_kb_no_files_is_agent():
    """agent + 选库 + 无会话文件 → AGENT。"""
    assert _resolve_retrieval_route("agent", ["kbA"], False, False) is Route.AGENT


def test_agent_multi_kb_no_files_is_agent():
    """agent + 多选库 + 无会话文件 → AGENT（修复多库只用主库的局限，Req 2.2）。"""
    assert _resolve_retrieval_route("agent", ["kbA", "kbB"], False, False) is Route.AGENT


def test_agent_only_session_files_is_agent():
    """agent + 未选库 + 有会话文件 → AGENT（仅会话源）。"""
    assert _resolve_retrieval_route("agent", [], True, False) is Route.AGENT


def test_agent_no_source_is_none():
    """agent + 未选库 + 无会话文件 → NONE（纯 LLM 兜底）。"""
    assert _resolve_retrieval_route("agent", [], False, False) is Route.NONE


def test_agent_skip_is_chitchat():
    """agent + skip_retrieval → CHITCHAT（即便有源也跳过检索）。"""
    assert _resolve_retrieval_route("agent", ["kbA"], True, True) is Route.CHITCHAT


# ---- 非 agent（hybrid/direct）：保持改造前路由（Property 4） ----

@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_multi_kb(mode):
    """非 agent + 多选库 → MULTI_KB。"""
    assert _resolve_retrieval_route(mode, ["kbA", "kbB"], False, False) is Route.MULTI_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_single_kb_with_files_is_multi_kb(mode):
    """非 agent + 单库 + 有会话文件 → MULTI_KB（会话源经 MultiKB 接入，现状不变）。"""
    assert _resolve_retrieval_route(mode, ["kbA"], True, False) is Route.MULTI_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_only_session_files_is_multi_kb(mode):
    """非 agent + 未选库 + 有会话文件 → MULTI_KB（仅会话源，Req 1.4）。"""
    assert _resolve_retrieval_route(mode, [], True, False) is Route.MULTI_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_single_kb_no_files_is_single_kb(mode):
    """非 agent + 单库（单选字段）+ 无会话文件 + 未用多选字段 → SINGLE_KB。"""
    assert _resolve_retrieval_route(mode, ["kbA"], False, False, multi_kb_requested=False) is Route.SINGLE_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_single_element_multi_field_is_multi_kb(mode):
    """非 agent + 仅 1 个库但用了多选字段 kb_ids → MULTI_KB（精确复刻改造前 use_multi_kb）。"""
    assert _resolve_retrieval_route(mode, ["kbA"], False, False, multi_kb_requested=True) is Route.MULTI_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_multi_field_defensive_len_gt1(mode):
    """非 agent + 多库 + 即便漏传 multi_kb_requested → 仍走 MULTI_KB（len>1 防御兜底）。"""
    assert _resolve_retrieval_route(mode, ["kbA", "kbB"], False, False, multi_kb_requested=False) is Route.MULTI_KB


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_skip_is_chitchat(mode):
    """非 agent + skip_retrieval → CHITCHAT。"""
    assert _resolve_retrieval_route(mode, ["kbA"], False, True) is Route.CHITCHAT


@pytest.mark.parametrize("mode", ["hybrid", "direct"])
def test_non_agent_no_source_is_none(mode):
    """非 agent + 无库 + 无会话文件 → NONE。"""
    assert _resolve_retrieval_route(mode, [], False, False) is Route.NONE
