"""口令哈希（bcrypt）。

bcrypt 自带 salt 与工作因子，跨平台 wheel 完善、无系统级依赖。
持久化的永远是哈希；明文绝不入库。

并发要点：bcrypt 是 CPU 密集型（单次哈希/校验在默认工作因子下约数十毫秒），
**绝不能**直接在 async 请求处理协程里同步执行——否则会独占事件循环，登录/改密
期间阻塞同进程内一切其它请求（问答、检索、用户体系）。因此对外暴露的
`hash_password`/`verify_password` 为 async，统一用 `asyncio.to_thread` 把 bcrypt
丢到线程池执行，事件循环在等待期间可继续调度其它协程。

同步实现 `_hash_password_sync`/`_verify_password_sync` 保留给"本就不在事件循环里"
的场景（如脱离请求上下文的同步初始化），不在请求热路径调用。
"""

from __future__ import annotations

import asyncio

import bcrypt

# bcrypt 单次哈希明文上限 72 字节，超出部分会被静默截断。
# 为避免"长口令被截断后等价"的反直觉行为，超长时显式拒绝（fail-fast，不偷偷截断）。
_BCRYPT_MAX_BYTES = 72


def _hash_password_sync(plain: str) -> str:
    """同步 bcrypt 哈希（CPU 密集）。仅供非事件循环上下文直接调用。"""
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raise ValueError(
            f"口令过长：bcrypt 仅支持 ≤{_BCRYPT_MAX_BYTES} 字节，请使用更短的口令"
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def _verify_password_sync(plain: str, password_hash: str) -> bool:
    """同步 bcrypt 校验。任何异常（格式错误等）均视为不匹配。"""
    try:
        raw = plain.encode("utf-8")
        if len(raw) > _BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def hash_password(plain: str) -> str:
    """对明文口令做 bcrypt 哈希（线程池执行，不阻塞事件循环）。"""
    return await asyncio.to_thread(_hash_password_sync, plain)


async def verify_password(plain: str, password_hash: str) -> bool:
    """校验明文是否匹配存储的哈希（线程池执行，不阻塞事件循环）。"""
    return await asyncio.to_thread(_verify_password_sync, plain, password_hash)
