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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_settings
from app.models.manager import get_model_manager
from app.retrieval.base import RetrievalResult
from app.retrieval.factory import build_hybrid_retriever
from app.retrieval.log_safety import sanitize_for_log
from app.retrieval.multi_kb import KBRetrievalConfig, MultiKBRetriever
from app.retrieval.vector import VectorRetriever
from app.session_upload.service import get_session_upload_service
from app.storage.database import async_session
from app.storage.milvus import (
    SESSION_FILES_KB_ID,
    MilvusClient,
    build_session_id_expr,
    get_milvus_client,
)

from app.api.deps import require_authenticated
from app.api.errors import PermissionDeniedError
from app.auth.identity import IdentityContext
from app.auth.kb_authz import KbAccessEnum
from app.auth.kb_scope import authorize_requested_kbs
from app.auth.session_ownership import verify_session_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/retrieval", tags=["Retrieval"])


async def _authorize_and_boundary(
    identity: IdentityContext,
    kb_ids: list[str],
    session_id: str | None = None,
) -> None:
    """召回前置授权（触达 Milvus 前先拒）。

    - 内容边界：超管默认不可读业务正文。
    - KB 读授权：逐个校验 ``kb_ids`` 处于身份可读范围（跨租户/不可读 → 404）。
    - 会话归属：若指定 ``session_id``，校验其归属调用者本人（防止凭他人 session_id
      召回其附件内容），非本人 → 404（存在性非泄露）。
    """
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")
    if kb_ids:
        async with async_session() as session:
            await authorize_requested_kbs(session, identity, kb_ids, KbAccessEnum.READ)
    if session_id:
        await verify_session_owner(session_id, identity)


# ============================================================
# 请求/响应模型
# ============================================================


class RetrievalTestRequest(BaseModel):
    """检索测试请求"""

    query: str = Field(..., min_length=1, description="查询文本")
    knowledge_base_id: str | None = Field(
        default=None, description="单知识库 ID（与 kb_ids 二选一；两者可同时省略而只传 session_id）"
    )
    kb_ids: list[str] | None = Field(
        default=None, description="多知识库联合检索的知识库 ID 列表（与 knowledge_base_id 二选一）"
    )
    session_id: str | None = Field(
        default=None,
        description="会话 ID：把该会话已上传的附件作为一路检索源并入召回（需为调用者本人会话）。"
        "可单独使用，也可与知识库联合。",
    )
    mode: str = Field(
        default="hybrid",
        description="检索模式: direct（仅稠密）/ hybrid（三路混合 + 平台开启图谱时并入图谱第四路）。"
        "注意：多源（多库或含会话附件）统一按 hybrid 混合召回口径执行，direct 仅在单库单源时生效。",
    )
    top_k: int = Field(default=10, ge=1, le=100, description="返回结果数量")

    def resolve_kb_ids(self) -> list[str]:
        """归并 ``kb_ids`` 与单选 ``knowledge_base_id`` 为去重后的知识库 ID 列表（保持顺序）。

        kb_ids 优先；仅传 knowledge_base_id 时退化为单元素列表；两者皆空返回空列表
        （此时必须提供 session_id，否则无检索范围）。
        """
        raw = list(self.kb_ids) if self.kb_ids else (
            [self.knowledge_base_id] if self.knowledge_base_id else []
        )
        seen: set[str] = set()
        deduped: list[str] = []
        for kb_id in raw:
            if kb_id and kb_id not in seen:
                seen.add(kb_id)
                deduped.append(kb_id)
        return deduped


class RetrievalResultItem(BaseModel):
    """单条检索结果（含多维分数与命中路由）"""

    chunk_id: str
    doc_id: str
    filename: str = ""
    # 命中来源类型，供前端选对原件接口：
    #   "knowledge_base" → 知识库文档，原件走 /api/documents/{doc_id}/raw
    #   "session"        → 会话附件，原件走 /api/sessions/{session_id}/files/{doc_id}/raw
    source_type: str = "knowledge_base"
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
    degraded: bool = False  # 是否有检索源失败（多源场景才可能为 True）
    failed_source_count: int = 0  # 失败的检索源数量（多源场景；单源恒为 0）


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
    kb_ids = body.resolve_kb_ids()
    session_id = body.session_id
    if not kb_ids and not session_id:
        raise HTTPException(
            status_code=400,
            detail="必须指定 knowledge_base_id / kb_ids 或 session_id",
        )

    # 触达 Milvus 前先校验：内容边界 + KB 读权限（跨租户/不可读 404）+ 会话归属（非本人 404）。
    await _authorize_and_boundary(identity, kb_ids, session_id)

    # 多源（多库 或 含会话附件）走 MultiKBRetriever 混合召回（与生产 chat 同口径）；
    # 单库单源保留原 direct/hybrid + trace 行为，零回归。
    is_multi_source = len(kb_ids) > 1 or session_id is not None
    if is_multi_source:
        return await _run_multi_source_retrieval(body, kb_ids, session_id, identity)

    return await _run_single_kb_retrieval(body, kb_ids[0])


async def _run_single_kb_retrieval(
    body: RetrievalTestRequest, kb_id: str
) -> RetrievalTestResponse:
    """单库单源检索：保留原 ``direct`` / ``hybrid`` + trace 行为（零回归）。

    授权已在 ``_run_retrieval`` 前置完成，此处只负责召回与结果组装。
    """
    start = time.perf_counter()

    if body.mode == "direct":
        manager = get_model_manager()
        retriever = VectorRetriever(manager.embedder, _get_milvus())
        results = await retriever.search(body.query, kb_id, top_k=body.top_k)
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
        body.query, kb_id, top_k=body.top_k
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


