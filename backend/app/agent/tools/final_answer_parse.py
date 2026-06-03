"""final_answer 参数的容错解析

LLM 生成 final_answer 工具调用的 arguments 时，偶尔会产出不合法的 JSON：
未转义的引号、尾随逗号、截断的右括号、把正则元字符（\\d、\\+）当字面量写入等。
若直接 json.loads 失败就把整段原始 JSON 当答案保存，用户会看到一坨 `{"answer":"..."}`。

本模块移植自 WeKnora internal/agent/tools 的三级容错策略：
  1. 严格 json.loads
  2. repair_json（修复尾随逗号、非法转义、括号配平）后再 loads
  3. 正则兜底抽取 "answer": "..." 字段

只要任一层拿到非空 answer 即返回。这样无论模型输出多脏，保存与展示的都是
答案正文本身，而非原始 JSON。
"""

from __future__ import annotations

import json
import re

# 兜底正则：从畸形 JSON 中尽力抽取 answer 字段值。
# (?:\\.|[^"\\])* 表示值体由「转义序列」或「非引号非反斜杠字符」组成，
# 从而正确跳过内部的 \" 转义引号。
_ANSWER_REGEX = re.compile(r'"answer"\s*:\s*"((?:\\.|[^"\\])*)"')

# JSON 合法转义字母
_VALID_ESCAPE_CHARS = set('"\\/bfnrtu')


def parse_final_answer_args(raw: str) -> tuple[str, bool]:
    """从 final_answer 的原始 arguments 中提取 answer 字段。

    Returns:
        (answer, ok)。ok=False 表示三层均无法恢复出非空答案，
        调用方应使用兜底文案，但仍须把该 tool_call 视为终止信号。
    """
    if not raw:
        return "", False

    # 1) 严格解析
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            answer = obj.get("answer", "")
            if isinstance(answer, str) and answer:
                return answer, True
    except (json.JSONDecodeError, TypeError):
        pass

    # 2) 修复后解析
    repaired = repair_json(raw)
    if repaired != raw:
        try:
            obj = json.loads(repaired)
            if isinstance(obj, dict):
                answer = obj.get("answer", "")
                if isinstance(answer, str) and answer:
                    return answer, True
        except (json.JSONDecodeError, TypeError):
            pass

    # 3) 正则兜底抽取
    m = _ANSWER_REGEX.search(raw)
    if m:
        body = m.group(1)
        # 先尝试按标准 JSON 字符串解码
        try:
            unquoted = json.loads('"' + body + '"')
            if isinstance(unquoted, str) and unquoted:
                return unquoted, True
        except (json.JSONDecodeError, TypeError):
            pass
        # 解码失败（含非法转义）：返回原始捕获，至少让用户看到内容
        if body:
            return body, True

    return "", False


def repair_json(s: str) -> str:
    """修复 LLM 输出中常见的 JSON 畸形，尽力使其可被 json.loads 解析。

    处理：缺失外层花括号、非法反斜杠转义、尾随逗号、括号/引号不配平。
    无法修复时返回原串（调用方自行处理解析失败）。
    """
    s = s.strip()
    if not s:
        return "{}"

    if s[0] != "{":
        # 模型可能漏了外层花括号
        if ":" in s or "=" in s:
            s = "{" + s + "}"
        else:
            return s

    # 顺序很重要：先修转义（不平衡的字符串会干扰后续逗号/括号扫描）
    s = _fix_invalid_escapes(s)
    s = _fix_trailing_commas(s)
    s = _balance_brackets(s)
    return s


def _fix_invalid_escapes(s: str) -> str:
    """把字符串内部的非法反斜杠转义改写为字面反斜杠（\\\\X）。

    JSON 仅允许 \\" \\\\ \\/ \\b \\f \\n \\r \\t \\u；其余如 \\d \\+ \\. 是解析错误，
    多为模型想传正则却没双重转义。改写后 json.loads 可成功，且解码出的字符串
    保留 \\X，符合模型意图。对已合法的 JSON 幂等。
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        # 字符串内的反斜杠
        if i + 1 >= n:
            out.append("\\\\")  # 末尾悬挂反斜杠
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in _VALID_ESCAPE_CHARS:
            out.append(ch)
            out.append(nxt)
            i += 2
        else:
            out.append("\\\\")
            out.append(nxt)
            i += 2
    return "".join(out)


def _fix_trailing_commas(s: str) -> str:
    """移除右括号/右花括号前的尾随逗号。"""
    result: list[str] = []
    in_string = False
    escaped = False
    n = len(s)
    for i, ch in enumerate(s):
        if escaped:
            escaped = False
            result.append(ch)
            continue
        if ch == "\\" and in_string:
            escaped = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            result.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < n and s[j] in " \t\n\r":
                j += 1
            if j < n and s[j] in "}]":
                continue  # 跳过该尾随逗号
        result.append(ch)
    return "".join(result)


def _balance_brackets(s: str) -> str:
    """补齐缺失的右括号/右花括号，并闭合未结束的字符串。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    if in_string:
        s += '"'
    for closer in reversed(stack):
        s += closer
    return s
