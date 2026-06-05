"""Chat API - OpenAI 兼容接口

实现 POST /v1/chat/completions 端点，支持流式和非流式响应，
集成三档检索模式（direct / hybrid / agent）。
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent.config import AgentConfig
from app.api.agent_config import get_effective_preset_config
from app.api.deps import require_authenticated
from app.api.query_understanding import understand_query
from app.auth.identity import IdentityContext
from app.auth.kb_authz import KbAccessEnum
from app.auth.kb_scope import authorize_requested_kbs
from app.agent.engine import AgentEngine
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.final_answer import FinalAnswerTool
from app.agent.tools.grep_chunks import GrepChunksTool
from app.agent.tools.knowledge_search import KnowledgeSearchTool
from app.agent.tools.list_chunks import ListKnowledgeChunksTool
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
from app.retrieval.log_safety import sanitize_for_log
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
from app.session_upload.service import get_session_upload_service
from app.storage.database import async_session
from app.storage.milvus import MilvusClient, SESSION_FILES_KB_ID, build_session_id_expr, get_milvus_client

from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter()

# 历史上下文最大轮数（每轮 = 1 user + 1 assistant）
MAX_HISTORY_ROUNDS = 10


async def _verify_session_owner(session_id: str, identity: IdentityContext) -> None:
    """校验 session_id 归属当前行事主体本人，否则 404（存在性非泄露）。

    会话/消息是个人对话历史。问答端点接收前端传入的 session_id 用于加载历史上下文
    与续写消息；若不校验归属，任何同租户用户都能凭他人 session_id 读取其历史（泄露进
    本次回答上下文）并向其会话注入消息。此处与 session API 的 owner 收敛保持一致。

    - 跨租户由 contextvar 兜底过滤为不可见 -> get 返回 None -> 404。
    - owner 不匹配（含同租户他人、无主历史会话）-> 404。
    - tenant_level 机器身份（acting_subject_id 为 None）不绑定自然人 -> 一律 404。
    """
    from app.api.errors import CrossTenantError

    subject = identity.acting_subject_id
    async with async_session() as session:
        cs = await session.get(ChatSession, session_id)
    if cs is None or subject is None or cs.owner_user_id != subject:
        raise CrossTenantError()


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


async def _save_message(session_id: str, role: str, content: str, references: list | None = None, agent_steps: list | None = None, kb_id: str | None = None, kb_ids: list | None = None, tenant_id: str | None = None) -> None:
    """保存一条消息到会话。

    tenant_id 由调用方在请求处理期间从 IdentityContext 取好后传入（后台任务在响应返回后
    执行，届时请求级 contextvar 已失效，故必须显式透传，不在此重新解析身份）。
    """
    msg = ChatMessageRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role=role,
        content=content,
        references=references,
        agent_steps=agent_steps,
        kb_id=kb_id,
        kb_ids=kb_ids,
        tenant_id=tenant_id,
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
        return _get_cached_llm("vllm", settings.llm_base_url, settings.llm_model, settings.llm_api_key), True, None, False
    return _get_cached_llm("ollama", settings.llm_base_url, settings.llm_model, ""), True, None, False


# 进程内 LLM 实例缓存：复用底层 httpx.AsyncClient 连接池，避免每个请求新建客户端
# 却从不关闭导致的连接/文件描述符泄漏（高并发下会耗尽 FD）。
# httpx.AsyncClient 绑定创建它的事件循环；生产是单循环长驻进程，缓存长期复用即可。
# 测试常为每个用例新建事件循环，故按「当前运行循环」缓存，循环切换时整体重建，
# 避免「Future attached to a different loop」错误。
_llm_cache: dict[tuple, LLMProvider] = {}
_llm_cache_loop: "asyncio.AbstractEventLoop | None" = None


def _get_cached_llm(provider: str, base_url: str, model: str, api_key: str) -> LLMProvider:
    """按 (provider, base_url, model, api_key) 复用 LLM 实例。

    同一配置返回同一实例（复用 httpx 连接池）；配置变更（如改地址/密钥）自然命中新 key
    生成新实例。注意：测试连通性端点（llm_config.py）仍各自新建并 close()，不走此缓存。
    """
    global _llm_cache, _llm_cache_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not _llm_cache_loop:
        # 事件循环切换（主要发生在测试）：丢弃旧缓存，旧实例随旧循环一并回收。
        _llm_cache = {}
        _llm_cache_loop = loop

    key = (provider, base_url, model, api_key)
    inst = _llm_cache.get(key)
    if inst is None:
        if provider == "ollama":
            inst = OllamaLLM(base_url=base_url, model=model)
        else:
            inst = VllmLLM(base_url=base_url, model=model, api_key=api_key)
        _llm_cache[key] = inst
    return inst


def _create_llm_from_config(config: LLMConfig) -> LLMProvider:
    """根据数据库配置创建（或复用）LLM 实例"""
    if config.provider == "ollama":
        return _get_cached_llm("ollama", config.base_url, config.model, "")
    else:
        # provider 字段当前承载基础设施类型（ollama/vllm）。对于 vLLM 兼容端点，
        # 实际模型厂商由 VllmLLM 内部根据 base_url 自动检测，用于 thinking 方言分派。
        return _get_cached_llm("vllm", config.base_url, config.model, config.api_key or "")



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
        max_tokens: 上下文最大 token 数，None 表示使用默认 200K（与 Agent 模式一致）
    """
    import re

    if not chunks:
        return "（未找到相关内容）"
    # 与 Agent 模式保持一致：模型未配置 max_context_tokens 时回退默认 200K
    if not max_tokens:
        max_tokens = AgentConfig.max_context_tokens
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

        # 会话临时文件的 doc_id 是 SessionFile.id（在 session_files 表，不在 documents 表），
        # 上面查不到。对未匹配到的 doc_id 再查 SessionFile 补齐文件名，否则前端会回退显示
        # doc_id 前 8 位（如 "101bed7c"）而非真实文件名。
        missing_ids = [d for d in doc_ids if d not in doc_filenames]
        if missing_ids:
            from app.schema.db import SessionFile
            sf_result = await session.execute(
                select(SessionFile.id, SessionFile.filename).where(SessionFile.id.in_(missing_ids))
            )
            for row in sf_result:
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
    return get_milvus_client()


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


