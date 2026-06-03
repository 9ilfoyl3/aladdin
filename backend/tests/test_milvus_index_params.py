"""HNSW 建索引参数（efConstruction / M）可配单元测试。

覆盖 Task 10.2：
- `_build_dense_index_params(ec, m)` 的 efConstruction/M 取入参；
- 默认调用回落 efConstruction=128、M=16；
- `_create_collection_sync` 透传 ef_construction/m 到 dense `create_index`。
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# pymilvus 在测试环境可能不可用，先 mock 再导入被测模块（对齐 test_milvus.py 模式）
pymilvus_mock = MagicMock()
pymilvus_mock.DataType = MagicMock()
pymilvus_mock.DataType.VARCHAR = "VARCHAR"
pymilvus_mock.DataType.FLOAT_VECTOR = "FLOAT_VECTOR"
pymilvus_mock.DataType.SPARSE_FLOAT_VECTOR = "SPARSE_FLOAT_VECTOR"
pymilvus_mock.DataType.INT64 = "INT64"
sys.modules.setdefault("pymilvus", pymilvus_mock)

from app.storage.milvus import (  # noqa: E402
    _DEFAULT_EF_CONSTRUCTION,
    _DEFAULT_M,
    MilvusClient,
    _build_dense_index_params,
)


class TestBuildDenseIndexParams:
    """`_build_dense_index_params` 纯函数行为。"""

    def test_uses_provided_values(self):
        """efConstruction / M 取传入实参。"""
        params = _build_dense_index_params(200, 32)
        assert params["params"]["efConstruction"] == 200
        assert params["params"]["M"] == 32
        assert params["index_type"] == "HNSW"
        assert params["metric_type"] == "COSINE"

    def test_default_values(self):
        """默认调用回落 efConstruction=128、M=16。"""
        params = _build_dense_index_params()
        assert params["params"]["efConstruction"] == 128
        assert params["params"]["M"] == 16

    def test_default_constants_match_spec(self):
        """模块默认常量与 requirements 规定（128 / 16）一致。"""
        assert _DEFAULT_EF_CONSTRUCTION == 128
        assert _DEFAULT_M == 16

    @pytest.mark.parametrize(
        "ec, m",
        [(8, 4), (64, 16), (256, 32), (512, 64)],
    )
    def test_various_values_passthrough(self, ec, m):
        """多组取值下 efConstruction / M 原样落入 params。"""
        params = _build_dense_index_params(ec, m)
        assert params["params"]["efConstruction"] == ec
        assert params["params"]["M"] == m


class TestCreateCollectionSyncIndexParams:
    """`_create_collection_sync` 把 ef_construction/m 透传给 dense 索引。"""

    def _run_and_get_dense_index_params(self, ef_construction, m):
        """以 mock 执行同步建库，返回 dense create_index 收到的 index_params。"""
        client = MilvusClient()
        client._connect = MagicMock()

        mock_collection = MagicMock()

        with patch("app.storage.milvus.utility") as mock_utility, \
             patch("app.storage.milvus.Collection", return_value=mock_collection), \
             patch("app.storage.milvus.CollectionSchema"):
            # collection 尚不存在，走创建分支
            mock_utility.has_collection.return_value = False
            client._create_collection_sync(
                "test_kb", ef_construction=ef_construction, m=m,
            )

        # 找到 dense_vector 那次 create_index 调用
        for call in mock_collection.create_index.call_args_list:
            if call.kwargs.get("field_name") == "dense_vector":
                return call.kwargs["index_params"]
        raise AssertionError("未找到 dense_vector 的 create_index 调用")

    def test_passes_custom_index_params(self):
        """传入 ef_construction=200/m=32 时 dense 索引参数对应生效。"""
        params = self._run_and_get_dense_index_params(200, 32)
        assert params["params"]["efConstruction"] == 200
        assert params["params"]["M"] == 32

    def test_defaults_when_none(self):
        """ef_construction/m 为 None 时回落默认 128/16。"""
        params = self._run_and_get_dense_index_params(None, None)
        assert params["params"]["efConstruction"] == 128
        assert params["params"]["M"] == 16
