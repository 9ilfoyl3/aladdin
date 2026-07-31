"""API Key AK/SK 签名认证（aksk-signing）。

面向"无后端的可信调用方"（脚本 / 自动化平台节点 / 桌面客户端）：不再把明文密钥
放进 ``Authorization: Bearer sk-...`` 每次上行，而是用一把 SK 对本次请求做 HMAC-SHA256
签名，服务端重算比对。控制台 / 抓包里只看到**每次现算的签名**，看不到长期密钥本身。

凭据模型
--------
- ``AK``（Access Key）= ``api_key.id``，可公开，仅用于定位密钥记录。
- ``SK``（Secret Key）由服务端从 ``jwt_secret`` 派生：``SK = HMAC(jwt_secret, "aksk:"+AK)``。
  **不落库**——DB 泄露也拿不到 SK、无法伪造签名（伪造需 jwt_secret，它只在环境变量里）。
  签发时向调用方展示一次，调用方妥善保存于自己的可信环境。

请求头
------
``Authorization: SAG-HMAC-SHA256 ak=<AK>,ts=<unix秒>,nonce=<随机hex>,sign=<hex签名>``

签名串（换行连接，顺序固定）::

    <METHOD大写>\n<PATH>\n<原始query串>\n<ts>\n<nonce>\n<X-External-User-Id或空>

不纳入 body：兼容浏览器/客户端 multipart 上传（客户端难以拿到最终多部分字节做哈希）。
body 完整性依赖 HTTPS 传输层，与 tenant-auth 的 situation-1（可信环境）定位一致。

防重放
------
- 时间窗：``|now - ts| > apikey_sign_window_seconds`` 直接拒绝。
- nonce 去重：Redis ``SET nonce NX EX 窗口``，命中已存在 = 重放 -> 拒绝。
  Redis 不可用时降级为"仅时间窗"（best-effort，不阻断可信调用主流程）。

失败一律映射 401（``UnauthenticatedError``），与其他凭据无效路径语义一致。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Mapping

from app.api.errors import UnauthenticatedError
from app.auth.constants import HEADER_EXTERNAL_USER_ID
from app.config import get_settings

logger = logging.getLogger(__name__)

# 签名方案标识（Authorization 头首段，大小写不敏感匹配）。
SIGN_SCHEME = "SAG-HMAC-SHA256"

# nonce 在 Redis 的键前缀（按 AK 命名空间，避免跨密钥碰撞）。
_NONCE_KEY_PREFIX = "aksk:nonce:"


def derive_signing_secret(api_key_id: str) -> str:
    """从 jwt_secret 派生某 AK 的 SK（不落库，可随时重算）。

    SK = HMAC_SHA256(jwt_secret, "aksk:"+AK) 的十六进制串。ak 公开、jwt_secret 保密，
    故 SK 只有持有 jwt_secret 的服务端可算出并签发；调用方拿到后当作长期密钥保存。
    """
    master = get_settings().jwt_secret
    if not master:
        raise RuntimeError("jwt_secret 未配置，无法派生 AK/SK 签名密钥")
    return hmac.new(
        master.encode("utf-8"), f"aksk:{api_key_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def is_signed_request(authorization: str | None) -> bool:
    """判断 Authorization 头是否为 AK/SK 签名方案（而非 Bearer）。"""
    if not authorization:
        return False
    prefix = SIGN_SCHEME + " "
    return authorization[: len(prefix)].upper() == prefix.upper()


def _parse_auth_params(authorization: str) -> dict[str, str]:
    """解析 ``SAG-HMAC-SHA256 ak=..,ts=..,nonce=..,sign=..`` 为字典。

    逗号分隔各段，每段按**首个** ``=`` 拆 k/v（sign 为 hex 无逗号，安全）。
    缺任一必需字段由调用方校验。
    """
    _, _, rest = authorization.partition(" ")
    params: dict[str, str] = {}
    for seg in rest.split(","):
        seg = seg.strip()
        if not seg:
            continue
        key, sep, value = seg.partition("=")
        if sep:
            params[key.strip().lower()] = value.strip()
    return params


def build_canonical_string(
    method: str, path: str, query: str, ts: str, nonce: str, external_user_id: str
) -> str:
    """拼接待签名串（客户端与服务端须逐字节一致）。"""
    return "\n".join(
        [method.upper(), path, query or "", ts, nonce, external_user_id or ""]
    )


def compute_signature(secret: str, canonical: str) -> str:
    """对 canonical 串用 SK 做 HMAC-SHA256，返回十六进制签名。"""
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


async def verify_signed_params(
    *,
    ak: str | None,
    ts: str | None,
    nonce: str | None,
    sign: str | None,
    method: str,
    path: str,
    query: str,
    external_user_id: str,
) -> str:
    """校验一组签名要素，成功返回 AK（``api_key.id``），失败抛 ``UnauthenticatedError``。

    与凭据来源（HTTP 头 / WS query）无关的纯校验核心：只做"签名 + 时间窗 + nonce"三项。
    密钥是否存在/被撤销/租户停用由上层 ``ApiKeyAuthenticator.authenticate_by_id`` 再确认。
    """
    if not (ak and ts and nonce and sign):
        raise UnauthenticatedError("签名缺少必需字段（ak/ts/nonce/sign）")

    # 1) 时间窗校验（防重放第一道）。
    try:
        ts_int = int(ts)
    except ValueError as e:
        raise UnauthenticatedError("签名时间戳非法") from e
    window = get_settings().apikey_sign_window_seconds
    if abs(int(time.time()) - ts_int) > window:
        raise UnauthenticatedError("签名已过期或时间戳偏差过大")

    # 2) 重算签名并常量时间比对。
    canonical = build_canonical_string(method, path, query, ts, nonce, external_user_id or "")
    expected = compute_signature(derive_signing_secret(ak), canonical)
    if not hmac.compare_digest(expected, sign):
        raise UnauthenticatedError("签名校验失败")

    # 3) nonce 去重（防重放第二道，Redis best-effort）。
    if not await _consume_nonce(ak, nonce, window):
        raise UnauthenticatedError("签名 nonce 已被使用（疑似重放）")

    return ak


async def verify_signature(
    *,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    authorization: str,
) -> str:
    """校验 HTTP 签名请求（凭据在 Authorization 头），成功返回 AK，失败抛异常。"""
    params = _parse_auth_params(authorization)
    return await verify_signed_params(
        ak=params.get("ak"),
        ts=params.get("ts"),
        nonce=params.get("nonce"),
        sign=params.get("sign"),
        method=method,
        path=path,
        query=query,
        external_user_id=headers.get(HEADER_EXTERNAL_USER_ID) or "",
    )


async def _consume_nonce(ak: str, nonce: str, window: int) -> bool:
    """在时间窗内标记 nonce 已用；未用过返回 True，已用过返回 False。

    Redis 不可用时降级放行（返回 True），此时仅靠时间窗防重放。
    """
    redis = await _get_nonce_redis()
    if redis is None:
        return True
    try:
        key = f"{_NONCE_KEY_PREFIX}{ak}:{nonce}"
        # SET key 1 NX EX window：仅当不存在时置位，TTL=窗口大小自动回收。
        ok = await redis.set(key, "1", nx=True, ex=max(window, 1))
        return bool(ok)
    except Exception:  # noqa: BLE001 — 防重放是加固项，Redis 抖动不应阻断可信调用
        logger.warning("AK/SK nonce 去重失败，降级为仅时间窗", exc_info=True)
        return True


# ---- nonce 用 Redis 客户端（懒创建单例，镜像项目既有 aioredis.from_url 模式） ----
_nonce_redis = None
_nonce_redis_inited = False


async def _get_nonce_redis():
    """懒创建 nonce 去重用的 Redis 客户端；不可用则返回 None（降级）。"""
    global _nonce_redis, _nonce_redis_inited
    if _nonce_redis_inited:
        return _nonce_redis
    _nonce_redis_inited = True
    try:
        import redis.asyncio as aioredis

        redis_url = get_settings().redis_url
        if not redis_url:
            logger.info("AK/SK nonce：redis_url 未配置，降级为仅时间窗防重放")
            _nonce_redis = None
            return None
        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        _nonce_redis = client
    except Exception as e:  # noqa: BLE001
        logger.warning("AK/SK nonce：Redis 初始化失败，降级为仅时间窗防重放: %s", e)
        _nonce_redis = None
    return _nonce_redis
