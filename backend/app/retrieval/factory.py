"""检索器工厂：集中构建混合检索器（含可选图谱第四路）。

此前 ``HybridRetriever`` 的装配（含按门控注入图谱第四路）内联在 ``app.api.chat`` 里，
仅问答链路可用。检索召回接口（``app.api.retrieval``）若要与生产问答链路保持同一召回口径
（含图谱第四路），需要复用同一套装配逻辑。将其下沉到 retrieval 层的工厂，供
``chat`` / ``retrieval`` 两个入口共享，避免各自维护一份、行为漂移。

- ``maybe_build_graph_retriever``：按全局开关 + 图存储可用性构造图谱召回第四路，任一不满足
  返回 ``None``（此时 HybridRetriever 不追加第四路，dense/sparse/bm25 + RRF 行为与未引入
  图谱时逐字节一致，Property 8 零回归）。
- ``build_hybrid_retriever``：三路（Dense + Sparse + BM25）+ 可选图谱第四路。
"""

import logging

from app.config import get_settings
from app.models.manager import get_model_manager
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.config import get_platform_config_store
from app.retrieval.graph_retriever import GraphRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.sparse import SparseRetriever
from app.retrieval.vector import VectorRetriever
from app.storage.database import async_session
from app.storage.milvus import get_milvus_client
from app.storage.milvus_event_store import get_milvus_event_store
from app.storage.graph_store import get_graph_store

logger = logging.getLogger(__name__)


async def maybe_build_graph_retriever() -> GraphRetriever | None:
    """按门控构造图谱召回第四路（design.md 4.5，Property 8 零回归的唯一开关）。

    仅当 **全局开关开启**（``settings.graph_enable``）**且图存储可用**
    （``get_graph_store()`` 非 None）时返回 ``GraphRetriever``；任一不满足返回 ``None``。
    返回 None 时 HybridRetriever 不追加第四路，dense/sparse/bm25 + RRF 行为与未引入本功能
    时逐字节一致（Requirements 7.2 / Property 8）。

    ``hops`` / ``max_chunks`` 取自平台配置（PlatformConfig）。``llm_provider`` 传 None：
    实体名抽取回退为分词（GraphRetriever 自带的软降级），避免与每请求 LLM 实例耦合；
    KB 级图谱开关与 store 非空在 GraphRetriever.search 内部再次自门控（双重保险）。

    事件中心召回（event-centric）：传入 ``event_store`` 启用入口A（事件向量召回）。
    ``get_milvus_event_store()`` 仅返回进程内单例（不主动连 Milvus，连接失败在 search 时
    按 collection 不存在 / not-loaded 干净降级返回 ``[]``）；若获取单例本身抛错则降级为
    ``event_store=None``（入口A 跳过，仅走入口B），保证图谱第四路仍可用。
    """
    settings = get_settings()
    # 第一道门控：全局开关。未开启 → 主链路零额外成本（不连 Neo4j、不构造第四路）。
    if not settings.graph_enable:
        return None
    # 第二道门控：图存储可用性。None 表示 Neo4j 不可用 / 驱动未安装 → 不注入第四路。
    store = await get_graph_store()
    if store is None:
        return None
    manager = get_model_manager()
    platform = await get_platform_config_store().get_effective()
    # 事件向量集合单例：获取失败则降级为 None（入口A 跳过，仅走入口B），不阻断第四路。
    try:
        event_store = get_milvus_event_store()
    except Exception:  # noqa: BLE001 - 获取单例失败按降级处理，不影响主链路
        logger.warning("获取 MilvusEventStore 失败，事件向量召回入口A 降级跳过", exc_info=True)
        event_store = None
    return GraphRetriever(
        store=store,
        db_session_factory=async_session,
        embedder=manager.embedder,
        llm_provider=None,
        hops=platform.graph_retriever_hops,
        max_chunks=platform.graph_retriever_max_chunks,
        event_store=event_store,
        # seed_k/max_events/coarse_top_k 为 KB 级配置，共享检索器不在此覆盖，
        # 沿用构造默认值（config.py DEFAULT_EVENT_*）。
    )


async def build_hybrid_retriever() -> HybridRetriever:
    """构建混合检索器（Dense + Sparse + BM25 + 可选图谱第四路）。

    BM25 检索器对旧 schema collection 自动降级为空结果，不影响现有功能。
    图谱第四路仅在全局开关开启且图存储可用时注入；否则 ``graph_retriever=None``，
    检索行为与未引入图谱功能时完全一致（Property 8 零回归）。
    """
    manager = get_model_manager()
    milvus = get_milvus_client()
    vector_retriever = VectorRetriever(manager.embedder, milvus)
    sparse_retriever = SparseRetriever(manager.embedder, milvus)
    bm25_retriever = BM25Retriever(milvus)
    graph_retriever = await maybe_build_graph_retriever()
    return HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
        bm25_retriever=bm25_retriever,
        graph_retriever=graph_retriever,
    )
