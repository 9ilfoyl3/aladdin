"""RetrievalFilter 单元测试"""

from app.retrieval.filter import RetrievalFilter


class TestRetrievalFilterToMilvusExpr:
    """测试 to_milvus_expr() 方法"""

    def test_no_filters_returns_none(self):
        """无过滤条件时返回 None"""
        f = RetrievalFilter()
        assert f.to_milvus_expr() is None

    def test_empty_lists_returns_none(self):
        """空列表等同于无过滤条件"""
        f = RetrievalFilter(doc_ids=[], file_types=[])
        assert f.to_milvus_expr() is None

    def test_single_doc_id(self):
        """单个 doc_id 过滤"""
        f = RetrievalFilter(doc_ids=["doc-123"])
        expr = f.to_milvus_expr()
        assert expr == 'doc_id in ["doc-123"]'

    def test_multiple_doc_ids(self):
        """多个 doc_id 过滤"""
        f = RetrievalFilter(doc_ids=["doc-1", "doc-2", "doc-3"])
        expr = f.to_milvus_expr()
        assert expr == 'doc_id in ["doc-1", "doc-2", "doc-3"]'

    def test_single_file_type(self):
        """单个 file_type 过滤"""
        f = RetrievalFilter(file_types=["pdf"])
        expr = f.to_milvus_expr()
        assert expr == 'file_type in ["pdf"]'

    def test_multiple_file_types(self):
        """多个 file_type 过滤"""
        f = RetrievalFilter(file_types=["pdf", "docx"])
        expr = f.to_milvus_expr()
        assert expr == 'file_type in ["pdf", "docx"]'

    def test_combined_filters(self):
        """doc_id 和 file_type 组合过滤"""
        f = RetrievalFilter(doc_ids=["doc-123", "doc-456"], file_types=["pdf"])
        expr = f.to_milvus_expr()
        assert expr == 'doc_id in ["doc-123", "doc-456"] and file_type in ["pdf"]'

    def test_doc_ids_only_with_empty_file_types(self):
        """仅 doc_ids 有值，file_types 为空列表"""
        f = RetrievalFilter(doc_ids=["doc-1"], file_types=[])
        expr = f.to_milvus_expr()
        assert expr == 'doc_id in ["doc-1"]'

    def test_file_types_only_with_none_doc_ids(self):
        """仅 file_types 有值，doc_ids 为 None"""
        f = RetrievalFilter(doc_ids=None, file_types=["md", "csv"])
        expr = f.to_milvus_expr()
        assert expr == 'file_type in ["md", "csv"]'
