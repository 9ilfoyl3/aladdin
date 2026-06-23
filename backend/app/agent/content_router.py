"""流式 content 路由器

ReAct 循环中，LLM 的普通 content（response_type="content"）在流式阶段语义不定：
- 若本轮随后发起工具调用，这些 content 是「调用前的思考」→ 应进思考面板
- 若本轮 natural_stop，这些 content 是模型直接给的「回答」

更棘手的是：部分模型（如千问）function-calling 能力弱，不发起标准 tool_call，
而是把 final_answer 的调用「写成纯文本 JSON」直接输出到 content，例如：
    {"answer": "你好！有什么可以帮你的吗？"}
甚至会把工具名也一并写到 content 前面（偶发，取决于模型当次的格式化方式）：
    final_answer {"answer": "你好！有什么可以帮你的吗？"}
    final_answer({"answer": "..."})
若不识别，这段原始 JSON（连同 `final_answer` 前缀）会被当作答案/思考原样展示给用户。

本路由器在流式阶段对普通 content 做最小且确定的判别：
- 一旦确认 content（剥除可忽略的 `final_answer` 工具名前缀后）以 `{` 开头并包含
  "answer" 键 → 判定为「内联 final_answer」，用 JSONFieldExtractor 逐 token 提取
  answer 字段值，作为答案正文流式输出；
- 一旦确认首个有效字符不是 `{`、也不是 `final_answer` 前缀（或缓冲超过探测上限仍未
  出现 answer 键）→ 判定为「思考」，原样作为思考流式输出。

判别在累积到足够信息前会短暂缓冲，确认后立即冲刷缓冲，不丢内容、不抢跑。
"""

from __future__ import annotations

from app.models.llm.json_field_extractor import JSONFieldExtractor

# 缓冲探测上限：见到 `{` 后累积超过该字符数仍未出现 "answer" 键，判定为思考。
# 取值需大于 `{"answer"` 的最长合理前缀（含空白），又不至于缓冲过多内容。
_PROBE_LIMIT = 48

# 可被忽略的内联工具名前缀（弱 function-calling 模型偶发写到 content 前面）。
_TOOL_NAME = "final_answer"

# 见到 `{` 之前的前缀探测上限：工具名 + 分隔符（如 `final_answer( ` / `final_answer: `）。
# 超过此长度仍未出现 `{`，说明不是可忽略前缀，判为思考。
_PREFIX_PROBE_LIMIT = len(_TOOL_NAME) + 8

# 工具名与 `{` 之间允许出现的分隔符（空白、冒号、括号、等号）。
_PREFIX_SEPARATORS = " \t\n\r:(=" + '"'


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

        # 剥除可忽略的 `final_answer` 工具名前缀后，定位真正的 JSON 起点。
        # decision ∈ {"json", "thought", "wait"}
        decision, json_part = self._locate_json(stripped)
        if decision == "wait":
            return ("", "")  # 仍在探测前缀，继续缓冲
        if decision == "thought":
            return self._switch_to_thought()

        # decision == "json"：json_part 以 `{` 开头
        if '"answer"' in json_part:
            # 确认是内联 final_answer JSON
            self.mode = self.MODE_ANSWER
            self._extractor = JSONFieldExtractor("answer")
            # 只把 JSON 部分喂给提取器，丢弃前面的工具名前缀
            chunk = self._extractor.feed(json_part)
            self._buffer = ""
            self.answer_text += chunk
            return ("answer", chunk)
        if len(json_part) > _PROBE_LIMIT:
            # 以 { 开头但迟迟没有 answer 键，判为思考
            return self._switch_to_thought()
        return ("", "")  # 继续缓冲等待 "answer" 出现

    def _locate_json(self, stripped: str) -> tuple[str, str]:
        """在已 lstrip 的缓冲中定位内联 JSON 起点。

        返回 (decision, json_part)：
          - ("json", "{...")：找到以 `{` 开头的 JSON（已剥除可忽略的工具名前缀）
          - ("thought", "") ：确认不是内联 final_answer，应作为思考处理
          - ("wait", "")    ：尚不能判定（前缀仍可能是 `final_answer`），继续缓冲
        """
        if stripped[0] == "{":
            return ("json", stripped)

        # 尝试匹配 `final_answer` 工具名前缀（大小写不敏感）。
        prefix = _TOOL_NAME
        head = stripped[: len(prefix)].lower()
        if head == prefix:
            # 工具名后跳过分隔符，寻找 `{`
            rest = stripped[len(prefix):]
            j = 0
            while j < len(rest) and rest[j] in _PREFIX_SEPARATORS:
                j += 1
            if j < len(rest):
                if rest[j] == "{":
                    return ("json", rest[j:])
                # 工具名后是别的内容，不是内联 JSON
                return ("thought", "")
            # 分隔符还没结束，等更多内容（但别无限等）
            if len(stripped) > _PREFIX_PROBE_LIMIT:
                return ("thought", "")
            return ("wait", "")

        # 前缀可能是 `final_answer` 的不完整片段（如 `final_an`），继续等
        if prefix.startswith(head) and len(stripped) < len(prefix):
            return ("wait", "")

        # 首个有效字符既不是 `{` 也不是 `final_answer` 前缀 → 思考
        return ("thought", "")

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
