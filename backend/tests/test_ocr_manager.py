"""OCRManager 单元测试"""

import pytest
from unittest.mock import AsyncMock

from app.pipeline.ocr.manager import OCRManager
from app.pipeline.ocr.provider import OCRProvider, OCRResult, PageOCRResult


from dataclasses import dataclass, field


@dataclass
class FakeOCRConfig:
    """测试用 OCRConfig 替代品，模拟 ORM 模型的属性"""
    id: str = "config-1"
    name: str = "测试服务"
    provider_type: str = "external_api"
    api_url: str = "http://localhost:8000"
    api_key: str | None = None
    timeout: float = 30.0
    is_default: bool = False
    is_fallback: bool = False
    extra_config: dict | None = None


def make_ocr_config(**kwargs) -> FakeOCRConfig:
    """创建测试用 OCRConfig 实例"""
    return FakeOCRConfig(**kwargs)


def make_ocr_result(provider_name: str = "test") -> OCRResult:
    """创建测试用 OCRResult"""
    return OCRResult(
        full_text="测试文本",
        pages=[PageOCRResult(page_num=1, blocks=[], full_text="测试文本")],
        avg_confidence=0.95,
        provider_name=provider_name,
    )


class FakeProvider(OCRProvider):
    """测试用 Provider"""

    def __init__(self, provider_name: str, available: bool = True):
        self._name = provider_name
        self._available = available
        self.recognize = AsyncMock(return_value=make_ocr_result(provider_name))

    @property
    def name(self) -> str:
        return self._name

    async def recognize(self, file_path: str) -> OCRResult:
        pass  # 被 AsyncMock 替代

    def is_available(self) -> bool:
        return self._available


class TestOCRManagerInit:
    """OCRManager 从数据库配置初始化测试"""

    def test_registers_available_external_provider(self):
        """注册 is_available() 返回 True 的远程 Provider"""
        config = make_ocr_config(
            id="ext-1",
            provider_type="external_api",
            api_url="http://ocr.test/api",
            is_default=True,
        )
        manager = OCRManager([config])

        assert "ext-1" in manager._providers
        assert manager._default_name == "ext-1"

    def test_skips_unavailable_providers(self):
        """不注册 is_available() 返回 False 的 Provider（api_url 为空）"""
        config = make_ocr_config(id="ext-1", provider_type="external_api", api_url="")
        manager = OCRManager([config])

        assert len(manager._providers) == 0

    def test_passes_config_to_external_provider(self):
        """将配置参数传递给 ExternalAPI Provider"""
        config = make_ocr_config(
            id="ext-1",
            provider_type="external_api",
            api_url="http://ocr.test/api",
            api_key="secret",
            timeout=60.0,
        )
        manager = OCRManager([config])

        provider = manager._providers["ext-1"]
        assert provider._api_url == "http://ocr.test/api"
        assert provider._api_key == "secret"
        assert provider._timeout == 60.0

    def test_registers_textin_provider(self):
        """注册 TextIn Provider"""
        config = make_ocr_config(
            id="textin-1",
            provider_type="textin",
            api_url="http://textin.test/api",
            api_key="key",
        )
        manager = OCRManager([config])

        provider = manager._providers["textin-1"]
        assert provider.name == "textin"
        assert provider._api_url == "http://textin.test/api"

    def test_sets_default_and_fallback(self):
        """正确设置 default 和 fallback Provider"""
        configs = [
            make_ocr_config(id="ext-1", provider_type="external_api", api_url="http://a", is_default=True),
            make_ocr_config(id="textin-1", provider_type="textin", api_url="http://b", is_fallback=True),
        ]
        manager = OCRManager(configs)

        assert manager._default_name == "ext-1"
        assert manager._fallback_name == "textin-1"
        assert len(manager._providers) == 2

    def test_empty_configs(self):
        """空配置列表时正常初始化"""
        manager = OCRManager([])

        assert len(manager._providers) == 0
        assert manager._default_name == ""
        assert manager._fallback_name == ""

    def test_unsupported_provider_type(self):
        """不支持的 provider_type 返回 None，不注册"""
        config = make_ocr_config(id="unknown-1", provider_type="unknown_type")
        manager = OCRManager([config])

        assert len(manager._providers) == 0