def _build_degraded_metadata(failed_kb_ids: list[str]) -> dict:
    """从失败源列表派生前端可消费的降级元数据（Req 2.x：区分会话源 vs 知识库源）。

    - ``failed_source_count``：失败源总数（向后兼容既有字段）。
    - ``failed_kb_ids``：失败源 ID 列表（含 ``SESSION_FILES_KB_ID`` 时表示会话文件源失败）。
    - ``session_source_failed``：会话文件源是否失败（前端据此提示"会话文件检索失败"）。
    - ``kb_source_failed``：是否有正式知识库源失败（前端据此提示"知识库检索失败"）。

    前端拿到后即可分别渲染"会话文件检索失败"与"知识库检索失败"两类提示（design C7.1）。
    """
    session_failed = SESSION_FILES_KB_ID in failed_kb_ids
    kb_failed = any(kid != SESSION_FILES_KB_ID for kid in failed_kb_ids)
    return {
        "failed_source_count": len(failed_kb_ids),
        "failed_kb_ids": list(failed_kb_ids),
        "session_source_failed": session_failed,
        "kb_source_failed": kb_failed,
    }


async def _retrieve_multi_kb(
    query: str,
    kb_ids: list[str],
    filter_obj: RetrievalFilter | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
) -> tuple[list[RetrievalResult], bool, list[str]]:
    """多知识库联合检索（含可选会话文件源）

    第一个 kb_id 为主库 (priority=1.0)，其余为辅助库 (priority=0.8)。
    若 ``session_id`` 给定且该会话有上传文件，则把会话文件源（kb_id=
    ``SESSION_FILES_KB_ID``、priority=1.0、expr=``session_id == "{sid}"``）作为
    一个额外检索源接入，与主库同权参与合并，最终顺序由统一 rerank 按真实语义相关性
    决定（Req 2.1/2.3/2.4/2.6）。会话源由此自动纳入 bugfix H6 的 ``Semaphore`` 并发限流（占一个并发位，
    总源数 = ``len(kb_ids) + 1``）；失败按 bugfix H3 经 ``failed_kb_ids`` 透传
    （含 ``"session_files"``）供前端区分"会话文件检索失败"与"知识库检索失败"。
    返回 ``(检索结果, 是否降级, 失败知识库 ID 列表)``。

    H3：完整透传降级信息——不再丢弃 ``failed_kb_ids``，供上层填充 SSE meta 的
    ``failed_source_count`` 并据此向用户提示结果不完整。

    Args:
        tenant_id: 显式租户 ID（H5）。透传给 ``MultiKBRetriever.search``，确保流式响应中
            contextvar 已 reset 时仍能取到正确租户检索配置；None 时底层回退 contextvar。
        session_id: 当前会话 ID。非 None 且该会话已上传文件时追加会话源（Req 1.3/1.5/
            1.11）。会话源以 ``session_id == "..."`` 标量 expr 强制会话隔离。
    """
    # 构建知识库配置
    kb_configs: list[KBRetrievalConfig] = []
    for i, kb_id in enumerate(kb_ids):
        priority = 1.0 if i == 0 else 0.8
        kb_configs.append(KBRetrievalConfig(kb_id=kb_id, priority=priority))

    # 追加会话文件源（仅当指定 session_id 且该会话有上传文件，Req 1.3/1.5/2.4）。
    # 用 SESSION_FILES_KB_ID 常量 + session_id 标量 expr 隔离，复用 MultiKBRetriever
    # 的并发限流 + 降级透传链路，不引入新的检索分支（design C7 / C7.1）。
    if session_id:
        try:
            session_upload_service = get_session_upload_service()
            if await session_upload_service.has_files(session_id):
                kb_configs.append(
                    KBRetrievalConfig(
                        kb_id=SESSION_FILES_KB_ID,
                        # 与主库同权（1.0）：会话文件与所选知识库公平竞争，最终顺序交由
                        # 统一 rerank 按真实语义相关性决定。此前用 1.2 加权会让会话文件
                        # chunk 在合并排序时整体抬到 KB 之前，叠加 rerank 候选窗口截断
                        # （top_k*2）与软阈值过滤后，KB 候选被挤出 / 砍掉，导致"选了知识库
                        # 仍只答临时文件"。会话文件"刚上传即可被检索"已保证其可见性，
                        # 无需再人为加权垄断候选池。
                        priority=1.0,
                        expr=build_session_id_expr(session_id),
                    )
                )
        except Exception as e:
            # 会话源探测失败（例如 DB 临时抖动）不应阻塞正式 KB 检索；
            # 退化为"无会话源"继续主流程（Req 9.2 安全降级）。
            logger.warning(
                "探测会话文件源失败，本次检索将不包含会话源: %s",
                sanitize_for_log(e),
            )

    # 构建 HybridRetriever
    hybrid_retriever = _build_hybrid_retriever()

    # 使用 MultiKBRetriever 执行联合检索
    multi_kb = MultiKBRetriever(hybrid_retriever)
    multi_result: MultiKBSearchResult = await multi_kb.search(
        query, kb_configs, top_k=10, filters=filter_obj, tenant_id=tenant_id
    )
    return multi_result.results, multi_result.degraded, multi_result.failed_kb_ids


