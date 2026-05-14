"""PipelineEmbedder 单元测试

验证管道向量化节点的批量处理、空输入处理和结果合并逻辑。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.provider import EmbedProvider
from app.pipeline.embedder import EmbedResult, PipelineEmbedder


def _make_provider(dense_fn=None, sparse_fn=None) -> EmbedProvider:
    """构造 mock EmbedProvider"""
    provider = MagicMock(spec=EmbedProvider)
    provider.embed = AsyncMock(side_effect=dense_fn)
    provider.embed_sparse = AsyncMock(side_effect=sparse_fn)
    return provider


class TestPipelineEmbedder:
    """PipelineEmbedder 测试"""

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_result(self):
        """空文本列表返回空结果"""
        provider = _make_provider()
        embedder = PipelineEmbedder(provider, batch_size=4)

        result = await embedder.embed([])

        assert result.dense_vectors == []
        assert result.sparse_vectors == []
        # 不应调用 provider
        provider.embed.assert_not_called()
        provider.embed_sparse.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_batch(self):
        """文本数量小于 batch_size 时只调用一次"""
        texts = ["hello", "world"]
        fake_dense = [[0.1, 0.2], [0.3, 0.4]]
        fake_sparse = [{1: 0.5}, {2: 0.6}]

        provider = _make_provider(
            dense_fn=lambda t: fake_dense,
            sparse_fn=lambda t: fake_sparse,
        )
        embedder = PipelineEmbedder(provider, batch_size=10)

        result = await embedder.embed(texts)

        assert result.dense_vectors == fake_dense
        assert result.sparse_vectors == fake_sparse
        assert provider.embed.call_count == 1
        assert provider.embed_sparse.call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_batches(self):
        """文本数量超过 batch_size 时分批调用并合并结果"""
        texts = ["a", "b", "c", "d", "e"]

        # 每次调用返回与输入等长的向量
        async def dense_fn(batch):
            return [[float(i)] for i in range(len(batch))]

        async def sparse_fn(batch):
            return [{i: 1.0} for i in range(len(batch))]

        provider = MagicMock(spec=EmbedProvider)
        provider.embed = AsyncMock(side_effect=dense_fn)
        provider.embed_sparse = AsyncMock(side_effect=sparse_fn)

        embedder = PipelineEmbedder(provider, batch_size=2)
        result = await embedder.embed(texts)

        # 5 个文本，batch_size=2 → 3 批 (2+2+1)
        assert provider.embed.call_count == 3
        assert provider.embed_sparse.call_count == 3
        # 结果总数应为 5
        assert len(result.dense_vectors) == 5
        assert len(result.sparse_vectors) == 5

    @pytest.mark.asyncio
    async def test_batch_boundaries_correct(self):
        """验证分批时传入 provider 的文本切片正确"""
        texts = ["t0", "t1", "t2", "t3", "t4"]
        call_log = []

        async def dense_fn(batch):
            call_log.append(list(batch))
            return [[1.0]] * len(batch)

        async def sparse_fn(batch):
            return [{0: 1.0}] * len(batch)

        provider = MagicMock(spec=EmbedProvider)
        provider.embed = AsyncMock(side_effect=dense_fn)
        provider.embed_sparse = AsyncMock(side_effect=sparse_fn)

        embedder = PipelineEmbedder(provider, batch_size=2)
        await embedder.embed(texts)

        assert call_log == [["t0", "t1"], ["t2", "t3"], ["t4"]]

    @pytest.mark.asyncio
    async def test_result_dataclass_structure(self):
        """验证返回值为 EmbedResult 数据类"""
        provider = _make_provider(
            dense_fn=lambda t: [[0.0]],
            sparse_fn=lambda t: [{0: 0.0}],
        )
        embedder = PipelineEmbedder(provider, batch_size=32)

        result = await embedder.embed(["test"])

        assert isinstance(result, EmbedResult)
        assert hasattr(result, "dense_vectors")
        assert hasattr(result, "sparse_vectors")

    @pytest.mark.asyncio
    async def test_default_batch_size(self):
        """默认 batch_size 为 32"""
        provider = _make_provider()
        embedder = PipelineEmbedder(provider)
        assert embedder.batch_size == 32
