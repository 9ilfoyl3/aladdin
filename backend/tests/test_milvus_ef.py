"""HNSW 查询 ef 透传单元测试（任务 9.3）

验证：
- `_search_dense_sync` 传入的 ef 出现在 collection.search 的 param.params.ef
- ef=None / 不传时回落默认 128
- `VectorRetriever.search` 把 ef 透传给 milvus.search_dense（wiring 断言）

参考 test_milvus.py 的 pymilvus mock 模式。
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.storage.milvus import MilvusClient  # noqa: E402
from app.retrieval.vector import VectorRetriever  # noqa: E402


def _build_client_with_capture():
    """构造 MilvusClient，mock 掉连接与 Collection，捕获 collection.search 的 kwargs。"""
    client = MilvusClient()
    client._connect = MagicMock()

    captured: dict = {}
    mock_collection = MagicMock()

    def fake_search(**kwargs):
        captured["search_kwargs"] = kwargs
        return [[]]  # 空结果，解析为 []

    mock_collection.search.side_effect = fake_search
    return client, mock_collection, captured


class TestSearchDenseEf:
    """_search_dense_sync 的 ef 参数化"""

    def test_ef_passed_into_search_param(self):
        """传入的 ef 出现在 collection.search 的 param.params.ef"""
        client, mock_collection, captured = _build_client_with_capture()

        with patch("app.storage.milvus.Collection", return_value=mock_collection):
            client._search_dense_sync("test_kb", [0.1] * 1024, top_k=5, ef=256)

        assert captured["search_kwargs"]["param"]["params"]["ef"] == 256

    def test_ef_none_falls_back_to_128(self):
        """ef=None 回落默认 128"""
        client, mock_collection, captured = _build_client_with_capture()

        with patch("app.storage.milvus.Collection", return_value=mock_collection):
            client._search_dense_sync("test_kb", [0.1] * 1024, top_k=5, ef=None)

        assert captured["search_kwargs"]["param"]["params"]["ef"] == 128

    def test_ef_default_when_omitted(self):
        """不传 ef 时使用默认 128（保证旧调用点行为不变）"""
        client, mock_collection, captured = _build_client_with_capture()

        with patch("app.storage.milvus.Collection", return_value=mock_collection):
            client._search_dense_sync("test_kb", [0.1] * 1024, top_k=5)

        assert captured["search_kwargs"]["param"]["params"]["ef"] == 128


class TestSearchDenseAsyncEf:
    """search_dense（async）的 ef 透传"""

    @pytest.mark.asyncio
    async def test_async_passes_ef_to_sync(self):
        """search_dense 把 ef 透传给 _search_dense_sync"""
        client = MilvusClient()
        client._search_dense_sync = MagicMock(return_value=[])

        vector = [0.1] * 1024
        await client.search_dense("test_kb", vector, top_k=5, ef=200)
        client._search_dense_sync.assert_called_once_with("test_kb", vector, 5, None, 200, 0)

    @pytest.mark.asyncio
    async def test_async_ef_none_falls_back_to_128(self):
        """search_dense 不传 ef 时回落 128 透传给同步实现"""
        client = MilvusClient()
        client._search_dense_sync = MagicMock(return_value=[])

        vector = [0.1] * 1024
        await client.search_dense("test_kb", vector, top_k=5)
        client._search_dense_sync.assert_called_once_with("test_kb", vector, 5, None, 128, 0)


class TestVectorRetrieverEfWiring:
    """VectorRetriever.search 把 ef 透传给 milvus.search_dense"""

    @pytest.mark.asyncio
    async def test_ef_forwarded_to_milvus(self):
        """从 kwargs 取出的 ef 被透传到 milvus.search_dense(ef=...)"""
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

        milvus = MagicMock()
        milvus.search_dense = AsyncMock(return_value=[])

        retriever = VectorRetriever(embed_provider=embedder, milvus_client=milvus)
        await retriever.search("查询", kb_id="kb_001", top_k=5, ef=321)

        _, kwargs = milvus.search_dense.call_args
        assert kwargs["ef"] == 321

    @pytest.mark.asyncio
    async def test_ef_none_when_not_provided(self):
        """未提供 ef 时透传 ef=None，由 milvus 侧回落 128"""
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

        milvus = MagicMock()
        milvus.search_dense = AsyncMock(return_value=[])

        retriever = VectorRetriever(embed_provider=embedder, milvus_client=milvus)
        await retriever.search("查询", kb_id="kb_001", top_k=5)

        _, kwargs = milvus.search_dense.call_args
        assert kwargs["ef"] is None
