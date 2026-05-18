"""PaddleOCRProvider 单元测试"""

import pytest
from unittest.mock import patch, MagicMock

from app.pipeline.ocr.paddleocr_provider import PaddleOCRProvider
from app.pipeline.ocr.provider import OCRBlock, OCRResult, PageOCRResult


class TestPaddleOCRProviderInit:
    """初始化与基本属性测试"""

    def test_name_property(self):
        """name 属性返回 'paddleocr'"""
        provider = PaddleOCRProvider()
        assert provider.name == "paddleocr"

    def test_init_does_not_create_engine(self):
        """__init__ 不创建引擎实例（懒加载）"""
        provider = PaddleOCRProvider(lang="en", use_gpu=True)
        assert provider._engine is None
        assert provider._lang == "en"
        assert provider._use_gpu is True

    def test_default_params(self):
        """默认参数为 lang='ch', use_gpu=False"""
        provider = PaddleOCRProvider()
        assert provider._lang == "ch"
        assert provider._use_gpu is False


class TestPaddleOCRProviderIsAvailable:
    """is_available 方法测试"""

    def test_is_available_when_installed(self):
        """paddleocr 包已安装时返回 True"""
        provider = PaddleOCRProvider()
        with patch.dict("sys.modules", {"paddleocr": MagicMock()}):
            assert provider.is_available() is True

    def test_is_available_when_not_installed(self):
        """paddleocr 包未安装时返回 False"""
        provider = PaddleOCRProvider()
        with patch.dict("sys.modules", {"paddleocr": None}):
            # 当模块在 sys.modules 中为 None 时，import 会抛 ImportError
            assert provider.is_available() is False


class TestPaddleOCRProviderLazyLoading:
    """懒加载引擎测试"""

    @patch("app.pipeline.ocr.paddleocr_provider.PaddleOCRProvider._get_engine")
    def test_get_engine_creates_once(self, mock_get_engine):
        """_get_engine 只创建一次引擎实例"""
        provider = PaddleOCRProvider()
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        engine1 = provider._get_engine()
        engine2 = provider._get_engine()

        assert engine1 is engine2


class TestPaddleOCRProviderRecognize:
    """recognize 方法测试"""

    @pytest.mark.asyncio
    async def test_recognize_single_page_image(self):
        """单页图片 OCR 结果适配"""
        provider = PaddleOCRProvider()

        # 模拟 PaddleOCR 图片返回格式
        mock_result = [
            [[[10, 20], [100, 20], [100, 40], [10, 40]], ("你好世界", 0.95)],
            [[[10, 50], [100, 50], [100, 70], [10, 70]], ("测试文本", 0.88)],
        ]

        mock_engine = MagicMock()
        mock_engine.ocr.return_value = mock_result
        provider._engine = mock_engine

        result = await provider.recognize("/fake/image.png")

        assert isinstance(result, OCRResult)
        assert result.provider_name == "paddleocr"
        assert result.full_text == "你好世界\n测试文本"
        assert len(result.pages) == 1
        assert result.pages[0].page_num == 1
        assert len(result.pages[0].blocks) == 2
        assert result.pages[0].blocks[0].text == "你好世界"
        assert result.pages[0].blocks[0].confidence == 0.95
        assert result.pages[0].blocks[0].bbox == (10, 20, 100, 40)
        assert result.pages[0].blocks[1].text == "测试文本"
        assert result.pages[0].blocks[1].confidence == 0.88
        assert abs(result.avg_confidence - (0.95 + 0.88) / 2) < 1e-6

    @pytest.mark.asyncio
    async def test_recognize_multi_page_pdf(self):
        """多页 PDF OCR 结果适配"""
        provider = PaddleOCRProvider()

        # 模拟 PaddleOCR PDF 返回格式：list of pages
        mock_result = [
            # Page 1
            [
                [[[10, 20], [100, 20], [100, 40], [10, 40]], ("第一页内容", 0.90)],
            ],
            # Page 2
            [
                [[[10, 20], [100, 20], [100, 40], [10, 40]], ("第二页内容", 0.85)],
                [[[10, 50], [200, 50], [200, 70], [10, 70]], ("更多内容", 0.92)],
            ],
        ]

        mock_engine = MagicMock()
        mock_engine.ocr.return_value = mock_result
        provider._engine = mock_engine

        result = await provider.recognize("/fake/doc.pdf")

        assert len(result.pages) == 2
        assert result.pages[0].page_num == 1
        assert result.pages[0].full_text == "第一页内容"
        assert result.pages[1].page_num == 2
        assert result.pages[1].full_text == "第二页内容\n更多内容"
        assert "第一页内容" in result.full_text
        assert "第二页内容" in result.full_text

    @pytest.mark.asyncio
    async def test_recognize_empty_result(self):
        """空结果处理"""
        provider = PaddleOCRProvider()

        mock_engine = MagicMock()
        mock_engine.ocr.return_value = []
        provider._engine = mock_engine

        result = await provider.recognize("/fake/blank.png")

        assert result.full_text == ""
        assert result.pages == []
        assert result.avg_confidence == 0.0
        assert result.provider_name == "paddleocr"

    @pytest.mark.asyncio
    async def test_recognize_none_result(self):
        """None 结果处理"""
        provider = PaddleOCRProvider()

        mock_engine = MagicMock()
        mock_engine.ocr.return_value = None
        provider._engine = mock_engine

        result = await provider.recognize("/fake/blank.png")

        assert result.full_text == ""
        assert result.pages == []
        assert result.avg_confidence == 0.0

    @pytest.mark.asyncio
    async def test_recognize_page_with_none(self):
        """PDF 某页为 None 的情况"""
        provider = PaddleOCRProvider()

        mock_result = [
            None,  # 空页
            [
                [[[10, 20], [100, 20], [100, 40], [10, 40]], ("有内容", 0.90)],
            ],
        ]

        mock_engine = MagicMock()
        mock_engine.ocr.return_value = mock_result
        provider._engine = mock_engine

        result = await provider.recognize("/fake/doc.pdf")

        assert len(result.pages) == 2
        assert result.pages[0].full_text == ""
        assert result.pages[1].full_text == "有内容"


class TestPaddleOCRProviderBboxConvert:
    """bbox 转换测试"""

    def test_convert_bbox_normal(self):
        """正常 4 点 bbox 转换为 (x_min, y_min, x_max, y_max)"""
        bbox_raw = [[10, 20], [100, 20], [100, 40], [10, 40]]
        result = PaddleOCRProvider._convert_bbox(bbox_raw)
        assert result == (10, 20, 100, 40)

    def test_convert_bbox_empty(self):
        """空 bbox 返回 None"""
        assert PaddleOCRProvider._convert_bbox([]) is None
        assert PaddleOCRProvider._convert_bbox(None) is None

    def test_convert_bbox_insufficient_points(self):
        """不足 4 个点返回 None"""
        bbox_raw = [[10, 20], [100, 20]]
        assert PaddleOCRProvider._convert_bbox(bbox_raw) is None
