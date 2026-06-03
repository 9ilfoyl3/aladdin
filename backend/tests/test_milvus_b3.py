"""B3 跳过重复 load + 时间戳 + TTL + not-loaded 重试单元/属性测试（Task 18）

覆盖：
- 18.3 属性测试 P10：TTL 跳过/过期的加载判定（直接测 `_ensure_loaded`，
  用受控时钟 + 模型对照 load 调用次数增量）。
- 18.4 B3 有状态行为单元测试：首次 load + 记时间戳、TTL 内跳过、TTL 过期重 load、
  ttl=0 每次 load、写/删清标记、not-loaded 清标记+重 load+重试一次、二次失败上抛、
  非 not-loaded 错误直接上抛、无 LRU/容量上限逻辑。

用 `sys.modules.setdefault("pymilvus", MagicMock())` 规避 pymilvus 导入依赖（沿用现有测试模式）。
受控时钟通过 patch `app.storage.milvus.time` 注入，使 `time.monotonic()` 可推进。

Feature: kb-retrieval-optimization
"""

import sys
from unittest.mock import MagicMock, patch

# pymilvus 在测试环境可能不可用，先 mock 再导入被测模块。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.storage.milvus import MilvusClient  # noqa: E402


class _Clock:
    """受控单调时钟：monotonic() 返回当前值，advance(dt) 向前推进。"""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ============================================================
# 18.3 属性测试 P10：TTL 跳过/过期的加载判定
# ============================================================


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    ttl=st.integers(min_value=0, max_value=100),
    ops=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=200),  # dt：时间推进量
            st.booleans(),  # 是否在本步触发 pop（模拟写/删/not-loaded 清标记）
        ),
        min_size=1,
        max_size=30,
    ),
)
def test_property_ttl_load_decision(ttl, ops):
    """Feature: kb-retrieval-optimization, Property 10: TTL 跳过/过期的加载判定

    For any 调用序列与 ttl>=0、时间戳推进 dt：`_ensure_loaded` SHALL 满足——
    - 首次（无标记）必 load 并记时间戳；
    - ttl>0 且距上次 load 的间隔 < ttl 时跳过 load；
    - 间隔 >= ttl 时重新 load 并刷新时间戳；
    - ttl=0 时每次必 load；
    - 任一 pop(name)（写/删/not-loaded）后下次必 load。

    用一个对照"模型"（跟踪 loaded / last_load_time）预测每步是否应 load，
    与被测 `_ensure_loaded` 实际 collection.load 调用次数增量逐步比对。

    Validates: Requirements 15.1, 15.2, 15.3, 15.5
    """
    name = "kb_test"
    clock = _Clock()

    with patch("app.storage.milvus.time") as mock_time:
        mock_time.monotonic = clock.monotonic

        client = MilvusClient()
        collection = MagicMock()

        # 对照模型状态
        model_loaded = False
        model_last_load = None
        expected_load_calls = 0

        for dt, do_pop in ops:
            clock.advance(dt)

            if do_pop:
                client._loaded_at.pop(name, None)
                model_loaded = False

            now = clock.now
            # 预测本步是否应 load
            if ttl > 0 and model_loaded and (now - model_last_load) < ttl:
                should_load = False
            else:
                should_load = True

            client._ensure_loaded(name, collection, ttl)

            if should_load:
                expected_load_calls += 1
                model_loaded = True
                model_last_load = now
                # load 后必记录当前时间戳
                assert client._loaded_at.get(name) == now

            assert collection.load.call_count == expected_load_calls


# ============================================================
# 18.4 _ensure_loaded 有状态行为（确定性示例）
# ============================================================


