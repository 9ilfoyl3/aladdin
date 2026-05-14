"""ModelManager 单元测试

通过 mock FlagEmbedding 模块验证 ModelManager 初始化逻辑，
不需要 GPU 或模型下载。
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# 在导入 manager 之前 mock FlagEmbedding 模块
mock_flag_embedding = MagicMock()
sys.modules["FlagEmbedding"] = mock_flag_embedding

from app.config import Settings
from app.models.manager import ModelManager, get_model_manager
from app.models.provider import LLMProvider, EmbedProvider, RerankProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM


@pytest.fixture
def ollama_config():
    """Ollama 模式配置"""
    return Settings(
        llm_provider="ollama",
        llm_base_url="http://localhost:11434",
        llm_model="qwen2.5:7b",
        embed_model="BAAI/bge-m3",
        embed_device="cpu",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_device="cpu",
    )


@pytest.fixture
def vllm_config():
    """vLLM 模式配置"""
    return Settings(
        llm_provider="vllm",
        llm_base_url="http://localhost:8000",
        llm_model="Qwen/Qwen2.5-7B-Instruct",
        embed_model="BAAI/bge-m3",
        embed_device="cpu",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_device="cpu",
    )


class TestModelManagerInit:
    """测试 ModelManager 初始化"""

    def test_ollama_provider(self, ollama_config):
        """ollama 配置应初始化 OllamaLLM"""
        manager = ModelManager(ollama_config)
        assert isinstance(manager.llm, OllamaLLM)
        assert isinstance(manager.llm, LLMProvider)
        assert manager.llm.base_url == "http://localhost:11434"
        assert manager.llm.model == "qwen2.5:7b"

    def test_vllm_provider(self, vllm_config):
        """vllm 配置应初始化 VllmLLM"""
        manager = ModelManager(vllm_config)
        assert isinstance(manager.llm, VllmLLM)
        assert isinstance(manager.llm, LLMProvider)
        assert manager.llm.base_url == "http://localhost:8000"
        assert manager.llm.model == "Qwen/Qwen2.5-7B-Instruct"

    def test_embedder_initialized(self, ollama_config):
        """应正确初始化 embedder"""
        manager = ModelManager(ollama_config)
        assert isinstance(manager.embedder, EmbedProvider)

    def test_reranker_initialized(self, ollama_config):
        """应正确初始化 reranker"""
        manager = ModelManager(ollama_config)
        assert isinstance(manager.reranker, RerankProvider)

    def test_embedder_params(self, ollama_config):
        """embedder 应使用配置中的模型名和设备"""
        manager = ModelManager(ollama_config)
        # 验证 embedder 存储了正确的参数
        assert manager.embedder.model_name == "BAAI/bge-m3"
        assert manager.embedder.device == "cpu"

    def test_reranker_params(self, ollama_config):
        """reranker 应使用配置中的模型名和设备"""
        manager = ModelManager(ollama_config)
        # 验证 reranker 存储了正确的参数
        assert manager.reranker.model_name == "BAAI/bge-reranker-v2-m3"
        assert manager.reranker.device == "cpu"


class TestModelManagerClose:
    """测试资源清理"""

    @pytest.mark.asyncio
    async def test_close_closes_llm_client(self, ollama_config):
        """close() 应关闭 LLM 的 httpx 客户端"""
        manager = ModelManager(ollama_config)
        await manager.close()
        assert manager.llm._client.is_closed


class TestGetModelManager:
    """测试单例获取函数"""

    def test_returns_same_instance(self, ollama_config):
        """多次调用应返回同一实例"""
        import app.models.manager as mgr_module

        mgr_module._manager = None
        m1 = get_model_manager(ollama_config)
        m2 = get_model_manager()
        assert m1 is m2
        # 清理
        mgr_module._manager = None

    def test_without_config_uses_get_settings(self):
        """无 config 参数时应使用 get_settings()"""
        import app.models.manager as mgr_module

        mgr_module._manager = None
        with patch("app.config.get_settings") as mock_get:
            mock_get.return_value = Settings(
                llm_provider="ollama",
                embed_device="cpu",
                rerank_device="cpu",
            )
            m = get_model_manager()
            mock_get.assert_called_once()
            assert m is not None
        mgr_module._manager = None
