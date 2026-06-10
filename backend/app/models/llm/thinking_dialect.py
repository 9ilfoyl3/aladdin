"""思考链（thinking）请求方言分派

不同模型厂商通过不同的请求字段控制「是否输出思考链」。前端只暴露一个布尔开关
（thinking_enabled），后端据厂商自动选择正确的字段格式注入到请求体。采用
per-provider RequestCustomizer 的分派表实现。

设计要点：
- 每个 customizer 接收 (payload, enable, model)，**就地修改并返回 payload**。
- payload 是即将 POST 给 /chat/completions 的 dict（OpenAI 兼容协议）。
- 不认识 thinking 的厂商（OpenAI、GLM 多数、generic 非 vLLM 等）使用 noop，
  即不注入任何字段——这保证未知/普通模型的请求体与「不传」完全一致，零副作用。
- generic（自部署 vLLM/SGLang）走 chat_template_kwargs.enable_thinking。
- 阿里云 Qwen 云走顶层 enable_thinking。
- 火山引擎 / LKEAP-V3 走 thinking: {type: enabled|disabled}。

调用方（VllmLLM）在构造 payload 后、发请求前调用 apply_thinking()。
"""

from __future__ import annotations

from typing import Callable

from app.models.llm.provider_detect import (
    LLMProviderName,
    detect_provider,
    is_deepseek_thinking_model,
    is_qwen_thinking_model,
)

# customizer 签名：就地改写并返回 payload。
ThinkingCustomizer = Callable[[dict, bool, str], dict]


def _noop(payload: dict, enable: bool, model: str) -> dict:
    """不注入任何 thinking 字段（厂商不支持或无需控制）。"""
    return payload


def _generic_vllm(payload: dict, enable: bool, model: str) -> dict:
    """自部署 vLLM / SGLang：通过 chat_template_kwargs.enable_thinking 控制。

    这是 Qwen3 等模型在 vLLM 上的标准开关位置；放在顶层 enable_thinking 会被忽略。
    """
    ctk = payload.get("chat_template_kwargs")
    if not isinstance(ctk, dict):
        ctk = {}
    ctk["enable_thinking"] = enable
    payload["chat_template_kwargs"] = ctk
    return payload


def _aliyun_qwen(payload: dict, enable: bool, model: str) -> dict:
    """阿里云 DashScope / 百炼（compatible-mode）：顶层 enable_thinking。

    百炼网关对 Qwen 与 DeepSeek（v3.x / v4 系列）统一用顶层 ``enable_thinking`` 参数
    控制思考模式（见百炼 DeepSeek API 文档）。此前仅放行 Qwen，导致在百炼上跑
    DeepSeek 时 thinking 开关不注入 → 思维链混入普通 content 被当正文展示。
    R1（deepseek-reasoner）思考默认开启，由 is_deepseek_thinking_model 排除、不注入。
    其它阿里云模型保持原样。
    """
    if is_qwen_thinking_model(model) or is_deepseek_thinking_model(model):
        payload["enable_thinking"] = enable
    return payload


def _thinking_type(payload: dict, enable: bool, model: str) -> dict:
    """火山引擎 Ark / 其它使用 thinking:{type} 协议的厂商。"""
    payload["thinking"] = {"type": "enabled" if enable else "disabled"}
    return payload


def _lkeap(payload: dict, enable: bool, model: str) -> dict:
    """腾讯云 LKEAP：DeepSeek-V3 及以后（V3/V3.x/V4…）需显式开关；R1 默认开启不注入。"""
    if is_deepseek_thinking_model(model):
        payload["thinking"] = {"type": "enabled" if enable else "disabled"}
    return payload


# 厂商 → customizer。未列出的厂商默认 noop。
_DIALECTS: dict[LLMProviderName, ThinkingCustomizer] = {
    LLMProviderName.GENERIC: _generic_vllm,
    LLMProviderName.NVIDIA: _generic_vllm,
    LLMProviderName.ALIYUN: _aliyun_qwen,
    LLMProviderName.VOLCENGINE: _thinking_type,
    # DeepSeek 官方端点（api.deepseek.com）：思考开关协议与火山一致，均为
    # thinking:{type: enabled|disabled}（见官方 thinking_mode 文档）。此前缺失此项
    # → 走 _noop 不注入任何字段 → 模型把思维链写进普通 content（而非 reasoning_content
    # 通道）→ 前端无法区分思考与正文，思维链被当作答案展示。
    LLMProviderName.DEEPSEEK: _thinking_type,
    LLMProviderName.LKEAP: _lkeap,
}


def apply_thinking(
    payload: dict,
    enable: bool | None,
    base_url: str,
    model: str,
    provider: LLMProviderName | None = None,
) -> dict:
    """按厂商方言把 thinking 开关注入到请求 payload。

    Args:
        payload: 即将 POST 的请求体（就地修改）。
        enable: 是否启用思考。None 表示「未配置」→ 不注入任何字段（保持厂商默认）。
        base_url / model: 用于自动检测厂商（provider 显式给定时优先）。
        provider: 可选，显式指定厂商，跳过自动检测。

    Returns:
        修改后的 payload。
    """
    if enable is None:
        return payload
    name = provider or detect_provider(base_url, model)
    customizer = _DIALECTS.get(name, _noop)
    return customizer(payload, enable, model)
