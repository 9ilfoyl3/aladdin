"""模型厂商（provider）自动检测

VllmLLM 通过 OpenAI 兼容协议承载众多厂商端点（vLLM 自部署、阿里云 Qwen 云、
火山引擎、DeepSeek、OpenAI、智谱 GLM 等）。不同厂商控制「思考链（thinking）」
开关的请求字段格式各不相同，必须按厂商分派（见 thinking_dialect.py）。

本模块仅负责「这是哪家厂商」的识别，输入是 base_url（主）与 model 名（辅），
输出统一的 LLMProviderName 枚举。前端无需配置厂商——后端据 base_url 自动判定。
"""

from __future__ import annotations

from enum import Enum


class LLMProviderName(str, Enum):
    """OpenAI 兼容端点背后的实际模型厂商。"""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    ALIYUN = "aliyun"  # 阿里云 DashScope（Qwen 云）
    ZHIPU = "zhipu"  # 智谱 GLM
    DEEPSEEK = "deepseek"
    VOLCENGINE = "volcengine"  # 火山引擎 Ark（豆包）
    LKEAP = "lkeap"  # 腾讯云 LKEAP
    MOONSHOT = "moonshot"  # 月之暗面 Kimi
    SILICONFLOW = "siliconflow"  # 硅基流动
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    NVIDIA = "nvidia"
    MINIMAX = "minimax"
    HUNYUAN = "hunyuan"
    GENERIC = "generic"  # 自部署 vLLM / SGLang / 其它 OpenAI 兼容端点


# base_url 子串 → 厂商。顺序不敏感（子串互不重叠）。
_URL_MARKERS: list[tuple[tuple[str, ...], LLMProviderName]] = [
    (("dashscope.aliyuncs.com",), LLMProviderName.ALIYUN),
    (("open.bigmodel.cn", "bigmodel", "zhipu"), LLMProviderName.ZHIPU),
    (("openrouter.ai",), LLMProviderName.OPENROUTER),
    (("siliconflow.cn", "siliconflow"), LLMProviderName.SILICONFLOW),
    (("openai.azure.com",), LLMProviderName.AZURE_OPENAI),
    (("api.openai.com",), LLMProviderName.OPENAI),
    (("api.deepseek.com",), LLMProviderName.DEEPSEEK),
    (("generativelanguage.googleapis.com",), LLMProviderName.GEMINI),
    (("volces.com", "volcengine"), LLMProviderName.VOLCENGINE),
    (("lkeap.cloud.tencent.com", "api.lkeap"), LLMProviderName.LKEAP),
    (("hunyuan.cloud.tencent.com",), LLMProviderName.HUNYUAN),
    (("minimax.io", "minimaxi.com"), LLMProviderName.MINIMAX),
    (("moonshot.ai", "moonshot.cn"), LLMProviderName.MOONSHOT),
    (("nvidia.com",), LLMProviderName.NVIDIA),
]


def detect_provider(base_url: str, model: str = "") -> LLMProviderName:
    """根据 base_url（主）推断模型厂商，未命中则归为 GENERIC（自部署 OpenAI 兼容）。

    model 名当前不参与判定，仅作为签名预留，便于将来对同一端点下不同模型族细分。
    """
    url = (base_url or "").lower()
    for markers, name in _URL_MARKERS:
        if any(m in url for m in markers):
            return name
    return LLMProviderName.GENERIC


# --- 模型族匹配（同一厂商下需进一步区分思考能力时使用）---

def is_qwen_thinking_model(model: str) -> bool:
    """是否为支持思维链、需特殊处理 enable_thinking 的 Qwen 模型。

    覆盖 Qwen3 全系，以及 qwen-plus/max/turbo。
    """
    name = (model or "").lower()
    return (
        name.startswith("qwen3")
        or name.startswith("qwen-plus")
        or name.startswith("qwen-max")
        or name.startswith("qwen-turbo")
    )


def is_deepseek_v3_model(model: str) -> bool:
    """是否为 DeepSeek-V3.x 系列（thinking 可显式开关；R1 默认开启不需控制）。"""
    return "deepseek-v3" in (model or "").lower()
