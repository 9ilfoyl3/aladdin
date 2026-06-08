"""流式 JSON 字段提取器

从 LLM tool_call 的增量 arguments 片段中提取指定字符串字段的值，
用于将 final_answer 工具的 answer 字段逐 token 流式输出为正文（answer），
或将 thinking 工具的 thought 字段流式输出为思考（thought）。

使用状态机跳过 JSON 前缀（如 `{"answer":"`）后定位值内容起点，再按 JSON 转义规则
安全地处理跨 chunk 的转义序列，避免把半个 `\\uXXXX` 或末尾悬挂的 `\\` 当作
普通字符吐给前端（这正是此前手写 `.replace()` 链产生斜杠/横杠乱码的根因）。

Python str 是 Unicode，每个码点占一个下标，httpx 解码后逐 chunk 拿到的都是完整
str，不会出现半个多字节 UTF-8 字符，因此无需字节级 UTF-8 边界处理；唯一需要
跨 chunk 防护的是 JSON 转义序列。
"""

from __future__ import annotations

# JSON 字符串中合法的转义字母（紧跟在反斜杠之后）
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}


def _find_field_value_start(buf: str, field: str) -> int:
    """定位字段字符串值内容的起始下标（开引号之后）。未找到返回 -1。"""
    key = '"' + field + '"'
    idx = buf.find(key)
    if idx < 0:
        return -1
    pos = idx + len(key)
    n = len(buf)
    # 跳过冒号与空白，直到值的开引号
    while pos < n:
        ch = buf[pos]
        if ch == ":" or ch in " \t\n\r":
            pos += 1
            continue
        if ch == '"':
            return pos + 1
        # 出现非预期字符（如值不是字符串），放弃提取
        return -1
    return -1  # 还没看到开引号


def _find_safe_end(value: str, start: int) -> tuple[int, bool]:
    """从 start 起扫描值内容，返回 (可安全输出到的下标, 是否遇到收尾引号)。

    遇到不完整的转义序列（末尾悬挂的 `\\`、不足 6 字符的 `\\uXXXX`）时，
    在其之前停下，等待后续 chunk 补全，避免吐出半个转义。
    """
    i = start
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\":
            if i + 1 >= n:
                return i, False  # 末尾悬挂的反斜杠，等待下一片段
            nxt = value[i + 1]
            if nxt == "u":
                if i + 5 >= n:
                    return i, False  # \uXXXX 不完整
                i += 6
            else:
                i += 2
        elif ch == '"':
            return i, True  # 值的收尾引号
        else:
            i += 1
    return i, False


def _unescape_json_string(s: str) -> str:
    """将 JSON 字符串转义序列还原为实际字符。"""
    if "\\" not in s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            simple = _SIMPLE_ESCAPES.get(nxt)
            if simple is not None:
                out.append(simple)
                i += 2
            elif nxt == "u":
                if i + 5 < n:
                    hex_str = s[i + 2 : i + 6]
                    try:
                        out.append(chr(int(hex_str, 16)))
                        i += 6
                    except ValueError:
                        # 非法十六进制，原样保留反斜杠
                        out.append(c)
                        i += 1
                else:
                    out.append(c)
                    i += 1
            else:
                # 未知转义：原样保留反斜杠 + 后续字符（不破坏内容）
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class JSONFieldExtractor:
    """从流式 JSON 片段中提取单个字符串字段的值。

    期望格式：``{"<field>":"...内容..."}``。每次 ``feed`` 一个增量片段，
    返回本次可安全输出的新内容（已完成 JSON 反转义）。当字段值的收尾引号
    被识别后，``done`` 置为 True，后续 ``feed`` 返回空串。

    用法::

        ex = JSONFieldExtractor("answer")
        for delta in arg_deltas:
            chunk = ex.feed(delta)
            if chunk:
                emit(chunk)
    """

    def __init__(self, field_name: str) -> None:
        self._field = field_name
        self._buffer = ""
        self._value_start = -1  # 值内容在 buffer 中的起始下标（开引号之后）
        self._last_emit = 0  # 已输出到值内容的下标
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, delta: str) -> str:
        """喂入一个增量片段，返回本次新增的、可安全输出的反转义内容。"""
        if self._done or not delta:
            if self._done:
                return ""
        self._buffer += delta

        if self._value_start < 0:
            idx = _find_field_value_start(self._buffer, self._field)
            if idx < 0:
                return ""  # 尚未看到字段值起点
            self._value_start = idx
            self._last_emit = 0

        value = self._buffer[self._value_start :]
        safe_end, finished = _find_safe_end(value, self._last_emit)
        if safe_end <= self._last_emit:
            if finished:
                self._done = True
            return ""

        raw_chunk = value[self._last_emit : safe_end]
        unescaped = _unescape_json_string(raw_chunk)
        self._last_emit = safe_end
        if finished:
            self._done = True
        return unescaped
