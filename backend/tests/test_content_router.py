"""ContentStreamRouter 单元测试

验证流式 content 被正确路由为「思考」或「内联 final_answer 答案」。
"""

from app.agent.content_router import ContentStreamRouter


def _run(chunks: list[str]) -> tuple[list[str], list[str]]:
    """喂入分片，返回 (thought_texts, answer_texts)。"""
    r = ContentStreamRouter()
    thoughts, answers = [], []
    for c in chunks:
        kind, text = r.feed(c)
        if text and kind == "thought":
            thoughts.append(text)
        elif text and kind == "answer":
            answers.append(text)
    kind, text = r.flush()
    if text and kind == "thought":
        thoughts.append(text)
    elif text and kind == "answer":
        answers.append(text)
    return thoughts, answers


class TestThoughtRouting:
    def test_plain_text_is_thought(self):
        thoughts, answers = _run(["让我想想", "这个问题"])
        assert "".join(thoughts) == "让我想想这个问题"
        assert answers == []

    def test_text_starting_with_brace_but_not_answer(self):
        # 以 { 开头但不是 final_answer JSON（超过探测上限判为思考）
        long = "{this is just some reasoning text that happens to start with a brace and keeps going"
        thoughts, answers = _run([long])
        assert "".join(thoughts) == long
        assert answers == []


class TestInlineAnswerRouting:
    def test_full_json_one_chunk(self):
        thoughts, answers = _run(['{"answer": "你好！有什么可以帮你的吗？"}'])
        assert "".join(answers) == "你好！有什么可以帮你的吗？"
        assert thoughts == []

    def test_json_split_across_chunks(self):
        chunks = ['{"answer": "', "你好！", "有什么", '可以帮你的吗？"}']
        thoughts, answers = _run(chunks)
        assert "".join(answers) == "你好！有什么可以帮你的吗？"
        assert thoughts == []

    def test_json_with_leading_whitespace(self):
        thoughts, answers = _run(['  {"answer":"hi"}'])
        assert "".join(answers) == "hi"
        assert thoughts == []

    def test_json_with_escapes(self):
        thoughts, answers = _run(['{"answer":"line1\\nline2"}'])
        assert "".join(answers) == "line1\nline2"

    def test_brace_arrives_before_answer_key(self):
        # { 先到，answer 键随后到达
        r = ContentStreamRouter()
        k1, t1 = r.feed("{")
        assert t1 == ""  # 缓冲中
        k2, t2 = r.feed('"answer":"ok"}')
        assert k2 == "answer"
        assert t2 == "ok"