async def _run_multi_source_retrieval(
    body: RetrievalTestRequest,
    kb_ids: list[str],
    session_id: str | None,
    identity: IdentityContext,
) -> RetrievalTestResponse:
    """多库 / 会话附件联合检索：走 ``MultiKBRetriever`` 混合召回（与生产 chat 同口径）。

    各源同权（priority=1.0），最终顺序交由统一 rerank 决定；trace 返回 ``null``
    （多源不聚合单源链路信号），并以 ``degraded`` / ``failed_source_count`` 反映源失败情况。
    会话附件源以 ``SESSION_FILES_KB_ID`` + ``session_id`` 标量 expr 隔离，与知识库源同权并入。
    """
    start = time.perf_counter()
    kb_configs: list[KBRetrievalConfig] = [
        KBRetrievalConfig(kb_id=kb_id, priority=1.0) for kb_id in kb_ids
    ]

    # 追加会话附件源（仅当会话确有已上传文件；探测失败降级为不含会话源，不阻塞主流程）。
    if session_id:
        try:
            if await get_session_upload_service().has_files(session_id):
                kb_configs.append(
                    KBRetrievalConfig(
                        kb_id=SESSION_FILES_KB_ID,
                        priority=1.0,
                        expr=build_session_id_expr(session_id),
                    )
                )
        except Exception as e:
            logger.warning(
                "探测会话文件源失败，本次检索将不包含会话源: %s", sanitize_for_log(e)
            )

    # 只传了 session_id 但该会话无附件 → 无检索源，返回空结果（非错误）。
    if not kb_configs:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return RetrievalTestResponse(
            query=body.query,
            mode="hybrid",
            total=0,
            elapsed_ms=elapsed_ms,
            results=[],
            trace=None,
        )

    hybrid_retriever = await build_hybrid_retriever()
    multi_kb = MultiKBRetriever(hybrid_retriever)
    multi_result = await multi_kb.search(
        body.query, kb_configs, top_k=body.top_k, tenant_id=identity.tenant_id
    )
    items = await _build_result_items(multi_result.results, session_id=session_id)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    return RetrievalTestResponse(
        query=body.query,
        mode="hybrid",
        total=len(items),
        elapsed_ms=elapsed_ms,
        results=items,
        trace=None,
        degraded=multi_result.degraded,
        failed_source_count=len(multi_result.failed_kb_ids),
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

    检索范围三选一/可组合（至少其一）：
    - ``knowledge_base_id``：单知识库；
    - ``kb_ids``：多知识库联合检索；
    - ``session_id``：并入该会话（须本人）已上传附件作为一路检索源。

    单库单源保留 direct/hybrid 与完整 ``trace``；多源（多库或含会话附件）统一按 hybrid 混合
    召回，``trace`` 为 ``null``，并以 ``degraded`` / ``failed_source_count`` 反映源失败情况。
    """
    return await _run_retrieval(body, identity)


async def _build_result_items(
    results: list[RetrievalResult],
    per_result: dict | None = None,
    session_id: str | None = None,
) -> list[RetrievalResultItem]:
    """将检索结果转换为响应格式，附带文件名与（可选的）链路分数信号。

    命中来源分两类，按 ``doc_id`` 归属识别，并以 ``source_type`` 标注供前端选对原件接口：
    - 知识库文档（``source_type="knowledge_base"``）：``doc_id`` 命中 ``documents`` 表，
      原件走 ``/api/documents/{doc_id}/raw``。
    - 会话附件（``source_type="session"``）：``doc_id`` 是 ``SessionFile.id``（不在 documents
      表），仅在带 ``session_id`` 的多源检索中出现，从 ``session_files`` 表补齐文件名，
      原件走 ``/api/sessions/{session_id}/files/{doc_id}/raw``。

    原件获取由第三方前端按 ``doc_id`` + ``source_type`` 自行调用对应原件接口，本响应不返回原件 URL。
    """
    doc_ids = list({r.doc_id for r in results})
    doc_filenames: dict[str, str] = {}
    session_file_ids: set[str] = set()
    if doc_ids:
        async with async_session() as session:
            from app.schema.db import Document

            result = await session.execute(
                select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
            )
            for row in result:
                doc_filenames[row.id] = row.filename

            # 会话附件的 doc_id 是 SessionFile.id（不在 documents 表）。仅在带 session_id 的
            # 多源检索中才可能出现，补查 session_files 表回填文件名并标记来源，否则命中项
            # 文件名为空、且前端无法区分来源类型。
            missing_ids = [d for d in doc_ids if d not in doc_filenames]
            if missing_ids and session_id:
                from app.schema.db import SessionFile

                sf_result = await session.execute(
                    select(SessionFile.id, SessionFile.filename).where(
                        SessionFile.id.in_(missing_ids)
                    )
                )
                for row in sf_result:
                    doc_filenames[row.id] = row.filename
                    session_file_ids.add(row.id)

    items: list[RetrievalResultItem] = []
    for r in results:
        trace_entry = (per_result or {}).get(r.chunk_id, {})
        source_type = "session" if r.doc_id in session_file_ids else "knowledge_base"
        items.append(
            RetrievalResultItem(
                chunk_id=r.chunk_id,
                doc_id=r.doc_id,
                filename=doc_filenames.get(r.doc_id, ""),
                source_type=source_type,
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
