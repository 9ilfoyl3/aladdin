"""outbound MCP 目标 URL 校验（SSRF 护栏）。

MCP server 地址由平台超管填写，Artoo 服务端会主动向该地址发起请求 —— 这是一个
典型的 SSRF 面：一旦填成云元数据地址（``169.254.169.254``）就能把实例凭据读出来。
"超管可信"不等于不需要护栏（配置可能被误填、被上游系统灌入、或超管账号被盗）。

默认策略（兼顾内网部署这一主流场景）：
- 仅允许 ``http`` / ``https``，必须有 host。
- **恒久阻断** link-local（169.254.0.0/16、fe80::/10，含全部云厂商元数据端点）、
  未指定地址（0.0.0.0、::）、多播与保留段。
- 私网（10/8、172.16/12、192.168/16）与环回（127.0.0.1、localhost）**默认允许**：
  MCP server 与 Artoo 同处内网/同 docker 网络是常态，默认阻断会直接打断正常集成。
  需要更严策略时置 ``MCP_BLOCK_PRIVATE_NETWORK=true``。

DNS rebinding：保存配置时做一次域名解析检查（``resolve=True``），把解析结果一并
按上述规则判定；运行时只做静态检查，不为每次调用付一次 DNS 解析成本。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from app.config import get_settings

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeMcpUrlError(ValueError):
    """目标 URL 未通过 SSRF 护栏。"""


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, block_private: bool) -> None:
    if ip.is_link_local:
        raise UnsafeMcpUrlError(
            f"拒绝连接 link-local 地址 {ip}（云实例元数据端点位于该网段）"
        )
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise UnsafeMcpUrlError(f"拒绝连接非常规地址 {ip}")
    if block_private and (ip.is_private or ip.is_loopback):
        raise UnsafeMcpUrlError(
            f"当前策略禁止连接内网/环回地址 {ip}（MCP_BLOCK_PRIVATE_NETWORK=true）"
        )


def _resolve_host(host: str) -> list[str]:
    """解析域名为 IP 列表；解析失败返回空列表（交由实际请求阶段报错）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning("[MCP] 域名解析失败，跳过 IP 层校验: %s", host)
        return []
    return [info[4][0] for info in infos]


def validate_mcp_url(url: str, *, resolve: bool = False) -> str:
    """校验并返回规范化后的 base URL（去尾部斜杠）。

    Args:
        url: 待校验的 MCP server base URL。
        resolve: 是否解析域名并对解析结果做 IP 层校验（保存配置时用 True）。

    Raises:
        UnsafeMcpUrlError: scheme 非法、缺 host，或命中被阻断的地址段。
    """
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        raise UnsafeMcpUrlError("MCP server 地址不能为空")

    parsed = urlparse(normalized)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeMcpUrlError(f"仅支持 http / https，收到 {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeMcpUrlError("MCP server 地址缺少主机名")

    block_private = get_settings().mcp_block_private_network

    # host 本身是 IP 字面量 -> 直接判定
    try:
        _check_ip(ipaddress.ip_address(host), block_private)
        return normalized
    except ValueError as e:
        if isinstance(e, UnsafeMcpUrlError):
            raise
        # 不是 IP 字面量，按域名处理

    if host.lower() == "localhost":
        if block_private:
            raise UnsafeMcpUrlError("当前策略禁止连接 localhost（MCP_BLOCK_PRIVATE_NETWORK=true）")
        return normalized

    if resolve:
        for addr in _resolve_host(host):
            try:
                _check_ip(ipaddress.ip_address(addr), block_private)
            except ValueError as e:
                if isinstance(e, UnsafeMcpUrlError):
                    raise UnsafeMcpUrlError(f"{host} 解析到不允许的地址：{e}") from e
    return normalized
