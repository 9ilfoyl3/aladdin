"""app.retrieval.textutil 单元测试：词级分词与 Jaccard

覆盖：
- 中文 jieba 分词得到词级 token（区别于字符级）
- 纯英文按空白切分、过滤单字
- Jaccard 边界（空集、相同、对称、范围）
- jieba 不可用时退化字符 bigram 的分词与降级开关
"""

import importlib

import pytest

from app.retrieval import textutil
from app.retrieval.textutil import jaccard, tokenize


class TestTokenize:
    """tokenize() 词级分词"""

    def test_empty(self):
        assert tokenize("") == frozenset()
        assert tokenize("   ") == frozenset()

    def test_chinese_word_level(self):
        """中文得到词级 token（含多字词，过滤单字）"""
        tokens = tokenize("检索结果分析")
        # 至少应包含「检索」「结果」等多字词，且不应是逐字拆分
        assert any(len(t) >= 2 for t in tokens)
        # 单字 token 被过滤
        assert all(len(t) > 1 for t in tokens)

    def test_english_whitespace(self):
        """纯英文按空白切分并小写化，过滤单字"""
        tokens = tokenize("Hello World a b")
        assert "hello" in tokens
        assert "world" in tokens
        # 单字 token a / b 被过滤
        assert "a" not in tokens
        assert "b" not in tokens

    def test_punctuation_filtered(self):
        """纯标点 token 被过滤"""
        tokens = tokenize("hello , . ! world")
        assert "hello" in tokens
        assert "world" in tokens
        assert "," not in tokens

    def test_returns_frozenset(self):
        """返回可哈希的 frozenset"""
        assert isinstance(tokenize("测试文本内容"), frozenset)


class TestJaccard:
    """jaccard() 相似度"""

    def test_both_empty(self):
        assert jaccard(frozenset(), frozenset()) == 0.0

    def test_one_empty(self):
        assert jaccard(frozenset({"a"}), frozenset()) == 0.0

    def test_identical(self):
        s = frozenset({"x", "y", "z"})
        assert jaccard(s, s) == 1.0

    def test_partial(self):
        a = frozenset({"a", "b", "c", "d"})
        b = frozenset({"c", "d", "e", "f"})
        # inter=2, union=6
        assert abs(jaccard(a, b) - 2.0 / 6.0) < 1e-9

    def test_symmetric(self):
        a = frozenset({"a", "b", "c"})
        b = frozenset({"b", "c", "d", "e"})
        assert jaccard(a, b) == jaccard(b, a)

    def test_range_bounded(self):
        a = tokenize("合同违约责任条款的详细说明")
        b = tokenize("合同金额计算方式的相关规定")
        assert 0.0 <= jaccard(a, b) <= 1.0


class TestCharBigramFallback:
    """jieba 不可用时退化字符 bigram"""

    def test_char_bigrams_basic(self):
        assert textutil._char_bigrams("检索结果") == ["检索", "索结", "结果"]

    def test_char_bigrams_single_char(self):
        assert textutil._char_bigrams("检") == ["检"]

    def test_char_bigrams_strips_whitespace(self):
        assert textutil._char_bigrams("检 索") == ["检索"]

    def test_tokenize_uses_bigram_when_jieba_unavailable(self, monkeypatch):
        """强制 jieba 不可用时，中文走字符 bigram 分词且仍可算 Jaccard"""
        monkeypatch.setattr(textutil, "_get_jieba", lambda: None)
        tokens = tokenize("检索结果")
        assert tokens == frozenset({"检索", "索结", "结果"})
        # 与自身 Jaccard 仍为 1.0，链路不因缺包中断
        assert jaccard(tokens, tokens) == 1.0


@pytest.fixture(autouse=True)
def _reset_jieba_state():
    """每个测试后复位 jieba 延迟加载状态，避免 monkeypatch 跨用例污染。"""
    yield
    textutil._jieba_mod = None
    textutil._jieba_unavailable = False
    importlib.reload  # noqa: B018 — 占位，保持显式 import 不被裁剪
