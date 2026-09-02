"""Artoo 经 MCP 暴露的工具定义与实现（inbound 能力层）。

从 ``app/mcp_server.py`` 迁入并加固，路由/协议层不再夹带业务逻辑。四个工具：

- ``knowledge_search``：语义检索（多查询并行）
- ``hybrid_search``：向量 + BM25 混合检索
- ``list_documents``：列出文档
- ``knowledge_qa``：基于知识库的**单轮**问答（旧名 ``chat``，见下）

三条不变量
----------
1. **范围收敛**：任何工具都不接受"搜全部库"这种隐式宽授权。指定 kb 必过
   ``authorize_requested_kbs(READ)``；不指定则收敛到当前身份可读集合，且受
   ``mcp_max_kb_fanout`` 上限保护。
2. **扇出有界且并发**：逐库 × 逐查询串行会把一次调用放大成几十次检索并拖到客户端
   超时。这里统一 ``asyncio.gather`` 并发 + 库数上限。
3. **不外泄内部细节**：错误对外只给稳定的中文原因，异常堆栈进服务端日志。

``chat`` -> ``knowledge_qa`` 的更名原因：旧 ``chat`` 声明了 ``session_id`` 参数但实现
从未使用，也不走 Agent 链路（无工具、无预设、无多轮），与 ``/api/chat`` 长期分叉，
等于对外承诺了不存在的能力。这里让名字与描述如实反映"单轮问答"，并移除
``session_id``。需要真正多轮请走 ``/api/chat``（见开放接口文档）。旧名 ``chat`` 作为
别名保留在 :data:`TOOL_ALIASES`，老客户端不会断。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 单个检索片段在工具输出里的最大字符数（控制上下文占用）
_SNIPPET_CHARS = 500

# 旧工具名 -> 新工具名。老客户端按旧名调用仍可用，tools/list 只声明新名。
TOOL_ALIASES: dict[str, str] = {"chat": "knowledge_qa"}

# 工具定义（JSON Schema，符合 MCP 协议 tools/list 的 Tool 结构）
MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "knowledge_search",
        "description": (
            "语义检索知识库内容。使用向量相似度搜索，适合自然语言查询。"
            "不指定 knowledge_base_id 时，检索范围为当前凭据可读的知识库。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "搜索查询列表，支持 1-5 个查询并行检索",
                    "maxItems": 5,
                },
                "knowledge_base_id": {
                    "type": "string",
                    "description": "知识库 ID（可选，不指定则检索当前凭据可读的知识库）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量",
                    "default": 5,
                },
            },
            "required": ["queries"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "hybrid_search",
        "description": "混合检索（向量 + BM25 关键词），适合需要精确匹配和语义理解的查询。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询文本"},
                "knowledge_base_id": {"type": "string", "description": "知识库 ID（可选）"},
                "top_k": {"type": "integer", "description": "返回结果数量", "default": 5},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_documents",
        "description": "列出知识库中的文档列表，包含文档名称、状态和基本信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "knowledge_base_id": {
                    "type": "string",
                    "description": "知识库 ID（可选，不指定则列出当前凭据可读知识库的文档）",
                },
                "page": {"type": "integer", "description": "页码", "default": 1},
                "page_size": {"type": "integer", "description": "每页数量", "default": 20},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "knowledge_qa",
        "description": (
            "基于知识库内容的单轮问答：检索相关片段后由大模型作答。"
            "不维护会话历史；需要多轮对话请使用 Artoo 的 /api/chat 接口。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题"},
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "知识库 ID 列表（可选，当前仅取第一个）",
                },
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]

TOOL_NAMES = frozenset(t["name"] for t in MCP_TOOLS)


def resolve_tool_name(name: str) -> str | None:
    """把（可能是旧别名的）工具名解析为当前实现名；未知工具返回 None。"""
    canonical = TOOL_ALIASES.get(name, name)
    return canonical if canonical in TOOL_NAMES else None


# ============================================================
# 范围收敛
# ============================================================


async def resolve_kb_ids(identity, requested_kb_id: str | None) -> tuple[list[str], bool]:
    """把工具参数里的 kb 收敛为身份可读范围。

    - 指定 ``kb_id``：经 ``authorize_requested_kbs(READ)`` 校验，越权直接抛（上层转错误）。
    - 不指定：取身份可读集合，排序后截断到 ``mcp_max_kb_fanout``（替代"搜索所有知识库"
      这个既慢又危险的默认）。

    Returns:
        ``(kb_ids, truncated)``。``truncated`` 为 True 时说明可读库数超过上限、本次
        只覆盖了一部分，输出里会据此提示调用方显式指定 ``knowledge_base_id``。
    """
    from app.auth.kb_authz import KbAccessEnum
    from app.auth.kb_scope import assemble_allowed_kb_ids, authorize_requested_kbs
    from app.storage.database import async_session

    async with async_session() as session:
        if requested_kb_id:
            await authorize_requested_kbs(session, identity, [requested_kb_id], KbAccessEnum.READ)
            return [requested_kb_id], False
        allowed = sorted(await assemble_allowed_kb_ids(session, identity))

    limit = get_settings().mcp_max_kb_fanout
    if limit > 0 and len(allowed) > limit:
        return allowed[:limit], True
    return allowed, False


# ============================================================
# 检索执行（并发 + 单点失败容忍）
# ============================================================


async def _search_concurrent(
    retriever, queries: list[str], kb_ids: list[str], top_k: int
) -> list[dict]:
    """(query × kb) 全组合并发检索，返回合并后的原始结果。

    单个组合失败只记 WARNING 并跳过：一个库索引异常不应让整次调用失败（与 Agent
    检索链路的降级取向一致）。
    """

    async def _one(query: str, kb_id: str) -> list[dict]:
        try:
            return await retriever.search(query=query, knowledge_base_id=kb_id, top_k=top_k)
        except Exception as e:  # noqa: BLE001 — 单库失败降级跳过
            logger.warning("[MCP] 检索失败 kb=%s: %s", kb_id, e)
            return []

    tasks = [_one(q, kb) for q in queries for kb in kb_ids]
    if not tasks:
        return []
    results = await asyncio.gather(*tasks)
    return [item for group in results for item in group]


def _dedup_top_k(results: list[dict], top_k: int) -> list[dict]:
    """按 chunk_id 去重（保留最高分）后取前 top_k。"""
    seen: dict[str, dict] = {}
    for r in results:
        chunk_id = r.get("chunk_id", r.get("id", ""))
        score = r.get("score", 0)
        if chunk_id not in seen or score > seen[chunk_id].get("score", 0):
            seen[chunk_id] = r
    return sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)[:top_k]


def _format_results(results: list[dict], truncated: bool) -> str:
    if not results:
        return "No results found."
    lines = [f"Found {len(results)} results:"]
    for i, r in enumerate(results, 1):
        content = r.get("content", r.get("text", ""))[:_SNIPPET_CHARS]
        score = r.get("score", 0)
        doc_name = r.get("document_name", r.get("doc_name", "unknown"))
        lines.append(f"\n[{i}] (score: {score:.3f}) [{doc_name}]\n{content}")
    if truncated:
        lines.append(
            f"\n[提示] 可读知识库数量超过单次检索上限（{get_settings().mcp_max_kb_fanout}），"
            "本次仅覆盖其中一部分。如需精确检索请显式指定 knowledge_base_id。"
        )
    return "\n".join(lines)


# ============================================================
# 工具实现
# ============================================================


async def execute_tool(name: str, arguments: dict, identity) -> str:
    """按名称执行工具，返回给模型看的文本。

    调用方（:mod:`app.mcp.server`）负责把返回值包成 MCP ``content`` 并处理异常。
    """
    canonical = resolve_tool_name(name)
    if canonical == "knowledge_search":
        return await _knowledge_search(arguments, identity)
    if canonical == "hybrid_search":
        return await _hybrid_search(arguments, identity)
    if canonical == "list_documents":
        return await _list_documents(arguments, identity)
    if canonical == "knowledge_qa":
        return await _knowledge_qa(arguments, identity)
    raise KeyError(name)


async def _knowledge_search(arguments: dict, identity) -> str:
    from app.retrieval.hybrid import HybridRetriever

    queries = [q for q in (arguments.get("queries") or []) if isinstance(q, str) and q.strip()]
    if not queries:
        return "Error: 'queries' parameter is required and must be non-empty"
    top_k = int(arguments.get("top_k", 5) or 5)

    kb_ids, truncated = await resolve_kb_ids(identity, arguments.get("knowledge_base_id"))
    if not kb_ids:
        return "No results found."

    raw = await _search_concurrent(HybridRetriever(), queries[:5], kb_ids, top_k)
    return _format_results(_dedup_top_k(raw, top_k), truncated)


async def _hybrid_search(arguments: dict, identity) -> str:
    from app.retrieval.hybrid import HybridRetriever

    query = (arguments.get("query") or "").strip()
    if not query:
        return "Error: 'query' parameter is required"
    top_k = int(arguments.get("top_k", 5) or 5)

    kb_ids, truncated = await resolve_kb_ids(identity, arguments.get("knowledge_base_id"))
    if not kb_ids:
        return "No results found."

    raw = await _search_concurrent(HybridRetriever(), [query], kb_ids, top_k)
    return _format_results(_dedup_top_k(raw, top_k), truncated)


async def _list_documents(arguments: dict, identity) -> str:
    from sqlalchemy import select

    from app.schema.db import Document
    from app.storage.database import async_session

    page = max(1, int(arguments.get("page", 1) or 1))
    page_size = max(1, min(100, int(arguments.get("page_size", 20) or 20)))

    kb_ids, truncated = await resolve_kb_ids(identity, arguments.get("knowledge_base_id"))
    if not kb_ids:
        return "No documents found."

    async with async_session() as session:
        stmt = (
            select(Document)
            .where(Document.kb_id.in_(kb_ids))
            .order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        docs = (await session.execute(stmt)).scalars().all()

    if not docs:
        return "No documents found."

    lines = [f"Documents (page {page}, {len(docs)} items):"]
    for doc in docs:
        status = getattr(doc, "status", "unknown")
        name = getattr(doc, "filename", getattr(doc, "name", "unknown"))
        lines.append(f"  - [{status}] {name} (id: {getattr(doc, 'id', '')})")
    if truncated:
        lines.append(
            "[提示] 可读知识库数量超过上限，本次仅列出其中一部分，请显式指定 knowledge_base_id。"
        )
    return "\n".join(lines)


async def _knowledge_qa(arguments: dict, identity) -> str:
    """单轮知识库问答：检索 + LLM 作答。LLM 不可用时降级返回检索原文。"""
    from app.retrieval.hybrid import HybridRetriever

    query = (arguments.get("query") or "").strip()
    if not query:
        return "Error: 'query' parameter is required"

    requested = (arguments.get("knowledge_base_ids") or [None])[0]
    kb_ids, _ = await resolve_kb_ids(identity, requested)
    if not kb_ids:
        return "未找到相关知识库内容来回答此问题。"

    raw = await _search_concurrent(HybridRetriever(), [query], kb_ids, 5)
    results = _dedup_top_k(raw, 5)
    if not results:
        return "未找到相关知识库内容来回答此问题。"

    context = "\n\n---\n\n".join(
        c for c in (r.get("content", r.get("text", "")) for r in results) if c
    )

    try:
        from app.models.manager import ModelManager

        llm = await ModelManager().get_active_llm()
        return await llm.generate([
            {
                "role": "system",
                "content": (
                    "你是一个知识库问答助手。根据提供的参考资料回答用户问题。"
                    "如果参考资料中没有相关信息，请如实说明。"
                ),
            },
            {"role": "user", "content": f"参考资料：\n{context}\n\n问题：{query}"},
        ])
    except Exception as e:  # noqa: BLE001 — LLM 不可用时退回检索原文，仍有可用信息
        logger.warning("[MCP] knowledge_qa 的 LLM 调用失败，返回检索原文: %s", e)
        return f"检索到 {len(results)} 条相关内容（模型暂不可用，返回原始片段）：\n\n{context[:2000]}"
