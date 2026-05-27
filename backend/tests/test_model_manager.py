"""ModelManager 单元测试

验证 ModelManager 初始化逻辑：
- 配置了远程服务地址时使用 RemoteEmbedder/RemoteReranker
- 未配置时使用占位 Provider（允许启动）
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock pymilvus
sys.modules.setdefault("pymilvus", MagicMock())

from app.config import Settings
from app.models.manager import ModelManager, get_model_manager, _PlaceholderEmbedder, _PlaceholderReranker
from app.models.provider import EmbedProvider, RerankProvider


@pytest.fixture
def config_with_remote():
    """配置了远程 Embedding/Rerank 服务"""
    return Settings(
        llm_provider="ollama",
        llm_base_url="http://localhost:11434",
        llm_model="qwen2.5:7b",
        embed_model="BAAI/bge-m3",
        embed_base_url="http://embedding-server:8080/v1",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_base_url="http://rerank-server:8001/v1",
    )


@pytest.fixture
def config_without_remote():
    """未配置远程服务地址（允许启动，后续通过前端配置）"""
    return Settings(
        llm_provider="vllm",
        llm_base_url="http://localhost:8000",
        llm_model="Qwen/Qwen2.5-7B-Instruct",
        embed_model="BAAI/bge-m3",
        embed_base_url="",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_base_url="",
    )


class TestModelManagerInit:
    """测试 ModelManager 初始化"""

    def test_remote_embedder_initialized(self, config_with_remote):
        """配置了远程地址时应初始化 RemoteEmbedder"""
        from app.models.embedding.remote import RemoteEmbedder
        manager = ModelManager(config_with_remote)
        assert isinstance(manager.embedder, EmbedProvider)
        assert isinstance(manager.embedder, RemoteEmbedder)

    def test_remote_reranker_initialized(self, config_with_remote):
        """配置了远程地址时应初始化 RemoteReranker"""
        from app.models.rerank.remote import RemoteReranker
        manager = ModelManager(config_with_remote)
        assert isinstance(manager.reranker, RerankProvider)
        assert isinstance(manager.reranker, RemoteReranker)

    def test_placeholder_embedder_when_no_url(self, config_without_remote):
        """未配置远程地址时应使用占位 Provider"""
        manager = ModelManager(config_without_remote)
        assert isinstance(manager.embedder, _PlaceholderEmbedder)

    def test_placeholder_reranker_when_no_url(self, config_without_remote):
        """未配置远程地址时应使用占位 Provider"""
        manager = ModelManager(config_without_remote)
        assert isinstance(manager.reranker, _PlaceholderReranker)

    @pytest.mark.asyncio
    async def test_placeholder_embedder_raises(self, config_without_remote):
        """占位 Provider 调用时应抛出明确错误"""
        manager = ModelManager(config_without_remote)
        with pytest.raises(RuntimeError, match="Embedding 服务未配置"):
            await manager.embedder.embed(["test"])

    @pytest.mark.asyncio
    async def test_placeholder_reranker_raises(self, config_without_remote):
        """占位 Provider 调用时应抛出明确错误"""
        manager = ModelManager(config_without_remote)
        with pytest.raises(RuntimeError, match="Rerank 服务未配置"):
            await manager.reranker.rerank("query", ["doc1"])


class TestModelManagerReload:
    """测试动态重载"""

    def test_reload_embedder(self, config_without_remote):
        """reload_embedder 应替换为 RemoteEmbedder"""
        from app.models.embedding.remote import RemoteEmbedder
        manager = ModelManager(config_without_remote)
        assert isinstance(manager.embedder, _PlaceholderEmbedder)

        manager.reload_embedder(
            base_url="http://new-server:8080/v1",
            model_name="BAAI/bge-m3",
        )
        assert isinstance(manager.embedder, RemoteEmbedder)

    def test_reload_reranker(self, config_without_remote):
        """reload_reranker 应替换为 RemoteReranker"""
        from app.models.rerank.remote import RemoteReranker
        manager = ModelManager(config_without_remote)
        assert isinstance(manager.reranker, _PlaceholderReranker)

        manager.reload_reranker(
            base_url="http://new-server:8001/v1",
            model_name="BAAI/bge-reranker-v2-m3",
        )
        assert isinstance(manager.reranker, RemoteReranker)


class TestModelManagerClose:
    """测试资源清理"""

    @pytest.mark.asyncio
    async def test_close(self, config_with_remote):
        """close() 应正常执行"""
        manager = ModelManager(config_with_remote)
        await manager.close()


class TestGetModelManager:
    """测试单例获取函数"""

    def test_returns_same_instance(self, config_with_remote):
        """多次调用应返回同一实例"""
        import app.models.manager as mgr_module

        mgr_module._manager = None
        m1 = get_model_manager(config_with_remote)
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
            )
            m = get_model_manager()
            mock_get.assert_called_once()
            assert m is not None
        mgr_module._manager = None
