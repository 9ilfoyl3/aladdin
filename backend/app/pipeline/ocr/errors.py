"""OCR 领域异常

契约失配、输入类型不支持等情形抛出本模块的异常，而不是让裸
``AttributeError`` / ``KeyError`` / ``TypeError`` 逃逸到 pipeline
——后者对排障毫无帮助（历史故障：``'list' object has no attribute 'get'``）。
"""

from __future__ import annotations

# 响应样本在错误信息中的最大长度（避免把整篇 OCR 结果写进 Document.error_message）
_SAMPLE_MAX_CHARS = 300


def _truncate_sample(sample: object) -> str:
    """把任意响应对象转成可读且有界的样本字符串"""
    text = repr(sample)
    if len(text) <= _SAMPLE_MAX_CHARS:
        return text
    return text[:_SAMPLE_MAX_CHARS] + "...(已截断)"


class OCRError(Exception):
    """OCR 领域异常基类"""


class OCRResponseFormatError(OCRError):
    """外部 OCR 服务的响应不符合该 Provider 的契约。

    显式 Provider 不做跨格式探测与回落：格式不符即刻失败，并在错误信息里带上
    端点地址与响应样本，让"配错服务类型 / 配错端点"能被一眼看出。

    Attributes:
        provider: Provider 标识（``vl`` / ``paddle`` / ``mineru``）。
        endpoint: 请求的 API 地址。
        reason: 契约不符的具体原因（期望什么、实际是什么）。
        sample: 响应样本（已截断）。
    """

    def __init__(
        self, provider: str, endpoint: str, reason: str, sample: object = None
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.reason = reason
        self.sample = _truncate_sample(sample) if sample is not None else ""
        message = f"{provider} Provider 响应格式不符：{reason}（端点 {endpoint}）"
        if self.sample:
            message += f"，实际响应 {self.sample}"
        super().__init__(message)


class OCRUnsupportedInputError(OCRError):
    """输入类型无法被目标 Provider 处理，且无法通过输入准备弥合。

    Attributes:
        provider: Provider 标识。
        input_kind: 输入类型描述（扩展名或 ``image`` / ``pdf``）。
        accepts: 该 Provider 声明可接受的输入类型集合。
    """

    def __init__(
        self, provider: str, input_kind: str, accepts: frozenset[str] | None = None
    ) -> None:
        self.provider = provider
        self.input_kind = input_kind
        self.accepts = accepts or frozenset()
        accepted = "、".join(sorted(self.accepts)) if self.accepts else "无"
        super().__init__(
            f"{provider} Provider 不支持输入类型 {input_kind}（可接受：{accepted}）"
        )
