"""MCP Server 路由层 —— 把 Artoo 知识库能力按**标准 MCP 协议**暴露。

传输：Streamable HTTP（单端点 ``/mcp``，JSON-RPC 2.0）
------------------------------------------------------
- ``POST /mcp``：承载全部客户端 -> 服务端消息（initialize / tools/list / tools/call /
  ping / notifications/*）。请求是单条 JSON-RPC 消息或消息数组（后者兼容 2025-03-26
  的 batching）。只含通知时按规范返回 ``202 Accepted`` 空体。
- ``GET /mcp``：本服务端不主动向客户端推送消息，按规范返回 ``405``。
- ``DELETE /mcp``：显式终止会话。

这样 Claude Desktop / Cursor / 官方 SDK（Python ``mcp``、TypeScript ``@modelcontextprotocol/sdk``）
可以直接接入，第三方不再需要为 Artoo 手写私有协议适配。

鉴权
----
**所有** ``/mcp*`` 端点都要求 ``Authorization: Bearer sk-<API Key>``，包括工具枚举。
改造前 ``/mcp/tools/list`` 可匿名访问（能枚举工具与描述），属信息泄露，此处一并收口。
检索范围由 Key 的授权范围收敛（代理 Key 还需 ``X-External-User-Id``），与其余业务
接口同一套判定。

兼容层（deprecated）
--------------------
``GET /mcp/tools/list`` / ``POST /mcp/tools/call`` / ``GET /mcp/sse`` 是切标准协议前的
私有 REST 形态，保留以免打断已接入方，响应带 ``Deprecation`` 头。新集成一律走
``POST /mcp``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.mcp import server as mcp_dispatch
from app.mcp import tools as mcp_tools
from app.mcp.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAUTHENTICATED,
    JsonRpcError,
    error_response,
    parse_message,
    success_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP"])

# 兼容端点的废弃提示头（RFC 8594 风格）。
# 值必须是 latin-1 可编码的纯 ASCII —— HTTP 头不允许非 latin-1 字符，
# 写中文会在构造响应时抛 UnicodeEncodeError。
_DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Link": '</mcp>; rel="successor-version"',
    "Warning": '299 - "Legacy private REST endpoint is deprecated, use standard MCP POST /mcp"',
}

# 向后兼容：旧代码/测试从本模块导入工具定义
MCP_TOOLS = mcp_tools.MCP_TOOLS


# ============================================================
# 鉴权
# ============================================================


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


# ============================================================
# 标准 MCP：Streamable HTTP
# ============================================================


@router.post("")
async def streamable_http(request: Request) -> Response:
    """MCP Streamable HTTP 端点：处理一条或一批 JSON-RPC 消息。

    响应形态：
    - 含请求（带 id）-> ``200`` + JSON-RPC 响应（单条对应对象，批量对应数组）。
    - 只含通知 -> ``202`` 空体。
    - 鉴权失败 -> ``401`` + JSON-RPC error（HTTP 状态与协议错误同时给出，便于两类
      客户端各取所需）。
    """
    from app.api.errors import AppError

    # 1) 解析 body（先解析再鉴权：解析失败与凭据无关，返回 -32700 更准确）
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content=error_response(None, PARSE_ERROR, "请求体不是合法 JSON"),
        )

    # 2) 鉴权（含工具枚举在内的所有方法）
    try:
        identity = await _authenticate_mcp(request)
    except AppError as ae:
        return JSONResponse(
            status_code=ae.http_status,
            content=error_response(None, UNAUTHENTICATED, ae.detail),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3) 会话校验：带了未知 / 已过期的会话 id -> 404，客户端据此重新 initialize
    session_id = request.headers.get(mcp_dispatch.HEADER_SESSION)
    if session_id and not mcp_dispatch.session_store.touch(session_id):
        return JSONResponse(
            status_code=404,
            content=error_response(None, INVALID_REQUEST, "MCP 会话不存在或已过期，请重新 initialize"),
        )

    batch = raw if isinstance(raw, list) else [raw]
    if not batch:
        return JSONResponse(
            status_code=400, content=error_response(None, INVALID_REQUEST, "消息批次为空")
        )

    headers_map = dict(request.headers)
    responses: list[dict[str, Any]] = []
    new_session_id: str | None = None

    for item in batch:
        try:
            message = parse_message(item)
        except JsonRpcError as e:
            responses.append(error_response(_peek_id(item), e.code, e.message, e.data))
            continue

        try:
            result = await mcp_dispatch.handle_message(message, identity, headers_map)
        except JsonRpcError as e:
            if not message.is_notification:
                responses.append(error_response(message.id, e.code, e.message, e.data))
            continue
        except Exception:
            logger.exception("[MCP] 处理消息失败 method=%s", message.method)
            if not message.is_notification:
                responses.append(
                    error_response(message.id, INTERNAL_ERROR, "服务端内部错误")
                )
            continue

        if message.is_notification:
            continue
        if message.method == "initialize":
            new_session_id = mcp_dispatch.session_store.create()
        responses.append(success_response(message.id, result))

    # 只含通知：规范要求 202 且无响应体
    if not responses:
        return Response(status_code=202)

    out_headers: dict[str, str] = {}
    if new_session_id:
        out_headers[mcp_dispatch.HEADER_SESSION] = new_session_id
    payload: Any = responses if isinstance(raw, list) else responses[0]
    status = _http_status_for(payload)
    return JSONResponse(status_code=status, content=payload, headers=out_headers)


@router.get("")
async def streamable_http_get() -> Response:
    """本服务端不提供服务端 -> 客户端的主动消息流，按规范返回 405。"""
    return JSONResponse(
        status_code=405,
        content=error_response(None, INVALID_REQUEST, "本服务端不支持 SSE 下行流，请用 POST /mcp"),
        headers={"Allow": "POST, DELETE"},
    )


@router.delete("")
async def terminate_session(request: Request) -> Response:
    """显式终止 MCP 会话。"""
    session_id = request.headers.get(mcp_dispatch.HEADER_SESSION)
    if not session_id:
        return JSONResponse(
            status_code=400,
            content=error_response(None, INVALID_REQUEST, f"缺少 {mcp_dispatch.HEADER_SESSION} 头"),
        )
    mcp_dispatch.session_store.terminate(session_id)
    return Response(status_code=204)


def _peek_id(item: Any) -> Any:
    """从未通过校验的原始消息里尽力取出 id，让错误响应可被客户端对上号。"""
    if isinstance(item, dict):
        msg_id = item.get("id")
        if isinstance(msg_id, (str, int)):
            return msg_id
    return None


def _http_status_for(payload: Any) -> int:
    """限流错误额外映射到 HTTP 429，方便网关/客户端按状态码退避。"""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        err = item.get("error") if isinstance(item, dict) else None
        if isinstance(err, dict) and err.get("code") == RATE_LIMITED:
            return 429
    return 200


# ============================================================
# 兼容层（deprecated，切标准协议前的私有 REST 形态）
# ============================================================


@router.get("/tools/list", deprecated=True)
async def list_tools_legacy(request: Request) -> JSONResponse:
    """[已废弃] 私有 REST 工具列表。改用 ``POST /mcp`` 的 ``tools/list``。

    与改造前的差异：**现在要求 API Key**。匿名枚举工具属信息泄露，一并收口。
    """
    from app.api.errors import AppError

    try:
        await _authenticate_mcp(request)
    except AppError as ae:
        return JSONResponse(
            status_code=ae.http_status,
            content={"detail": ae.detail},
            headers={**_DEPRECATION_HEADERS, "WWW-Authenticate": "Bearer"},
        )
    return JSONResponse(content={"tools": mcp_tools.MCP_TOOLS}, headers=_DEPRECATION_HEADERS)


@router.post("/tools/call", deprecated=True)
async def call_tool_legacy(request: Request) -> JSONResponse:
    """[已废弃] 私有 REST 工具调用。改用 ``POST /mcp`` 的 ``tools/call``。

    请求 ``{"name":..., "arguments":{...}}``，响应 ``{"content":[...], "isError":bool}``。
    """
    from app.api.errors import AppError

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={"content": [{"type": "text", "text": "Invalid JSON in request body"}],
                     "isError": True},
            headers=_DEPRECATION_HEADERS,
        )

    tool_name = body.get("name", "") if isinstance(body, dict) else ""
    if mcp_tools.resolve_tool_name(tool_name) is None:
        return JSONResponse(
            status_code=400,
            content={"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                     "isError": True},
            headers=_DEPRECATION_HEADERS,
        )

    try:
        identity = await _authenticate_mcp(request)
    except AppError as ae:
        return JSONResponse(
            status_code=ae.http_status,
            content={"content": [{"type": "text", "text": ae.detail}], "isError": True},
            headers={**_DEPRECATION_HEADERS, "WWW-Authenticate": "Bearer"},
        )

    params = {"name": tool_name, "arguments": body.get("arguments") or {}}
    try:
        result = await mcp_dispatch._call_tool(params, identity, dict(request.headers))
    except JsonRpcError as e:
        status = 429 if e.code == RATE_LIMITED else 400
        return JSONResponse(
            status_code=status,
            content={"content": [{"type": "text", "text": e.message}], "isError": True},
            headers=_DEPRECATION_HEADERS,
        )
    return JSONResponse(content=result, headers=_DEPRECATION_HEADERS)


@router.get("/sse", deprecated=True)
async def sse_endpoint(request: Request):
    """[已废弃] 早期私有 SSE 端点：只回一个 endpoint 事件 + 心跳，不承载消息。

    标准 MCP 的 HTTP+SSE 传输（2024-11-05）与 Streamable HTTP（2025-03-26 起）都不是
    这个形状，保留仅为不打断老接入方；新集成请用 ``POST /mcp``。
    """

    async def event_generator():
        yield {
            "event": "endpoint",
            "data": json.dumps({"uri": "/mcp", "transport": "streamable-http"}),
        }
        try:
            while True:
                if await request.is_disconnected():
                    break
                yield {"event": "ping", "data": ""}
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator(), headers=_DEPRECATION_HEADERS)
