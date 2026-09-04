"""普通 content 通道的能力差异兼容分类器。

支持 native reasoning 的模型不需要这个分类器：reasoning 通道永远渲染为思考，
content 通道永远渲染为正文。Qwen 等小参数模型可能没有独立 reasoning 通道，
或在 content 中使用 `<think>…</think>`。这类模型在 tool-call 轮写出的普通
content 由 Agent loop 归类为思考；自然停止时归类为正文。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelFragment:
    """一段已确定语义的 content 片段"""

    kind: str  # "reasoning" | "text"
    text: str


class PlainContentClassifier:
    """按 `<think>` 标记增量分类；无标记内容先缓冲，结束后按 loop 语义归类。"""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._mode = "probe"  # probe | thinking | text | buffered
        self._buffer = ""

    def feed(self, delta: str) -> list[ChannelFragment]:
        """喂入 content 增量，返回当前已能确定语义的片段。"""
        if not delta:
            return []
        if self._mode == "text":
            return [ChannelFragment("text", delta)]
        if self._mode == "thinking":
            return self._drain_thinking(delta)
        if self._mode == "buffered":
            self._buffer += delta
            return []

        self._buffer += delta
        return self._probe()

    def finalize(self, fallback_kind: str) -> list[ChannelFragment]:
        """流结束时归类残留内容。fallback 来自 tool calls / finish reason。"""
        if not self._buffer:
            return []
        kind = "reasoning" if self._mode == "thinking" else fallback_kind
        fragment = ChannelFragment(kind, self._buffer)
        self._buffer = ""
        return [fragment]

    def _probe(self) -> list[ChannelFragment]:
        open_at = self._buffer.find(self._OPEN)
        if open_at >= 0:
            fragments: list[ChannelFragment] = []
            prefix = self._buffer[:open_at]
            if prefix:
                fragments.append(ChannelFragment("reasoning", prefix))
            self._mode = "thinking"
            self._buffer = self._buffer[open_at + len(self._OPEN) :]
            fragments.extend(self._drain_thinking(""))
            return fragments

        # 完整 opener 不可能出现时才停止等待，进入“结束后归类”状态。
        opener = self._OPEN
        suffix_is_partial_opener = any(
            opener.startswith(self._buffer[-size:])
            for size in range(1, min(len(opener), len(self._buffer)) + 1)
        )
        if len(self._buffer) >= len(opener) and not suffix_is_partial_opener:
            self._mode = "buffered"
        return []

    def _drain_thinking(self, delta: str) -> list[ChannelFragment]:
        if delta:
            self._buffer += delta
        fragments: list[ChannelFragment] = []
        close_at = self._buffer.find(self._CLOSE)
        if close_at < 0:
            closer = self._CLOSE
            hold = next(
                (
                    size
                    for size in range(1, min(len(closer), len(self._buffer)) + 1)
                    if closer.startswith(self._buffer[-size:])
                ),
                0,
            )
            safe = self._buffer[:-hold] if hold else self._buffer
            if safe:
                fragments.append(ChannelFragment("reasoning", safe))
                self._buffer = self._buffer[len(safe):]
            return fragments

        thinking = self._buffer[:close_at]
        if thinking:
            fragments.append(ChannelFragment("reasoning", thinking))
        remainder = self._buffer[close_at + len(self._CLOSE) :]
        self._mode = "text"
        self._buffer = ""
        if remainder:
            fragments.append(ChannelFragment("text", remainder))
        return fragments
