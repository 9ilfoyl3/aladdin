"""流式 content 路由器

ReAct 循环中，LLM 的普通 content（response_type="content"）在流式阶段语义不定：
- 若本轮随后发起工具调用，这些 content 是「调用前的思考」→ 应进思考面板
- 若本轮 natural_stop，这些 content 是模型直接给的「回答」

更棘手的是：部分模型（如千问）function-calling 能力弱，不发起标准 tool_call，
而是把 final_answer 的调用「写成纯文本 JSON」直接输出到 content，例如：
    {"answer": "你好！有什么可以帮你的吗？"}
若不识别，这段原始 JSON 会被当作答案/思考原样展示给用户。

本路由器在流式阶段对普通 content 做最小且确定的判别：
- 一旦确认 content 以 `{` 开头并包含 "answer" 键 → 判定为「内联 final_answer」，
  用 JSONFieldExtractor 逐 token 提取 answer 字段值，作为答案正文流式输出；
- 一旦确认首个非空白字符不是 `{`（或缓冲超过探测上限仍未出现 answer 键）
  → 判定为「思考」，原样作为思考流式输出。

判别在累积到足够信息前会短暂缓冲，确认后立即冲刷缓冲，不丢内容、不抢跑。
"""

from __future__ import annotations

from app.models.llm.json_field_extractor import JSONFieldExtractor

# 缓冲探测上限：以 `{` 开头但累积超过该字符数仍未出现 "answer" 键，判定为思考。
# 取值需大于 `{"answer"` 的最长合理前缀（含空白），又不至于缓冲过多内容。
_PROBE_LIMIT = 48


class ContentStreamRouter:
    """将 LLM 普通 content 流路由为「思考」或「内联 final_answer 答案」。"""

    MODE_UNKNOWN = "unknown"
    MODE_THOUGHT = "thought"
    MODE_ANSWER = "answer"

    def __init__(self) -> None:
        self.mode = self.MODE_UNKNOWN
        self._buffer = ""
        self._extractor: JSONFieldExtractor | None = None
        self.answer_text = ""  # 内联答案累积（仅 MODE_ANSWER 下有值）

    def feed(self, delta: str) -> tuple[str, str]:
        """喂入一个 content 增量，返回 (kind, text)。

        kind ∈ {"thought", "answer", ""}：
          - "thought"：text 应作为思考（THOUGHT）发射
          - "answer" ：text 应作为答案正文（FINAL_ANSWER）发射
          - ""       ：本次无可发射内容（仍在缓冲探测中）
        """
        if not delta:
            return ("", "")

        if self.mode == self.MODE_THOUGHT:
            return ("thought", delta)

        if self.mode == self.MODE_ANSWER:
            assert self._extractor is not None
            chunk = self._extractor.feed(delta)
            self.answer_text += chunk
            return ("answer", chunk)

        # MODE_UNKNOWN：累积并尝试判别
        self._buffer += delta
        stripped = self._buffer.lstrip()
        if not stripped:
            return ("", "")  # 全是空白，继续等

        if stripped[0] == "{":
            if '"answer"' in stripped:
                # 确认是内联 final_answer JSON
                self.mode = self.MODE_ANSWER
                self._extractor = JSONFieldExtractor("answer")
                chunk = self._extractor.feed(self._buffer)
                self._buffer = ""
                self.answer_text += chunk
                return ("answer", chunk)
            if len(stripped) > _PROBE_LIMIT:
                # 以 { 开头但迟迟没有 answer 键，判为思考
                return self._switch_to_thought()
            return ("", "")  # 继续缓冲等待 "answer" 出现

        # 首个非空白字符不是 { → 普通思考
        return self._switch_to_thought()

    def flush(self) -> tuple[str, str]:
        """流结束时冲刷缓冲。仍处 UNKNOWN 的残留按思考处理。"""
        if self.mode == self.MODE_UNKNOWN and self._buffer:
            return self._switch_to_thought()
        return ("", "")

    def _switch_to_thought(self) -> tuple[str, str]:
        self.mode = self.MODE_THOUGHT
        buffered = self._buffer
        self._buffer = ""
        return ("thought", buffered) if buffered else ("", "")
