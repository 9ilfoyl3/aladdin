"""Chat API - OpenAI 兼容接口

实现 POST /v1/chat/completions 端点，支持流式和非流式响应，
集成三档检索模式（direct / hybrid / agent）。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent.orchestrator import AgentOrchestrator, AgentResult
from app.agent.executor import RetrievalExecutor
from app.agent.reflector import Reflector
from app.agent.rewriter import QueryRewriter
from app.agent.router import QueryRouter
from app.config import get_settings
from app.models.manager import get_model_manager
from app.models.provider import LLMProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM
from app.retrieval.base import RetrievalResult
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.sparse import SparseRetriever
from app.retrieval.vector import VectorRetriever
from app.schema.api import (
    ChatChoice,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    DeltaContent,
    ReferenceItem,
    ResponseMessage,
    StreamChoice,
    UsageInfo,
)
from app.schema.db import KnowledgeBase, LLMConfig
from app.storage.database import async_session
from app.storage.milvus import MilvusClient

from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_llm_for_request(model_config_id: str | None) -> LLMProvider:
    """根据 model_config_id 获取 LLM 实例

    优先级：指定 ID > 数据库中的默认配置 > 系统全局配置
    """
    if model_config_id:
        async with async_session() as session:
            result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == model_config_id)
            )
            config = result.scalar_one_or_none()
            if config:
                return _create_llm_from_config(config)

    # 尝试使用数据库中标记为默认的配置
    async with async_session() as session:
        result = await session.execute(
            select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            return _create_llm_from_config(config)

    # 回退到系统全局配置
    return get_model_manager().llm


def _create_llm_from_config(config: LLMConfig) -> LLMProvider:
    """根据数据库配置创建 LLM 实例"""
    if config.provider == "ollama":
        return OllamaLLM(base_url=config.base_url, model=config.model)
    else:
        return VllmLLM(base_url=config.base_url, model=config.model, api_key=config.api_key or "")

# RAG 系统提示词模板
_SYSTEM_PROMPT = """你是一个知识库问答助手。请根据以下检索到的参考内容回答用户问题。
如果参考内容中没有相关信息，请如实告知用户。不要编造信息。

参考内容：
{context}"""


def _estimate_tokens(text: str) -> int:
    """简单的 token 数量估算（基于字符数）

    中文约 1.5 字符/token，英文约 4 字符/token，取平均约 2 字符/token
    """
    return max(1, len(text) // 2)


def _build_context(chunks: list[RetrievalResult]) -> str:
    """将检索结果拼接为上下文文本"""
    if not chunks:
        return "（未找到相关内容）"
    parts = []
    for i, chunk in enumerate(chunks, 1):
        parts.append(f"[{i}] {chunk.content}")
    return "\n\n".join(parts)


async def _build_references(chunks: list[RetrievalResult]) -> list[ReferenceItem]:
    """将检索结果转换为引用来源列表，包含文件名"""
    if not chunks:
        return []

    # 批量查询所有涉及的文档文件名
    doc_ids = list(set(chunk.doc_id for chunk in chunks))
    doc_filenames: dict[str, str] = {}
    async with async_session() as session:
        from app.schema.db import Document
        result = await session.execute(
            select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
        )
        for row in result:
            doc_filenames[row.id] = row.filename

    return [
        ReferenceItem(
            doc_id=chunk.doc_id,
            chunk_id=chunk.chunk_id,
            filename=doc_filenames.get(chunk.doc_id, ""),
            content=chunk.content[:500],
            child_content=chunk.child_content[:500] if chunk.child_content else chunk.content[:500],
            score=round(chunk.score, 4),
        )
        for chunk in chunks
    ]


def _get_milvus_client() -> MilvusClient:
    """获取 Milvus 客户端实例"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


