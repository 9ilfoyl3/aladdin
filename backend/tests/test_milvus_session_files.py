"""会话文件库 schema / 过滤的单元测试（Task 4.1）

覆盖 design.md C6 + tasks.md Task 4 的会话文件库能力（使用 mock，不需要真实 Milvus）：
- ensure 幂等：`_ensure_session_files_collection_sync` 在 collection 已存在时跳过重建；
- schema 隔离：会话库 schema（`_SESSION_FIELDS`）含 `session_id` 字段，正式库 schema（`_FIELDS`）不含；
- 会话库建表用 `_SESSION_FIELDS` 并为 `session_id` 建标量索引 `idx_session_id`；
- 按 session_id 删除：`_delete_session_sync` 用 expr `session_id == "{sid}"` 仅影响该会话；
- 检索 expr 注入正确：三路 `_search_*_sync` 把 `session_id == "{sid}"` 透传给 `collection.search`，
  且跨会话（用别的 session_id 的 expr）查不到本会话向量。

沿用现有 milvus 测试的 mock 模式（patch `app.storage.milvus` 的 `utility`/`Collection`/`CollectionSchema`）。

Feature: session-file-upload
_Requirements: 1.7, 1.11_
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# pymilvus 已在 artoo env 安装（3.0.0）。优先使用真实 pymilvus，使 _FIELDS/_SESSION_FIELDS
# 的字段名解析为真实字符串；仅当导入失败时回退到 mock（对齐 test_milvus 系列的容错模式）。
# 注意：本特性的行为断言（建表/删除/检索 expr）均通过 patch Collection/utility 完成，
# 不依赖字段名是否为真实字符串。
try:
    import pymilvus  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules.setdefault("pymilvus", MagicMock())

from app.storage.milvus import (  # noqa: E402
    SESSION_FILES_KB_ID,
    MilvusClient,
    _FIELDS,
    _SESSION_FIELDS,
    build_session_id_expr,
)

# 当整体测试套件先行运行 test_milvus.py（其将 pymilvus 全局替换为 MagicMock）时，
# 此处 FieldSchema().name 会是 MagicMock 而非字符串。精确字段名断言据此守卫，
# 结构性/行为性断言不受影响（始终运行）。
_NAMES_REAL = all(isinstance(f.name, str) for f in _FIELDS)


# ============================================================
# Schema 隔离：会话库含 session_id 字段、正式库不含（Req 1.7）
# ============================================================


class TestSessionSchemaIsolation:
    """`_SESSION_FIELDS` 与 `_FIELDS` 的字段差异（结构性，mock 无关）。"""

    def test_session_fields_extend_regular_fields_by_exactly_one(self):
        """`_SESSION_FIELDS` = `_FIELDS` + [额外一个字段]：前 N 个为同一批字段，仅多 1 个。

        长度差与前缀同一性在真实/被 mock 的 pymilvus 下都成立（mock 无关）。
        """
        assert len(_SESSION_FIELDS) == len(_FIELDS) + 1
        # 前 len(_FIELDS) 个是同一批 FieldSchema 对象（实现为 _FIELDS + [FieldSchema(session_id)]）
        assert _SESSION_FIELDS[: len(_FIELDS)] == _FIELDS

    @pytest.mark.skipif(not _NAMES_REAL, reason="pymilvus 被全局 mock，字段名非真实字符串")
    def test_session_fields_add_only_session_id(self):
        """会话库相对正式库恰好新增 session_id 一个字段名（真实 pymilvus 下精确校验）。"""
        reg = [f.name for f in _FIELDS]
        sess = [f.name for f in _SESSION_FIELDS]
        assert set(sess) - set(reg) == {"session_id"}

    @pytest.mark.skipif(not _NAMES_REAL, reason="pymilvus 被全局 mock，字段名非真实字符串")
    def test_session_fields_contain_session_id(self):
        """会话库 schema 含 session_id 标量字段（真实 pymilvus 下精确校验字段名）。"""
        assert "session_id" in [f.name for f in _SESSION_FIELDS]

    @pytest.mark.skipif(not _NAMES_REAL, reason="pymilvus 被全局 mock，字段名非真实字符串")
    def test_regular_fields_exclude_session_id(self):
        """正式库 schema 不含 session_id（不被迫加该字段）。"""
        assert "session_id" not in [f.name for f in _FIELDS]

    @pytest.mark.skipif(not _NAMES_REAL, reason="pymilvus 被全局 mock，字段名非真实字符串")
    def test_session_id_field_is_varchar64(self):
        """session_id 字段为 VARCHAR(64)，与 doc_id/chunk_id 量级一致。"""
        field = next(f for f in _SESSION_FIELDS if f.name == "session_id")
        max_len = getattr(field, "params", {}).get("max_length")
        assert max_len == 64

    def test_session_files_kb_id_resolves_to_physical_collection(self):
        """逻辑 kb_id "session_files" 经 _collection_name 解析为物理 collection "kb_session_files"。"""
        assert SESSION_FILES_KB_ID == "session_files"
        assert MilvusClient._collection_name(SESSION_FILES_KB_ID) == "kb_session_files"


# ============================================================
# ensure 幂等 + 用 _SESSION_FIELDS 建表（Req 1.7）
# ============================================================


class TestEnsureSessionFilesCollection:
    """`_ensure_session_files_collection_sync` 的幂等性与建表 schema。"""

    def test_skips_when_collection_exists(self):
        """已存在则跳过：不构造 Collection、不建索引（幂等）。"""
        client = MilvusClient()
        client._connect = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection") as coll_cls:
            util.has_collection.return_value = True

            client._ensure_session_files_collection_sync()

            coll_cls.assert_not_called()

    def test_checks_correct_physical_collection_name(self):
        """幂等判定查询的是物理 collection 名 kb_session_files。"""
        client = MilvusClient()
        client._connect = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection"):
            util.has_collection.return_value = True

            client._ensure_session_files_collection_sync()

            util.has_collection.assert_called_once_with("kb_session_files", using="default")

    def test_creates_with_session_schema_when_absent(self):
        """不存在时用含 session_id 的 `_SESSION_FIELDS` 建表。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection), \
             patch("app.storage.milvus.CollectionSchema") as schema_cls:
            util.has_collection.return_value = False

            client._ensure_session_files_collection_sync()

            # 结构性断言（mock 无关）：传入的就是 _SESSION_FIELDS 这批字段（含额外 session_id 字段）
            fields = schema_cls.call_args.kwargs["fields"]
            assert fields == _SESSION_FIELDS
            assert len(fields) == len(_FIELDS) + 1

    def test_creates_session_id_scalar_index(self):
        """建表时为 session_id 建标量索引 idx_session_id（加速会话级 expr 过滤）。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection), \
             patch("app.storage.milvus.CollectionSchema"):
            util.has_collection.return_value = False

            client._ensure_session_files_collection_sync()

            index_calls = {
                c.kwargs.get("field_name"): c.kwargs
                for c in mock_collection.create_index.call_args_list
            }
            assert "session_id" in index_calls
            assert index_calls["session_id"]["index_name"] == "idx_session_id"

    def test_idempotent_second_call_skips_creation(self):
        """连续两次调用：首次（不存在）建表，二次（已存在）跳过——只建一次。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection) as coll_cls, \
             patch("app.storage.milvus.CollectionSchema"):
            util.has_collection.side_effect = [False, True]

            client._ensure_session_files_collection_sync()  # 建表
            client._ensure_session_files_collection_sync()  # 跳过

            assert coll_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_async_wrapper_delegates_to_sync(self):
        """async ensure_session_files_collection 委托同步实现。"""
        client = MilvusClient()
        client._ensure_session_files_collection_sync = MagicMock()

        await client.ensure_session_files_collection()

        client._ensure_session_files_collection_sync.assert_called_once_with()


