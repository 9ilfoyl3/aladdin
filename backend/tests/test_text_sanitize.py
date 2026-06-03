"""strip_think_blocks 单元测试"""

from app.agent.tools.text_sanitize import strip_think_blocks


def test_removes_think_block():
    assert strip_think_blocks("<think>reasoning</think>answer") == "answer"


def test_removes_multiline_think():
    text = "<think>\nstep1\nstep2\n</think>\n\nFinal answer"
    assert strip_think_blocks(text) == "Final answer"


def test_removes_multiple_blocks():
    assert strip_think_blocks("<think>a</think>X<think>b</think>Y") == "XY"


def test_no_think_returns_trimmed():
    assert strip_think_blocks("  hello  ") == "hello"


def test_empty():
    assert strip_think_blocks("") == ""