async def _get_retrieval_mode(kb_id: str, request_mode: str | None) -> str:
    """确定检索模式：请求指定 > 知识库配置 > 默认 hybrid"""
    if request_mode:
        return request_mode

    # 从数据库查询知识库配置
    async with async_session() as session:
        stmt = select(KnowledgeBase.retrieval_mode).where(KnowledgeBase.id == kb_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row:
            return row

    return "hybrid"


async def _retrieve_chunks(
    query: str, kb_id: str, mode: str, llm: LLMProvider, progress_queue: asyncio.Queue | None = None
) -> tuple[list[RetrievalResult], bool]:
    """根据模式执行检索，返回 (检索结果, 是否降级)

    三档模式：
    - direct: 仅稠密向量检索
    - hybrid: 混合检索 + RRF + Rerank
    - agent: 完整 Agent 编排（路由→改写→迭代检索→反思）

    Args:
        progress_queue: 可选的异步队列，用于推送 Agent 进度事件
    """
    manager = get_model_manager()
    milvus = _get_milvus_client()
    settings = get_settings()

    if mode == "direct":
        # 直检索：仅稠密向量
        retriever = VectorRetriever(manager.embedder, milvus)
        results = await retriever.search(query, kb_id, top_k=30)
        return results, False

    elif mode == "agent":
        # Agent 模式：完整编排，使用对话选择的 LLM
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

        # 构建进度回调：将事件放入队列
        async def on_progress(step: str, detail: str):
            if progress_queue:
                await progress_queue.put({"type": "agent_progress", "step": step, "detail": detail})

        agent_result: AgentResult = await orchestrator.run(query, kb_id, on_progress=on_progress)
        return agent_result.chunks, agent_result.degraded

    else:
        # hybrid 模式（默认）：混合检索 + RRF + Rerank
        vector_retriever = VectorRetriever(manager.embedder, milvus)
        sparse_retriever = SparseRetriever(manager.embedder, milvus)
        hybrid_retriever = HybridRetriever(
            vector_retriever=vector_retriever,
            sparse_retriever=sparse_retriever,
            rerank_provider=manager.reranker,
            db_session_factory=async_session,
        )
        results = await hybrid_retriever.search(query, kb_id, top_k=30)
        return results, False


def _build_messages(
    request: ChatCompletionRequest, context: str, has_context: bool
) -> list[dict]:
    """构建发送给 LLM 的消息列表（有检索结果时注入上下文）"""
    user_messages = [
        {"role": msg.role, "content": msg.content} for msg in request.messages
    ]
    if has_context:
        system_msg = {"role": "system", "content": _SYSTEM_PROMPT.format(context=context)}
        return [system_msg] + user_messages
    return user_messages


async def _stream_response(
    request: ChatCompletionRequest,
    query: str,
    kb_id: str | None,
    mode: str,
    llm: LLMProvider,
) -> AsyncGenerator[str, None]:
    """生成 SSE 流式响应，包含 Agent 进度事件"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Agent 模式：边检索边推送进度
    chunks: list[RetrievalResult] = []
    degraded = False

    if kb_id and mode == "agent":
        # 使用队列实现进度推送
        progress_queue: asyncio.Queue = asyncio.Queue()

        # 启动检索任务
        retrieve_task = asyncio.create_task(
            _retrieve_chunks(query, kb_id, mode, llm, progress_queue=progress_queue)
        )

        # 持续读取进度事件并推送给前端
        while not retrieve_task.done():
            try:
                event = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
                yield json.dumps(event, ensure_ascii=False)
            except asyncio.TimeoutError:
                continue

        # 获取检索结果
        chunks, degraded = retrieve_task.result()

        # 排空队列中剩余的事件
        while not progress_queue.empty():
            event = progress_queue.get_nowait()
            yield json.dumps(event, ensure_ascii=False)

    elif kb_id:
        # 非 agent 模式：直接检索，无进度事件
        try:
            chunks, degraded = await _retrieve_chunks(query, kb_id, mode, llm)
        except Exception as e:
            logger.error("检索失败: %s", e)
            chunks = []

    # 构建上下文和消息
    context = _build_context(chunks)
    has_context = len(chunks) > 0
    messages = _build_messages(request, context, has_context)
    llm_degraded = False

    # 发送第一个 chunk（包含 role）
    first_chunk = ChatCompletionChunk(
        id=completion_id,
        choices=[StreamChoice(delta=DeltaContent(role="assistant"))],
    )
    yield json.dumps(first_chunk.model_dump(), ensure_ascii=False)

    # 流式生成内容，LLM 异常时降级为返回检索上下文
    try:
        async for token in llm.stream(messages):
            chunk_data = ChatCompletionChunk(
                id=completion_id,
                choices=[StreamChoice(delta=DeltaContent(content=token))],
            )
            yield json.dumps(chunk_data.model_dump(), ensure_ascii=False)
    except Exception as e:
        logger.warning("LLM 流式生成失败，降级为纯检索结果: %s", e)
        llm_degraded = True
        # 降级：直接输出检索上下文
        fallback_chunk = ChatCompletionChunk(
            id=completion_id,
            choices=[StreamChoice(delta=DeltaContent(content=context))],
        )
        yield json.dumps(fallback_chunk.model_dump(), ensure_ascii=False)

    # 发送结束标记
    final_chunk = ChatCompletionChunk(
        id=completion_id,
        choices=[StreamChoice(delta=DeltaContent(), finish_reason="stop")],
    )
    yield json.dumps(final_chunk.model_dump(), ensure_ascii=False)

    # 发送引用来源和元数据（作为额外事件）
    references = await _build_references(chunks)
    meta_event = {
        "references": [ref.model_dump() for ref in references],
        "metadata": {
            "retrieval_mode": mode,
            "degraded": degraded or llm_degraded,
            "llm_degraded": llm_degraded,
        },
    }
    yield json.dumps(meta_event, ensure_ascii=False)


@router.post("/v1/chat/completions")
@router.post("/api/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """Chat Completion 端点（OpenAI 兼容）

    支持流式和非流式两种响应模式，集成三档检索模式调度。
    """
    # 提取用户最后一条消息作为查询
    user_query = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            user_query = msg.content
            break

    if not user_query:
        raise HTTPException(status_code=400, detail="消息列表中缺少 user 角色消息")

    # 确定检索模式
    mode = await _get_retrieval_mode(request.knowledge_base_id, request.retrieval_mode)

    # 获取 LLM 实例（根据 model_config_id 动态选择）
    llm = await _get_llm_for_request(request.model_config_id)

    # 执行检索（未指定知识库时跳过检索）
    chunks: list[RetrievalResult] = []
    degraded = False

    # 流式响应（检索和生成一体化，支持进度推送）
    if request.stream:
        return EventSourceResponse(
            _stream_response(request, user_query, request.knowledge_base_id, mode, llm),
            media_type="text/event-stream",
        )

    # 非流式响应
    if request.knowledge_base_id:
        try:
            chunks, degraded = await _retrieve_chunks(user_query, request.knowledge_base_id, mode, llm)
        except Exception as e:
            logger.error("检索失败: %s", e)
            raise HTTPException(status_code=500, detail=f"检索失败: {e}")
    context = _build_context(chunks)
    has_context = len(chunks) > 0
    messages = _build_messages(request, context, has_context)

    # 尝试 LLM 生成，失败时降级为纯检索结果
    llm_degraded = False
    try:
        answer = await llm.generate(messages)
    except Exception as e:
        logger.warning("LLM 生成失败，降级为纯检索结果: %s", e)
        answer = _build_context(chunks)
        llm_degraded = True

    # 计算 token 使用量
    prompt_text = " ".join(m["content"] for m in messages)
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(answer)

    # 构建响应
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    response = ChatCompletionResponse(
        id=completion_id,
        choices=[
            ChatChoice(
                message=ResponseMessage(content=answer),
            )
        ],
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        references=await _build_references(chunks),
        metadata={
            "retrieval_mode": mode,
            "degraded": degraded or llm_degraded,
            "llm_degraded": llm_degraded,
        },
    )

    return response
