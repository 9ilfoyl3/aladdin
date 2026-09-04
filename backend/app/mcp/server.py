"""MCP 方法分发（inbound）：initialize / tools/list / tools/call / ping。

本模块是纯粹的协议分发层：**不碰 HTTP**（由 ``app/mcp_server.py`` 的路由负责）、
**不做鉴权**（身份由路由层解析后传入），只把 JSON-RPC 消息映射到
:mod:`app.mcp.tools` 的能力实现，并按 MCP 规范组织结果。

协议版本
--------
支持 ``2025-06-18``（最新）/ ``2025-03-26`` / ``2024-11-05``。initialize 时若客户端
声明的版本在支持列表内则回声该版本，否则回最新版本 —— 由客户端决定是否继续
（规范要求的降级协商方式）。

会话
----
initialize 时生成 ``Mcp-Session-Id`` 并在响应头返回；后续请求带上该头即可复用。
会话只承载"客户端已完成握手"这一事实与空闲过期，**不承载身份**：身份每次请求都由
API Key 重新解析，避免会话变成一个绕过鉴权的长期令牌。

失败语义
--------
- 协议级问题（方法不存在、参数非法、限流）-> JSON-RPC ``error``。
- 工具自身失败 -> ``result.isError = true`` + 文本原因。这是 MCP 的明确要求：模型需要
  看到失败原因并改变策略，而不是拿到一个传输层错误。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.mcp.context import CallerContext, from_headers, from_meta
from app.mcp.jsonrpc import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RATE_LIMITED,
    JsonRpcError,
    JsonRpcRequest,
)

logger = logging.getLogger(__name__)

# 支持的协议版本，从新到旧
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (LATEST_PROTOCOL_VERSION, "2025-03-26", "2024-11-05")

SERVER_INFO = {"name": "artoo-knowledge", "title": "Artoo 知识库", "version": "1.0.0"}

# 会话头（MCP 规范定义的名字，大小写不敏感由 HTTP 层保证）
HEADER_SESSION = "Mcp-Session-Id"
HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"


# ============================================================
# 会话表（进程内）
# ============================================================


class _SessionStore:
    """MCP 会话表：session_id -> 最后活跃时间戳。

    进程内即可满足需求：会话不承载身份也不承载状态，多进程部署时客户端命中另一个
    进程只会被判为"未知会话"，按规范返回 404 后客户端重新 initialize 即恢复。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        self._evict_expired()
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = time.monotonic()
        return session_id

    def touch(self, session_id: str) -> bool:
        self._evict_expired()
        if session_id not in self._sessions:
            return False
        self._sessions[session_id] = time.monotonic()
        return True

    def terminate(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def _evict_expired(self) -> None:
        ttl = get_settings().mcp_session_ttl_seconds
        if ttl <= 0:
            return
        now = time.monotonic()
        for sid, last in list(self._sessions.items()):
            if now - last > ttl:
                del self._sessions[sid]


session_store = _SessionStore()


# ============================================================
# 限流（进程内令牌桶，按 API Key 维度）
# ============================================================


class _RateLimiter:
    """每把 Key 每分钟 N 次的滑动窗口限流。

    进程内计数：多 worker 部署时实际上限为 N × worker 数。这是有意的取舍——限流在此
    是"防单个集成把检索链路打爆"的粗粒度护栏，不是计费口径，不值得为它引入 Redis
    往返；需要精确全局配额时应在网关层做。
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        limit = get_settings().mcp_rate_limit_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        window = [t for t in self._hits.get(key, []) if now - t < 60.0]
        if len(window) >= limit:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        return True


rate_limiter = _RateLimiter()


# ============================================================
# 方法分发
# ============================================================


async def handle_message(
    message: JsonRpcRequest,
    identity: Any,
    headers: dict[str, str] | None = None,
) -> Any:
    """处理一条 JSON-RPC 请求，返回 result 载荷（通知返回 None）。

    Args:
        message: 已解析的请求 / 通知。
        identity: 路由层解析出的 ``IdentityContext``，工具据此收敛可读范围。
        headers: 原始请求头，用于读取上游透传的调用方上下文（仅审计用途）。

    Raises:
        JsonRpcError: 协议级错误，由路由层转为 error 响应。
    """
    method = message.method

    if method == "initialize":
        return _initialize(message.params)

    # 通知：按规范不返回响应体
    if method.startswith("notifications/"):
        logger.debug("[MCP] 收到通知: %s", method)
        return None

    if method == "ping":
        return {}

    if method == "tools/list":
        return {"tools": mcp_tools.MCP_TOOLS}

    if method == "tools/call":
        return await _call_tool(message.params, identity, headers or {})

    raise JsonRpcError(METHOD_NOT_FOUND, f"不支持的方法: {method}")


def _initialize(params: dict) -> dict:
    """握手：协商协议版本并声明服务端能力。"""
    requested = params.get("protocolVersion")
    version = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    )
    if requested and requested not in SUPPORTED_PROTOCOL_VERSIONS:
        logger.info("[MCP] 客户端协议版本 %s 不受支持，回应 %s", requested, version)
    return {
        "protocolVersion": version,
        # 只声明真正实现的能力。listChanged=False：工具集是静态常量，不会运行时变更，
        # 谎报会让客户端白等一个永不到来的通知。
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "Artoo 知识库 MCP server。检索范围由所用 API Key 的授权范围决定："
            "代理 Key 需同时携带 X-External-User-Id，不同外部用户之间互相隔离。"
        ),
    }


async def _call_tool(params: dict, identity: Any, headers: dict[str, str]) -> dict:
    """执行工具调用。"""
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise JsonRpcError(INVALID_PARAMS, "缺少工具名 params.name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise JsonRpcError(INVALID_PARAMS, "params.arguments 必须是 JSON 对象")

    canonical = mcp_tools.resolve_tool_name(name)
    if canonical is None:
        raise JsonRpcError(INVALID_PARAMS, f"未知工具: {name}")

    # 限流按 API Key 维度（无 api_key_id 时退化到 tenant，两者都无则统一桶）
    bucket = (
        getattr(identity, "api_key_id", None)
        or getattr(identity, "tenant_id", None)
        or "anonymous"
    )
    if not rate_limiter.allow(bucket):
        raise JsonRpcError(
            RATE_LIMITED,
            f"调用过于频繁，已超出每分钟 {get_settings().mcp_rate_limit_per_minute} 次限制",
        )

    # 上游透传的调用方上下文：仅记日志，**不参与授权**（授权只认 API Key）。
    # 不带签名的上下文是不可验证的自称，采信它做授权等于自废鉴权。
    caller = _extract_caller_context(params, headers)
    logger.info(
        "[MCP] tools/call name=%s api_key=%s upstream_caller=%s",
        canonical,
        bucket,
        caller.to_dict() or "-",
    )

    try:
        text = await mcp_tools.execute_tool(canonical, arguments, identity)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except JsonRpcError:
        raise
    except Exception as e:
        # 工具失败按 MCP 规范走 isError，让模型能看到并改变策略。
        # 对外只给稳定原因，堆栈进日志（避免内部实现细节泄露给调用方）。
        detail = _safe_reason(e)
        logger.exception("[MCP] 工具执行失败 name=%s", canonical)
        return {
            "content": [{"type": "text", "text": f"工具 {canonical} 执行失败: {detail}"}],
            "isError": True,
        }


def _extract_caller_context(params: dict, headers: dict[str, str]) -> CallerContext:
    """从 ``params._meta`` 或 HTTP header 读出上游调用方上下文（_meta 优先）。"""
    ctx, _ = from_meta(params.get("_meta"))
    if not ctx.is_empty:
        return ctx
    ctx, _ = from_headers(headers)
    return ctx


def _safe_reason(exc: Exception) -> str:
    """把异常转为对外安全的原因文本。

    已知的业务异常（AppError 家族带 detail）如实回传，其余一律归为通用失败，
    不把 ``str(e)`` 直接外泄。
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return "服务端处理该请求时出错，请稍后重试或联系管理员"
