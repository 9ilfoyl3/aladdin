"""检索文本工具：词级分词与 Jaccard 相似度

供 MMR 去冗余计算「结果间冗余度」使用。

分词策略：
- 含中文：用 jieba 搜索模式分词，得到词级边界（如「检索结果」→「检索」「结果」），
  比字符级（→「检」「索」「结」「果」）更能反映语义单元，且能区分仅语序不同的文本
  （「甲诉乙」与「乙诉甲」词集合相同但不会被字符噪声放大）。
- 纯非中文：按空白切分。
- 统一过滤单字 token 与纯标点/符号，降低噪声。

jieba 不可用时（裁剪部署 / 离线环境未装）退化为字符 bigram（如「检索结果」→
「检索」「索结」「结果」），保证功能可用，仅去冗余精度略降，绝不因缺包而中断检索。
"""

from __future__ import annotations

import logging
import unicodedata

logger = logging.getLogger(__name__)

# jieba 延迟加载状态：None=未尝试，模块对象=可用，False=已尝试且不可用。
_jieba_mod: object | None = None
_jieba_unavailable: bool = False


def _get_jieba():
    """延迟加载 jieba，加载失败则永久退化（只记一次 WARNING）。

    首次切词时才触发字典构建，避免 import 期副作用拖慢启动。
    """
    global _jieba_mod, _jieba_unavailable
    if _jieba_mod is not None:
        return _jieba_mod
    if _jieba_unavailable:
        return None
    try:
        import warnings

        # jieba 内部 import pkg_resources 触发 setuptools 弃用告警，与本功能无关，抑制之。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba

        # 抑制 jieba 首次构建前缀字典的 INFO 噪声日志。
        jieba.setLogLevel(logging.WARNING)
        _jieba_mod = jieba
        return jieba
    except Exception as e:
        _jieba_unavailable = True
        logger.warning("jieba 不可用，MMR 分词退化为字符 bigram: %s", e)
        return None


def _contains_chinese(text: str) -> bool:
    """文本是否含 CJK 统一表意文字（用于选择分词路径）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _is_all_punct(token: str) -> bool:
    """token 是否全为标点 / 空白 / 符号（这类 token 无语义，应过滤）。"""
    for ch in token:
        cat = unicodedata.category(ch)
        if not (cat.startswith("P") or cat.startswith("S") or ch.isspace()):
            return False
    return True


def _char_bigrams(text: str) -> list[str]:
    """字符 bigram 切分（jieba 不可用时的退化分词）。

    去掉空白后按相邻两字滑窗，如「检索结果」→「检索」「索结」「结果」。
    """
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 2:
        return chars
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def tokenize(text: str) -> frozenset[str]:
    """将文本切分为去重的词级 token 集合。

    含中文走 jieba 搜索模式（不可用时退化字符 bigram），纯非中文按空白切分；
    统一小写化，过滤单字 token 与纯标点/符号。

    Returns:
        token 的 frozenset（可哈希，便于缓存与集合运算）；空文本返回空集。
    """
    text = text.strip().lower()
    if not text:
        return frozenset()

    if _contains_chinese(text):
        jb = _get_jieba()
        words = jb.lcut_for_search(text) if jb is not None else _char_bigrams(text)
    else:
        words = text.split()

    return frozenset(
        w
        for w in (token.strip() for token in words)
        if len(w) > 1 and not _is_all_punct(w)
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """两个 token 集合的 Jaccard 相似度，取值 [0.0, 1.0]。

    两个空集返回 0.0（与上游 searchutil.Jaccard 行为一致：空 ∩ 空 视为不相似）。
    """
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a) + len(b) - intersection
    return intersection / union if union else 0.0
