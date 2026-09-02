"""对称加密小盒子：把需要**原文回放**的第三方凭据加密后落库。

用途与既有 ``derive_signing_secret`` 的区别
------------------------------------------
AK/SK 那套是"派生而非存储"——服务端随时能重算，故根本不落库。但 outbound 场景
（Artoo 调用第三方 MCP server 时要带上人家给的 token）必须能拿回**原文**，无法派生，
只能加密存储。

密钥来源：从 ``jwt_secret`` 派生（``HMAC_SHA256(jwt_secret, "secret-box:v1")``），
不新增一份需要运维单独管理的密钥；jwt_secret 只在环境变量里，DB 泄露拿不到明文。

密文格式：``v1:<base64url(nonce||ciphertext)>``。AES-256-GCM 自带完整性校验，
密文被篡改会在解密时失败（不会静默返回错误明文）。

兼容既有明文：:func:`decrypt` 遇到不带 ``v1:`` 前缀的值时原样返回，让存量明文配置
继续可用；下一次保存即自动升级为密文。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

from app.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "v1:"
_NONCE_BYTES = 12  # AES-GCM 推荐 96 bit nonce
_KEY_INFO = b"secret-box:v1"


def _derive_key() -> bytes:
    """从 jwt_secret 派生 32 字节 AES 密钥。"""
    secret = get_settings().jwt_secret.encode("utf-8")
    return hmac.new(secret, _KEY_INFO, hashlib.sha256).digest()


def _aesgcm():
    """惰性导入 cryptography，缺失时给出可操作的错误信息。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover - 依赖缺失属部署问题
        raise RuntimeError(
            "缺少 cryptography 依赖：无法加密存储第三方凭据。"
            "请安装 requirements.txt 中的 cryptography。"
        ) from e
    return AESGCM(_derive_key())


def encrypt(plaintext: str | None) -> str | None:
    """加密明文。``None`` / 空串原样返回（表示"没有凭据"，不是加密后的空值）。"""
    if not plaintext:
        return plaintext
    nonce = os.urandom(_NONCE_BYTES)
    ct = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(stored: str | None) -> str | None:
    """解密密文。

    - ``None`` / 空串 -> 原样返回。
    - 无 ``v1:`` 前缀 -> 视为存量明文，原样返回（平滑兼容）。
    - 有前缀但解密失败 -> 返回 ``None`` 并记 WARNING。此时凭据不可用（例如 jwt_secret
      被更换），但绝不让异常冒泡打断 Agent 主链路；调用方会退化为"不带凭据"。
    """
    if not stored:
        return stored
    if not stored.startswith(_PREFIX):
        return stored
    try:
        raw = base64.urlsafe_b64decode(stored[len(_PREFIX):].encode("ascii"))
        nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return _aesgcm().decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:  # noqa: BLE001 — 解密失败降级为"无凭据"，不打断主链路
        logger.warning(
            "凭据解密失败（jwt_secret 是否被更换？）：%s，本次按无凭据处理", type(e).__name__
        )
        return None


def mask(plaintext: str | None) -> str | None:
    """给 API 响应用的掩码：只回显尾部 4 位，避免把凭据回吐到前端/日志。"""
    if not plaintext:
        return None
    if len(plaintext) <= 4:
        return "*" * len(plaintext)
    return "*" * 8 + plaintext[-4:]
