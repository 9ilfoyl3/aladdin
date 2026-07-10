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
from app.retrieval.factory import build_hybrid_retriever
from app.retrieval.vector import VectorRetriever
from app.storage.database import async_session
from app.storage.milvus import MilvusClient, get_milvus_client

from fastapi import Depends
from app.api.deps import require_authenticated
from app.api.errors import PermissionDeniedError
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
    mode: str = Field(
        default="hybrid",
        description="检索模式: direct（仅稠密）/ hybrid（三路混合 + 平台开启图谱时并入图谱第四路）",
    )
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
    return get_milvus_client()


async def _run_retrieval(
    body: RetrievalTestRequest, identity: IdentityContext
) -> RetrievalTestResponse:
    """执行纯检索召回（不经 LLM），供 ``/test`` 与 ``/search`` 共用同一实现。

    - ``direct``：仅稠密向量单路，最快，无 trace。
    - ``hybrid``：三路（Dense + Sparse + BM25）+ 可选图谱第四路 + RRF + Rerank + MMR +
      父块扩展，并返回链路追踪。图谱第四路经 ``build_hybrid_retriever`` 按全局开关 + 图存储
      可用性注入，与生产问答链路（chat）同口径；未开启图谱时行为与三路完全一致。
    """
    # 触达 Milvus 前先校验 KB 读权限（跨租户/不可读 404）+ 内容边界
    await _authorize_and_boundary(identity, body.knowledge_base_id)

    start = time.perf_counter()

    if body.mode == "direct":
        manager = get_model_manager()
        retriever = VectorRetriever(manager.embedder, _get_milvus())
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

    # hybrid 模式：三路 + 可选图谱第四路（由工厂按门控注入）+ 链路追踪。
    hybrid_retriever = await build_hybrid_retriever()
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


@router.post("/test", response_model=RetrievalTestResponse)
async def retrieval_test(
    body: RetrievalTestRequest,
    identity: IdentityContext = Depends(require_authenticated()),
) -> RetrievalTestResponse:
    """纯检索测试：direct（稠密）/ hybrid（三路混合 + 可选图谱第四路 + 链路追踪）。

    不经过 LLM 生成，仅返回检索召回的 chunk 及其分数信号，主要用于前端调参页。
    与对外的 ``/search`` 行为一致（同一底层实现），保留此路径用于既有前端调用。
    """
    return await _run_retrieval(body, identity)


@router.post("/search", response_model=RetrievalTestResponse)
async def retrieval_search(
    body: RetrievalTestRequest,
    identity: IdentityContext = Depends(require_authenticated()),
) -> RetrievalTestResponse:
    """对外召回接口：direct（稠密）/ hybrid（三路 + 可选图谱第四路）。

    与 ``/test`` 能力一致（同一底层实现），独立路径供第三方集成直接调用，语义上是"检索召回"
    而非"测试"。hybrid 模式在平台开启图谱且图存储可用时自动并入图谱第四路，与生产问答链路
    的召回口径一致。可用代理 Key + ``X-External-User-Id`` 调用。
    """
    return await _run_retrieval(body, identity)


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
