"""口令哈希（bcrypt）。

bcrypt 自带 salt 与工作因子，跨平台 wheel 完善、无系统级依赖。
持久化的永远是哈希；明文绝不入库。
"""

from __future__ import annotations

import bcrypt

# bcrypt 单次哈希明文上限 72 字节，超出部分会被静默截断。
# 为避免"长口令被截断后等价"的反直觉行为，超长时显式拒绝（fail-fast，不偷偷截断）。
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    """对明文口令做 bcrypt 哈希，返回可入库的字符串。"""
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raise ValueError(
            f"口令过长：bcrypt 仅支持 ≤{_BCRYPT_MAX_BYTES} 字节，请使用更短的口令"
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """校验明文是否匹配存储的哈希。任何异常（格式错误等）均视为不匹配。"""
    try:
        raw = plain.encode("utf-8")
        if len(raw) > _BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
