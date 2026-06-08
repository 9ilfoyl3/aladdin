"""MCP Server - 将 Artoo 知识库能力暴露为 MCP 协议

提供 /mcp/tools/list 和 /mcp/tools/call 端点，
让外部 AI 工具（Claude、Cursor 等）可以直接调用 Artoo 的知识库能力。

暴露的工具：
- knowledge_search: 语义检索
- hybrid_search: 混合检索
- list_documents: 列出文档
- chat: 完整对话
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP"])

# MCP 工具定义（JSON Schema 格式，符合 MCP 协议规范）
MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "knowledge_search",
        "description": "语义检索知识库内容。使用向量相似度搜索，适合自然语言查询。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "搜索查询列表，支持 1-5 个查询并行检索",
                },
                "knowledge_base_id": {
                    "type": "string",
                    "description": "知识库 ID（可选，不指定则搜索所有知识库）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量",
                    "default": 5,
                },
            },
            "required": ["queries"],
        },
    },
    {
        "name": "hybrid_search",
        "description": "混合检索（向量 + BM25 关键词），适合需要精确匹配和语义理解的查询。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询文本",
                },
                "knowledge_base_id": {
                    "type": "string",
                    "description": "知识库 ID（可选）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_documents",
        "description": "列出知识库中的文档列表，包含文档名称、状态和基本信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "knowledge_base_id": {
                    "type": "string",
                    "description": "知识库 ID（可选，不指定则列出所有文档）",
                },
                "page": {
                    "type": "integer",
                    "description": "页码",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页数量",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "chat",
        "description": "与知识库进行对话，获取基于知识库内容的 AI 回答。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户问题",
                },
                "session_id": {
                    "type": "string",
                    "description": "会话 ID（可选，不指定则创建新会话）",
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "知识库 ID 列表（可选）",
                },
            },
            "required": ["query"],
        },
    },
]


@router.get("/tools/list")
async def list_tools() -> JSONResponse:
    """列出所有可用的 MCP 工具定义

    返回符合 MCP 协议的工具列表，包含名称、描述和参数 JSON Schema。
    """
    return JSONResponse(content={"tools": MCP_TOOLS})


@router.post("/tools/call")
async def call_tool(request: Request) -> JSONResponse:
    """执行指定的 MCP 工具

    请求体格式:
    {
        "name": "tool_name",
        "arguments": { ... }
    }

    返回格式:
    {
        "content": [{"type": "text", "text": "..."}],
        "isError": false
    }
    """
    try:
        body = await request.json()
        tool_name = body.get("name", "")
        arguments = body.get("arguments", {})

        # 验证工具名称
        valid_names = {t["name"] for t in MCP_TOOLS}
        if tool_name not in valid_names:
            return JSONResponse(
                content={
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
                status_code=400,
            )

        # tenant-auth：MCP 工具经 API Key 鉴权解析身份（无有效 Key -> 401）。
        from app.api.errors import AppError
        try:
            identity = await _authenticate_mcp(request)
        except AppError as ae:
            return JSONResponse(
                content={"content": [{"type": "text", "text": ae.detail}], "isError": True},
                status_code=ae.http_status,
            )

        # 执行工具（身份透传，范围收敛到该身份可读 KB）
        result = await _execute_mcp_tool(tool_name, arguments, identity)
        return JSONResponse(content=result)

    except json.JSONDecodeError:
        return JSONResponse(
            content={
                "content": [{"type": "text", "text": "Invalid JSON in request body"}],
                "isError": True,
            },
            status_code=400,
        )
    except Exception as e:
        logger.exception("MCP tool call failed: %s", e)
        return JSONResponse(
            content={
                "content": [{"type": "text", "text": f"Internal error: {str(e)}"}],
                "isError": True,
            },
            status_code=500,
        )


@router.get("/sse")
async def sse_endpoint(request: Request):
    """SSE 端点 - 提供 MCP 协议的 Server-Sent Events 通信

    客户端通过此端点建立 SSE 连接，接收服务端推送的消息。
    MCP 协议使用 SSE 作为传输层之一，支持工具调用请求和结果响应。
    """

    async def event_generator():
        """生成 SSE 事件流"""
        # 发送初始连接确认
        yield {
            "event": "endpoint",
            "data": json.dumps({
                "uri": "/mcp/tools/call",
                "transport": "sse",
            }),
        }

        # 保持连接活跃，定期发送心跳
        try:
            while True:
                if await request.is_disconnected():
                    break
                yield {"event": "ping", "data": ""}
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


async def _execute_mcp_tool(name: str, arguments: dict, identity) -> dict:
    """执行 MCP 工具的内部逻辑（identity 用于租户范围收敛与授权）。"""
    try:
        if name == "knowledge_search":
            result = await _tool_knowledge_search(arguments, identity)
        elif name == "hybrid_search":
            result = await _tool_hybrid_search(arguments, identity)
        elif name == "list_documents":
            result = await _tool_list_documents(arguments, identity)
        elif name == "chat":
            result = await _tool_chat(arguments, identity)
        else:
            return {
                "content": [{"type": "text", "text": f"Tool not implemented: {name}"}],
                "isError": True,
            }

        return {
            "content": [{"type": "text", "text": result}],
            "isError": False,
        }
    except Exception as e:
        logger.exception("MCP tool execution error [%s]: %s", name, e)
        return {
            "content": [{"type": "text", "text": f"Error executing {name}: {str(e)}"}],
            "isError": True,
        }


async def _authenticate_mcp(request: Request):
    """从 Authorization 头解析 API Key 身份；无有效凭据抛 AppError(401/...)。"""
    from app.api.errors import UnauthenticatedError
    from app.auth.apikey_auth import ApiKeyAuthenticator
    from app.storage.database import async_session

    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise UnauthenticatedError("缺少 Authorization 凭据")
    token = auth[7:].strip()
    if not token.startswith("sk-"):
        raise UnauthenticatedError("MCP 仅支持 API Key 调用")
    async with async_session() as session:
        return await ApiKeyAuthenticator(session).authenticate(token, request.headers)


async def _resolve_mcp_kb_ids(identity, requested_kb_id: str | None) -> list[str]:
    """把 MCP 的 kb 参数收敛为身份可读范围：
    - 指定 kb_id：经 kb_authorization_decision(READ) 校验（越权抛 -> 上层转错误）。
    - 不指定：返回身份可读 KB 集合（替换原"搜索所有知识库"的危险默认）。
    """
    from app.auth.kb_authz import KbAccessEnum
    from app.auth.kb_scope import assemble_allowed_kb_ids, authorize_requested_kbs
    from app.storage.database import async_session

    async with async_session() as session:
        if requested_kb_id:
            await authorize_requested_kbs(session, identity, [requested_kb_id], KbAccessEnum.READ)
            return [requested_kb_id]
        return list(await assemble_allowed_kb_ids(session, identity))


async def _tool_knowledge_search(arguments: dict, identity) -> str:
    """knowledge_search 工具实现 - 语义检索（范围收敛到身份可读 KB）"""
    from app.retrieval.hybrid import HybridRetriever

    queries = arguments.get("queries", [])
    kb_id = arguments.get("knowledge_base_id")
    top_k = arguments.get("top_k", 5)

    if not queries:
        return "Error: 'queries' parameter is required and must be non-empty"

    # 范围收敛：指定 kb 经读授权；不指定则取身份可读范围（替换"搜索所有库"危险默认）
    allowed_kb_ids = await _resolve_mcp_kb_ids(identity, kb_id)
    if not allowed_kb_ids:
        return "No results found."

    retriever = HybridRetriever()
    all_results = []
    for query in queries[:5]:  # 最多 5 个查询
        for target_kb in allowed_kb_ids:
            results = await retriever.search(
                query=query,
                knowledge_base_id=target_kb,
                top_k=top_k,
            )
            all_results.extend(results)

    # chunk_id 去重，保留最高分
    seen = {}
    for r in all_results:
        chunk_id = r.get("chunk_id", r.get("id", ""))
        score = r.get("score", 0)
        if chunk_id not in seen or score > seen[chunk_id].get("score", 0):
            seen[chunk_id] = r

    unique_results = sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)[:top_k]

    # 格式化输出
    if not unique_results:
        return "No results found."

    output_lines = [f"Found {len(unique_results)} results:"]
    for i, r in enumerate(unique_results, 1):
        content = r.get("content", r.get("text", ""))[:500]
        score = r.get("score", 0)
        doc_name = r.get("document_name", r.get("doc_name", "unknown"))
        output_lines.append(f"\n[{i}] (score: {score:.3f}) [{doc_name}]\n{content}")

    return "\n".join(output_lines)


async def _tool_hybrid_search(arguments: dict, identity) -> str:
    """hybrid_search 工具实现 - 混合检索（范围收敛到身份可读 KB）"""
    from app.retrieval.hybrid import HybridRetriever

    query = arguments.get("query", "")
    kb_id = arguments.get("knowledge_base_id")
    top_k = arguments.get("top_k", 5)

    if not query:
        return "Error: 'query' parameter is required"

    allowed_kb_ids = await _resolve_mcp_kb_ids(identity, kb_id)
    if not allowed_kb_ids:
        return "No results found."

    retriever = HybridRetriever()
    results = []
    for target_kb in allowed_kb_ids:
        r = await retriever.search(query=query, knowledge_base_id=target_kb, top_k=top_k)
        results.extend(r)

    if not results:
        return "No results found."

    results = sorted(results, key=lambda x: x.get("score", 0), reverse=True)[:top_k]
    output_lines = [f"Found {len(results)} results:"]
    for i, r in enumerate(results, 1):
        content = r.get("content", r.get("text", ""))[:500]
        score = r.get("score", 0)
        doc_name = r.get("document_name", r.get("doc_name", "unknown"))
        output_lines.append(f"\n[{i}] (score: {score:.3f}) [{doc_name}]\n{content}")

    return "\n".join(output_lines)


async def _tool_list_documents(arguments: dict, identity) -> str:
    """list_documents 工具实现 - 列出文档（仅身份可读 KB 范围内）"""
    from sqlalchemy import select

    from app.schema.db import Document
    from app.storage.database import async_session

    kb_id = arguments.get("knowledge_base_id")
    page = arguments.get("page", 1)
    page_size = arguments.get("page_size", 20)

    allowed_kb_ids = await _resolve_mcp_kb_ids(identity, kb_id)
    if not allowed_kb_ids:
        return "No documents found."

    async with async_session() as session:
        # 修正原 knowledge_base_id 笔误为 kb_id；范围限定在可读 KB
        stmt = (
            select(Document)
            .where(Document.kb_id.in_(allowed_kb_ids))
            .order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await session.execute(stmt)
        docs = result.scalars().all()

    if not docs:
        return "No documents found."

    output_lines = [f"Documents (page {page}, {len(docs)} items):"]
    for doc in docs:
        status = getattr(doc, "status", "unknown")
        name = getattr(doc, "filename", getattr(doc, "name", "unknown"))
        doc_id = getattr(doc, "id", "")
        output_lines.append(f"  - [{status}] {name} (id: {doc_id})")

    return "\n".join(output_lines)


async def _tool_chat(arguments: dict, identity) -> str:
    """chat 工具实现 - 知识库对话（范围收敛到身份可读 KB）"""
    query = arguments.get("query", "")
    if not query:
        return "Error: 'query' parameter is required"

    from app.retrieval.hybrid import HybridRetriever

    kb_ids = arguments.get("knowledge_base_ids", [])
    requested = kb_ids[0] if kb_ids else None
    allowed_kb_ids = await _resolve_mcp_kb_ids(identity, requested)
    if not allowed_kb_ids:
        return "未找到相关知识库内容来回答此问题。"

    retriever = HybridRetriever()
    results = []
    for target_kb in allowed_kb_ids:
        r = await retriever.search(query=query, knowledge_base_id=target_kb, top_k=5)
        results.extend(r)

    if not results:
        return "未找到相关知识库内容来回答此问题。"

    # 构建上下文
    context_parts = []
    for r in results:
        content = r.get("content", r.get("text", ""))
        if content:
            context_parts.append(content)

    context = "\n\n---\n\n".join(context_parts)

    # 调用 LLM 生成答案
    try:
        from app.models.manager import ModelManager

        manager = ModelManager()
        llm = await manager.get_active_llm()

        messages = [
            {
                "role": "system",
                "content": "你是一个知识库问答助手。根据提供的参考资料回答用户问题。如果参考资料中没有相关信息，请如实说明。",
            },
            {
                "role": "user",
                "content": f"参考资料：\n{context}\n\n问题：{query}",
            },
        ]

        answer = await llm.generate(messages)
        return answer
    except Exception as e:
        # LLM 不可用时，返回检索结果摘要
        logger.warning("LLM unavailable for MCP chat, returning raw results: %s", e)
        return f"检索到 {len(results)} 条相关内容（LLM 不可用，返回原始结果）：\n\n{context[:2000]}"
