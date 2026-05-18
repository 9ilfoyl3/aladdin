"""ExternalAPIProvider 单元测试"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx

from app.pipeline.ocr.external_api_provider import ExternalAPIProvider
from app.pipeline.ocr.provider import OCRResult


class TestExternalAPIProviderInit:
    """初始化与基本属性测试"""

    def test_name_property(self):
        """name 属性返回 'external_api'"""
        provider = ExternalAPIProvider(api_url="http://ocr-service/recognize")
        assert provider.name == "external_api"

    def test_init_stores_config(self):
        """__init__ 正确存储配置参数"""
        provider = ExternalAPIProvider(
            api_url="http://ocr-service/recognize",
            api_key="test-key",
            timeout=60.0,
        )
        assert provider._api_url == "http://ocr-service/recognize"
        assert provider._api_key == "test-key"
        assert provider._timeout == 60.0

    def test_default_params(self):
        """默认参数为 api_key='', timeout=30.0"""
        provider = ExternalAPIProvider(api_url="http://ocr-service/recognize")
        assert provider._api_key == ""
        assert provider._timeout == 30.0


class TestExternalAPIProviderIsAvailable:
    """is_available 方法测试"""

    def test_is_available_with_url(self):
        """api_url 不为空时返回 True"""
        provider = ExternalAPIProvider(api_url="http://ocr-service/recognize")
        assert provider.is_available() is True

    def test_is_not_available_with_empty_url(self):
        """api_url 为空时返回 False"""
        provider = ExternalAPIProvider(api_url="")
        assert provider.is_available() is False


class TestExternalAPIProviderRecognize:
    """recognize 方法测试"""

    @pytest.mark.asyncio
    async def test_recognize_success(self, tmp_path):
        """成功调用外部 API 并返回 OCRResult"""
        # 创建临时文件
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        provider = ExternalAPIProvider(
            api_url="http://ocr-service/recognize", api_key="my-key"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "full_text": "识别到的文本",
            "avg_confidence": 0.92,
            "pages": [],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.recognize(str(test_file))

        assert isinstance(result, OCRResult)
        assert result.full_text == "识别到的文本"
        assert result.avg_confidence == 0.92
        assert result.provider_name == "external_api"

        # 验证 Authorization header 被设置
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer my-key"

    @pytest.mark.asyncio
    async def test_recognize_without_api_key(self, tmp_path):
        """不带 api_key 时不发送 Authorization 头"""
        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"fake image content")

        provider = ExternalAPIProvider(api_url="http://ocr-service/recognize")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "hello", "confidence": 0.85}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.recognize(str(test_file))

        # 验证没有 Authorization header
        call_kwargs = mock_client.post.call_args
        assert "Authorization" not in call_kwargs.kwargs["headers"]
        assert result.full_text == "hello"

    @pytest.mark.asyncio
    async def test_recognize_raises_on_http_error(self, tmp_path):
        """外部 API 返回非 200 状态码时抛出 HTTPStatusError"""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        provider = ExternalAPIProvider(api_url="http://ocr-service/recognize")

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=mock_response,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await provider.recognize(str(test_file))

    @pytest.mark.asyncio
    async def test_recognize_raises_on_timeout(self, tmp_path):
        """请求超时时抛出 TimeoutException"""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        provider = ExternalAPIProvider(
            api_url="http://ocr-service/recognize", timeout=5.0
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Request timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.TimeoutException):
                await provider.recognize(str(test_file))

    @pytest.mark.asyncio
    async def test_recognize_uses_configured_timeout(self, tmp_path):
        """使用配置的超时时间"""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        provider = ExternalAPIProvider(
            api_url="http://ocr-service/recognize", timeout=45.0
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "ok"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.recognize(str(test_file))

        # 验证 timeout 被传递给 AsyncClient
        mock_client_cls.assert_called_once_with(timeout=45.0)


class TestExternalAPIProviderAdaptResponse:
    """_adapt_response 方法测试"""

    def test_adapt_full_text_field(self):
        """从 full_text 字段提取文本"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {"full_text": "完整文本内容", "avg_confidence": 0.95}

        result = provider._adapt_response(data)

        assert result.full_text == "完整文本内容"
        assert result.avg_confidence == 0.95
        assert result.provider_name == "external_api"

    def test_adapt_text_field_fallback(self):
        """无 full_text 时从 text 字段提取"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {"text": "备用文本", "confidence": 0.88}

        result = provider._adapt_response(data)

        assert result.full_text == "备用文本"
        assert result.avg_confidence == 0.88

    def test_adapt_with_pages(self):
        """从 pages 数组提取按页结果"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {
            "full_text": "第一页\n\n第二页",
            "avg_confidence": 0.90,
            "pages": [
                {
                    "page_num": 1,
                    "full_text": "第一页",
                    "blocks": [
                        {"text": "第一页", "confidence": 0.90, "bbox": [10, 20, 100, 40]},
                    ],
                },
                {
                    "page_num": 2,
                    "full_text": "第二页",
                    "blocks": [
                        {"text": "第二页", "confidence": 0.92, "bbox": [10, 20, 100, 40]},
                    ],
                },
            ],
        }

        result = provider._adapt_response(data)

        assert len(result.pages) == 2
        assert result.pages[0].page_num == 1
        assert result.pages[0].full_text == "第一页"
        assert len(result.pages[0].blocks) == 1
        assert result.pages[0].blocks[0].text == "第一页"
        assert result.pages[0].blocks[0].confidence == 0.90
        assert result.pages[0].blocks[0].bbox == (10, 20, 100, 40)
        assert result.pages[1].page_num == 2
        assert result.pages[1].full_text == "第二页"

    def test_adapt_pages_without_full_text_uses_blocks(self):
        """页面无 full_text 时从 blocks 拼接"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {
            "pages": [
                {
                    "blocks": [
                        {"text": "行一", "confidence": 0.9},
                        {"text": "行二", "confidence": 0.85},
                    ]
                }
            ]
        }

        result = provider._adapt_response(data)

        assert result.pages[0].full_text == "行一\n行二"
        # full_text 从 pages 拼接
        assert result.full_text == "行一\n行二"

    def test_adapt_empty_response(self):
        """空响应返回空 OCRResult"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {}

        result = provider._adapt_response(data)

        assert result.full_text == ""
        assert result.pages == []
        assert result.avg_confidence == 0.0
        assert result.provider_name == "external_api"

    def test_adapt_invalid_bbox_ignored(self):
        """无效的 bbox 格式被忽略（设为 None）"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {
            "pages": [
                {
                    "blocks": [
                        {"text": "文本", "confidence": 0.9, "bbox": [10, 20]},
                    ]
                }
            ]
        }

        result = provider._adapt_response(data)

        assert result.pages[0].blocks[0].bbox is None

    def test_adapt_metadata_passthrough(self):
        """metadata 字段透传"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {
            "text": "内容",
            "metadata": {"engine": "custom-ocr", "version": "2.0"},
        }

        result = provider._adapt_response(data)

        assert result.metadata == {"engine": "custom-ocr", "version": "2.0"}

    def test_adapt_page_num_defaults_to_index(self):
        """pages 无 page_num 时默认使用索引 + 1"""
        provider = ExternalAPIProvider(api_url="http://example.com")
        data = {
            "pages": [
                {"full_text": "第一页内容"},
                {"full_text": "第二页内容"},
            ]
        }

        result = provider._adapt_response(data)

        assert result.pages[0].page_num == 1
        assert result.pages[1].page_num == 2
