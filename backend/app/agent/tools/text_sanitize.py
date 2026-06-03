"""Agent 输出文本清理

部分模型（DeepSeek、Qwen 等）会把思维链直接以 <think>...</think> 块嵌进
content 字段。这类内容属于推理过程，不应出现在展示给用户的答案里。本模块在
答案发射前统一剥离，使最终答案干净、与模型无关。

移植自 WeKnora internal/agent/tools/strip_think.go。
"""

from __future__ import annotations

import re

# (?s) 让 . 匹配换行，非贪婪匹配成对的 <think>…</think>
_THINK_BLOCK_RE = re.compile(r"(?s)<think>.*?</think>")


def strip_think_blocks(content: str) -> str:
    """移除 content 中的 <think>…</think> 块并修剪首尾空白。"""
    if not content:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", content)
    return cleaned.strip()
