"""OCR 服务配置 API 测试

测试 GET /api/ocr-configs 和 POST /api/ocr-configs 端点。
使用 httpx AsyncClient + 内存 SQLite 数据库。
"""

import sys
from unittest.mock import MagicMock

# Mock 重型依赖模块
sys.modules.setdefault("pymilvus", MagicMock())

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.schema.db import Base

_test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_test_session_factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with _test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture
async def client():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.main import app
    from app.storage.database import get_db
    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class TestListOCRConfigs:
    """GET /api/ocr-configs"""

    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        """无配置时返回空列表"""
        resp = await client.get("/api/ocr-configs")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_ordered_by_created_at_desc(self, client):
        """列表按 created_at 倒序"""
        resp1 = await client.post("/api/ocr-configs", json={
            "name": "First", "provider_type": "paddleocr", "api_url": "http://a"
        })
        resp2 = await client.post("/api/ocr-configs", json={
            "name": "Second", "provider_type": "paddleocr", "api_url": "http://b"
        })
        resp = await client.get("/api/ocr-configs")
        data = resp.json()
        assert len(data) == 2
        # 验证按 created_at 倒序（后创建的在前，或同时创建时顺序一致）
        assert data[0]["created_at"] >= data[1]["created_at"]

    @pytest.mark.asyncio
    async def test_api_key_masked(self, client):
        """api_key 脱敏，仅返回 api_key_set"""
        await client.post("/api/ocr-configs", json={
            "name": "WithKey", "provider_type": "external_api",
            "api_url": "http://api", "api_key": "secret123"
        })
        resp = await client.get("/api/ocr-configs")
        item = resp.json()[0]
        assert item["api_key_set"] is True
        assert "api_key" not in item or item.get("api_key") is None

    @pytest.mark.asyncio
    async def test_api_key_set_false_when_no_key(self, client):
        """未设置 api_key 时 api_key_set 为 False"""
        await client.post("/api/ocr-configs", json={
            "name": "NoKey", "provider_type": "paddleocr", "api_url": "http://local"
        })
        resp = await client.get("/api/ocr-configs")
        assert resp.json()[0]["api_key_set"] is False


