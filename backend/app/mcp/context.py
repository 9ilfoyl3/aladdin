"""MCP 调用方上下文（CallerContext）：定义 + 双通道编解码 + 可验证签名。

**这是通用传输层元数据，不含任何业务语义**——语义上类比 W3C ``traceparent``：
Artoo 只负责把"是谁、在哪个会话里"如实带给远端 MCP server，业务含义完全留在第三方。
一次实现，所有第三方 MCP server 受益。

两个通道（内容等价，同时发送，第三方任选其一读取）：

1. **HTTP header**：``X-Artoo-*`` 系列。适用于 Streamable HTTP 传输，也兼容
   非标准 REST 服务端。
2. **JSON-RPC ``params._meta``**：key 为 ``artoo.dev/caller``。MCP 规范为 ``_meta``
   预留了实现自定义元数据，且它随消息本体走，stdio 等无 header 的传输也能用。

信任模型（**必须在集成文档里同步给第三方**）
--------------------------------------------
- 不带签名时，这些字段是**不可验证的提示（hint）**：任何能连到该 MCP server 的人都能
  伪造。第三方**不得**仅凭它做授权判定，只能用于关联 / 定位 / 审计。
- 配置了共享密钥（MCP 配置里的凭据）时，Artoo 额外发送
  ``X-Artoo-Context-Timestamp`` + ``X-Artoo-Context-Signature``：对规范化上下文串做
  HMAC-SHA256。第三方用同一密钥重算即可确认"这确实是 Artoo 发出的、且未被篡改"，
  此时上下文升级为**可验证断言（assertion）**，可作为授权输入。
- 签名覆盖时间戳，配合时间窗（默认 300 秒）限制重放。

隐私边界：是否透传由**每个 MCP 配置单独开关**（``forward_context``，默认关闭）。
理由：把 A 方终端用户标识发给不相关的第三方 C 属跨方数据泄露，不能全局默认开启。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# _meta 的 key：MCP 规范要求实现自定义元数据使用带前缀的 key（反向域名风格）
META_KEY = "artoo.dev/caller"

# —— HTTP header 名（与 CallerContext 字段一一对应）——
HEADER_SESSION_ID = "X-Artoo-Session-Id"
HEADER_TENANT_ID = "X-Artoo-Tenant-Id"
HEADER_SUBJECT_TYPE = "X-Artoo-Subject-Type"
HEADER_SUBJECT_ID = "X-Artoo-Subject-Id"
HEADER_EXTERNAL_USER_ID = "X-Artoo-External-User-Id"
HEADER_API_KEY_ID = "X-Artoo-Api-Key-Id"
HEADER_REQUEST_ID = "X-Artoo-Request-Id"
HEADER_CONTEXT_TIMESTAMP = "X-Artoo-Context-Timestamp"
HEADER_CONTEXT_SIGNATURE = "X-Artoo-Context-Signature"

# 字段名 -> header 名。顺序即规范化签名串的字段顺序（固定，不依赖 dict 序）。
_FIELD_HEADERS: tuple[tuple[str, str], ...] = (
    ("api_key_id", HEADER_API_KEY_ID),
    ("external_user_id", HEADER_EXTERNAL_USER_ID),
    ("request_id", HEADER_REQUEST_ID),
    ("session_id", HEADER_SESSION_ID),
    ("subject_id", HEADER_SUBJECT_ID),
    ("subject_type", HEADER_SUBJECT_TYPE),
    ("tenant_id", HEADER_TENANT_ID),
)

# 签名时间窗（秒）：|now - ts| 超过即视为过期，抑制重放
SIGNATURE_MAX_SKEW_SECONDS = 300

# 主体类型取值
SUBJECT_TYPE_USER = "user"                    # 平台注册用户（JWT / 用户级 Key）
SUBJECT_TYPE_EXTERNAL_USER = "external_user"  # 第三方自有用户体系的终端用户（代理 Key）
SUBJECT_TYPE_MACHINE = "machine"              # 机器身份（租户级 Key，无自然人）


@dataclass(frozen=True)
class CallerContext:
    """一次 MCP 调用的调用方上下文（只读）。

    字段全部可空：不同凭据通道能提供的信息不同（例如 JWT 登录用户没有
    ``external_user_id``，租户级机器 Key 没有任何自然人主体）。缺失字段一律省略而
    不是发空串，让第三方能区分"没有"与"空"。

    ``subject_id`` 是**统一主体标识**：注册用户为内部 user_id、外部用户为
    external_user_id。第三方只需读它就能做用户级隔离，无需分辨凭据类型。

    从 ``IdentityContext`` 到本结构的映射发生在 Agent 侧
    （:meth:`app.agent.tools.base.ToolContext.from_identity`）—— 协议层不反向依赖鉴权
    实现，字段同名，转换在 ``mcp_client`` 一处用 ``asdict`` 直转。
    """

    session_id: str | None = None
    tenant_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    external_user_id: str | None = None
    api_key_id: str | None = None
    request_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(asdict(self).values())

    def to_dict(self) -> dict[str, str]:
        """转为 _meta 载荷（省略空字段）。"""
        return {k: v for k, v in asdict(self).items() if v}


def canonical_string(ctx: CallerContext, timestamp: str) -> str:
    """规范化签名串：固定字段顺序的 ``k=v`` 换行连接，末行为 ``ts=<timestamp>``。

    省略空字段（与实际发送的 header 集合一致），保证签名方与验签方对同一份数据
    算出同一个串。
    """
    data = asdict(ctx)
    lines = [f"{field}={data[field]}" for field, _ in _FIELD_HEADERS if data.get(field)]
    lines.append(f"ts={timestamp}")
    return "\n".join(lines)


def sign(ctx: CallerContext, secret: str, timestamp: str) -> str:
    """对上下文做 HMAC-SHA256 签名，返回十六进制串。"""
    return hmac.new(
        secret.encode("utf-8"), canonical_string(ctx, timestamp).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def to_headers(ctx: CallerContext, secret: str | None = None) -> dict[str, str]:
    """把上下文编码为 HTTP header；提供 secret 时附加时间戳与签名。"""
    if ctx.is_empty:
        return {}
    data = asdict(ctx)
    headers = {header: data[field] for field, header in _FIELD_HEADERS if data.get(field)}
    if secret:
        ts = str(int(time.time()))
        headers[HEADER_CONTEXT_TIMESTAMP] = ts
        headers[HEADER_CONTEXT_SIGNATURE] = sign(ctx, secret, ts)
    return headers


def to_meta(ctx: CallerContext, secret: str | None = None) -> dict[str, Any]:
    """把上下文编码为 JSON-RPC ``params._meta`` 载荷（含可选签名）。"""
    if ctx.is_empty:
        return {}
    payload: dict[str, Any] = dict(ctx.to_dict())
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["signature"] = sign(ctx, secret, ts)
    return {META_KEY: payload}


def from_headers(
    headers: Mapping[str, str],
    secret: str | None = None,
    max_skew_seconds: int = SIGNATURE_MAX_SKEW_SECONDS,
) -> tuple[CallerContext, bool]:
    """从 HTTP header 解析上下文。

    Returns:
        ``(ctx, verified)``。``verified`` 仅在"提供了 secret 且签名有效且在时间窗内"
        时为 True。未提供 secret 时恒为 False —— 调用方据此决定是否允许把上下文用于
        授权判定。
    """
    values = {field: (headers.get(header) or "").strip() for field, header in _FIELD_HEADERS}
    ctx = CallerContext(**{k: (v or None) for k, v in values.items()})
    if ctx.is_empty or not secret:
        return ctx, False

    ts = (headers.get(HEADER_CONTEXT_TIMESTAMP) or "").strip()
    provided = (headers.get(HEADER_CONTEXT_SIGNATURE) or "").strip()
    return ctx, _verify(ctx, ts, provided, secret, max_skew_seconds)


def from_meta(
    meta: Mapping[str, Any] | None,
    secret: str | None = None,
    max_skew_seconds: int = SIGNATURE_MAX_SKEW_SECONDS,
) -> tuple[CallerContext, bool]:
    """从 JSON-RPC ``params._meta`` 解析上下文（语义与 :func:`from_headers` 一致）。"""
    payload = (meta or {}).get(META_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(payload, Mapping):
        return CallerContext(), False
    ctx = CallerContext(
        **{
            field: (str(payload.get(field)).strip() or None) if payload.get(field) else None
            for field, _ in _FIELD_HEADERS
        }
    )
    if ctx.is_empty or not secret:
        return ctx, False
    ts = str(payload.get("timestamp") or "").strip()
    provided = str(payload.get("signature") or "").strip()
    return ctx, _verify(ctx, ts, provided, secret, max_skew_seconds)


def _verify(
    ctx: CallerContext, ts: str, provided: str, secret: str, max_skew_seconds: int
) -> bool:
    """校验时间窗 + 常量时间比对签名。任何异常一律判为未验证（fail-closed）。"""
    if not ts or not provided:
        return False
    try:
        skew = abs(int(time.time()) - int(ts))
    except ValueError:
        return False
    if skew > max_skew_seconds:
        logger.warning("[MCP] 调用方上下文签名超出时间窗: skew=%ss", skew)
        return False
    return hmac.compare_digest(sign(ctx, secret, ts), provided)