class TestEnsureLoadedStateful:
    """`_ensure_loaded` 的跳过/过期/ttl=0/pop 行为。"""

    def test_first_call_loads_and_records_timestamp(self):
        """首次（无标记）必 load 并记录当前 monotonic 时间戳。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)

            assert collection.load.call_count == 1
            assert client._loaded_at["kb_a"] == clock.now

    def test_skip_load_within_ttl(self):
        """TTL 内第二次 _ensure_loaded 跳过 load。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)
            clock.advance(10)  # < 30
            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)

            assert collection.load.call_count == 1  # 跳过第二次

    def test_reload_after_ttl_expired(self):
        """TTL 过期后重新 load 并刷新时间戳。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)
            clock.advance(30)  # 恰好 >= ttl → 过期
            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)

            assert collection.load.call_count == 2
            assert client._loaded_at["kb_a"] == clock.now

    def test_ttl_zero_loads_every_time(self):
        """ttl=0 时每次必 load（关闭跳过优化，Req 15.5）。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            for _ in range(5):
                client._ensure_loaded("kb_a", collection, load_cache_ttl=0)

            assert collection.load.call_count == 5

    def test_pop_forces_reload(self):
        """pop(name) 后下次必 load（即使仍在 TTL 内）。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)
            client._loaded_at.pop("kb_a", None)  # 清标记
            clock.advance(1)  # 仍在 TTL 内
            client._ensure_loaded("kb_a", collection, load_cache_ttl=30)

            assert collection.load.call_count == 2

    def test_no_lru_eviction(self):
        """仅 dict 标记，无 LRU/容量上限驱逐：大量 collection 标记全部保留。"""
        clock = _Clock()
        with patch("app.storage.milvus.time") as mt:
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            collection = MagicMock()

            for i in range(1000):
                client._ensure_loaded(f"kb_{i}", collection, load_cache_ttl=100)

            assert len(client._loaded_at) == 1000  # 无驱逐


# ============================================================
# 18.4 _is_not_loaded_error 判定
# ============================================================


class TestIsNotLoadedError:
    """`_is_not_loaded_error` 文本匹配。"""

    @pytest.mark.parametrize(
        "msg",
        [
            "collection not loaded",
            "Collection NOT LOADED",
            "collection has not been loaded",
            "error: collection not loaded yet",
        ],
    )
    def test_detects_not_loaded(self, msg):
        assert MilvusClient._is_not_loaded_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        ["timeout", "invalid parameter", "connection refused", "type mismatch"],
    )
    def test_other_errors_not_matched(self, msg):
        assert MilvusClient._is_not_loaded_error(Exception(msg)) is False


# ============================================================
# 18.4 _search_dense_sync 有状态行为（首查 load、TTL 内跳过、not-loaded 重试）
# ============================================================


def _make_client_with_collection(collection):
    """构造 MilvusClient，mock 掉连接，patch Collection 返回给定 mock。"""
    client = MilvusClient()
    client._connect = MagicMock()
    return client


class TestSearchDenseLoadCache:
    """`_search_dense_sync` 与 Collection_Load_Cache 的联动。"""

    def test_first_search_loads_within_ttl_skips(self):
        """首次搜索触发 load；TTL 内二次搜索跳过 load。"""
        clock = _Clock()
        collection = MagicMock()
        collection.search.return_value = [[]]

        with patch("app.storage.milvus.time") as mt, \
             patch("app.storage.milvus.Collection", return_value=collection):
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            client._connect = MagicMock()

            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 1

            clock.advance(5)  # 仍在 TTL 内
            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 1  # 跳过

            clock.advance(30)  # TTL 过期
            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 2  # 重 load

    def test_ttl_zero_loads_each_search(self):
        """ttl=0（默认）时每次搜索都 load。"""
        collection = MagicMock()
        collection.search.return_value = [[]]

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()

            client._search_dense_sync("kb", [0.1] * 8, top_k=5)  # 默认 ttl=0
            client._search_dense_sync("kb", [0.1] * 8, top_k=5)

            assert collection.load.call_count == 2

    def test_not_loaded_retry_succeeds(self):
        """搜索首次抛 not-loaded → 清标记 + load + 重试一次，最终成功。"""
        clock = _Clock()
        collection = MagicMock()
        collection.search.side_effect = [Exception("collection not loaded"), [[]]]

        with patch("app.storage.milvus.time") as mt, \
             patch("app.storage.milvus.Collection", return_value=collection):
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            client._connect = MagicMock()

            result = client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)

            assert result == []
            # search 调用两次（首次失败 + 重试成功）
            assert collection.search.call_count == 2
            # load 调用两次：首次 _ensure_loaded（无标记）+ not-loaded 重试 load
            assert collection.load.call_count == 2
            # 重试后标记被刷新
            assert client._loaded_at["kb_kb"] == clock.now

    def test_not_loaded_retry_skips_load_when_marked(self):
        """已有标记（TTL 内跳过首 load）时遇 not-loaded：清标记 + load + 重试。"""
        clock = _Clock()
        collection = MagicMock()
        # 首次搜索成功（建立标记），第二次搜索首抛 not-loaded 再成功
        collection.search.side_effect = [[[]], Exception("not loaded"), [[]]]

        with patch("app.storage.milvus.time") as mt, \
             patch("app.storage.milvus.Collection", return_value=collection):
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            client._connect = MagicMock()

            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 1  # 首次 ensure_loaded

            clock.advance(1)  # TTL 内 → 第二次搜索跳过 ensure_loaded 的 load
            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)

            # 第二次搜索：ensure_loaded 跳过（标记有效），search 抛 not-loaded → load 重试
            assert collection.load.call_count == 2
            assert collection.search.call_count == 3

    def test_non_not_loaded_error_propagates_no_retry(self):
        """非 not-loaded 错误直接上抛，不触发重试。"""
        collection = MagicMock()
        collection.search.side_effect = RuntimeError("some other error")

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()

            with pytest.raises(RuntimeError, match="some other error"):
                client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)

            assert collection.search.call_count == 1  # 不重试

    def test_not_loaded_retry_fails_again_propagates(self):
        """重试后仍 not-loaded → 异常自然上抛（最多重试一次）。"""
        collection = MagicMock()
        collection.search.side_effect = [
            Exception("collection not loaded"),
            Exception("collection not loaded"),
        ]

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()

            with pytest.raises(Exception, match="not loaded"):
                client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)

            assert collection.search.call_count == 2  # 仅重试一次


# ============================================================
# 18.4 _search_sparse_sync not-loaded 重试
# ============================================================


class TestSearchSparseLoadCache:
    def test_not_loaded_retry_succeeds(self):
        """sparse 搜索首次抛 not-loaded → 重载重试一次成功。"""
        collection = MagicMock()
        collection.search.side_effect = [Exception("collection not loaded"), [[]]]

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()

            result = client._search_sparse_sync("kb", {1: 0.5}, top_k=5, load_cache_ttl=30)

            assert result == []
            assert collection.search.call_count == 2
            assert collection.load.call_count == 2

    def test_non_not_loaded_error_propagates(self):
        """sparse 非 not-loaded 错误直接上抛。"""
        collection = MagicMock()
        collection.search.side_effect = RuntimeError("boom")

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()

            with pytest.raises(RuntimeError, match="boom"):
                client._search_sparse_sync("kb", {1: 0.5}, top_k=5, load_cache_ttl=30)

            assert collection.search.call_count == 1


# ============================================================
# 18.4 _search_bm25_sync not-loaded 内层重试 + 容错语义
# ============================================================


class TestSearchBm25LoadCache:
    def _make_collection_with_bm25(self):
        collection = MagicMock()
        f = MagicMock()
        f.name = "bm25_vector"
        collection.schema.fields = [f]
        return collection

    def test_not_loaded_retry_succeeds(self):
        """bm25 搜索首次抛 not-loaded → 内层先重载重试一次成功。"""
        collection = self._make_collection_with_bm25()
        collection.search.side_effect = [Exception("collection not loaded"), [[]]]

        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True
            client = MilvusClient()
            client._connect = MagicMock()

            result = client._search_bm25_sync("kb", "查询", top_k=5, load_cache_ttl=30)

            assert result == []
            assert collection.search.call_count == 2  # 重试一次
            assert collection.load.call_count == 2

    def test_not_loaded_retry_fails_returns_empty(self):
        """bm25 重试仍失败 → 沿用容错语义返回 []（不抛）。"""
        collection = self._make_collection_with_bm25()
        collection.search.side_effect = [
            Exception("collection not loaded"),
            Exception("collection not loaded"),
        ]

        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True
            client = MilvusClient()
            client._connect = MagicMock()

            result = client._search_bm25_sync("kb", "查询", top_k=5, load_cache_ttl=30)

            assert result == []
            assert collection.search.call_count == 2

    def test_other_error_returns_empty(self):
        """bm25 其它异常沿用现有容错：记 WARNING 返回 []（bm25 可选路）。"""
        collection = self._make_collection_with_bm25()
        collection.search.side_effect = RuntimeError("schema mismatch")

        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True
            client = MilvusClient()
            client._connect = MagicMock()

            result = client._search_bm25_sync("kb", "查询", top_k=5, load_cache_ttl=30)

            assert result == []
            assert collection.search.call_count == 1  # 非 not-loaded 不重试


# ============================================================
# 18.4 写/删清标记
# ============================================================


class TestWriteDeleteClearsMarker:
    def test_insert_clears_marker(self):
        """_insert_sync 成功后清除该 collection 的加载标记。"""
        collection = MagicMock()
        collection.insert.return_value = MagicMock(insert_count=3)
        name = MilvusClient._collection_name("kb")

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()
            client._loaded_at[name] = 123.0  # 预置标记

            count = client._insert_sync("kb", [{"chunk_id": "c1", "content": "x"}])

            assert count == 3
            assert name not in client._loaded_at  # 标记被清

    def test_delete_clears_marker(self):
        """_delete_sync 成功后清除加载标记。"""
        collection = MagicMock()
        name = MilvusClient._collection_name("kb")

        with patch("app.storage.milvus.Collection", return_value=collection):
            client = MilvusClient()
            client._connect = MagicMock()
            client._loaded_at[name] = 123.0

            client._delete_sync("kb", ["c1", "c2"])

            assert name not in client._loaded_at

    def test_delete_by_doc_id_clears_marker(self):
        """_delete_by_doc_id_sync 成功后清除加载标记。"""
        collection = MagicMock()
        name = MilvusClient._collection_name("kb")

        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True
            client = MilvusClient()
            client._connect = MagicMock()
            client._loaded_at[name] = 123.0

            client._delete_by_doc_id_sync("kb", "doc-1")

            assert name not in client._loaded_at

    def test_delete_by_doc_ids_clears_marker(self):
        """_delete_by_doc_ids_sync 成功后清除加载标记。"""
        collection = MagicMock()
        name = MilvusClient._collection_name("kb")

        with patch("app.storage.milvus.utility") as util, \
             patch("app.storage.milvus.Collection", return_value=collection):
            util.has_collection.return_value = True
            client = MilvusClient()
            client._connect = MagicMock()
            client._loaded_at[name] = 123.0

            client._delete_by_doc_ids_sync("kb", ["doc-1", "doc-2"])

            assert name not in client._loaded_at

    def test_delete_then_search_reloads(self):
        """删除清标记后，TTL 内的下次搜索仍强制重 load。"""
        clock = _Clock()
        collection = MagicMock()
        collection.search.return_value = [[]]

        with patch("app.storage.milvus.time") as mt, \
             patch("app.storage.milvus.Collection", return_value=collection):
            mt.monotonic = clock.monotonic
            client = MilvusClient()
            client._connect = MagicMock()

            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 1

            client._delete_sync("kb", ["c1"])  # 清标记

            clock.advance(1)  # 仍在 TTL 内
            client._search_dense_sync("kb", [0.1] * 8, top_k=5, load_cache_ttl=30)
            assert collection.load.call_count == 2  # 标记被清 → 重 load
