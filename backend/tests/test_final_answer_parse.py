"""final_answer 参数容错解析单元测试

验证三级容错（strict → repair → regex）能从各种畸形 LLM 输出中恢复出答案正文，
确保保存与展示的永远是答案本身而非原始 JSON。
"""

from app.agent.tools.final_answer_parse import parse_final_answer_args, repair_json


class TestStrictParse:
    def test_valid_json(self):
        ans, ok = parse_final_answer_args('{"answer":"hello"}')
        assert ok is True
        assert ans == "hello"

    def test_valid_json_with_escapes(self):
        ans, ok = parse_final_answer_args('{"answer":"line1\\nline2"}')
        assert ok is True
        assert ans == "line1\nline2"

    def test_unicode(self):
        ans, ok = parse_final_answer_args('{"answer":"你好世界"}')
        assert ok is True
        assert ans == "你好世界"

    def test_empty_answer_is_not_ok(self):
        ans, ok = parse_final_answer_args('{"answer":""}')
        assert ok is False


class TestRepairParse:
    def test_trailing_comma(self):
        ans, ok = parse_final_answer_args('{"answer":"hi",}')
        assert ok is True
        assert ans == "hi"

    def test_missing_closing_brace(self):
        ans, ok = parse_final_answer_args('{"answer":"truncated answer"')
        assert ok is True
        assert ans == "truncated answer"

    def test_invalid_regex_escape(self):
        # 模型把正则 \d 直接写进字符串（非法 JSON 转义）
        ans, ok = parse_final_answer_args('{"answer":"match \\d+ digits"}')
        assert ok is True
        assert "\\d+" in ans


class TestRegexFallback:
    def test_unescaped_inner_quotes_after_answer(self):
        # 无法被 repair 修复的畸形，靠正则兜底抽取
        raw = '{"answer":"see [1]","extra":}'
        ans, ok = parse_final_answer_args(raw)
        assert ok is True
        assert ans == "see [1]"


class TestUnrecoverable:
    def test_completely_broken(self):
        ans, ok = parse_final_answer_args("not json at all no answer field")
        assert ok is False
        assert ans == ""

    def test_empty_input(self):
        ans, ok = parse_final_answer_args("")
        assert ok is False


class TestRepairJSON:
    def test_idempotent_on_valid(self):
        s = '{"answer":"ok"}'
        # repair 不应破坏已合法的 JSON 语义
        import json

        assert json.loads(repair_json(s)) == {"answer": "ok"}

    def test_balances_brackets(self):
        out = repair_json('{"a":["x","y"')
        import json

        # 修复后应可解析
        assert json.loads(out) == {"a": ["x", "y"]}