async def _retrieve_chunks(
    query: str, kb_id: str, mode: str, llm: LLMProvider, progress_queue: asyncio.Queue | None = None,
    expr: str | None = None, tenant_id: str | None = None,
) -> tuple[list[RetrievalResult], bool]:
    """根据模式执行检索，返回 (检索结果, 是否降级)

    三档模式：
    - direct: 仅稠密向量检索
    - hybrid: 混合检索 + RRF + Rerank
    - agent: 完整 Agent 编排（路由→改写→迭代检索→反思）

    Args:
        progress_queue: 兼容保留参数（旧 agent 分支用于推送进度事件）。当前 agent 分支已改走
            统一的 _run_agent_nonstream，不再消费此参数；direct/hybrid 分支本就不使用。
        tenant_id: 显式租户 ID（H5）。透传给 hybrid 检索与 agent 模式的 KnowledgeSearchTool，
            确保流式响应中 contextvar 已 reset 时仍能取到正确租户检索配置；None 时底层回退 contextvar。
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
        # Agent 模式：复用统一的 _run_agent_nonstream（与流式/非流式主链路共用
        # _build_agent_runtime），不再在此维护第三份独立的工具注册/配置副本。
        # 该分支现已不在生产主链路触达（非流式单库 agent 在 chat_completions 中直接走
        # _run_agent_nonstream），保留仅为兼容潜在的内部调用方，返回 (refs, degraded)。
        answer, refs, degraded, _steps = await _run_agent_nonstream(
            query, kb_id, llm, preset_cfg={},
            max_context_tokens=None, thinking_enabled=False,
            tenant_id=tenant_id, session_id=None, history=None,
        )
        return refs, degraded

    else:
        # hybrid 模式（默认）：混合检索 + RRF + Rerank
        hybrid_retriever = _build_hybrid_retriever()
        # H3：用 search_with_degraded 取真实路级降级（经返回结构承载，并发安全），不再硬编码 False。
        results, degraded = await hybrid_retriever.search_with_degraded(
            query, kb_id, top_k=10, expr=expr, tenant_id=tenant_id
        )
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, degraded


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
    elif event.type == EventType.TOKEN_USAGE:
        return {
            "type": "token_usage",
            "prompt_tokens": event.data.get("prompt_tokens", 0),
            "completion_tokens": event.data.get("completion_tokens", 0),
            "total_tokens": event.data.get("total_tokens", 0),
            "max_context_tokens": event.data.get("max_context_tokens", 0),
            "current_context_tokens": event.data.get("current_context_tokens", 0),
        }
    elif event.type == EventType.ERROR:
        return {
            "type": "error",
            "content": event.data.get("error", ""),
        }
    return None


def _build_agent_runtime(
    kb_id: str,
    llm: LLMProvider,
    preset_cfg: dict,
    max_context_tokens: int | None,
    thinking_enabled: bool,
    tenant_id: str | None,
    session_id: str | None,
) -> tuple[AgentEngine, AgentState, EventBus]:
    """构建 Agent 运行时（工具注册 + 配置 + 引擎），流式与非流式共用。

    统一两条链路的 Agent 编排，消除「流式 / 非流式各自重建一套」导致的行为分叉
    （预设 allowed_tools 过滤、thinking、temperature、system_prompt 等过去仅流式生效）。

    Returns:
        (engine, state, event_bus)。其中 ``state`` 是传给工具的 AgentState，
        ``knowledge_refs`` / ``degraded`` 由工具写入此对象；引擎 ``execute()`` 返回的
        是另一个内部 state，``final_answer`` / ``steps`` 在那上面（与既有约定一致）。
    """
    milvus = _get_milvus_client()
    hybrid_retriever = _build_hybrid_retriever()
    bm25_retriever = BM25Retriever(milvus)

    state = AgentState()
    tool_registry = ToolRegistry()
    event_bus = EventBus()

    # 按预设 allowed_tools 过滤；final_answer 始终注册以保证 Agent 能终止
    preset_allowed = preset_cfg.get("allowed_tools")

    def _tool_enabled(tool_name: str) -> bool:
        if tool_name == "final_answer":
            return True
        if not preset_allowed:
            return True
        return tool_name in preset_allowed

    if _tool_enabled("knowledge_search"):
        tool_registry.register(KnowledgeSearchTool(hybrid_retriever, kb_id, state, tenant_id=tenant_id))
    if _tool_enabled("grep_chunks"):
        tool_registry.register(GrepChunksTool(bm25_retriever, kb_id, state))
    if _tool_enabled("list_knowledge_chunks"):
        tool_registry.register(ListKnowledgeChunksTool())
    tool_registry.register(FinalAnswerTool(state, event_bus, session_id or ""))

    # 可选工具：web_search（需配置 searxng_url 且预设允许）
    settings = get_settings()
    web_search_on = bool(settings.searxng_url) and _tool_enabled("web_search")
    if web_search_on:
        tool_registry.register(WebSearchTool(searxng_url=settings.searxng_url))

    # system_prompt：预设自定义优先，否则用默认 Progressive RAG 模板。
    # 两者都经 render_system_prompt 做占位符替换（{knowledge_base_names} / {available_tools}）。
    preset_system_prompt = (preset_cfg.get("system_prompt") or "").strip()
    system_prompt = render_system_prompt(
        AgentConfig(system_prompt=preset_system_prompt),
        kb_names=[kb_id],
        available_tools=tool_registry.list_tools(),
        web_search_enabled=web_search_on,
    )
    config = AgentConfig(
        max_iterations=preset_cfg.get("max_iterations", settings.agent_max_iterations),
        max_context_tokens=max_context_tokens or AgentConfig.max_context_tokens,
        temperature=preset_cfg.get("temperature", AgentConfig.temperature),
        web_search_enabled=web_search_on,
        thinking_enabled=preset_cfg.get("thinking_enabled", thinking_enabled),
        system_prompt=system_prompt,
    )
    engine = AgentEngine(config, llm, tool_registry, event_bus)
    return engine, state, event_bus


async def _run_agent_nonstream(
    query: str,
    kb_id: str,
    llm: LLMProvider,
    preset_cfg: dict,
    max_context_tokens: int | None,
    thinking_enabled: bool,
    tenant_id: str | None,
    session_id: str | None,
    history: list[dict] | None,
) -> tuple[str, list[RetrievalResult], bool, list[dict]]:
    """非流式运行 Agent ReAct 引擎，直接返回其最终答案（不二次走普通 RAG 生成）。

    与流式路径共用 ``_build_agent_runtime``，消除双入口分叉。事件经 handler 收集为
    agent_steps 落库，保证与流式会话历史结构一致（前端可还原思考/工具步骤）。

    Returns:
        (final_answer, knowledge_refs, degraded, agent_steps)
    """
    engine, state, event_bus = _build_agent_runtime(
        kb_id, llm, preset_cfg, max_context_tokens, thinking_enabled, tenant_id, session_id,
    )

    steps_collected: list[dict] = []

    async def _collect(event: AgentEvent):
        sse = _agent_event_to_sse(event)
        if sse:
            steps_collected.append(sse)

    event_bus.on(None, _collect)

    llm_context = history if history else None
    agent_start_time = time.time()
    result_state: AgentState | None = None
    try:
        result_state = await engine.execute(session_id or "", query, llm_context=llm_context)
        answer = result_state.final_answer or ""
        degraded = state.degraded
    except Exception as e:
        # 引擎内部已兜住 LLM 永久错误 / max_iterations；此处仅捕获其余未预期异常，
        # 返回友好降级文案而非让请求 500，保证非流式链路不中断。
        logger.error("非流式 Agent 执行异常: %s", sanitize_for_log(e))
        answer = "抱歉，处理您的请求时发生了错误，请稍后重试。"
        degraded = True

    total_steps = len(result_state.steps) if result_state else 0
    steps_collected.append({
        "type": "complete",
        "total_steps": total_steps,
        "total_duration_ms": int((time.time() - agent_start_time) * 1000),
    })
    return answer, state.knowledge_refs, degraded, steps_collected


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
    preset_cfg: dict | None = None,
    tenant_id: str | None = None,
    retrieval_query: str | None = None,
    skip_retrieval: bool = False,
) -> AsyncGenerator[str, None]:
    """生成 SSE 流式响应，包含 Agent 进度事件

    Args:
        query: 用户原始问题，用于答案生成与消息保存。
        retrieval_query: 经查询理解改写后、用于检索的查询（None 时回退到 query）。
            仅单轮检索链路（direct/hybrid）使用；agent 模式自行处理指代。
        skip_retrieval: 查询理解判定为闲聊/纯历史追问时为 True，跳过检索直接作答。
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    preset_cfg = preset_cfg or {}
    if retrieval_query is None:
        retrieval_query = query

    # Agent 模式：边检索边推送进度
    chunks: list[RetrievalResult] = []
    degraded = False
    failed_kb_ids: list[str] = []  # 哪些源失败（含 SESSION_FILES_KB_ID 时为会话源失败），供前端区分提示
    agent_steps_collected: list[dict] = []

    if kb_ids is not None:
        # 多知识库联合检索（kb_ids=[] 表示仅会话文件源，由 _retrieve_multi_kb
        # 内部按 session_id 追加单源 cfg；用 ``is not None`` 而非 ``if kb_ids:``
        # 才能让"仅会话文件"场景正确进入多源链路）。
        try:
            filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
            if skip_retrieval:
                chunks = []
            else:
                # H3：完整接收 (results, degraded, failed_kb_ids)，不丢弃失败列表。
                chunks, degraded, failed_kb_ids = await _retrieve_multi_kb(
                    retrieval_query, kb_ids, filter_obj, tenant_id=tenant_id,
                    session_id=session_id,
                )
        except Exception as e:
            # H3：except 分支异常导致结果缺失 → degraded=True（不硬编码 False）。
            logger.error("多知识库联合检索失败: %s", sanitize_for_log(e))
            chunks = []
            degraded = True
            # 整体异常无法区分具体失败源；若本次含会话源（kb_ids 为空即仅会话源，
            # 或有 session_id 且该会话有文件），把会话源标记为失败以便前端区分提示。
            # failed_kb_ids 失败源列表由 _build_degraded_metadata 据此派生 failed_source_count。
            failed_kb_ids = list(kb_ids) if kb_ids else [SESSION_FILES_KB_ID]

    elif kb_id and mode == "agent":
        # Agent 模式：使用 EventBus→SSE 桥接
        # 创建 asyncio.Queue 接收 AgentEvent
        event_queue: asyncio.Queue = asyncio.Queue()

        # 构建 Agent 运行时（与非流式共用 _build_agent_runtime，消除双入口分叉）
        engine, state, event_bus = _build_agent_runtime(
            kb_id, llm, preset_cfg, max_context_tokens, thinking_enabled, tenant_id, session_id,
        )

        async def _event_to_queue(event: AgentEvent):
            await event_queue.put(event)

        event_bus.on(None, _event_to_queue)

        # 构建 LLM 上下文（历史对话）
        llm_context = history if history else None

        # 启动 Agent 执行任务
        agent_start_time = time.time()
        agent_task = asyncio.create_task(
            engine.execute(session_id or "", query, llm_context=llm_context)
        )

        # 从 event_queue 读取事件并转换为 SSE JSON。
        # 用 try/finally 保证：无论 agent_task 是否抛异常，都先排空队列里已产生的事件
        # 再发 complete/meta、落库，避免「result() 重抛异常 → 排空被跳过 → SSE 断流、
        # 事件与会话历史丢失」。
        result_state: AgentState | None = None
        agent_error: Exception | None = None
        try:
            while not agent_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    sse_data = _agent_event_to_sse(event)
                    if sse_data:
                        agent_steps_collected.append(sse_data)
                        yield json.dumps(sse_data, ensure_ascii=False)
                except asyncio.TimeoutError:
                    continue

            # 获取最终状态；agent_task 抛异常时在此捕获，转为友好降级而非断流。
            try:
                result_state = agent_task.result()
            except Exception as e:
                agent_error = e
                logger.error("流式 Agent 执行异常: %s", sanitize_for_log(e))
        finally:
            # 排空队列中剩余的事件（异常路径同样执行，不丢已产生的思考/工具事件）
            while not event_queue.empty():
                event = event_queue.get_nowait()
                sse_data = _agent_event_to_sse(event)
                if sse_data:
                    agent_steps_collected.append(sse_data)
                    yield json.dumps(sse_data, ensure_ascii=False)

        # Agent 模式下 final_answer 就是最终响应，knowledge_refs 是引用。
        # 注意：工具持有的 state 对象和引擎内部的 state 是不同的，
        # knowledge_refs 被 KnowledgeSearchTool 写入到传给工具的 state 中。
        chunks = state.knowledge_refs
        # H3：agent 模式 degraded 取工具写入的真实降级状态（不再恒 False）。
        # KnowledgeSearchTool 持有此 state，检索源失败/路级降级时置 state.degraded=True。
        degraded = state.degraded
        full_response = result_state.final_answer if result_state else ""

        if agent_error is not None:
            # 引擎抛出未兜住的异常：补发降级答案 + 标记 degraded，保证前端有正文、链路不中断。
            degraded = True
            if not full_response:
                full_response = "抱歉，处理您的请求时发生了错误，请稍后重试。"
                answer_event = {"type": "final_answer", "content": full_response, "done": False}
                agent_steps_collected.append(answer_event)
                yield json.dumps(answer_event, ensure_ascii=False)
            yield json.dumps({"type": "final_answer", "content": "", "done": True}, ensure_ascii=False)
        full_response = full_response or ""

        # 发射 complete 事件（携带整体耗时，供前端步骤统计展示）
        total_duration_ms = int((time.time() - agent_start_time) * 1000)
        complete_event = {
            "type": "complete",
            "total_steps": len(result_state.steps) if result_state else 0,
            "total_duration_ms": total_duration_ms,
        }
        agent_steps_collected.append(complete_event)
        yield json.dumps(complete_event, ensure_ascii=False)

        # 发送引用来源和元数据
        references = await _build_references(chunks)
        meta_event = {
            "references": [ref.model_dump() for ref in references],
            "metadata": {
                "retrieval_mode": mode,
                "degraded": degraded,
                "llm_degraded": False,
                **_build_degraded_metadata(failed_kb_ids),
            },
        }
        yield json.dumps(meta_event, ensure_ascii=False)

        # 保存消息到会话（不阻塞 SSE 关闭）
        if session_id and full_response:
            try:
                await _save_message(session_id, "user", query, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
                refs_data = [ref.model_dump() for ref in references] if references else None
                steps_data = agent_steps_collected if agent_steps_collected else None
                await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
                # 标题生成放到后台，不阻塞 SSE 关闭
                asyncio.create_task(_auto_title_session(session_id, query, full_response))
            except Exception as e:
                logger.warning("保存会话消息失败: %s", e)

        # Agent 模式到此结束，不走后续的 LLM 生成流程
        return

    elif kb_id:
        # 非 agent 模式：直接检索，无进度事件
        if skip_retrieval:
            # 闲聊/纯历史追问：不检索，直接基于历史让 LLM 作答
            chunks = []
        else:
            try:
                chunks, degraded = await _retrieve_chunks(retrieval_query, kb_id, mode, llm, expr=expr, tenant_id=tenant_id)
            except Exception as e:
                # H3：检索整体异常导致结果为空 → degraded=True（不硬编码 False）。
                logger.error("检索失败: %s", sanitize_for_log(e))
                chunks = []
                degraded = True
                # 单库 direct/hybrid 路径只有正式知识库源（无会话源），失败即知识库源失败。
                failed_kb_ids = [kb_id]

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
            **_build_degraded_metadata(failed_kb_ids),
        },
    }
    yield json.dumps(meta_event, ensure_ascii=False)

    # 保存消息到会话（如果指定了 session_id）
    if session_id and full_response:
        try:
            await _save_message(session_id, "user", query, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
            refs_data = [ref.model_dump() for ref in references] if references else None
            steps_data = agent_steps_collected if agent_steps_collected else None
            await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
            await _auto_title_session(session_id, query, full_response)
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)


