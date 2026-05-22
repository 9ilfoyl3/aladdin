"""API Key 认证工具模块

提供 API Key 的生成、哈希存储和验证功能。
Key 格式: sk- + 48 位随机十六进制字符
存储方式: SHA256 哈希，数据库中不保存明文
"""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schema.db import ApiKey

# Key 前缀
_KEY_PREFIX = "sk-"
# 随机部分长度（十六进制字符数）
_KEY_RANDOM_LENGTH = 48


def generate_api_key() -> str:
    """生成随机 API Key

    格式: sk- + 48 位随机十六进制字符
    """
    random_part = secrets.token_hex(_KEY_RANDOM_LENGTH // 2)
    return f"{_KEY_PREFIX}{random_part}"


def hash_key(key: str) -> str:
    """对 API Key 进行 SHA256 哈希

    用于安全存储，数据库中只保存哈希值。
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_key_prefix(key: str) -> str:
    """提取 Key 前缀用于展示（sk-xxxx...）"""
    return key[:11] + "..."


async def verify_key(key: str, session: AsyncSession) -> ApiKey | None:
    """验证 API Key 是否有效

    查找哈希匹配且处于激活状态的 Key 记录。
    验证通过后自动更新 call_count 和 last_used_at。

    Returns:
        匹配的 ApiKey 记录，无效时返回 None
    """
    key_hash = hash_key(key)
    stmt = select(ApiKey).where(
        ApiKey.key_hash == key_hash,
        ApiKey.is_active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    api_key = result.scalar_one_or_none()

    if api_key is None:
        return None

    # 更新调用计数和最后使用时间
    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key.id)
        .values(call_count=ApiKey.call_count + 1, last_used_at=datetime.utcnow())
    )
    await session.commit()

    return api_key
