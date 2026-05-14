"""SparseRetriever 单元测试"""

import sys
from typing import Dict, List, Optional
from unittest.mock import MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.retrieval.base import RetrievalResult  # noqa: E402
from app.retrieval.sparse import SparseRetriever  # noqa: E402


class FakeEmbedProvider:
    """模拟 EmbedProvider，返回固定稀疏向量"""

    async def embed(self, texts: List[str]) -> List[List[float]]:
        return [[0.1] * 1024 for _ in texts]

    async def embed_sparse(self, texts: List[str]) -> List[Dict[int, float]]:
        return [{0: 1.0, 5: 0.8, 100: 0.3} for _ in texts]


class FakeMilvusClient:
    """模拟 MilvusClient，返回预设搜索结果"""

    def __init__(self, results: Optional[List[dict]] = None):
        self._results = results or []
        self.last_kb_id: Optional[str] = None
        self.last_sparse_vector: Optional[Dict[int, float]] = None
        self.last_top_k: Optional[int] = None

    async def search_sparse(
        self, kb_id: str, sparse_vector: dict[int, float], top_k: int = 10
    ) -> list[dict]:
        self.last_kb_id = kb_id
        self.last_sparse_vector = sparse_vector
        self.last_top_k = top_k
        return self._results


@pytest.mark.asyncio
async def test_search_returns_results():
    """测试基本检索流程：生成稀疏向量 → 搜索 → 转换结果"""
    fake_hits = [
        {
            "chunk_id": "c1",
            "doc_id": "d1",
            "content": "第一条内容",
            "parent_id": "p1",
            "chunk_index": 0,
            "score": 0.95,
        },
        {
            "chunk_id": "c2",
            "doc_id": "d1",
            "content": "第二条内容",
            "parent_id": "p1",
            "chunk_index": 1,
            "score": 0.80,
        },
    ]

    embedder = FakeEmbedProvider()
    milvus = FakeMilvusClient(results=fake_hits)
    retriever = SparseRetriever(embed_provider=embedder, milvus_client=milvus)

    results = await retriever.search("测试查询", kb_id="kb_001", top_k=5)

    # 验证返回类型和数量
    assert len(results) == 2
    assert all(isinstance(r, RetrievalResult) for r in results)

    # 验证传递给 Milvus 的参数
    assert milvus.last_kb_id == "kb_001"
    assert milvus.last_top_k == 5
    assert milvus.last_sparse_vector == {0: 1.0, 5: 0.8, 100: 0.3}


@pytest.mark.asyncio
async def test_search_results_sorted_by_score_descending():
    """测试结果按分数降序排列"""
    fake_hits = [
        {"chunk_id": "c1", "doc_id": "d1", "content": "低分", "parent_id": "", "chunk_index": 0, "score": 0.5},
        {"chunk_id": "c2", "doc_id": "d1", "content": "高分", "parent_id": "", "chunk_index": 1, "score": 0.99},
        {"chunk_id": "c3", "doc_id": "d2", "content": "中分", "parent_id": "", "chunk_index": 0, "score": 0.75},
    ]

    embedder = FakeEmbedProvider()
    milvus = FakeMilvusClient(results=fake_hits)
    retriever = SparseRetriever(embed_provider=embedder, milvus_client=milvus)

    results = await retriever.search("查询", kb_id="kb_002")

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0].chunk_id == "c2"
    assert results[1].chunk_id == "c3"
    assert results[2].chunk_id == "c1"


@pytest.mark.asyncio
async def test_search_empty_results():
    """测试无结果时返回空列表"""
    embedder = FakeEmbedProvider()
    milvus = FakeMilvusClient(results=[])
    retriever = SparseRetriever(embed_provider=embedder, milvus_client=milvus)

    results = await retriever.search("无匹配查询", kb_id="kb_003")

    assert results == []


@pytest.mark.asyncio
async def test_search_result_metadata():
    """测试结果 metadata 包含 parent_id 和 chunk_index"""
    fake_hits = [
        {
            "chunk_id": "c1",
            "doc_id": "d1",
            "content": "内容",
            "parent_id": "parent_001",
            "chunk_index": 3,
            "score": 0.9,
        },
    ]

    embedder = FakeEmbedProvider()
    milvus = FakeMilvusClient(results=fake_hits)
    retriever = SparseRetriever(embed_provider=embedder, milvus_client=milvus)

    results = await retriever.search("查询", kb_id="kb_004")

    assert results[0].metadata["parent_id"] == "parent_001"
    assert results[0].metadata["chunk_index"] == 3
