"""JSONFieldExtractor 单元测试

验证从流式 JSON 片段中增量提取字符串字段值的正确性，重点覆盖：
- 一次性 / 逐字符 / 任意切分的 chunk 边界
- JSON 转义（\\n \\t \\" \\\\ \\uXXXX）的正确还原
- 跨 chunk 的不完整转义序列不被提前吐出（旧手写 replace 链的乱码根因）
"""

from app.models.llm.json_field_extractor import JSONFieldExtractor


def _feed_all(field: str, chunks: list[str]) -> str:
    ex = JSONFieldExtractor(field)
    out = []
    for c in chunks:
        out.append(ex.feed(c))
    return "".join(out)


class TestBasic:
    def test_one_chunk(self):
        assert _feed_all("answer", ['{"answer":"complete answer here"}']) == "complete answer here"

    def test_with_space_around_colon(self):
        assert _feed_all("answer", ['{"answer": "hi"}']) == "hi"

    def test_empty_value(self):
        assert _feed_all("answer", ['{"answer":""}']) == ""

    def test_thought_field(self):
        assert _feed_all("thought", ['{"thought":"planning"}']) == "planning"


class TestChunking:
    def test_char_by_char(self):
        payload = '{"answer":"Hello world"}'
        assert _feed_all("answer", list(payload)) == "Hello world"

    def test_arbitrary_splits(self):
        # 把 JSON 切成不规则片段
        chunks = ['{"ans', 'wer":', '"part', 'ial ', 'stream"}']
        assert _feed_all("answer", chunks) == "partial stream"

    def test_prefix_arrives_before_value(self):
        ex = JSONFieldExtractor("answer")
        # 还没到值起点，不应吐内容
        assert ex.feed('{"ans') == ""
        assert ex.feed('wer":"') == ""
        assert ex.feed("abc") == "abc"


class TestEscapes:
    def test_newline_and_quote(self):
        # {"answer":"line1\nline2 and \"q\""}
        raw = '{"answer":"line1\\nline2 and \\"q\\""}'
        assert _feed_all("answer", [raw]) == 'line1\nline2 and "q"'

    def test_backslash_literal(self):
        # {"answer":"C:\\path"}  → C:\path
        raw = '{"answer":"C:\\\\path"}'
        assert _feed_all("answer", [raw]) == "C:\\path"

    def test_tab(self):
        raw = '{"answer":"a\\tb"}'
        assert _feed_all("answer", [raw]) == "a\tb"

    def test_unicode_escape(self):
        raw = '{"answer":"\\u4f60\\u597d"}'
        assert _feed_all("answer", [raw]) == "你好"

    def test_incomplete_escape_at_chunk_boundary(self):
        # 转义被切断：第一片以 \\ 结尾，不应提前吐出半个转义
        ex = JSONFieldExtractor("answer")
        out1 = ex.feed('{"answer":"a\\')
        # 此时不应吐出反斜杠（可能是 \n / \" 等的前半）
        assert out1 == "a"
        out2 = ex.feed('nb"}')
        assert out2 == "\nb"

    def test_incomplete_unicode_at_boundary(self):
        ex = JSONFieldExtractor("answer")
        out1 = ex.feed('{"answer":"\\u4f')
        assert out1 == ""  # \u4f 不完整，必须等
        out2 = ex.feed('60!"}')
        assert out2 == "你!"


class TestDone:
    def test_done_after_closing_quote(self):
        ex = JSONFieldExtractor("answer")
        ex.feed('{"answer":"hi"}')
        assert ex.done is True
        # done 后再喂入返回空
        assert ex.feed("more") == ""

    def test_not_done_until_closing_quote(self):
        ex = JSONFieldExtractor("answer")
        ex.feed('{"answer":"partial')
        assert ex.done is False
