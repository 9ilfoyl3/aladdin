"""ContextManager 上下文管理模块单元测试

测试 token 估算、工具输出截断、消息压缩、历史 KB 结果脱敏等功能。
"""

import pytest

from app.agent.memory.context_manager import (
    ContextManager,
    estimate_tokens,
    redact_historical_kb_results,
    truncate_tool_output,
)


# ============================================================
# estimate_tokens Tests
# ============================================================


class TestEstimateTokens:
    """token 估算测试"""

    def test_estimate_tokens_chinese(self):
        """纯中文文本 token 估算：每字符 ≈ 0.67 tokens"""
        text = "你好世界"  # 4 个中文字符
        tokens = estimate_tokens(text)
        # 4 / 1.5 ≈ 2.67 → int(2.67) = 2, max(1, 2) = 2
        assert tokens == 2

    def test_estimate_tokens_english(self):
        """纯英文文本 token 估算：每字符 ≈ 0.25 tokens"""
        text = "hello world"  # 11 个 ASCII 字符（含空格）
        tokens = estimate_tokens(text)
        # 11 / 4 = 2.75 → int(2.75) = 2, max(1, 2) = 2
        assert tokens == 2

    def test_estimate_tokens_mixed(self):
        """中英混合文本 token 估算"""
        text = "你好hello"  # 2 中文 + 5 ASCII
        tokens = estimate_tokens(text)
        # 2/1.5 + 5/4 = 1.33 + 1.25 = 2.58 → int(2.58) = 2
        assert tokens == 2

    def test_estimate_tokens_empty(self):
        """空文本返回 0"""
        assert estimate_tokens("") == 0

    def test_estimate_tokens_single_char(self):
        """单字符至少返回 1"""
        assert estimate_tokens("a") >= 1

    def test_estimate_tokens_long_text(self):
        """长文本 token 估算合理"""
        text = "这是一段较长的中文文本，用于测试 token 估算的准确性。" * 10
        tokens = estimate_tokens(text)
        assert tokens > 0
        # 大约 26 个字符 * 10 = 260 字符，中文约 20 个 * 10 = 200
        # 200/1.5 + 60/4 ≈ 133 + 15 = 148
        assert tokens > 50


# ============================================================
# truncate_tool_output Tests
# ============================================================


class TestTruncateToolOutput:
    """工具输出截断测试"""

    def test_truncate_tool_output_short(self):
        """短文本（未超限）原样返回"""
        text = "短文本内容"
        result = truncate_tool_output(text, max_chars=100)
        assert result == text

    def test_truncate_tool_output_exact_limit(self):
        """恰好等于限制时原样返回"""
        text = "a" * 100
        result = truncate_tool_output(text, max_chars=100)
        assert result == text

    def test_truncate_tool_output_long(self):
        """超长文本截断：保留头部 40% + 截断标记 + 尾部 40%"""
        text = "x" * 1000
        result = truncate_tool_output(text, max_chars=500)

        # 应包含截断标记
        assert "[...truncated" in result
        assert "chars...]" in result

        # 头部 400 字符 + 尾部 400 字符 + 截断标记
        assert result.startswith("x" * 400)
        assert result.endswith("x" * 400)

    def test_truncate_tool_output_preserves_head_tail(self):
        """截断后头尾内容正确"""
        # 构造有区分度的文本
        text = "HEAD" * 100 + "MIDDLE" * 100 + "TAIL" * 100
        result = truncate_tool_output(text, max_chars=500)

        # 头部应包含 HEAD
        assert result.startswith("HEAD")
        # 尾部应包含 TAIL
        assert result.endswith("TAIL" * 10)  # 尾部 40% 应有 TAIL


# ============================================================
# ContextManager.compress_messages Tests
# ============================================================


