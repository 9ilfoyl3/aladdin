"""API Key 工具模块（密钥生成 / SHA256 哈希 / 前缀展示）。

仅保留 Key 的生成、哈希、前缀工具。认证与身份合成（校验 + 判型 + 解析外部用户）
已统一迁入 `app/auth/apikey_auth.py` 的 ApiKeyAuthenticator（三处收敛点之一，
经 Authorization_Guard 调用），不再在此提供独立的 verify_key 仅校验函数，
避免两处鉴权逻辑漂移。
"""

import hashlib
import secrets

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
