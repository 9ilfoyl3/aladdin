"""测试 Loader 基类和工厂方法"""

import pytest
from app.pipeline.loader import (
    BaseLoader,
    LoadResult,
    SUPPORTED_TYPES,
    get_loader,
)


class TestLoadResult:
    """测试 LoadResult 数据类"""

    def test_create_with_content_only(self):
        result = LoadResult(content="hello")
        assert result.content == "hello"
        assert result.metadata == {}

    def test_create_with_metadata(self):
        meta = {"filename": "test.pdf", "page": 1}
        result = LoadResult(content="text", metadata=meta)
        assert result.content == "text"
        assert result.metadata == meta


class TestBaseLoader:
    """测试 BaseLoader 抽象类"""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseLoader()

    def test_subclass_must_implement_load(self):
        class IncompleteLoader(BaseLoader):
            pass

        with pytest.raises(TypeError):
            IncompleteLoader()

    def test_subclass_with_load_works(self):
        class DummyLoader(BaseLoader):
            def load(self, file_path: str) -> LoadResult:
                return LoadResult(content="dummy", metadata={"path": file_path})

        loader = DummyLoader()
        result = loader.load("/tmp/test.txt")
        assert result.content == "dummy"
        assert result.metadata["path"] == "/tmp/test.txt"


class TestGetLoader:
    """测试工厂方法"""

    def test_unsupported_type_raises_value_error(self):
        with pytest.raises(ValueError, match="不支持的文件类型"):
            get_loader("csv")

    def test_unsupported_type_error_includes_supported_list(self):
        with pytest.raises(ValueError, match="支持的格式"):
            get_loader("html")

    def test_case_insensitive(self):
        """文件类型应不区分大小写"""
        with pytest.raises(ValueError, match="不支持的文件类型"):
            get_loader("CSV")

    def test_strips_dot_prefix(self):
        """应能处理带点号的扩展名"""
        # 带点号的不支持类型仍应报错
        with pytest.raises(ValueError, match="不支持的文件类型"):
            get_loader(".csv")

    def test_supported_types_set(self):
        """验证支持的类型集合"""
        assert SUPPORTED_TYPES == {"md", "txt", "pdf", "docx", "xlsx", "pptx"}