class TestCompressMessages:
    """消息压缩测试"""

    def setup_method(self):
        self.cm = ContextManager()

    def test_compress_messages_under_threshold(self):
        """总 token 低于 80% 阈值时原样返回"""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        # max_tokens 设置很大，确保不触发压缩
        result = self.cm.compress_messages(messages, max_tokens=100000)
        assert result == messages

    def test_compress_messages_over_threshold(self):
        """总 token 超过 80% 阈值时压缩中间轮次的 tool results"""
        # 构造超过阈值的消息列表
        long_content = "这是一段很长的内容。" * 200  # 大量中文内容

        messages = [
            {"role": "system", "content": "System prompt"},
            # 第一轮（会被压缩）
            {"role": "user", "content": "第一个问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "knowledge_search",
                            "arguments": '{"queries": ["q1"]}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": long_content},
            # 第二轮（会被压缩）
            {"role": "user", "content": "第二个问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_2",
                        "type": "function",
                        "function": {
                            "name": "grep_chunks",
                            "arguments": '{"query": "test"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_2", "content": long_content},
            # 第三轮（保留）
            {"role": "user", "content": "第三个问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_3",
                        "type": "function",
                        "function": {
                            "name": "knowledge_search",
                            "arguments": '{"queries": ["q3"]}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_3", "content": long_content},
            # 第四轮（保留）
            {"role": "user", "content": "第四个问题"},
            {"role": "assistant", "content": "最终回答"},
        ]

        # 设置较小的 max_tokens 触发压缩
        result = self.cm.compress_messages(messages, max_tokens=100)

        # system prompt 应保留
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "System prompt"

        # 中间轮次的 tool results 应被压缩为 summary
        compressed_tools = [
            m for m in result
            if m.get("role") == "tool" and "[Summary:" in (m.get("content") or "")
        ]
        assert len(compressed_tools) > 0

        # 最后 2 轮的 tool results 应保留原始内容
        # 找到最后一个 tool 消息（属于第三轮，应保留）
        last_tool_msgs = [
            m for m in result
            if m.get("role") == "tool" and "[Summary:" not in (m.get("content") or "")
        ]
        assert len(last_tool_msgs) > 0
        assert last_tool_msgs[-1]["content"] == long_content

    def test_compress_messages_empty(self):
        """空消息列表原样返回"""
        result = self.cm.compress_messages([], max_tokens=1000)
        assert result == []

    def test_compress_messages_two_rounds_no_compress(self):
        """只有 2 轮或更少时不压缩"""
        messages = [
            {"role": "system", "content": "System" * 1000},
            {"role": "user", "content": "Q1" * 1000},
            {"role": "assistant", "content": "A1" * 1000},
            {"role": "user", "content": "Q2" * 1000},
            {"role": "assistant", "content": "A2" * 1000},
        ]
        # 即使超过阈值，只有 2 轮也不压缩
        result = self.cm.compress_messages(messages, max_tokens=10)
        assert result == messages


# ============================================================
# redact_historical_kb_results Tests
# ============================================================


class TestRedactHistoricalKbResults:
    """历史 KB 结果脱敏测试"""

    def test_redact_previous_iteration_results(self):
        """非当前轮次的 knowledge_search 结果被替换为占位符"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "问题"},
            # 第 0 轮 assistant + tool result
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_0",
                        "function": {"name": "knowledge_search", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_0",
                "content": "第一轮检索结果内容很长...",
            },
            # 第 1 轮 assistant + tool result（当前轮）
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "function": {"name": "knowledge_search", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_1",
                "content": "第二轮检索结果",
            },
        ]

        # current_iteration=1，第 0 轮的结果应被脱敏
        result = redact_historical_kb_results(messages, current_iteration=1)

        # 第 0 轮的 tool result 应被替换
        tool_msg_0 = result[3]
        assert "redacted" in tool_msg_0["content"].lower()

        # 第 1 轮（当前轮）的 tool result 应保留
        tool_msg_1 = result[5]
        assert tool_msg_1["content"] == "第二轮检索结果"

    def test_redact_grep_chunks_results(self):
        """grep_chunks 工具结果也会被脱敏"""
        messages = [
            {"role": "system", "content": "System"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_grep",
                        "function": {"name": "grep_chunks", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_grep",
                "content": "grep 结果内容",
            },
        ]

        # current_iteration=1，第 0 轮的 grep_chunks 应被脱敏
        result = redact_historical_kb_results(messages, current_iteration=1)

        tool_msg = result[2]
        assert "redacted" in tool_msg["content"].lower()

    def test_non_kb_tools_not_redacted(self):
        """非 KB 工具（如 thinking）的结果不被脱敏"""
        messages = [
            {"role": "system", "content": "System"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_think",
                        "function": {"name": "thinking", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_think",
                "content": "思考内容不应被脱敏",
            },
        ]

        result = redact_historical_kb_results(messages, current_iteration=1)

        tool_msg = result[2]
        assert tool_msg["content"] == "思考内容不应被脱敏"

    def test_current_iteration_not_redacted(self):
        """当前轮次的 KB 结果不被脱敏"""
        messages = [
            {"role": "system", "content": "System"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_current",
                        "function": {"name": "knowledge_search", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_current",
                "content": "当前轮次结果",
            },
        ]

        # current_iteration=0，第 0 轮不应被脱敏
        result = redact_historical_kb_results(messages, current_iteration=0)

        tool_msg = result[2]
        assert tool_msg["content"] == "当前轮次结果"

    def test_empty_messages(self):
        """空消息列表返回空列表"""
        result = redact_historical_kb_results([], current_iteration=0)
        assert result == []