class TestCreateOCRConfig:
    """POST /api/ocr-configs"""

    @pytest.mark.asyncio
    async def test_create_success(self, client):
        """成功创建返回 201"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "PaddleOCR 本地",
            "provider_type": "paddleocr",
            "api_url": "http://localhost:8866",
            "timeout": 30.0,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "PaddleOCR 本地"
        assert data["provider_type"] == "paddleocr"
        assert data["api_url"] == "http://localhost:8866"
        assert data["timeout"] == 30.0
        assert data["is_default"] is False
        assert data["is_fallback"] is False
        assert data["api_key_set"] is False
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_create_empty_name(self, client):
        """name 为空返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "", "provider_type": "paddleocr", "api_url": "http://a"
        })
        assert resp.status_code == 422
        assert "名称不能为空" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_whitespace_name(self, client):
        """name 仅空白返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "   ", "provider_type": "paddleocr", "api_url": "http://a"
        })
        assert resp.status_code == 422
        assert "名称不能为空" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_name_too_long(self, client):
        """name 超过 100 字符返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "x" * 101, "provider_type": "paddleocr", "api_url": "http://a"
        })
        assert resp.status_code == 422
        assert "名称过长" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_invalid_provider_type(self, client):
        """provider_type 非法返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "invalid", "api_url": "http://a"
        })
        assert resp.status_code == 422
        assert "类型无效" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_empty_api_url(self, client):
        """api_url 为空返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": ""
        })
        assert resp.status_code == 422
        assert "API 地址不能为空" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_timeout_too_low(self, client):
        """timeout < 1 返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a", "timeout": 0.5
        })
        assert resp.status_code == 422
        assert "超时时间" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_timeout_too_high(self, client):
        """timeout > 300 返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a", "timeout": 301
        })
        assert resp.status_code == 422
        assert "超时时间" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_default_and_fallback_rejected(self, client):
        """同时设 is_default 和 is_fallback 返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
            "is_default": True, "is_fallback": True,
        })
        assert resp.status_code == 422
        assert "同一服务不能同时设为默认和备用" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_default_clears_previous(self, client):
        """设置新默认时旧默认被取消"""
        await client.post("/api/ocr-configs", json={
            "name": "Old Default", "provider_type": "paddleocr",
            "api_url": "http://a", "is_default": True,
        })
        await client.post("/api/ocr-configs", json={
            "name": "New Default", "provider_type": "paddleocr",
            "api_url": "http://b", "is_default": True,
        })
        resp = await client.get("/api/ocr-configs")
        data = resp.json()
        defaults = [c for c in data if c["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["name"] == "New Default"

    @pytest.mark.asyncio
    async def test_create_fallback_clears_previous(self, client):
        """设置新 fallback 时旧 fallback 被取消"""
        await client.post("/api/ocr-configs", json={
            "name": "Old Fallback", "provider_type": "paddleocr",
            "api_url": "http://a", "is_fallback": True,
        })
        await client.post("/api/ocr-configs", json={
            "name": "New Fallback", "provider_type": "paddleocr",
            "api_url": "http://b", "is_fallback": True,
        })
        resp = await client.get("/api/ocr-configs")
        data = resp.json()
        fallbacks = [c for c in data if c["is_fallback"]]
        assert len(fallbacks) == 1
        assert fallbacks[0]["name"] == "New Fallback"

    @pytest.mark.asyncio
    async def test_create_with_extra_config(self, client):
        """创建时可传入 extra_config"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "PaddleOCR", "provider_type": "paddleocr",
            "api_url": "http://localhost:8866",
            "extra_config": {"lang": "en", "use_gpu": True},
        })
        assert resp.status_code == 201
        assert resp.json()["extra_config"] == {"lang": "en", "use_gpu": True}

    @pytest.mark.asyncio
    async def test_create_name_stripped(self, client):
        """name 前后空白被去除"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "  Trimmed  ", "provider_type": "paddleocr", "api_url": "http://a"
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "Trimmed"


class TestUpdateOCRConfig:
    """PUT /api/ocr-configs/{config_id}"""

    @pytest.mark.asyncio
    async def test_update_partial_fields(self, client):
        """部分更新仅修改提供的字段"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Original", "provider_type": "paddleocr",
            "api_url": "http://original", "timeout": 30.0,
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "name": "Updated",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated"
        assert data["provider_type"] == "paddleocr"
        assert data["api_url"] == "http://original"
        assert data["timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_update_empty_api_key_keeps_original(self, client):
        """api_key 为空字符串时保持原值"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "WithKey", "provider_type": "external_api",
            "api_url": "http://api", "api_key": "secret123",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "api_key": "",
        })
        assert resp.status_code == 200
        assert resp.json()["api_key_set"] is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_404(self, client):
        """更新不存在的 ID 返回 404"""
        resp = await client.put("/api/ocr-configs/nonexistent-id", json={
            "name": "Test",
        })
        assert resp.status_code == 404
        assert "OCR 配置不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_empty_name_rejected(self, client):
        """更新时 name 为空返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "name": "",
        })
        assert resp.status_code == 422
        assert "名称不能为空" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_invalid_provider_type(self, client):
        """更新时 provider_type 非法返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "provider_type": "invalid",
        })
        assert resp.status_code == 422
        assert "类型无效" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_default_clears_previous(self, client):
        """更新设置新默认时旧默认被取消"""
        resp1 = await client.post("/api/ocr-configs", json={
            "name": "First", "provider_type": "paddleocr",
            "api_url": "http://a", "is_default": True,
        })
        resp2 = await client.post("/api/ocr-configs", json={
            "name": "Second", "provider_type": "paddleocr",
            "api_url": "http://b",
        })
        second_id = resp2.json()["id"]

        await client.put(f"/api/ocr-configs/{second_id}", json={
            "is_default": True,
        })

        resp = await client.get("/api/ocr-configs")
        defaults = [c for c in resp.json() if c["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["name"] == "Second"

    @pytest.mark.asyncio
    async def test_update_fallback_clears_previous(self, client):
        """更新设置新 fallback 时旧 fallback 被取消"""
        resp1 = await client.post("/api/ocr-configs", json={
            "name": "First", "provider_type": "paddleocr",
            "api_url": "http://a", "is_fallback": True,
        })
        resp2 = await client.post("/api/ocr-configs", json={
            "name": "Second", "provider_type": "paddleocr",
            "api_url": "http://b",
        })
        second_id = resp2.json()["id"]

        await client.put(f"/api/ocr-configs/{second_id}", json={
            "is_fallback": True,
        })

        resp = await client.get("/api/ocr-configs")
        fallbacks = [c for c in resp.json() if c["is_fallback"]]
        assert len(fallbacks) == 1
        assert fallbacks[0]["name"] == "Second"

    @pytest.mark.asyncio
    async def test_update_default_and_fallback_rejected(self, client):
        """更新时同时设 is_default 和 is_fallback 返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "is_default": True, "is_fallback": True,
        })
        assert resp.status_code == 422
        assert "同一服务不能同时设为默认和备用" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_existing_default_add_fallback_rejected(self, client):
        """已是默认的配置更新为 fallback 时返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Default", "provider_type": "paddleocr",
            "api_url": "http://a", "is_default": True,
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "is_fallback": True,
        })
        assert resp.status_code == 422
        assert "同一服务不能同时设为默认和备用" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_name_stripped(self, client):
        """更新时 name 前后空白被去除"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "name": "  Trimmed  ",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Trimmed"

    @pytest.mark.asyncio
    async def test_update_timeout_out_of_range(self, client):
        """更新时 timeout 超出范围返回 422"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.put(f"/api/ocr-configs/{config_id}", json={
            "timeout": 0.5,
        })
        assert resp.status_code == 422
        assert "超时时间" in resp.json()["detail"]


class TestDeleteOCRConfig:
    """DELETE /api/ocr-configs/{config_id}"""

    @pytest.mark.asyncio
    async def test_delete_success(self, client):
        """删除返回 204"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "ToDelete", "provider_type": "paddleocr", "api_url": "http://a",
        })
        config_id = resp.json()["id"]

        resp = await client.delete(f"/api/ocr-configs/{config_id}")
        assert resp.status_code == 204

        # 确认已删除
        resp = await client.get("/api/ocr-configs")
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client):
        """删除不存在的 ID 返回 404"""
        resp = await client.delete("/api/ocr-configs/nonexistent-id")
        assert resp.status_code == 404
        assert "OCR 配置不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_default_allowed(self, client):
        """允许删除默认服务"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Default", "provider_type": "paddleocr",
            "api_url": "http://a", "is_default": True,
        })
        config_id = resp.json()["id"]

        resp = await client.delete(f"/api/ocr-configs/{config_id}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_fallback_allowed(self, client):
        """允许删除备用服务"""
        resp = await client.post("/api/ocr-configs", json={
            "name": "Fallback", "provider_type": "paddleocr",
            "api_url": "http://a", "is_fallback": True,
        })
        config_id = resp.json()["id"]

        resp = await client.delete(f"/api/ocr-configs/{config_id}")
        assert resp.status_code == 204

class TestOCRConnectionTest:
    """连通性测试端点测试"""

    @pytest.mark.asyncio
    async def test_temp_test_paddleocr_available(self, client):
        """PaddleOCR 临时测试 - 检查 is_available 返回结果"""
        from unittest.mock import patch

        with patch("app.pipeline.ocr.paddleocr_provider.PaddleOCRProvider.is_available", return_value=True):
            resp = await client.post("/api/ocr-configs/test", json={
                "provider_type": "paddleocr",
                "api_url": "http://localhost:8000",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "可用" in data["message"]
        assert data["elapsed_ms"] is not None

    @pytest.mark.asyncio
    async def test_temp_test_paddleocr_unavailable(self, client):
        """PaddleOCR 临时测试 - 未安装时返回 success=false"""
        from unittest.mock import patch

        with patch("app.pipeline.ocr.paddleocr_provider.PaddleOCRProvider.is_available", return_value=False):
            resp = await client.post("/api/ocr-configs/test", json={
                "provider_type": "paddleocr",
                "api_url": "http://localhost:8000",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "未安装" in data["message"]

    @pytest.mark.asyncio
    async def test_temp_test_external_api_success(self, client):
        """External API 临时测试 - 连接成功"""
        from unittest.mock import AsyncMock, patch

        mock_response = AsyncMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient.head", return_value=mock_response):
            resp = await client.post("/api/ocr-configs/test", json={
                "provider_type": "external_api",
                "api_url": "http://example.com/ocr",
                "api_key": "test-key",
                "timeout": 10.0,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "200" in data["message"]
        assert data["elapsed_ms"] is not None

    @pytest.mark.asyncio
    async def test_temp_test_external_api_timeout(self, client):
        """External API 临时测试 - 超时返回 success=false"""
        from unittest.mock import patch

        with patch("httpx.AsyncClient.head", side_effect=httpx.TimeoutException("timeout")):
            resp = await client.post("/api/ocr-configs/test", json={
                "provider_type": "external_api",
                "api_url": "http://example.com/ocr",
                "timeout": 1.0,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "超时" in data["message"]

    @pytest.mark.asyncio
    async def test_temp_test_external_api_connection_error(self, client):
        """External API 临时测试 - 连接异常返回 success=false"""
        from unittest.mock import patch

        with patch("httpx.AsyncClient.head", side_effect=httpx.ConnectError("connection refused")):
            resp = await client.post("/api/ocr-configs/test", json={
                "provider_type": "external_api",
                "api_url": "http://example.com/ocr",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "连接失败" in data["message"]

    @pytest.mark.asyncio
    async def test_saved_config_test_success(self, client):
        """已保存配置测试 - 正常执行"""
        from unittest.mock import patch

        # 先创建一个配置
        resp = await client.post("/api/ocr-configs", json={
            "name": "Test PaddleOCR",
            "provider_type": "paddleocr",
            "api_url": "http://localhost:8000",
        })
        config_id = resp.json()["id"]

        with patch("app.pipeline.ocr.paddleocr_provider.PaddleOCRProvider.is_available", return_value=True):
            resp = await client.post(f"/api/ocr-configs/{config_id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_saved_config_test_not_found(self, client):
        """已保存配置测试 - config_id 不存在返回 404"""
        resp = await client.post("/api/ocr-configs/nonexistent-id/test")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "OCR 配置不存在"

    @pytest.mark.asyncio
    async def test_temp_test_invalid_provider_type(self, client):
        """临时测试 - 不支持的 provider 类型返回 success=false"""
        resp = await client.post("/api/ocr-configs/test", json={
            "provider_type": "unknown",
            "api_url": "http://example.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "不支持" in data["message"]
