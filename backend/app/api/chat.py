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

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.final_answer import FinalAnswerTool
from app.agent.tools.grep_chunks import GrepChunksTool
from app.agent.tools.knowledge_search import KnowledgeSearchTool
from app.agent.tools.list_chunks import ListKnowledgeChunksTool
from app.agent.tools.thinking import ThinkingTool
from app.agent.tools.web_search import WebSearchTool
from app.agent.tools.registry import ToolRegistry
from app.agent.prompts.progressive_rag import render_system_prompt
from app.config import get_settings
from app.models.manager import get_model_manager
from app.models.provider import LLMProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM
from app.retrieval.base import RetrievalResult
from app.retrieval.bm25 import BM25Retriever
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
from app.schema.db import LLMConfig, ChatSession, ChatMessageRecord
from app.storage.database import async_session
from app.storage.milvus import MilvusClient

from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter()

# 历史上下文最大轮数（每轮 = 1 user + 1 assistant）
MAX_HISTORY_ROUNDS = 10


async def _load_session_history(session_id: str) -> list[dict]:
    """从数据库加载会话历史消息，返回最近 N 轮对话

    只保留最近 MAX_HISTORY_ROUNDS 轮（user+assistant 各一条算一轮），
    避免上下文过长超出 LLM token 限制。

    对于 assistant 消息中包含 agent_steps 的，追加工具使用摘要，
    给 LLM 提供上一轮使用了哪些工具的上下文。
    """
    async with async_session() as session:
        try:
            result = await session.execute(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.session_id == session_id)
                .order_by(ChatMessageRecord.created_at)
            )
            messages = result.scalars().all()
        except Exception as e:
            # agent_steps 列可能不存在（需要数据库迁移），降级为仅查询基础字段
            logger.warning("加载会话历史异常（可能缺少 agent_steps 列），降级查询: %s", e)
            await session.rollback()
            from sqlalchemy import text
            raw_result = await session.execute(
                text("SELECT id, session_id, role, content, created_at FROM chat_messages WHERE session_id = :sid ORDER BY created_at"),
                {"sid": session_id},
            )
            rows = raw_result.fetchall()
            messages = None
            history = [{"role": row.role, "content": row.content} for row in rows]
            max_messages = MAX_HISTORY_ROUNDS * 2
            if len(history) > max_messages:
                history = history[-max_messages:]
            return history

    if not messages:
        return []

    # 转换为 dict 列表，assistant 消息附带工具摘要
    history = []
    for m in messages:
        content = m.content
        if m.role == "assistant" and hasattr(m, 'agent_steps') and m.agent_steps:
            # 从 agent_steps 中提取工具调用摘要
            tool_summary = _summarize_agent_steps(m.agent_steps)
            if tool_summary:
                content = f"{content}\n{tool_summary}"
        history.append({"role": m.role, "content": content})

    # 截取最近 N 轮（2N 条消息）
    max_messages = MAX_HISTORY_ROUNDS * 2
    if len(history) > max_messages:
        history = history[-max_messages:]

    return history


def _summarize_agent_steps(agent_steps: list) -> str:
    """从 agent_steps 中提取工具调用摘要

    格式: [Agent used: tool1(Nms), tool2(Nms)]
    """
    tool_calls = []
    for step in agent_steps:
        if isinstance(step, dict) and step.get("type") == "tool_result":
            tool_name = step.get("tool_name", "")
            duration_ms = step.get("duration_ms", 0)
            if tool_name:
                tool_calls.append(f"{tool_name}({duration_ms}ms)")

    if not tool_calls:
        return ""

    return f"[Agent used: {', '.join(tool_calls)}]"


async def _save_message(session_id: str, role: str, content: str, references: list | None = None, agent_steps: list | None = None) -> None:
    """保存一条消息到会话"""
    msg = ChatMessageRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role=role,
        content=content,
        references=references,
        agent_steps=agent_steps,
    )
    async with async_session() as session:
        session.add(msg)
        await session.commit()


