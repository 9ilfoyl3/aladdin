"""API Key 工具函数测试（密钥生成 / SHA256 哈希 / 前缀展示）。

说明（tenant-auth 重构后）：
- 原 `verify_key` 仅校验函数已迁出，认证与身份合成统一收敛到
  `app/auth/apikey_auth.py` 的 ApiKeyAuthenticator（经 Authorization_Guard 调用）。
- 原全局 `ApiKeyAuthMiddleware` 已退役删除，鉴权改由各路由 Depends(authorization_guard(...))。
- 因此本文件只保留仍然有效的纯工具函数测试；API Key 三模型认证、CRUD、通道边界、
  撤销/计数等行为由 tenant-auth 测试套件覆盖：
    tests/test_tenant_auth_properties.py / test_tenant_auth_db_properties.py
    tests/test_tenant_auth_integration*.py 及 e2e 脚本。
"""

from app.api.auth import generate_api_key, get_key_prefix, hash_key


class TestAuthUtils:
    """API Key 生成 / 哈希 / 前缀工具函数测试。"""

    def test_generate_api_key_format(self):
        """生成的 Key 应以 sk- 开头，总长度 51 字符（sk- + 48 hex）。"""
        key = generate_api_key()
        assert key.startswith("sk-")
        assert len(key) == 51

    def test_generate_api_key_uniqueness(self):
        """每次生成的 Key 应不同。"""
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100

    def test_hash_key_deterministic(self):
        """相同 Key 的哈希值应一致。"""
        key = "sk-abc123"
        assert hash_key(key) == hash_key(key)

    def test_hash_key_different_for_different_keys(self):
        """不同 Key 的哈希值应不同。"""
        assert hash_key("sk-aaa") != hash_key("sk-bbb")

    def test_hash_key_is_sha256(self):
        """哈希值应为 64 位十六进制字符串（SHA256）。"""
        h = hash_key("sk-test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_get_key_prefix(self):
        """前缀应为前 11 个字符 + ...。"""
        key = "sk-abcdef1234567890abcdef1234567890abcdef12345678"
        prefix = get_key_prefix(key)
        assert prefix == "sk-abcdef12..."
        assert len(prefix) == 14
