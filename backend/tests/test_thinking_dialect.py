"""thinking 方言分派 + 厂商检测单元测试

验证前端单一 thinking 开关经后端按厂商自动注入到正确的请求字段。
"""

from app.models.llm.provider_detect import (
    LLMProviderName,
    detect_provider,
    is_deepseek_v3_model,
    is_qwen_thinking_model,
)
from app.models.llm.thinking_dialect import apply_thinking


class TestDetectProvider:
    def test_aliyun(self):
        assert detect_provider("https://dashscope.aliyuncs.com/compatible-mode/v1") == LLMProviderName.ALIYUN

    def test_volcengine(self):
        assert detect_provider("https://ark.cn-beijing.volces.com/api/v3") == LLMProviderName.VOLCENGINE

    def test_deepseek(self):
        assert detect_provider("https://api.deepseek.com/v1") == LLMProviderName.DEEPSEEK

    def test_lkeap(self):
        assert detect_provider("https://api.lkeap.cloud.tencent.com/v1") == LLMProviderName.LKEAP

    def test_zhipu(self):
        assert detect_provider("https://open.bigmodel.cn/api/paas/v4") == LLMProviderName.ZHIPU

    def test_openai(self):
        assert detect_provider("https://api.openai.com/v1") == LLMProviderName.OPENAI

    def test_selfhosted_vllm_is_generic(self):
        assert detect_provider("http://localhost:8000/v1") == LLMProviderName.GENERIC

    def test_unknown_is_generic(self):
        assert detect_provider("https://my-internal-llm.corp/v1") == LLMProviderName.GENERIC


class TestModelMatchers:
    def test_qwen3_thinking(self):
        assert is_qwen_thinking_model("Qwen3-30B-A3B")
        assert is_qwen_thinking_model("qwen-plus")
        assert not is_qwen_thinking_model("qwen2.5-7b-instruct")

    def test_deepseek_v3(self):
        assert is_deepseek_v3_model("deepseek-v3.1")
        assert not is_deepseek_v3_model("deepseek-r1")


def _payload():
    return {"model": "m", "messages": [], "stream": True}


class TestApplyThinking:
    def test_generic_vllm_uses_chat_template_kwargs(self):
        p = apply_thinking(_payload(), True, "http://localhost:8000/v1", "Qwen3-30B")
        assert p["chat_template_kwargs"] == {"enable_thinking": True}
        assert "enable_thinking" not in p  # 不放顶层

    def test_generic_vllm_disable(self):
        p = apply_thinking(_payload(), False, "http://localhost:8000/v1", "Qwen3-30B")
        assert p["chat_template_kwargs"] == {"enable_thinking": False}

    def test_aliyun_qwen_top_level(self):
        p = apply_thinking(_payload(), True, "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus")
        assert p["enable_thinking"] is True
        assert "chat_template_kwargs" not in p

    def test_aliyun_non_thinking_model_untouched(self):
        p = apply_thinking(_payload(), True, "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen2.5-7b")
        assert "enable_thinking" not in p

    def test_volcengine_thinking_type(self):
        p = apply_thinking(_payload(), True, "https://ark.cn-beijing.volces.com/api/v3", "doubao-1.5-pro")
        assert p["thinking"] == {"type": "enabled"}

    def test_volcengine_disable(self):
        p = apply_thinking(_payload(), False, "https://ark.cn-beijing.volces.com/api/v3", "doubao-1.5-pro")
        assert p["thinking"] == {"type": "disabled"}

    def test_lkeap_v3_only(self):
        p_v3 = apply_thinking(_payload(), True, "https://api.lkeap.cloud.tencent.com/v1", "deepseek-v3.1")
        assert p_v3["thinking"] == {"type": "enabled"}
        p_r1 = apply_thinking(_payload(), True, "https://api.lkeap.cloud.tencent.com/v1", "deepseek-r1")
        assert "thinking" not in p_r1  # R1 默认开启，不注入

    def test_openai_noop(self):
        p = apply_thinking(_payload(), True, "https://api.openai.com/v1", "gpt-4o")
        assert "enable_thinking" not in p
        assert "chat_template_kwargs" not in p
        assert "thinking" not in p

    def test_none_enable_injects_nothing(self):
        # enable=None 表示未配置 → 任何厂商都不注入
        p = apply_thinking(_payload(), None, "http://localhost:8000/v1", "Qwen3-30B")
        assert "chat_template_kwargs" not in p
        assert "enable_thinking" not in p

    def test_explicit_provider_override(self):
        # 显式 provider 跳过 base_url 检测
        p = apply_thinking(
            _payload(), True, "http://anything/v1", "doubao",
            provider=LLMProviderName.VOLCENGINE,
        )
        assert p["thinking"] == {"type": "enabled"}
