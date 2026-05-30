"""检索测试接口

提供纯检索测试能力（不经过 LLM 生成），用于调参与召回质量验证：
- direct 模式：仅稠密向量检索，最快，观察纯语义召回
- hybrid 模式：三路混合检索（Dense + Sparse + BM25）+ RRF + Rerank + MMR + 父块扩展，
  并返回检索链路各阶段的中间信号（各路召回数、漏斗、每条结果的多维分数与命中路由），
  这是 Langfuse 等被动观测工具无法提供的"主动探针"能力，专为调参设计。

TODO: [准度风险] 当知识库中大量表格 chunk（如 CSV 5万+条）与少量文档 chunk 共存时，
  表格 chunk 可能在检索时"淹没"其他文档结果。后续可通过：
  1. 检索时加 doc_id / file_type 过滤
  2. 按文档类型加权评分
  3. 调整 top_k 策略
  来缓解。
"""

import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_settings
from app.models.manager import get_model_manager
from app.retrieval.base import RetrievalResult
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.sparse import SparseRetriever
from app.retrieval.vector import VectorRetriever
from app.storage.database import async_session
from app.storage.milvus import MilvusClient

from fastapi import Depends
from app.api.deps import authorization_guard
from app.api.errors import PermissionDeniedError
from app.auth.constants import PermissionEnum
from app.auth.identity import IdentityContext
from app.auth.kb_authz import KbAccessEnum
from app.auth.kb_scope import authorize_requested_kbs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/retrieval", tags=["Retrieval"])


async def _authorize_and_boundary(identity: IdentityContext, kb_id: str) -> None:
    """召回前置：内容边界（超管默认不可读正文）+ KB 读授权（触达 Milvus 前先拒）。"""
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")
    async with async_session() as session:
        await authorize_requested_kbs(session, identity, [kb_id], KbAccessEnum.READ)


# ============================================================
# 请求/响应模型
# ============================================================


class RetrievalTestRequest(BaseModel):
    """检索测试请求"""

    query: str = Field(..., min_length=1, description="查询文本")
    knowledge_base_id: str = Field(..., description="知识库 ID")
    mode: str = Field(default="hybrid", description="检索模式: direct / hybrid")
    top_k: int = Field(default=10, ge=1, le=100, description="返回结果数量")


class RetrievalResultItem(BaseModel):
    """单条检索结果（含多维分数与命中路由）"""

    chunk_id: str
    doc_id: str
    filename: str = ""
    content: str
    child_content: str = ""
    score: float  # 最终分数（hybrid=composite，direct=稠密相似度）
    rrf_score: float | None = None  # RRF 融合分数（仅 hybrid）
    rerank_score: float | None = None  # Rerank 精排分数（仅 hybrid）
    routes: list[str] = Field(default_factory=list)  # 命中路由：dense/sparse/bm25
    metadata: dict = Field(default_factory=dict)


class RouteInfo(BaseModel):
    """单路检索召回统计"""

    name: str
    recalled: int
    enabled: bool = True


class FunnelStage(BaseModel):
    """检索链路单个阶段的结果数"""

    stage: str
    count: int


class RetrievalTrace(BaseModel):
    """检索链路追踪信息（仅 hybrid 模式返回）"""

    routes: list[RouteInfo]
    funnel: list[FunnelStage]


class RetrievalTestResponse(BaseModel):
    """检索测试响应"""

    query: str
    mode: str
    total: int
    elapsed_ms: int
    results: list[RetrievalResultItem]
    trace: RetrievalTrace | None = None


# ============================================================
# 接口实现
# ============================================================


def _get_milvus() -> MilvusClient:
    """获取 Milvus 客户端"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


@router.post("/test", response_model=RetrievalTestResponse)
async def retrieval_test(
    body: RetrievalTestRequest,
    identity: IdentityContext = Depends(
        authorization_guard(required_permissions={PermissionEnum.RECALL_INVOKE.value})
    ),
) -> RetrievalTestResponse:
    """纯检索测试：direct（稠密）/ hybrid（三路混合 + 链路追踪）

    不经过 LLM 生成，仅返回检索召回的 chunk 及其分数信号，专用于调参。
    """
    # 触达 Milvus 前先校验 KB 读权限（跨租户/不可读 404）+ 内容边界
    await _authorize_and_boundary(identity, body.knowledge_base_id)

    start = time.perf_counter()
    manager = get_model_manager()
    milvus = _get_milvus()

    if body.mode == "direct":
        retriever = VectorRetriever(manager.embedder, milvus)
        results = await retriever.search(body.query, body.knowledge_base_id, top_k=body.top_k)
        items = await _build_result_items(results)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return RetrievalTestResponse(
            query=body.query,
            mode="direct",
            total=len(items),
            elapsed_ms=elapsed_ms,
            results=items,
            trace=None,
        )

    # hybrid 模式（三路：Dense + Sparse + BM25）+ 链路追踪
    vector_retriever = VectorRetriever(manager.embedder, milvus)
    sparse_retriever = SparseRetriever(manager.embedder, milvus)
    bm25_retriever = BM25Retriever(milvus)
    hybrid_retriever = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
        bm25_retriever=bm25_retriever,
    )

    results, trace_data = await hybrid_retriever.search_with_trace(
        body.query, body.knowledge_base_id, top_k=body.top_k
    )
    items = await _build_result_items(results, trace_data.get("per_result"))
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    trace = RetrievalTrace(
        routes=[RouteInfo(**r) for r in trace_data.get("routes", [])],
        funnel=[FunnelStage(**f) for f in trace_data.get("funnel", [])],
    )
    return RetrievalTestResponse(
        query=body.query,
        mode="hybrid",
        total=len(items),
        elapsed_ms=elapsed_ms,
        results=items,
        trace=trace,
    )


async def _build_result_items(
    results: list[RetrievalResult],
    per_result: dict | None = None,
) -> list[RetrievalResultItem]:
    """将检索结果转换为响应格式，附带文件名与（可选的）链路分数信号"""
    doc_ids = list({r.doc_id for r in results})
    doc_filenames: dict[str, str] = {}
    if doc_ids:
        async with async_session() as session:
            from app.schema.db import Document

            result = await session.execute(
                select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
            )
            for row in result:
                doc_filenames[row.id] = row.filename

    items: list[RetrievalResultItem] = []
    for r in results:
        trace_entry = (per_result or {}).get(r.chunk_id, {})
        items.append(
            RetrievalResultItem(
                chunk_id=r.chunk_id,
                doc_id=r.doc_id,
                filename=doc_filenames.get(r.doc_id, ""),
                content=r.content,
                child_content=r.child_content or r.content,
                score=round(r.score, 4),
                rrf_score=trace_entry.get("rrf_score"),
                rerank_score=trace_entry.get("rerank_score"),
                routes=trace_entry.get("routes", []),
                metadata=r.metadata,
            )
        )
    return items
