"""Milvus 客户端单元测试（使用 mock，不需要真实 Milvus 实例）"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# 由于 pymilvus 在当前环境可能无法导入，使用 mock 替代
import sys
from unittest.mock import MagicMock as _MagicMock

# 模拟 pymilvus 模块
pymilvus_mock = _MagicMock()
pymilvus_mock.DataType = _MagicMock()
pymilvus_mock.DataType.VARCHAR = "VARCHAR"
pymilvus_mock.DataType.FLOAT_VECTOR = "FLOAT_VECTOR"
pymilvus_mock.DataType.SPARSE_FLOAT_VECTOR = "SPARSE_FLOAT_VECTOR"
pymilvus_mock.DataType.INT64 = "INT64"
sys.modules["pymilvus"] = pymilvus_mock

from app.storage.milvus import MilvusClient


class TestMilvusClient:
    """MilvusClient 基本功能测试"""

    def test_init_default(self):
        """测试默认参数初始化"""
        client = MilvusClient()
        assert client._host == "localhost"
        assert client._port == 19530
        assert client._alias == "default"

    def test_init_custom(self):
        """测试自定义参数初始化"""
        client = MilvusClient(host="milvus-server", port=19531, alias="custom")
        assert client._host == "milvus-server"
        assert client._port == 19531
        assert client._alias == "custom"

    def test_collection_name(self):
        """测试 collection 命名规则"""
        assert MilvusClient._collection_name("abc123") == "kb_abc123"
        assert MilvusClient._collection_name("test") == "kb_test"

    def test_parse_search_results_empty(self):
        """测试空搜索结果解析"""
        results = [[]]
        parsed = MilvusClient._parse_search_results(results)
        assert parsed == []

    def test_parse_search_results(self):
        """测试搜索结果解析"""
        # 模拟 pymilvus 搜索结果
        hit = MagicMock()
        hit.entity.get = lambda field: {
            "chunk_id": "chk_001",
            "doc_id": "doc_001",
            "content": "测试内容",
            "parent_id": "parent_001",
            "chunk_index": 0,
        }.get(field)
        hit.score = 0.95

        results = [[hit]]
        parsed = MilvusClient._parse_search_results(results)

        assert len(parsed) == 1
        assert parsed[0]["chunk_id"] == "chk_001"
        assert parsed[0]["doc_id"] == "doc_001"
        assert parsed[0]["content"] == "测试内容"
        assert parsed[0]["parent_id"] == "parent_001"
        assert parsed[0]["chunk_index"] == 0
        assert parsed[0]["score"] == 0.95

    def test_parse_search_results_multiple(self):
        """测试多条搜索结果解析"""
        hit1 = MagicMock()
        hit1.entity.get = lambda field: {
            "chunk_id": "chk_001",
            "doc_id": "doc_001",
            "content": "内容1",
            "parent_id": "",
            "chunk_index": 0,
        }.get(field)
        hit1.score = 0.95

        hit2 = MagicMock()
        hit2.entity.get = lambda field: {
            "chunk_id": "chk_002",
            "doc_id": "doc_001",
            "content": "内容2",
            "parent_id": "chk_001",
            "chunk_index": 1,
        }.get(field)
        hit2.score = 0.88

        results = [[hit1, hit2]]
        parsed = MilvusClient._parse_search_results(results)

        assert len(parsed) == 2
        assert parsed[0]["score"] == 0.95
        assert parsed[1]["score"] == 0.88

    @pytest.mark.asyncio
    async def test_has_collection(self):
        """测试 has_collection 异步调用"""
        client = MilvusClient()
        client._has_collection_sync = MagicMock(return_value=True)

        result = await client.has_collection("test_kb")
        assert result is True
        client._has_collection_sync.assert_called_once_with("test_kb")

    @pytest.mark.asyncio
    async def test_create_collection(self):
        """测试 create_collection 异步调用"""
        client = MilvusClient()
        client._create_collection_sync = MagicMock()

        await client.create_collection("test_kb")
        client._create_collection_sync.assert_called_once_with("test_kb")

    @pytest.mark.asyncio
    async def test_insert(self):
        """测试 insert 异步调用"""
        client = MilvusClient()
        client._insert_sync = MagicMock(return_value=5)

        data = [{"chunk_id": f"chk_{i}"} for i in range(5)]
        count = await client.insert("test_kb", data)
        assert count == 5
        client._insert_sync.assert_called_once_with("test_kb", data)

    @pytest.mark.asyncio
    async def test_delete(self):
        """测试 delete 异步调用"""
        client = MilvusClient()
        client._delete_sync = MagicMock()

        chunk_ids = ["chk_001", "chk_002"]
        await client.delete("test_kb", chunk_ids)
        client._delete_sync.assert_called_once_with("test_kb", chunk_ids)

    @pytest.mark.asyncio
    async def test_drop_collection(self):
        """测试 drop_collection 异步调用"""
        client = MilvusClient()
        client._drop_collection_sync = MagicMock()

        await client.drop_collection("test_kb")
        client._drop_collection_sync.assert_called_once_with("test_kb")

    @pytest.mark.asyncio
    async def test_search_dense(self):
        """测试 search_dense 异步调用"""
        client = MilvusClient()
        expected = [{"chunk_id": "chk_001", "score": 0.9}]
        client._search_dense_sync = MagicMock(return_value=expected)

        vector = [0.1] * 1024
        result = await client.search_dense("test_kb", vector, top_k=5)
        assert result == expected
        client._search_dense_sync.assert_called_once_with("test_kb", vector, 5, None)

    @pytest.mark.asyncio
    async def test_search_dense_with_expr(self):
        """测试 search_dense 带 expr 参数"""
        client = MilvusClient()
        expected = [{"chunk_id": "chk_001", "score": 0.9}]
        client._search_dense_sync = MagicMock(return_value=expected)

        vector = [0.1] * 1024
        expr = 'file_type in ["pdf"]'
        result = await client.search_dense("test_kb", vector, top_k=5, expr=expr)
        assert result == expected
        client._search_dense_sync.assert_called_once_with("test_kb", vector, 5, expr)

    @pytest.mark.asyncio
    async def test_search_sparse(self):
        """测试 search_sparse 异步调用"""
        client = MilvusClient()
        expected = [{"chunk_id": "chk_002", "score": 0.8}]
        client._search_sparse_sync = MagicMock(return_value=expected)

        sparse_vec = {1: 0.5, 100: 0.3, 500: 0.2}
        result = await client.search_sparse("test_kb", sparse_vec, top_k=3)
        assert result == expected
        client._search_sparse_sync.assert_called_once_with("test_kb", sparse_vec, 3, None)

    @pytest.mark.asyncio
    async def test_search_sparse_with_expr(self):
        """测试 search_sparse 带 expr 参数"""
        client = MilvusClient()
        expected = [{"chunk_id": "chk_002", "score": 0.8}]
        client._search_sparse_sync = MagicMock(return_value=expected)

        sparse_vec = {1: 0.5, 100: 0.3, 500: 0.2}
        expr = 'doc_id in ["doc-123"]'
        result = await client.search_sparse("test_kb", sparse_vec, top_k=3, expr=expr)
        assert result == expected
        client._search_sparse_sync.assert_called_once_with("test_kb", sparse_vec, 3, expr)

    @pytest.mark.asyncio
    async def test_check_schema_version_async(self):
        """测试 check_schema_version 异步调用"""
        client = MilvusClient()
        expected = {"exists": True, "has_new_fields": True, "field_names": ["chunk_id", "file_type", "element_type"]}
        client._check_schema_version_sync = MagicMock(return_value=expected)

        result = await client.check_schema_version("test_kb")
        assert result == expected
        client._check_schema_version_sync.assert_called_once_with("test_kb")

    def test_check_schema_version_sync_no_collection(self):
        """测试 schema 版本检测 - collection 不存在"""
        client = MilvusClient()
        client._connect = MagicMock()

        with patch.object(pymilvus_mock, "utility") as mock_util:
            # 需要 patch 模块级别的 utility
            with patch("app.storage.milvus.utility") as mock_utility:
                mock_utility.has_collection.return_value = False

                result = client._check_schema_version_sync("test_kb")
                assert result == {"exists": False, "has_new_fields": False, "field_names": []}
                mock_utility.has_collection.assert_called_once_with(
                    "kb_test_kb", using="default"
                )

    def test_check_schema_version_sync_old_schema(self):
        """测试 schema 版本检测 - 旧版 schema（不含新字段）"""
        client = MilvusClient()
        client._connect = MagicMock()

        # 模拟旧版 schema（不含 file_type/element_type）
        mock_field1 = MagicMock()
        mock_field1.name = "chunk_id"
        mock_field2 = MagicMock()
        mock_field2.name = "doc_id"
        mock_field3 = MagicMock()
        mock_field3.name = "content"

        mock_collection = MagicMock()
        mock_collection.schema.fields = [mock_field1, mock_field2, mock_field3]

        with patch("app.storage.milvus.utility") as mock_utility, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            mock_utility.has_collection.return_value = True

            result = client._check_schema_version_sync("test_kb")
            assert result["exists"] is True
            assert result["has_new_fields"] is False
            assert result["field_names"] == ["chunk_id", "doc_id", "content"]

    def test_check_schema_version_sync_new_schema(self):
        """测试 schema 版本检测 - 新版 schema（包含新字段）"""
        client = MilvusClient()
        client._connect = MagicMock()

        # 模拟新版 schema（包含 file_type 和 element_type）
        field_names = ["chunk_id", "doc_id", "content", "dense_vector",
                       "sparse_vector", "parent_id", "chunk_index",
                       "file_type", "element_type"]
        mock_fields = []
        for name in field_names:
            f = MagicMock()
            f.name = name
            mock_fields.append(f)

        mock_collection = MagicMock()
        mock_collection.schema.fields = mock_fields

        with patch("app.storage.milvus.utility") as mock_utility, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            mock_utility.has_collection.return_value = True

            result = client._check_schema_version_sync("test_kb")
            assert result["exists"] is True
            assert result["has_new_fields"] is True
            assert result["field_names"] == field_names

    def test_check_schema_version_sync_partial_fields(self):
        """测试 schema 版本检测 - 只有 file_type 没有 element_type"""
        client = MilvusClient()
        client._connect = MagicMock()

        # 模拟只有 file_type 但没有 element_type
        field_names = ["chunk_id", "doc_id", "content", "file_type"]
        mock_fields = []
        for name in field_names:
            f = MagicMock()
            f.name = name
            mock_fields.append(f)

        mock_collection = MagicMock()
        mock_collection.schema.fields = mock_fields

        with patch("app.storage.milvus.utility") as mock_utility, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            mock_utility.has_collection.return_value = True

            result = client._check_schema_version_sync("test_kb")
            assert result["exists"] is True
            assert result["has_new_fields"] is False
            assert result["field_names"] == field_names
