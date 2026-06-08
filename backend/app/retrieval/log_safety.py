"""日志脱敏工具（防 CR/LF 日志注入）。

降级/失败日志常把来源标识（kb_id、异常文本等）拼入日志行。若这些值含
回车 / 换行 / 制表符，攻击者可借此伪造日志行（CR/LF 日志注入）。统一经
``sanitize_for_log`` 将 ``\\r`` / ``\\n`` / ``\\t`` 替换为空格后再入日志。

采用日志脱敏的通用思路。原为任务 2 在 ``hybrid.py`` 内联的
``replace`` 链，任务 3 抽取为公共函数供 hybrid / multi_kb / chat 复用。
"""

from __future__ import annotations


def sanitize_for_log(s: object) -> str:
    """将任意值转为字符串并替换 CR/LF/Tab，防日志注入。

    Args:
        s: 待脱敏的值（异常对象、kb_id 等任意类型，内部统一 ``str()``）。

    Returns:
        ``\\r`` / ``\\n`` / ``\\t`` 均替换为空格后的单行字符串。
    """
    return str(s).replace("\r", " ").replace("\n", " ").replace("\t", " ")