@router.post("/v1/chat/completions")
@router.post("/api/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    identity: IdentityContext = Depends(require_authenticated()),
):
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

    tenant_id = identity.tenant_id

    # 会话归属校验：若指定了 session_id，必须为本人会话，否则 404。
    # 防止凭他人 session_id 读取其历史（泄露进本次回答上下文）或向其会话注入消息。
    if request.session_id:
        await _verify_session_owner(request.session_id, identity)

    # 检索范围授权：触达 Milvus 前先校验所有被指定 KB 处于身份可读范围
    # （跨租户/不可读 -> 404；跨库问答 MultiKBRetriever 逻辑不变，仅前置裁剪）。
    requested_kb_ids: list[str] = []
    if request.kb_ids:
        requested_kb_ids = list(request.kb_ids)
    elif request.knowledge_base_id:
        requested_kb_ids = [request.knowledge_base_id]
    if requested_kb_ids:
        async with async_session() as _authz_session:
            await authorize_requested_kbs(_authz_session, identity, requested_kb_ids, KbAccessEnum.READ)

    # 加载生效的 Agent 预设（指定 > 默认 > 内置兜底）。
    # 传入 identity：显式指定的预设须在调用者可见范围内（内置 ∪ 自有 ∪ 本租户已开放），
    # 否则忽略回退默认，防止凭 id 使用他人私有预设。
    preset_cfg = await get_effective_preset_config(request.agent_preset_id, identity)

    # 确定检索模式：显式 retrieval_mode > 预设的 agent_mode > 默认 agent
    # 预设统一承载"快速问答(hybrid 单轮) / 智能推理(agent 多步)"的模式选择
    mode = request.retrieval_mode or preset_cfg.get("agent_mode") or "agent"

    # 获取 LLM 实例（根据 model_config_id 动态选择）
    llm, stream_enabled, max_context_tokens, thinking_enabled = await _get_llm_for_request(request.model_config_id)

    print(f"[Chat] query={user_query!r}, kb={request.knowledge_base_id}, mode={mode}, model_config={request.model_config_id}, preset={request.agent_preset_id}, stream={request.stream}, session={request.session_id}")

    # 加载会话历史上下文
    history: list[dict] | None = None
    if request.session_id:
        try:
            history = await _load_session_history(request.session_id)
        except Exception as e:
            logger.warning("加载会话历史失败: %s", e)
            history = None

    # 判断是否使用多知识库联合检索（包含"仅会话文件"场景，Req 1.4）：
    # - 选了 KB → 走多源链路；
    # - 未选 KB 但当前会话有上传文件 → 走多源链路（会话源单源处理，Req 2.4）；
    # - 否则按既有单库 / 闲聊分支。
    # 探测会话文件存在性失败（DB 抖动等）按"无会话文件"降级，不阻塞主流程（Req 9.2）。
    session_has_files = False
    if request.session_id:
        try:
            session_has_files = await get_session_upload_service().has_files(
                request.session_id
            )
        except Exception as e:
            logger.warning(
                "探测会话文件源失败，本次按无会话文件处理: %s",
                sanitize_for_log(e),
            )
            session_has_files = False
    use_multi_kb = bool(request.kb_ids) or session_has_files

    # 查询理解（仅单轮检索链路 direct/hybrid 需要）：
    # 这类链路无 ReAct 自我修正机会，必须在检索前一次性消解指代、判别闲聊，
    # 否则「它/这个」会直接进检索、闲聊也会触发无关召回。
    # agent 模式由其系统提示词内的 Turn Intent / Context Resolution 自行处理，不在此重复。
    retrieval_query = user_query
    skip_retrieval = False
    if mode != "agent" and (request.knowledge_base_id or use_multi_kb):
        understanding = await understand_query(llm, user_query, history)
        retrieval_query = understanding.rewrite_query
        skip_retrieval = not understanding.needs_retrieval

    # 构造过滤条件
    filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
    expr = filter_obj.to_milvus_expr()

    # 执行检索（未指定知识库时跳过检索）
    chunks: list[RetrievalResult] = []
    degraded = False
    failed_kb_ids: list[str] = []  # 失败源（含 SESSION_FILES_KB_ID 时为会话源），供前端区分提示

    # 流式响应（检索和生成一体化，支持进度推送）
    # use_multi_kb=True 时把"选中的全部库"（单选 knowledge_base_id 或多选 kb_ids，
    # 已由 requested_kb_ids 统一）传入；仅会话文件场景为 []；use_multi_kb=False → None。
    # 注意：此前用 request.kb_ids 会在"单选库 + 会话有临时文件"时丢掉选中的单库
    # （单选走 knowledge_base_id，kb_ids 为空），导致多源检索只查会话文件、漏掉知识库。
    if request.stream:
        stream_kb_ids = list(requested_kb_ids) if use_multi_kb else None
        return EventSourceResponse(
            _stream_response(request, user_query, request.knowledge_base_id, mode, llm, stream_enabled, max_context_tokens, thinking_enabled, expr=expr, kb_ids=stream_kb_ids, history=history, session_id=request.session_id, preset_cfg=preset_cfg, tenant_id=tenant_id, retrieval_query=retrieval_query, skip_retrieval=skip_retrieval),
            media_type="text/event-stream",
        )

    # 非流式响应
    # 单库 Agent 模式：跑 ReAct 引擎并直接采用其最终答案（与流式共用 _build_agent_runtime），
    # 不再丢弃 final_answer 后二次走普通 RAG 生成（既省一次 LLM 调用，也保留 Agent 推理结果）。
    if mode == "agent" and request.knowledge_base_id and not use_multi_kb and not skip_retrieval:
        answer, chunks, degraded, agent_steps = await _run_agent_nonstream(
            user_query, request.knowledge_base_id, llm, preset_cfg,
            max_context_tokens, thinking_enabled, tenant_id, request.session_id, history,
        )
        references = await _build_references(chunks)
        prompt_tokens = _estimate_tokens(user_query)
        completion_tokens = _estimate_tokens(answer)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        response = ChatCompletionResponse(
            id=completion_id,
            choices=[ChatChoice(message=ResponseMessage(content=answer))],
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            references=references,
            metadata={
                "retrieval_mode": mode,
                "degraded": degraded,
                "failed_source_count": 0,
                "llm_degraded": False,
            },
        )
        if request.session_id and answer:
            try:
                await _save_message(request.session_id, "user", user_query, kb_id=request.knowledge_base_id, tenant_id=tenant_id)
                refs_data = [ref.model_dump() for ref in references] if references else None
                steps_data = agent_steps if agent_steps else None
                await _save_message(request.session_id, "assistant", answer, references=refs_data, agent_steps=steps_data, kb_id=request.knowledge_base_id, tenant_id=tenant_id)
                await _auto_title_session(request.session_id, user_query, answer)
            except Exception as e:
                logger.warning("保存会话消息失败: %s", e)
        return response

    if skip_retrieval:
        # 闲聊/纯历史追问：不检索，直接基于历史让 LLM 作答
        chunks = []
    elif use_multi_kb:
        # 多知识库联合检索（含"仅会话文件"场景，kb_ids 可能为空列表）。
        # 用 requested_kb_ids（已统一单选 knowledge_base_id 与多选 kb_ids），避免
        # "单选库 + 会话有临时文件"时丢掉选中的单库、只查会话文件。
        try:
            # H3：完整接收 (results, degraded, failed_kb_ids)；失败源经 _build_degraded_metadata 透传前端。
            chunks, degraded, failed_kb_ids = await _retrieve_multi_kb(
                retrieval_query, list(requested_kb_ids), filter_obj, tenant_id=tenant_id,
                session_id=request.session_id,
            )
        except Exception as e:
            logger.error("多知识库联合检索失败: %s", sanitize_for_log(e))
            raise HTTPException(status_code=500, detail=f"多知识库联合检索失败: {e}")
    elif request.knowledge_base_id:
        try:
            chunks, degraded = await _retrieve_chunks(retrieval_query, request.knowledge_base_id, mode, llm, expr=expr, tenant_id=tenant_id)
        except Exception as e:
            logger.error("检索失败: %s", sanitize_for_log(e))
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
            **_build_degraded_metadata(failed_kb_ids),
        },
    )

    # 保存消息到会话（如果指定了 session_id）
    if request.session_id and answer:
        try:
            msg_kb_ids = request.kb_ids if use_multi_kb else None
            await _save_message(request.session_id, "user", user_query, kb_id=request.knowledge_base_id, kb_ids=msg_kb_ids, tenant_id=tenant_id)
            refs_data = [ref.model_dump() for ref in references] if references else None
            await _save_message(request.session_id, "assistant", answer, references=refs_data, kb_id=request.knowledge_base_id, kb_ids=msg_kb_ids, tenant_id=tenant_id)
            await _auto_title_session(request.session_id, user_query, answer)
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)

    return response
