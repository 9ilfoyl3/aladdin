"""HybridRetriever 配置注入 wiring 单元测试（任务 4.5）

验证 search / search_with_trace 在入口取一次有效配置快照，并把
recall_k / rrf_k / rerank_candidate_k / composite 权重 / mmr 参数
正确传递到链路各阶段。

参考 test_hybrid_retriever.py 的 pymilvus mock 模式。
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.retrieval.base import BaseRetriever, RetrievalResult  # noqa: E402
from app.retrieval.config import RetrievalConfig  # noqa: E402
from app.retrieval.hybrid import HybridRetriever  # noqa: E402


class RecordingRetriever(BaseRetriever):
    """记录每次 search 收到的 top_k，返回预设结果切片。"""

    def __init__(self, results: list[RetrievalResult]):
        self._results = results
        self.received_top_k: list[int] = []

    async def search(self, query: str, kb_id: str, top_k: int = 10, **kwargs):
        self.received_top_k.append(top_k)
        return self._results[:top_k]


class RecordingReranker:
    """记录每次 rerank 收到的候选文档数，按原序返回递减分数。"""

    def __init__(self):
        self.received_doc_counts: list[int] = []

    async def rerank(self, query: str, documents: list[str], top_k: int = 10):
        self.received_doc_counts.append(len(documents))
        count = min(top_k, len(documents))
        return [(i, 1.0 - i * 0.01) for i in range(count)]


class FakeAsyncSession:
    """模拟异步数据库会话（父块扩展查询返回空）。"""

    async def execute(self, stmt):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSessionFactory:
    def __call__(self):
        return FakeAsyncSession()


def _make_results(n: int) -> list[RetrievalResult]:
    """生成 n 条内容彼此独特的结果，避免被 MMR 误过滤。"""
    return [
        RetrievalResult(
            chunk_id=f"c{i}",
            content=f"独特内容编号{i}号-{'甲乙丙丁戊己庚辛壬癸'[i % 10]}文本片段{i}",
            score=1.0 - i * 0.05,
            doc_id=f"d{i}",
            metadata={"parent_id": "", "chunk_index": i},
        )
        for i in range(n)
    ]


def _make_config() -> RetrievalConfig:
    """构造一份与默认值明显不同的已知配置，便于断言透传。"""
    return RetrievalConfig(
        recall_k=5,
        rerank_candidate_k=3,
        rrf_k=99,
        composite_rerank_weight=0.5,
        composite_base_weight=0.4,
        composite_source_weight=0.1,
        mmr_lambda=0.55,
        mmr_threshold=0.66,
    )


def _build_hybrid(config: RetrievalConfig):
    """构造一个注入 mock config_store 的 HybridRetriever，并埋点捕获参数。"""
    dense = _make_results(8)
    sparse = _make_results(8)

    vector_retriever = RecordingRetriever(dense)
    sparse_retriever = RecordingRetriever(sparse)
    reranker = RecordingReranker()
    db_factory = FakeSessionFactory()

    config_store = MagicMock()
    config_store.get_effective = AsyncMock(return_value=config)

    platform_store = MagicMock()
    platform_store.get_load_cache_ttl = AsyncMock(return_value=0)

    hybrid = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=reranker,
        db_session_factory=db_factory,
        config_store=config_store,
        platform_store=platform_store,
    )

    # 埋点：捕获 _rrf_fusion 的 k
    captured: dict = {}
    orig_rrf = hybrid._rrf_fusion

    def spy_rrf(results_lists, k=60, type_weights=None):
        captured["rrf_k"] = k
        return orig_rrf(results_lists, k=k, type_weights=type_weights)

    hybrid._rrf_fusion = spy_rrf

    # 埋点：捕获 _apply_composite_scoring 收到的 config
    orig_composite = hybrid._apply_composite_scoring

    def spy_composite(results, config=None):
        captured["composite_config"] = config
        return orig_composite(results, config)

    hybrid._apply_composite_scoring = spy_composite

    # 埋点：捕获 _apply_mmr 的 lambda_param / threshold
    orig_mmr = HybridRetriever._apply_mmr

    def spy_mmr(results, lambda_param=0.7, threshold=0.7):
        captured["mmr_lambda"] = lambda_param
        captured["mmr_threshold"] = threshold
        return orig_mmr(results, lambda_param=lambda_param, threshold=threshold)

    hybrid._apply_mmr = spy_mmr

    return hybrid, config_store, vector_retriever, sparse_retriever, reranker, captured


@pytest.mark.asyncio
async def test_search_injects_recall_k_into_each_route():
    """config.recall_k 被用作每路召回的 top_k。"""
    config = _make_config()
    hybrid, _, vector_retriever, sparse_retriever, _, _ = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    assert vector_retriever.received_top_k == [config.recall_k]
    assert sparse_retriever.received_top_k == [config.recall_k]


@pytest.mark.asyncio
async def test_search_injects_rrf_k():
    """config.rrf_k 被传入 _rrf_fusion。"""
    config = _make_config()
    hybrid, _, _, _, _, captured = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    assert captured["rrf_k"] == config.rrf_k


@pytest.mark.asyncio
async def test_search_injects_rerank_candidate_k():
    """config.rerank_candidate_k 用作 rerank 候选切片大小。"""
    config = _make_config()
    hybrid, _, _, _, reranker, _ = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    # fused 去重后 >= rerank_candidate_k，候选被切到 rerank_candidate_k 条
    assert reranker.received_doc_counts == [config.rerank_candidate_k]


@pytest.mark.asyncio
async def test_search_injects_composite_weights():
    """composite 权重通过 config 透传到 _apply_composite_scoring。"""
    config = _make_config()
    hybrid, _, _, _, _, captured = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    passed = captured["composite_config"]
    assert passed is config
    assert passed.composite_rerank_weight == config.composite_rerank_weight
    assert passed.composite_base_weight == config.composite_base_weight
    assert passed.composite_source_weight == config.composite_source_weight


@pytest.mark.asyncio
async def test_search_injects_mmr_params():
    """config 的 mmr_lambda / mmr_threshold 被传入 _apply_mmr。"""
    config = _make_config()
    hybrid, _, _, _, _, captured = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    assert captured["mmr_lambda"] == config.mmr_lambda
    assert captured["mmr_threshold"] == config.mmr_threshold


@pytest.mark.asyncio
async def test_search_reads_config_once():
    """单次 search 只调用一次 get_effective（单次检索快照一致性）。

    无租户上下文（未进入 tenant_scope）时按 None 读取，回落全默认（Req 1.9）。
    """
    config = _make_config()
    hybrid, config_store, _, _, _, _ = _build_hybrid(config)

    await hybrid.search("查询", kb_id="kb_001", top_k=4)

    assert config_store.get_effective.await_count == 1
    # 无租户上下文 → 以 None 读取
    config_store.get_effective.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_search_with_trace_injects_config_and_reads_once():
    """search_with_trace 同样取一次快照并透传召回/融合/候选/打分/去重参数。"""
    config = _make_config()
    hybrid, config_store, vector_retriever, sparse_retriever, reranker, captured = _build_hybrid(config)

    await hybrid.search_with_trace("查询", kb_id="kb_001", top_k=4)

    assert config_store.get_effective.await_count == 1
    assert vector_retriever.received_top_k == [config.recall_k]
    assert sparse_retriever.received_top_k == [config.recall_k]
    assert captured["rrf_k"] == config.rrf_k
    assert reranker.received_doc_counts == [config.rerank_candidate_k]
    assert captured["composite_config"] is config
    assert captured["mmr_lambda"] == config.mmr_lambda
    assert captured["mmr_threshold"] == config.mmr_threshold