class TestOCRManagerGetProvider:
    """get_provider 和 list_providers 方法测试"""

    def _make_manager_with_fake_providers(self):
        """创建带有 fake provider 的 manager"""
        manager = OCRManager.__new__(OCRManager)
        manager._default_name = "config-1"
        manager._fallback_name = "config-2"
        manager._providers = {
            "config-1": FakeProvider("textin"),
            "config-2": FakeProvider("external_api"),
        }
        return manager

    def test_get_provider_by_name(self):
        """通过 ID 获取 Provider"""
        manager = self._make_manager_with_fake_providers()
        provider = manager.get_provider("config-2")
        assert provider.name == "external_api"

    def test_get_provider_default(self):
        """不传名称返回默认 Provider"""
        manager = self._make_manager_with_fake_providers()
        provider = manager.get_provider()
        assert provider.name == "textin"

    def test_get_provider_none_returns_default(self):
        """显式传 None 返回默认 Provider"""
        manager = self._make_manager_with_fake_providers()
        provider = manager.get_provider(None)
        assert provider.name == "textin"

    def test_get_provider_not_found_raises(self):
        """请求不存在的 Provider 抛出 ValueError"""
        manager = self._make_manager_with_fake_providers()
        with pytest.raises(ValueError, match="未注册或不可用"):
            manager.get_provider("nonexistent")

    def test_list_providers(self):
        """列出所有已注册 Provider ID"""
        manager = self._make_manager_with_fake_providers()
        providers = manager.list_providers()
        assert set(providers) == {"config-1", "config-2"}

    def test_list_providers_empty(self):
        """无注册 Provider 时返回空列表"""
        manager = OCRManager.__new__(OCRManager)
        manager._providers = {}
        assert manager.list_providers() == []


class TestOCRManagerRecognize:
    """recognize 方法与 fallback 测试"""

    def _make_manager_with_mocked_providers(
        self,
        primary_side_effect=None,
        fallback_side_effect=None,
    ):
        """创建带有 mock provider 的 manager"""
        manager = OCRManager.__new__(OCRManager)
        manager._default_name = "config-1"
        manager._fallback_name = "config-2"

        primary = FakeProvider("textin")
        fallback = FakeProvider("external_api")

        if primary_side_effect:
            primary.recognize = AsyncMock(side_effect=primary_side_effect)
        if fallback_side_effect:
            fallback.recognize = AsyncMock(side_effect=fallback_side_effect)

        manager._providers = {
            "config-1": primary,
            "config-2": fallback,
        }
        return manager, primary, fallback

    @pytest.mark.asyncio
    async def test_recognize_success(self):
        """正常调用主 Provider 返回结果"""
        manager, primary, _ = self._make_manager_with_mocked_providers()
        result = await manager.recognize("/test/file.pdf")

        assert result.provider_name == "textin"
        primary.recognize.assert_called_once_with("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_with_specified_provider(self):
        """指定 Provider ID 进行识别"""
        manager, _, fallback = self._make_manager_with_mocked_providers()
        result = await manager.recognize("/test/file.pdf", provider_name="config-2")

        assert result.provider_name == "external_api"
        fallback.recognize.assert_called_once_with("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_fallback_on_primary_failure(self):
        """主 Provider 失败时自动切换到 fallback"""
        manager, primary, fallback = self._make_manager_with_mocked_providers(
            primary_side_effect=RuntimeError("TextIn 服务故障"),
        )

        result = await manager.recognize("/test/file.pdf")

        assert result.provider_name == "external_api"
        primary.recognize.assert_called_once()
        fallback.recognize.assert_called_once_with("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_raises_original_when_both_fail(self):
        """主 Provider 和 fallback 都失败时抛出原始异常"""
        original_error = RuntimeError("主 Provider 失败")
        manager, _, _ = self._make_manager_with_mocked_providers(
            primary_side_effect=original_error,
            fallback_side_effect=RuntimeError("Fallback 也失败"),
        )

        with pytest.raises(RuntimeError, match="主 Provider 失败"):
            await manager.recognize("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_raises_when_no_fallback_configured(self):
        """无 fallback 配置时直接抛出异常"""
        manager = OCRManager.__new__(OCRManager)
        manager._default_name = "config-1"
        manager._fallback_name = ""

        primary = FakeProvider("textin")
        primary.recognize = AsyncMock(side_effect=RuntimeError("识别失败"))
        manager._providers = {"config-1": primary}

        with pytest.raises(RuntimeError, match="识别失败"):
            await manager.recognize("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_no_fallback_when_same_as_primary(self):
        """fallback 与 primary 相同时不重试"""
        manager = OCRManager.__new__(OCRManager)
        manager._default_name = "config-1"
        manager._fallback_name = "config-1"

        primary = FakeProvider("textin")
        primary.recognize = AsyncMock(side_effect=RuntimeError("识别失败"))
        manager._providers = {"config-1": primary}

        with pytest.raises(RuntimeError, match="识别失败"):
            await manager.recognize("/test/file.pdf")

    @pytest.mark.asyncio
    async def test_recognize_provider_not_found(self):
        """指定的 Provider 不存在时抛出 ValueError"""
        manager = OCRManager.__new__(OCRManager)
        manager._default_name = "config-1"
        manager._fallback_name = ""
        manager._providers = {}

        with pytest.raises(ValueError, match="未注册或不可用"):
            await manager.recognize("/test/file.pdf")
