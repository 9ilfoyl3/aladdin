"""JWTAuthenticator：JWT 签发与校验（PyJWT / HS256）。

JWT 仅承载身份标识与有效期，**绝不写入任何权限点**（权限每次请求实时解析）。
载荷：sub=user_id, tid=home_tenant_id, exp, iat, tv=token_version。
停用/重置口令时 users.token_version 自增，旧 token 的 tv 不匹配即失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

_ALGORITHM = "HS256"


class JwtError(Exception):
    """JWT 校验失败（过期/签名错/格式错）。由调用方映射为 401。"""


@dataclass(frozen=True)
class JwtClaims:
    """解析后的 JWT 载荷（仅身份与有效期，无权限）。"""

    user_id: str
    tenant_id: str | None
    token_version: int


def _secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        # 启动期硬依赖：auth 开启却无密钥，绝不静默以不安全状态运行。
        raise RuntimeError(
            "jwt_secret 未配置：请在环境变量/.env 设置 JWT_SECRET 后再启用鉴权"
        )
    return secret


def issue_token(
    user_id: str,
    tenant_id: str | None,
    token_version: int,
) -> str:
    """为指定用户签发 JWT。载荷不含任何权限点。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "tv": token_version,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def decode_token(token: str) -> JwtClaims:
    """校验并解析 JWT。过期/签名错/缺字段 -> JwtError（调用方转 401）。"""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise JwtError("token 已过期") from e
    except jwt.InvalidTokenError as e:
        raise JwtError("token 无效") from e

    user_id = payload.get("sub")
    if not user_id:
        raise JwtError("token 缺少 sub")
    return JwtClaims(
        user_id=user_id,
        tenant_id=payload.get("tid"),
        token_version=int(payload.get("tv", 0)),
    )
