"""检索测试接口

TODO: [准度风险] 当知识库中大量表格 chunk（如 CSV 5万+条）与少量文档 chunk 共存时，
  表格 chunk 可能在检索时"淹没"其他文档结果。后续可通过：
  1. 检索时加 doc_id / file_type 过滤
  2. 按文档类型加权评分
  3. 调整 top_k 策略
  来缓解。
"""

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.executor import RetrievalExecutor
from app.agent.orchestrator import AgentOrchestrator
from app.agent.reflector import Reflector
from app.agent.rewriter import QueryRewriter
from app.agent.router import QueryRouter
from app.config import get_settings
from app.models.manager import get_model_manager
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.sparse import SparseRetriever
from app.retrieval.vector import VectorRetriever
from app.schema.db import LLMConfig
from app.storage.database import async_session
from app.storage.milvus import MilvusClient

from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/retrieval", tags=["Retrieval"])


# ============================================================
# 请求/响应模型
# ============================================================


class RetrievalTestRequest(BaseModel):
    """检索测试请求"""
    query: str = Field(..., min_length=1, description="查询文本")
    knowledge_base_id: str = Field(..., description="知识库 ID")
    mode: str = Field(default="hybrid", description="检索模式: direct / hybrid / agent")
    top_k: int = Field(default=10, ge=1, le=100, description="返回结果数量")
    model_config_id: str | None = Field(default=None, description="LLM 模型配置 ID（agent 模式需要）")


class RetrievalResultItem(BaseModel):
    """单条检索结果"""
    chunk_id: str
    doc_id: str
    filename: str = ""
    content: str
    child_content: str = ""
    score: float
    metadata: dict = Field(default_factory=dict)


class RetrievalTestResponse(BaseModel):
    """检索测试响应"""
    query: str
    mode: str
    total: int
    iterations: int = 0
    degraded: bool = False
    results: list[RetrievalResultItem]


# ============================================================
# 接口实现
# ============================================================


def _get_milvus() -> MilvusClient:
    """获取 Milvus 客户端"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


@router.post("/test")
async def retrieval_test(body: RetrievalTestRequest):
    """检索测试：agent 模式返回 SSE 流式（含进度），其他模式返回 JSON"""
    if body.mode == "agent":
        return StreamingResponse(
            _stream_retrieval_test(body),
            media_type="text/event-stream",
        )
    return await _sync_retrieval_test(body)


async def _sync_retrieval_test(body: RetrievalTestRequest) -> RetrievalTestResponse:
    """非 agent 模式的同步检索测试"""
    manager = get_model_manager()
    milvus = _get_milvus()

    if body.mode == "direct":
        retriever = VectorRetriever(manager.embedder, milvus)
        results = await retriever.search(body.query, body.knowledge_base_id, top_k=body.top_k)
    else:
        # hybrid 模式
        vector_retriever = VectorRetriever(manager.embedder, milvus)
        sparse_retriever = SparseRetriever(manager.embedder, milvus)
        hybrid_retriever = HybridRetriever(
            vector_retriever=vector_retriever,
            sparse_retriever=sparse_retriever,
            rerank_provider=manager.reranker,
            db_session_factory=async_session,
        )
        results = await hybrid_retriever.search(body.query, body.knowledge_base_id, top_k=body.top_k)

    items = await _build_result_items(results)
    return RetrievalTestResponse(
        query=body.query,
        mode=body.mode,
        total=len(items),
        iterations=0,
        degraded=False,
        results=items,
    )


async def _stream_retrieval_test(body: RetrievalTestRequest):
    """Agent 模式的 SSE 流式检索测试，推送进度事件 + 最终结果"""
    manager = get_model_manager()
    milvus = _get_milvus()
    settings = get_settings()

    llm = await _get_llm_for_retrieval(body.model_config_id)
    vector_retriever = VectorRetriever(manager.embedder, milvus)
    sparse_retriever = SparseRetriever(manager.embedder, milvus)
    hybrid_retriever = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
    )
    orchestrator = AgentOrchestrator(
        router=QueryRouter(llm),
        rewriter=QueryRewriter(llm),
        executor=RetrievalExecutor(hybrid_retriever, embedder=manager.embedder),
        reflector=Reflector(llm),
        retriever=hybrid_retriever,
        max_iterations=settings.agent_max_iterations,
        timeout=settings.agent_timeout,
    )

    # 使用队列推送进度
    progress_queue: asyncio.Queue = asyncio.Queue()

    async def on_progress(step: str, detail: str):
        await progress_queue.put({"type": "progress", "step": step, "detail": detail})

    # 启动 agent 任务
    agent_task = asyncio.create_task(
        orchestrator.run(body.query, body.knowledge_base_id, on_progress=on_progress)
    )

    # 推送进度事件
    while not agent_task.done():
        try:
            event = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.TimeoutError:
            continue

    # 排空剩余事件
    while not progress_queue.empty():
        event = progress_queue.get_nowait()
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    # 获取结果
    agent_result = agent_task.result()
    items = await _build_result_items(agent_result.chunks)

    # 推送最终结果
    final = RetrievalTestResponse(
        query=body.query,
        mode=body.mode,
        total=len(items),
        iterations=agent_result.iterations,
        degraded=agent_result.degraded,
        results=items,
    )
    yield f"data: {json.dumps({'type': 'result', **final.model_dump()}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _build_result_items(results) -> list[RetrievalResultItem]:
    """将检索结果转换为响应格式"""
    doc_ids = list(set(r.doc_id for r in results))
    doc_filenames: dict[str, str] = {}
    if doc_ids:
        async with async_session() as session:
            from app.schema.db import Document
            result = await session.execute(
                select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
            )
            for row in result:
                doc_filenames[row.id] = row.filename

    return [
        RetrievalResultItem(
            chunk_id=r.chunk_id,
            doc_id=r.doc_id,
            filename=doc_filenames.get(r.doc_id, ""),
            content=r.content,
            child_content=r.child_content or r.content,
            score=round(r.score, 4),
            metadata=r.metadata,
        )
        for r in results
    ]


async def _get_llm_for_retrieval(model_config_id: str | None):
    """获取检索测试用的 LLM 实例"""
    if model_config_id:
        async with async_session() as session:
            result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == model_config_id)
            )
            config = result.scalar_one_or_none()
            if config:
                if config.provider == "ollama":
                    return OllamaLLM(base_url=config.base_url, model=config.model)
                return VllmLLM(base_url=config.base_url, model=config.model, api_key=config.api_key or "")

    # 尝试数据库中的默认配置
    async with async_session() as session:
        result = await session.execute(
            select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            if config.provider == "ollama":
                return OllamaLLM(base_url=config.base_url, model=config.model)
            return VllmLLM(base_url=config.base_url, model=config.model, api_key=config.api_key or "")

    # 回退到 env 配置
    settings = get_settings()
    if settings.llm_provider == "vllm":
        return VllmLLM(base_url=settings.llm_base_url, model=settings.llm_model, api_key=settings.llm_api_key)
    return OllamaLLM(base_url=settings.llm_base_url, model=settings.llm_model)
