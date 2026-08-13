"""JSON-RPC 2.0 信封编解码（MCP 传输层的公共基座）。

MCP 规定所有消息都是 JSON-RPC 2.0：请求（带 id）、通知（无 id）、响应（result 或 error）。
本模块只做信封层，不含任何 MCP 方法语义，inbound（server）与 outbound（client）共用。

错误码分区（遵循 JSON-RPC 2.0 + MCP 约定）：
- ``-32700 / -32600 / -32601 / -32602 / -32603``：标准码。
- ``-32000 ~ -32099``：实现自定义。本项目用 ``-32001`` 表示未认证、``-32029`` 表示限流。

**工具执行失败不用 error**：MCP 明确要求工具自身的失败经 ``result.isError=true``
返回，让模型能看到错误并改变策略；只有协议级问题（方法不存在、参数非法、鉴权失败）
才用 JSON-RPC error。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

# —— 标准错误码 ——
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# —— 实现自定义错误码 ——
UNAUTHENTICATED = -32001   # 缺少 / 无效凭据
PERMISSION_DENIED = -32003  # 凭据有效但无权
RATE_LIMITED = -32029       # 超出调用频率限制


class JsonRpcError(Exception):
    """协议级错误，由分发层捕获后转为 JSON-RPC error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


@dataclass(frozen=True)
class JsonRpcRequest:
    """已解析的 JSON-RPC 请求 / 通知。

    ``id is None`` 表示通知（notification）——按规范不得返回响应体。
    """

    method: str
    params: dict[str, Any]
    id: str | int | None = None

    @property
    def is_notification(self) -> bool:
        return self.id is None


def parse_message(raw: Any) -> JsonRpcRequest:
    """把单条已反序列化的 JSON 对象解析为 JsonRpcRequest。

    只校验信封结构（jsonrpc 版本 / method 存在 / params 类型），方法语义由分发层判断。
    """
    if not isinstance(raw, dict):
        raise JsonRpcError(INVALID_REQUEST, "请求必须是 JSON 对象")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcError(INVALID_REQUEST, "jsonrpc 字段必须为 '2.0'")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "缺少 method 字段")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # MCP 只使用对象形式的 params，位置参数不在协议范围内
        raise JsonRpcError(INVALID_PARAMS, "params 必须是 JSON 对象")
    msg_id = raw.get("id")
    if msg_id is not None and not isinstance(msg_id, (str, int)):
        raise JsonRpcError(INVALID_REQUEST, "id 必须是字符串或数字")
    return JsonRpcRequest(method=method, params=params, id=msg_id)


def success_response(msg_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def error_response(
    msg_id: str | int | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": err}


def build_request(
    method: str, params: dict[str, Any] | None, msg_id: str | int
) -> dict[str, Any]:
    """构造一条 JSON-RPC 请求（outbound 客户端用）。"""
    payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "method": method}
    if params:
        payload["params"] = params
    return payload


def build_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造一条 JSON-RPC 通知（无 id，服务端不得回响应）。"""
    payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params:
        payload["params"] = params
    return payload


def unwrap_result(raw: Any) -> dict[str, Any]:
    """校验 outbound 收到的响应信封并取出 result；error 则抛 JsonRpcError。"""
    if not isinstance(raw, dict):
        raise JsonRpcError(INVALID_REQUEST, "响应不是 JSON 对象")
    if "error" in raw and raw["error"] is not None:
        err = raw["error"] or {}
        code = err.get("code", INTERNAL_ERROR) if isinstance(err, dict) else INTERNAL_ERROR
        message = err.get("message", "远端返回未知错误") if isinstance(err, dict) else str(err)
        raise JsonRpcError(code, message, err.get("data") if isinstance(err, dict) else None)
    result = raw.get("result")
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise JsonRpcError(INTERNAL_ERROR, "响应 result 不是 JSON 对象")
    return result