async def _auto_title_session(session_id: str, user_query: str, assistant_answer: str = "") -> None:
    """自动为新会话生成标题

    首次消息时调用 LLM 生成 ≤15 字的简短标题，
    失败时回退到截断用户消息前 30 字符。
    """
    async with async_session() as session:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        chat_session = result.scalar_one_or_none()
        if chat_session and chat_session.title == "新对话":
            # 尝试用 LLM 生成标题
            title = await _generate_title_with_llm(user_query, assistant_answer)
            if not title:
                # 回退到截断
                title = user_query[:30] + ("..." if len(user_query) > 30 else "")
            chat_session.title = title
            await session.commit()


async def _generate_title_with_llm(user_query: str, assistant_answer: str) -> str | None:
    """调用 LLM 生成会话标题

    Returns:
        生成的标题字符串，失败时返回 None
    """
    try:
        llm, _, _, _ = await _get_llm_for_request(None)
        prompt = (
            f"根据以下对话生成一个≤15字的简短标题：\n"
            f"用户：{user_query[:200]}\n"
            f"助手：{assistant_answer[:200]}\n"
            f"标题："
        )
        title = await llm.generate([{"role": "user", "content": prompt}])
        if title and title.strip():
            # 清理引号和多余空白，确保不超过 30 字符
            title = title.strip().strip('"').strip("'").strip("《》")
            return title[:30]
    except Exception as e:
        logger.warning("LLM 生成标题失败: %s", e)

    return None


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