class TestRegularCollectionSchemaUnchanged:
    """正式库建表路径仍用不含 session_id 的 `_FIELDS`（schema 隔离）。"""

    def test_regular_create_collection_excludes_session_id(self):
        """`_create_collection_sync` 用 `_FIELDS` 建表，不含 session_id。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection), \
             patch("app.storage.milvus.CollectionSchema") as schema_cls:
            util.has_collection.return_value = False

            client._create_collection_sync("test_kb")

            # 结构性断言（mock 无关）：正式库用 _FIELDS（不含会话库的额外 session_id 字段）
            fields = schema_cls.call_args.kwargs["fields"]
            assert fields == _FIELDS
            assert len(fields) == len(_SESSION_FIELDS) - 1


# ============================================================
# 按 session_id 删除：只影响该会话（Req 1.6 / 1.11）
# ============================================================


class TestDeleteSession:
    """`_delete_session_sync` 的 expr 注入与作用域。"""

    def test_delete_uses_session_id_expr(self):
        """按 session_id 删除：expr 恰为 `session_id == "{sid}"`，并 flush + 清加载标记。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        name = "kb_session_files"
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            util.has_collection.return_value = True
            client._loaded_at[name] = 123.0  # 预置加载标记

            client._delete_session_sync("sess-A")

            mock_collection.delete.assert_called_once_with('session_id == "sess-A"')
            mock_collection.flush.assert_called_once()
            assert name not in client._loaded_at  # 删后清标记，下次搜索强制重 load

    def test_delete_only_targets_given_session(self):
        """不同 session 生成各自的 expr，互不影响（不会误删其他会话）。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            util.has_collection.return_value = True

            client._delete_session_sync("sess-B")

            expr = mock_collection.delete.call_args.args[0]
            assert expr == 'session_id == "sess-B"'
            assert "sess-A" not in expr  # 删 B 时绝不波及 A

    def test_delete_noop_when_collection_absent(self):
        """会话文件库尚不存在时直接返回，不调用 delete。"""
        client = MilvusClient()
        client._connect = MagicMock()
        mock_collection = MagicMock()
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=mock_collection):
            util.has_collection.return_value = False

            client._delete_session_sync("sess-A")

            mock_collection.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_wrapper_delegates_to_sync(self):
        """async delete_session 委托同步实现。"""
        client = MilvusClient()
        client._delete_session_sync = MagicMock()

        await client.delete_session("sess-A")

        client._delete_session_sync.assert_called_once_with("sess-A")


# ============================================================
# 检索 expr 注入正确 + 跨会话查不到（Req 1.11）
# ============================================================


def _make_hit(session_label: str) -> MagicMock:
    """构造一条 mock 命中（携带可识别的 doc_id 以校验来源）。"""
    hit = MagicMock()
    payload = {
        "chunk_id": f"chk_{session_label}",
        "doc_id": f"doc_{session_label}",
        "content": f"content_{session_label}",
        "parent_id": "",
        "chunk_index": 0,
        "file_type": "pdf",
        "element_type": "text",
    }
    hit.entity.get = lambda field, _p=payload: _p.get(field)
    hit.score = 0.9
    return hit


def _session_scoped_search(stored_session_id: str, hits: list):
    """模拟共享 collection：仅当搜索 expr 命中 stored_session_id 时返回数据，否则空。

    用于验证"靠 session_id expr 过滤天然隔离各会话"——别的会话的 expr 查不到本会话向量。
    """
    def _search(**kwargs):
        if kwargs.get("expr") == f'session_id == "{stored_session_id}"':
            return [hits]
        return [[]]
    return _search


class TestSessionSearchExprInjection:
    """三路检索把 session_id expr 透传给 `collection.search` 并实现跨会话隔离。"""

    def test_dense_search_injects_session_expr(self):
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        collection.search.return_value = [[]]
        with patch("app.storage.milvus.Collection", return_value=collection):
            client._search_dense_sync(
                "session_files", [0.1] * 8, top_k=5, expr='session_id == "sess-A"',
            )

            assert collection.search.call_args.kwargs["expr"] == 'session_id == "sess-A"'

    def test_sparse_search_injects_session_expr(self):
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        collection.search.return_value = [[]]
        with patch("app.storage.milvus.Collection", return_value=collection):
            client._search_sparse_sync(
                "session_files", {1: 0.5}, top_k=5, expr='session_id == "sess-A"',
            )

            assert collection.search.call_args.kwargs["expr"] == 'session_id == "sess-A"'

    def test_bm25_search_injects_session_expr(self):
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        bm25_field = MagicMock()
        bm25_field.name = "bm25_vector"
        collection.schema.fields = [bm25_field]
        collection.search.return_value = [[]]
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True

            client._search_bm25_sync(
                "session_files", "查询", top_k=5, expr='session_id == "sess-A"',
            )

            assert collection.search.call_args.kwargs["expr"] == 'session_id == "sess-A"'

    def test_dense_cross_session_returns_empty(self):
        """共享库只存 sess-A 数据：用 sess-A expr 能查到，用 sess-B expr 查不到。"""
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        collection.search.side_effect = _session_scoped_search("sess-A", [_make_hit("A")])
        with patch("app.storage.milvus.Collection", return_value=collection):
            own = client._search_dense_sync(
                "session_files", [0.1] * 8, top_k=5, expr='session_id == "sess-A"',
            )
            cross = client._search_dense_sync(
                "session_files", [0.1] * 8, top_k=5, expr='session_id == "sess-B"',
            )

            assert len(own) == 1
            assert own[0]["doc_id"] == "doc_A"
            assert cross == []

    def test_sparse_cross_session_returns_empty(self):
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        collection.search.side_effect = _session_scoped_search("sess-A", [_make_hit("A")])
        with patch("app.storage.milvus.Collection", return_value=collection):
            own = client._search_sparse_sync(
                "session_files", {1: 0.5}, top_k=5, expr='session_id == "sess-A"',
            )
            cross = client._search_sparse_sync(
                "session_files", {1: 0.5}, top_k=5, expr='session_id == "sess-B"',
            )

            assert len(own) == 1
            assert own[0]["doc_id"] == "doc_A"
            assert cross == []

    def test_bm25_cross_session_returns_empty(self):
        client = MilvusClient()
        client._connect = MagicMock()
        collection = MagicMock()
        bm25_field = MagicMock()
        bm25_field.name = "bm25_vector"
        collection.schema.fields = [bm25_field]
        collection.search.side_effect = _session_scoped_search("sess-A", [_make_hit("A")])
        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True

            own = client._search_bm25_sync(
                "session_files", "查询", top_k=5, expr='session_id == "sess-A"',
            )
            cross = client._search_bm25_sync(
                "session_files", "查询", top_k=5, expr='session_id == "sess-B"',
            )

            assert len(own) == 1
            assert own[0]["doc_id"] == "doc_A"
            assert cross == []


# ============================================================
# build_session_id_expr：会话隔离 expr 的纵深防御校验（加固点 1）
# ============================================================


class TestBuildSessionIdExpr:
    """``build_session_id_expr`` 对 session_id 做 UUID 字符集白名单校验后再拼 expr。

    生产中 session_id 恒为服务端 UUID 且经 _verify_session_owner 校验，本不可注入；
    本函数作为纵深防御，拒绝含引号 / 空格 / 布尔运算符等的非法输入，杜绝 expr 注入。
    """

    def test_valid_uuid_builds_expr(self):
        """合法 UUID → 正确拼出 `session_id == "<uuid>"`。"""
        import uuid

        sid = str(uuid.uuid4())
        assert build_session_id_expr(sid) == f'session_id == "{sid}"'

    def test_hex_and_hyphen_allowed(self):
        """十六进制 + 连字符字符集放行（覆盖 UUID 全字符集）。"""
        assert build_session_id_expr("abc-123-DEF") == 'session_id == "abc-123-DEF"'

    @pytest.mark.parametrize(
        "malicious",
        [
            'x" or "1"=="1',          # 引号闭合 + 布尔注入
            "sess or session_id != \"\"",  # 空格 + 运算符
            'a"; drop',               # 引号 + 分号
            "a b",                    # 空格
            "中文",                    # 非 ASCII
            "a'b",                    # 单引号
            "",                       # 空串
        ],
    )
    def test_injection_attempts_rejected(self, malicious):
        """任何含 UUID 字符集以外字符的输入 → ValueError（拒绝注入）。"""
        with pytest.raises(ValueError):
            build_session_id_expr(malicious)
