"""BgeM3Embedder 单元测试

通过 mock FlagEmbedding 模块验证接口行为，
不需要 GPU 或模型下载。
"""

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# 在导入 bge_m3 之前 mock FlagEmbedding 模块
mock_flag_embedding = MagicMock()
sys.modules["FlagEmbedding"] = mock_flag_embedding

from app.models.embedding.bge_m3 import BgeM3Embedder
from app.models.provider import EmbedProvider


@pytest.fixture(autouse=True)
def reset_mock():
    """每个测试前重置 mock"""
    mock_flag_embedding.reset_mock()
    yield


class TestBgeM3Embedder:
    """BgeM3Embedder 测试"""

    def test_inherits_embed_provider(self):
        """验证继承自 EmbedProvider"""
        embedder = BgeM3Embedder(model_name="test-model", device="cpu")
        assert isinstance(embedder, EmbedProvider)

    def test_init_with_cuda(self):
        """验证 cuda 设备启用 fp16"""
        mock_flag_embedding.reset_mock()
        BgeM3Embedder(model_name="BAAI/bge-m3", device="cuda")
        mock_flag_embedding.BGEM3FlagModel.assert_called_with(
            "BAAI/bge-m3", use_fp16=True, device="cuda"
        )

    def test_init_with_cpu(self):
        """验证 cpu 设备禁用 fp16"""
        mock_flag_embedding.reset_mock()
        BgeM3Embedder(model_name="BAAI/bge-m3", device="cpu")
        mock_flag_embedding.BGEM3FlagModel.assert_called_with(
            "BAAI/bge-m3", use_fp16=False, device="cpu"
        )

    @pytest.mark.asyncio
    async def test_embed_returns_dense_vectors(self):
        """验证 embed() 返回稠密向量列表"""
        # 模拟 1024 维稠密向量
        fake_dense = np.random.rand(2, 1024).astype(np.float32)
        mock_model_instance = mock_flag_embedding.BGEM3FlagModel.return_value
        mock_model_instance.encode.return_value = {
            "dense_vecs": fake_dense,
            "lexical_weights": [{1: 0.5}, {2: 0.3}],
        }

        embedder = BgeM3Embedder(model_name="test", device="cpu")
        result = await embedder.embed(["hello", "world"])

        assert len(result) == 2
        assert len(result[0]) == 1024
        assert isinstance(result[0], list)
        assert isinstance(result[0][0], float)

    @pytest.mark.asyncio
    async def test_embed_sparse_returns_sparse_vectors(self):
        """验证 embed_sparse() 返回稀疏向量列表"""
        fake_sparse = [{101: 0.8, 202: 0.3}, {303: 0.6}]
        mock_model_instance = mock_flag_embedding.BGEM3FlagModel.return_value
        mock_model_instance.encode.return_value = {
            "dense_vecs": np.zeros((2, 1024)),
            "lexical_weights": fake_sparse,
        }

        embedder = BgeM3Embedder(model_name="test", device="cpu")
        result = await embedder.embed_sparse(["hello", "world"])

        assert len(result) == 2
        assert result[0] == {101: 0.8, 202: 0.3}
        assert result[1] == {303: 0.6}

    @pytest.mark.asyncio
    async def test_encode_called_with_correct_params(self):
        """验证底层 encode 调用参数正确"""
        mock_model_instance = mock_flag_embedding.BGEM3FlagModel.return_value
        mock_model_instance.encode.return_value = {
            "dense_vecs": np.zeros((1, 1024)),
            "lexical_weights": [{1: 0.1}],
        }

        embedder = BgeM3Embedder(model_name="test", device="cpu")
        await embedder.embed(["test text"])

        mock_model_instance.encode.assert_called_with(
            ["test text"], return_dense=True, return_sparse=True
        )
