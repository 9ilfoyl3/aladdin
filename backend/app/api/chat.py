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
from app.retrieval.filter import RetrievalFilter
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.multi_kb import KBRetrievalConfig, MultiKBRetriever, MultiKBSearchResult
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
from app.schema.db import AgentNodeConfig, KnowledgeBase, LLMConfig
from app.storage.database import async_session
from app.storage.milvus import MilvusClient

from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_llm_for_request(model_config_id: str | None) -> tuple[LLMProvider, bool, int | None, bool]:
    """根据 model_config_id 获取 LLM 实例和配置

    优先级：指定 ID > 数据库中的默认配置 > 系统全局配置

    Returns:
        (LLM 实例, 是否启用流式, 最大上下文 token 数, 是否启用 thinking)
    """
    if model_config_id:
        async with async_session() as session:
            result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == model_config_id)
            )
            config = result.scalar_one_or_none()
            if config:
                return _create_llm_from_config(config), config.stream_enabled, config.max_context_tokens, config.thinking_enabled

    # 尝试使用数据库中标记为默认的配置
    async with async_session() as session:
        result = await session.execute(
            select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            return _create_llm_from_config(config), config.stream_enabled, config.max_context_tokens, config.thinking_enabled

    # 回退到系统全局配置
    settings = get_settings()
    if settings.llm_provider == "vllm":
        return VllmLLM(base_url=settings.llm_base_url, model=settings.llm_model, api_key=settings.llm_api_key), True, None, False
    return OllamaLLM(base_url=settings.llm_base_url, model=settings.llm_model), True, None, False


def _create_llm_from_config(config: LLMConfig) -> LLMProvider:
    """根据数据库配置创建 LLM 实例"""
    if config.provider == "ollama":
        return OllamaLLM(base_url=config.base_url, model=config.model)
    else:
        return VllmLLM(base_url=config.base_url, model=config.model, api_key=config.api_key or "")


async def _get_node_llm(node_name: str, fallback_llm: LLMProvider) -> LLMProvider:
    """获取指定 Agent 节点的独立 LLM 实例

    从 AgentNodeConfig 表查询节点配置，若配置有效则创建对应 LLM 实例；
    未配置或创建失败时返回 fallback_llm。

    Args:
        node_name: 节点名称（router / rewriter / reflector）
        fallback_llm: 回退使用的对话 LLM

    Returns:
        节点专属 LLM 或 fallback LLM
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(AgentNodeConfig).where(AgentNodeConfig.node_name == node_name)
            )
            node_config = result.scalar_one_or_none()
            if not node_config or not node_config.model_config_id:
                return fallback_llm

            llm_result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == node_config.model_config_id)
            )
            llm_config = llm_result.scalar_one_or_none()
            if not llm_config:
                return fallback_llm

            return _create_llm_from_config(llm_config)
    except Exception as e:
        logger.warning("加载节点 [%s] 独立模型失败，使用对话模型: %s", node_name, e)
        return fallback_llm


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


def _build_context(chunks: list[RetrievalResult], max_tokens: int | None = None) -> str:
    """将检索结果拼接为上下文文本，按 chunk 粒度控制总长度

    Args:
        chunks: 检索结果列表（已按相关性排序）
        max_tokens: 上下文最大 token 数，None 表示不限制
    """
    if not chunks:
        return "（未找到相关内容）"
    parts = []
    total_chars = 0
    # 按 2 字符/token 估算
    max_chars = max_tokens * 2 if max_tokens else None
    for i, chunk in enumerate(chunks, 1):
        entry = f"[{i}] {chunk.content}"
        if max_chars and total_chars + len(entry) > max_chars:
            break
        parts.append(entry)
        total_chars += len(entry)
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

    refs = []
    for chunk in chunks:
        child = chunk.child_content[:500] if chunk.child_content else ""
        parent = chunk.content[:1500] if chunk.content else ""

        # 如果截断后 child 和 parent 相同（子块就是父块本身），清空 child 避免前端误判
        if child and parent and child == parent:
            child = ""

        refs.append(
            ReferenceItem(
                doc_id=chunk.doc_id,
                chunk_id=chunk.chunk_id,
                filename=doc_filenames.get(chunk.doc_id, ""),
                content=parent,
                child_content=child,
                score=round(chunk.score, 4),
            )
        )
    return refs


def _get_milvus_client() -> MilvusClient:
    """获取 Milvus 客户端实例"""
    settings = get_settings()
    return MilvusClient(host=settings.milvus_host, port=settings.milvus_port)


async def _get_retrieval_mode(kb_id: str, request_mode: str | None) -> str:
    """确定检索模式：请求指定 > 默认 agent"""
    if request_mode:
        return request_mode
    return "agent"


async def _retrieve_multi_kb(
    query: str,
    kb_ids: list[str],
    filter_obj: RetrievalFilter | None = None,
) -> tuple[list[RetrievalResult], bool]:
    """多知识库联合检索

    第一个 kb_id 为主库 (priority=1.0)，其余为辅助库 (priority=0.8)。
    返回 (检索结果, 是否降级)。
    """
    # 构建知识库配置
    kb_configs = []
    for i, kb_id in enumerate(kb_ids):
        priority = 1.0 if i == 0 else 0.8
        kb_configs.append(KBRetrievalConfig(kb_id=kb_id, priority=priority))

    # 构建 HybridRetriever
    manager = get_model_manager()
    milvus = _get_milvus_client()
    vector_retriever = VectorRetriever(manager.embedder, milvus)
    sparse_retriever = SparseRetriever(manager.embedder, milvus)
    hybrid_retriever = HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
    )

    # 使用 MultiKBRetriever 执行联合检索
    multi_kb = MultiKBRetriever(hybrid_retriever)
    multi_result: MultiKBSearchResult = await multi_kb.search(query, kb_configs, top_k=30, filters=filter_obj)
    return multi_result.results, multi_result.degraded


async def _retrieve_chunks(
    query: str, kb_id: str, mode: str, llm: LLMProvider, progress_queue: asyncio.Queue | None = None,
    expr: str | None = None,
) -> tuple[list[RetrievalResult], bool]:
    """根据模式执行检索，返回 (检索结果, 是否降级)

    三档模式：
    - direct: 仅稠密向量检索
    - hybrid: 混合检索 + RRF + Rerank
    - agent: 完整 Agent 编排（路由→改写→迭代检索→反思）

    Args:
        progress_queue: 可选的异步队列，用于推送 Agent 进度事件
    """
    from app.retrieval.cache import get_retrieval_cache

    # 检查缓存（agent 模式不缓存，因为有迭代反思逻辑）
    cache = await get_retrieval_cache()
    if cache and mode != "agent":
        cached = await cache.get(kb_id, query, mode)
        if cached is not None:
            print(f"[Cache] 命中: query={query!r}, mode={mode}, 结果数={len(cached)}")
            return cached, False

    manager = get_model_manager()
    milvus = _get_milvus_client()
    settings = get_settings()

    if mode == "direct":
        # 直检索：仅稠密向量
        retriever = VectorRetriever(manager.embedder, milvus)
        results = await retriever.search(query, kb_id, top_k=30, expr=expr)
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, False

    elif mode == "agent":
        # Agent 模式：完整编排，各节点加载独立 LLM
        router_llm = await _get_node_llm("router", llm)
        rewriter_llm = await _get_node_llm("rewriter", llm)
        reflector_llm = await _get_node_llm("reflector", llm)

        vector_retriever = VectorRetriever(manager.embedder, milvus)
        sparse_retriever = SparseRetriever(manager.embedder, milvus)
        hybrid_retriever = HybridRetriever(
            vector_retriever=vector_retriever,
            sparse_retriever=sparse_retriever,
            rerank_provider=manager.reranker,
            db_session_factory=async_session,
        )
        orchestrator = AgentOrchestrator(
            router=QueryRouter(router_llm),
            rewriter=QueryRewriter(rewriter_llm),
            executor=RetrievalExecutor(hybrid_retriever, embedder=manager.embedder),
            reflector=Reflector(reflector_llm),
            retriever=hybrid_retriever,
            max_iterations=settings.agent_max_iterations,
            timeout=settings.agent_timeout,
        )

        # 构建进度回调：将事件放入队列
        async def on_progress(step: str, detail: str):
            if progress_queue:
                await progress_queue.put({"type": "agent_progress", "step": step, "detail": detail})

        agent_result: AgentResult = await orchestrator.run(query, kb_id, on_progress=on_progress, expr=expr)
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
        results = await hybrid_retriever.search(query, kb_id, top_k=30, expr=expr)
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
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
    stream_enabled: bool = True,
    max_context_tokens: int | None = None,
    thinking_enabled: bool = False,
    expr: str | None = None,
    kb_ids: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """生成 SSE 流式响应，包含 Agent 进度事件"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Agent 模式：边检索边推送进度
    chunks: list[RetrievalResult] = []
    degraded = False

    if kb_ids:
        # 多知识库联合检索
        try:
            filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
            chunks, degraded = await _retrieve_multi_kb(query, kb_ids, filter_obj)
        except Exception as e:
            logger.error("多知识库联合检索失败: %s", e)
            chunks = []

    elif kb_id and mode == "agent":
        # 使用队列实现进度推送
        progress_queue: asyncio.Queue = asyncio.Queue()

        # 启动检索任务
        retrieve_task = asyncio.create_task(
            _retrieve_chunks(query, kb_id, mode, llm, progress_queue=progress_queue, expr=expr)
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
            chunks, degraded = await _retrieve_chunks(query, kb_id, mode, llm, expr=expr)
        except Exception as e:
            logger.error("检索失败: %s", e)
            chunks = []

    # 构建上下文和消息
    context = _build_context(chunks, max_tokens=max_context_tokens)
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
    llm_kwargs = {}
    if thinking_enabled:
        llm_kwargs["enable_thinking"] = True
    else:
        llm_kwargs["enable_thinking"] = False
    try:
        if stream_enabled:
            async for token in llm.stream(messages, **llm_kwargs):
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamChoice(delta=DeltaContent(content=token))],
                )
                yield json.dumps(chunk_data.model_dump(), ensure_ascii=False)
        else:
            # 非流式生成：一次性获取完整回复，然后分段推送
            result = await llm.generate(messages, **llm_kwargs)
            chunk_size = 4
            for i in range(0, len(result), chunk_size):
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamChoice(delta=DeltaContent(content=result[i:i + chunk_size]))],
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
    llm, stream_enabled, max_context_tokens, thinking_enabled = await _get_llm_for_request(request.model_config_id)

    print(f"[Chat] query={user_query!r}, kb={request.knowledge_base_id}, mode={mode}, model_config={request.model_config_id}, stream={request.stream}")

    # 构造过滤条件
    filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
    expr = filter_obj.to_milvus_expr()

    # 判断是否使用多知识库联合检索
    use_multi_kb = bool(request.kb_ids)

    # 执行检索（未指定知识库时跳过检索）
    chunks: list[RetrievalResult] = []
    degraded = False

    # 流式响应（检索和生成一体化，支持进度推送）
    if request.stream:
        return EventSourceResponse(
            _stream_response(request, user_query, request.knowledge_base_id, mode, llm, stream_enabled, max_context_tokens, thinking_enabled, expr=expr, kb_ids=request.kb_ids if use_multi_kb else None),
            media_type="text/event-stream",
        )

    # 非流式响应
    if use_multi_kb:
        # 多知识库联合检索
        try:
            chunks, degraded = await _retrieve_multi_kb(user_query, request.kb_ids, filter_obj)
        except Exception as e:
            logger.error("多知识库联合检索失败: %s", e)
            raise HTTPException(status_code=500, detail=f"多知识库联合检索失败: {e}")
    elif request.knowledge_base_id:
        try:
            chunks, degraded = await _retrieve_chunks(user_query, request.knowledge_base_id, mode, llm, expr=expr)
        except Exception as e:
            logger.error("检索失败: %s", e)
            raise HTTPException(status_code=500, detail=f"检索失败: {e}")
    context = _build_context(chunks, max_tokens=max_context_tokens)
    has_context = len(chunks) > 0
    messages = _build_messages(request, context, has_context)

    # 尝试 LLM 生成，失败时降级为纯检索结果
    llm_degraded = False
    llm_kwargs = {}
    if thinking_enabled:
        llm_kwargs["enable_thinking"] = True
    else:
        llm_kwargs["enable_thinking"] = False
    try:
        answer = await llm.generate(messages, **llm_kwargs)
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