# RAG 系统提示词模板
_SYSTEM_PROMPT = """你是一个知识库问答助手。请根据以下检索到的参考内容回答用户问题。

规则：
1. 基于参考内容回答，不要编造信息
2. 如果参考内容不足以回答问题，如实告知用户
3. 回答应结构清晰，必要时使用编号或分点
4. 引用具体内容时，标注来源编号（如 [1]、[2]）
5. 根据用户意图选择回答方式：
   - 如果用户在寻找/筛选/列举内容（如"有哪些..."、"帮我找..."、"哪些是..."），
     从参考内容中筛选出符合条件的结果并逐一列举，说明每条为什么符合条件
   - 如果用户在问一个具体问题，直接基于参考内容回答该问题
6. 当参考内容包含多个文档/条目时，注意区分不同来源，不要混淆

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
    import re

    if not chunks:
        return "（未找到相关内容）"
    parts = []
    total_chars = 0
    # 按 2 字符/token 估算
    max_chars = max_tokens * 2 if max_tokens else None
    for i, chunk in enumerate(chunks, 1):
        # 去掉 BM25 content 前缀 [filename]
        content = chunk.content
        if content and content.startswith("["):
            content = re.sub(r'^\[[^\]]*\]\s*', '', content)
        entry = f"[{i}] {content}"
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

        # 去掉 BM25 content 前缀 [filename] （入库时为 BM25 检索加的文件名前缀）
        import re
        if child and child.startswith("["):
            child = re.sub(r'^\[[^\]]*\]\s*', '', child)
        if parent and parent.startswith("["):
            parent = re.sub(r'^\[[^\]]*\]\s*', '', parent)

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


def _build_hybrid_retriever() -> HybridRetriever:
    """构建三路混合检索器（Dense + Sparse + BM25）

    BM25 检索器对旧 schema collection 自动降级为空结果，不影响现有功能。
    """
    manager = get_model_manager()
    milvus = _get_milvus_client()
    vector_retriever = VectorRetriever(manager.embedder, milvus)
    sparse_retriever = SparseRetriever(manager.embedder, milvus)
    bm25_retriever = BM25Retriever(milvus)
    return HybridRetriever(
        vector_retriever=vector_retriever,
        sparse_retriever=sparse_retriever,
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
        bm25_retriever=bm25_retriever,
    )


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
    hybrid_retriever = _build_hybrid_retriever()

    # 使用 MultiKBRetriever 执行联合检索
    multi_kb = MultiKBRetriever(hybrid_retriever)
    multi_result: MultiKBSearchResult = await multi_kb.search(query, kb_configs, top_k=10, filters=filter_obj)
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
        results = await retriever.search(query, kb_id, top_k=10, expr=expr)
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, False

    elif mode == "agent":
        # Agent 模式：ReAct 循环引擎
        hybrid_retriever = _build_hybrid_retriever()
        bm25_retriever = BM25Retriever(milvus)

        # 1. 创建 AgentState 和 ToolRegistry
        state = AgentState()
        tool_registry = ToolRegistry()

        # 2. 创建 EventBus
        event_bus = EventBus()

        # 3. 注册工具
        tool_registry.register(KnowledgeSearchTool(hybrid_retriever, kb_id, state))
        tool_registry.register(GrepChunksTool(bm25_retriever, kb_id, state))
        tool_registry.register(ListKnowledgeChunksTool())
        tool_registry.register(FinalAnswerTool(state, event_bus, ""))

        # 4. 创建 AgentConfig（使用 Progressive RAG prompt）
        settings = get_settings()

        # 注册可选工具：web_search（当 searxng_url 配置时启用）
        if settings.searxng_url:
            tool_registry.register(WebSearchTool(searxng_url=settings.searxng_url))

        config = AgentConfig(
            max_iterations=settings.agent_max_iterations,
            web_search_enabled=bool(settings.searxng_url),
            system_prompt=render_system_prompt(
                AgentConfig(),
                kb_names=[kb_id],
                available_tools=tool_registry.list_tools(),
            ),
        )

        # 5. 创建 AgentEngine
        engine = AgentEngine(config, llm, tool_registry, event_bus)

        # 6. 构建进度回调：将事件放入队列
        if progress_queue:
            async def _on_event(event: AgentEvent):
                await progress_queue.put(event)
            event_bus.on(None, _on_event)

        # 7. 执行 Agent
        result_state = await engine.execute("", query)

        # 8. 返回 knowledge_refs 作为 chunks
        return result_state.knowledge_refs, False

    else:
        # hybrid 模式（默认）：混合检索 + RRF + Rerank
        hybrid_retriever = _build_hybrid_retriever()
        results = await hybrid_retriever.search(query, kb_id, top_k=10, expr=expr)
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, False


def _build_messages(
    request: ChatCompletionRequest, context: str, has_context: bool,
    history: list[dict] | None = None,
) -> list[dict]:
    """构建发送给 LLM 的消息列表（有检索结果时注入上下文，支持历史对话）

    Args:
        request: 请求体
        context: 检索到的参考内容
        has_context: 是否有检索结果
        history: 历史对话消息列表 [{"role": "user", "content": "..."}, ...]
    """
    user_messages = [
        {"role": msg.role, "content": msg.content} for msg in request.messages
    ]
    messages = []
    if has_context:
        system_msg = {"role": "system", "content": _SYSTEM_PROMPT.format(context=context)}
        messages.append(system_msg)
    # 插入历史对话（在 system 之后、当前用户消息之前）
    if history:
        messages.extend(history)
    messages.extend(user_messages)
    return messages


def _agent_event_to_sse(event: AgentEvent) -> dict | None:
    """将 AgentEvent 转换为 SSE JSON 格式

    返回格式：
    {"type": "thought", "content": "...", "iteration": 0}
    {"type": "tool_call", "tool_name": "...", "tool_call_id": "...", "iteration": 0}
    {"type": "tool_result", "tool_call_id": "...", "tool_name": "...", "success": true, "duration_ms": 350}
    {"type": "final_answer", "content": "...", "done": true}
    """
    if event.type == EventType.THOUGHT:
        return {
            "type": "thought",
            "content": event.data.get("content", ""),
            "iteration": event.data.get("iteration", 0),
        }
    elif event.type == EventType.TOOL_CALL:
        return {
            "type": "tool_call",
            "tool_name": event.data.get("tool_name", ""),
            "tool_call_id": event.data.get("tool_call_id", ""),
            "iteration": event.data.get("iteration", 0),
        }
    elif event.type == EventType.TOOL_RESULT:
        return {
            "type": "tool_result",
            "tool_call_id": event.data.get("tool_call_id", ""),
            "tool_name": event.data.get("tool_name", ""),
            "success": event.data.get("success", False),
            "duration_ms": event.data.get("duration_ms", 0),
        }
    elif event.type == EventType.FINAL_ANSWER:
        return {
            "type": "final_answer",
            "content": event.data.get("content", ""),
            "done": event.done,
        }
    elif event.type == EventType.ERROR:
        return {
            "type": "error",
            "content": event.data.get("error", ""),
        }
    return None


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
    history: list[dict] | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """生成 SSE 流式响应，包含 Agent 进度事件"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Agent 模式：边检索边推送进度
    chunks: list[RetrievalResult] = []
    degraded = False
    agent_steps_collected: list[dict] = []

    if kb_ids:
        # 多知识库联合检索
        try:
            filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
            chunks, degraded = await _retrieve_multi_kb(query, kb_ids, filter_obj)
        except Exception as e:
            logger.error("多知识库联合检索失败: %s", e)
            chunks = []

    elif kb_id and mode == "agent":
        # Agent 模式：使用 EventBus→SSE 桥接
        # 创建 asyncio.Queue 接收 AgentEvent
        event_queue: asyncio.Queue = asyncio.Queue()

        # 构建 Agent 组件
        manager = get_model_manager()
        milvus = _get_milvus_client()
        hybrid_retriever = _build_hybrid_retriever()
        bm25_retriever = BM25Retriever(milvus)

        # 创建 AgentState 和 ToolRegistry
        state = AgentState()
        tool_registry = ToolRegistry()

        # 创建 EventBus 并注册 handler 将事件放入 queue
        event_bus = EventBus()

        async def _event_to_queue(event: AgentEvent):
            await event_queue.put(event)

        event_bus.on(None, _event_to_queue)

        # 注册工具
        tool_registry.register(KnowledgeSearchTool(hybrid_retriever, kb_id, state))
        tool_registry.register(GrepChunksTool(bm25_retriever, kb_id, state))
        tool_registry.register(ListKnowledgeChunksTool())
        tool_registry.register(FinalAnswerTool(state, event_bus, session_id or ""))

        # 注册可选工具：web_search（当 searxng_url 配置时启用）
        settings = get_settings()
        if settings.searxng_url:
            tool_registry.register(WebSearchTool(searxng_url=settings.searxng_url))

        # 创建 AgentConfig
        config = AgentConfig(
            max_iterations=settings.agent_max_iterations,
            web_search_enabled=bool(settings.searxng_url),
            thinking_enabled=thinking_enabled,
            system_prompt=render_system_prompt(
                AgentConfig(),
                kb_names=[kb_id],
                available_tools=tool_registry.list_tools(),
            ),
        )

        # 创建 AgentEngine
        engine = AgentEngine(config, llm, tool_registry, event_bus)

        # 构建 LLM 上下文（历史对话）
        llm_context = history if history else None

        # 启动 Agent 执行任务
        agent_task = asyncio.create_task(
            engine.execute(session_id or "", query, llm_context=llm_context)
        )

        # 从 event_queue 读取事件并转换为 SSE JSON
        while not agent_task.done():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                sse_data = _agent_event_to_sse(event)
                if sse_data:
                    agent_steps_collected.append(sse_data)
                    yield json.dumps(sse_data, ensure_ascii=False)
            except asyncio.TimeoutError:
                continue

        # 获取最终状态
        result_state: AgentState = agent_task.result()

        # 排空队列中剩余的事件
        while not event_queue.empty():
            event = event_queue.get_nowait()
            sse_data = _agent_event_to_sse(event)
            if sse_data:
                agent_steps_collected.append(sse_data)
                yield json.dumps(sse_data, ensure_ascii=False)

        # 发射 complete 事件
        complete_event = {"type": "complete", "total_steps": len(result_state.steps)}
        yield json.dumps(complete_event, ensure_ascii=False)

        # Agent 模式下 final_answer 就是最终响应，knowledge_refs 是引用
        # 注意：工具持有的 state 对象和引擎内部的 state 是不同的
        # knowledge_refs 被 KnowledgeSearchTool 写入到传给工具的 state 中
        chunks = state.knowledge_refs
        full_response = result_state.final_answer or ""

        # 发送引用来源和元数据
        references = await _build_references(chunks)
        meta_event = {
            "references": [ref.model_dump() for ref in references],
            "metadata": {
                "retrieval_mode": mode,
                "degraded": False,
                "llm_degraded": False,
            },
        }
        yield json.dumps(meta_event, ensure_ascii=False)

        # 保存消息到会话（不阻塞 SSE 关闭）
        if session_id and full_response:
            try:
                await _save_message(session_id, "user", query)
                refs_data = [ref.model_dump() for ref in references] if references else None
                steps_data = agent_steps_collected if agent_steps_collected else None
                await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data)
                # 标题生成放到后台，不阻塞 SSE 关闭
                asyncio.create_task(_auto_title_session(session_id, query, full_response))
            except Exception as e:
                logger.warning("保存会话消息失败: %s", e)

        # Agent 模式到此结束，不走后续的 LLM 生成流程
        return

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
    messages = _build_messages(request, context, has_context, history=history)
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
    full_response = ""
    try:
        if stream_enabled:
            async for token in llm.stream(messages, **llm_kwargs):
                full_response += token
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamChoice(delta=DeltaContent(content=token))],
                )
                yield json.dumps(chunk_data.model_dump(), ensure_ascii=False)
        else:
            # 非流式生成：一次性获取完整回复，然后分段推送
            result = await llm.generate(messages, **llm_kwargs)
            full_response = result
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
        full_response = context
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

    # 保存消息到会话（如果指定了 session_id）
    if session_id and full_response:
        try:
            await _save_message(session_id, "user", query)
            refs_data = [ref.model_dump() for ref in references] if references else None
            steps_data = agent_steps_collected if agent_steps_collected else None
            await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data)
            await _auto_title_session(session_id, query, full_response)
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)


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

    print(f"[Chat] query={user_query!r}, kb={request.knowledge_base_id}, mode={mode}, model_config={request.model_config_id}, stream={request.stream}, session={request.session_id}")

    # 加载会话历史上下文
    history: list[dict] | None = None
    if request.session_id:
        try:
            history = await _load_session_history(request.session_id)
        except Exception as e:
            logger.warning("加载会话历史失败: %s", e)
            history = None

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
            _stream_response(request, user_query, request.knowledge_base_id, mode, llm, stream_enabled, max_context_tokens, thinking_enabled, expr=expr, kb_ids=request.kb_ids if use_multi_kb else None, history=history, session_id=request.session_id),
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
    messages = _build_messages(request, context, has_context, history=history)

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
    references = await _build_references(chunks)
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
        references=references,
        metadata={
            "retrieval_mode": mode,
            "degraded": degraded or llm_degraded,
            "llm_degraded": llm_degraded,
        },
    )

    # 保存消息到会话（如果指定了 session_id）
    if request.session_id and answer:
        try:
            await _save_message(request.session_id, "user", user_query)
            refs_data = [ref.model_dump() for ref in references] if references else None
            await _save_message(request.session_id, "assistant", answer, references=refs_data)
            await _auto_title_session(request.session_id, user_query, answer)
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)

    return response
