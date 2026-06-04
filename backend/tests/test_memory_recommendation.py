"""内存推荐（KB_Chunk_Cap）属性测试 + cgroup 检测单元测试

Feature: session-file-upload
Validates: Requirements 5.2, 5.3, 5.5 — Property 6

Property 6（内存推荐值的单调与保守性）:
*For any* 检测内存值 m(>0) 与活跃库数 k(≥1)，``recommend_kb_chunk_cap`` 产出的推荐值
SHALL 随 m 单调不减、随 k 单调不增，且不超过 ``m * SAFETY_FACTOR / CHUNK_BYTES / k``
（保守、不超卖）；输入异常 / 不可用时 SHALL 返回保守默认且不抛错。

单元测试覆盖 cgroup v2/v1 文件读取（'max'、超大值、有效值）与 psutil 回退路径。
"""

from __future__ import annotations

import builtins
import math
from unittest.mock import MagicMock, mock_open, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.session_upload import memory
from app.session_upload.memory import (
    _CHUNK_BYTES,
    _CGROUP_UNLIMITED_THRESHOLD,
    _CONSERVATIVE_DEFAULT_CAP,
    _DEFAULT_ACTIVE_KBS,
    _KB_CHUNK_CAP_MAX,
    _ROUND_GRANULARITY,
    _SAFETY_FACTOR,
    _read_cgroup_v1,
    _read_cgroup_v2,
    _recommended_cap_for,
    detect_memory_limit_bytes,
    recommend_kb_chunk_cap,
)

MODULE = "app.session_upload.memory"

# 用于内存字节生成的合理上界（约 1 PiB），避免极端值带来无意义的浮点误差
_MAX_MEM_BYTES = 1 << 50


# ============================================================
# 属性测试 (Property 6)
# Feature: session-file-upload, Property 6
# ============================================================


class TestProperty6MemoryRecommendation:
    """Property 6: 推荐值随内存单调不减、随活跃库数单调不增、不超过保守上界、异常降级。

    **Validates: Requirements 5.2, 5.3, 5.5 — Property 6**
    """

    @settings(max_examples=100)
    @given(
        m_low=st.integers(min_value=1, max_value=_MAX_MEM_BYTES),
        m_high=st.integers(min_value=1, max_value=_MAX_MEM_BYTES),
        k=st.integers(min_value=1, max_value=64),
    )
    def test_monotonic_non_decreasing_in_memory(self, m_low, m_high, k):
        """6a: 检测内存越大，推荐值单调不减（经公开 API，patch 内存检测）。

        **Validates: Requirements 5.3 — Property 6**
        """
        lo, hi = sorted((m_low, m_high))

        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=lo):
            cap_lo = recommend_kb_chunk_cap(k)["recommended_kb_chunk_cap"]
        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=hi):
            cap_hi = recommend_kb_chunk_cap(k)["recommended_kb_chunk_cap"]

        assert cap_lo <= cap_hi

    @settings(max_examples=100)
    @given(
        m=st.integers(min_value=1, max_value=_MAX_MEM_BYTES),
        k_low=st.integers(min_value=1, max_value=64),
        k_high=st.integers(min_value=1, max_value=64),
    )
    def test_monotonic_non_increasing_in_active_kbs(self, m, k_low, k_high):
        """6b: 活跃库数越多，推荐值单调不增。

        **Validates: Requirements 5.3 — Property 6**
        """
        lo, hi = sorted((k_low, k_high))  # lo <= hi
        cap_lo_kbs = _recommended_cap_for(m, lo)
        cap_hi_kbs = _recommended_cap_for(m, hi)
        assert cap_lo_kbs >= cap_hi_kbs

    @settings(max_examples=100)
    @given(
        m=st.integers(min_value=1, max_value=_MAX_MEM_BYTES),
        k=st.integers(min_value=1, max_value=64),
    )
    def test_never_exceeds_conservative_upper_bound(self, m, k):
        """6c: 推荐值不超过 m * SAFETY_FACTOR / CHUNK_BYTES / k（保守，不超卖）。

        **Validates: Requirements 5.3 — Property 6**
        """
        cap = _recommended_cap_for(m, k)
        # 复刻模块内的浮点运算顺序，保证比较精确
        upper = float(m) * _SAFETY_FACTOR / _CHUNK_BYTES / k
        assert cap <= upper
        # 推荐值恒为非负整数且不超过硬上界
        assert isinstance(cap, int)
        assert 0 <= cap <= _KB_CHUNK_CAP_MAX
        # 向下取整到粒度
        assert cap % _ROUND_GRANULARITY == 0

    @settings(max_examples=100)
    @given(
        bad_mem=st.one_of(
            st.none(),
            st.integers(max_value=0),
            st.just(float("nan")),
            st.just(float("inf")),
            st.just(float("-inf")),
        ),
        k=st.integers(min_value=-5, max_value=64),
    )
    def test_abnormal_memory_degrades_to_conservative_default(self, bad_mem, k):
        """6d-1: 异常内存值 → 保守默认推荐，绝不抛错。

        **Validates: Requirements 5.5 — Property 6**
        """
        cap = _recommended_cap_for(bad_mem, k)
        assert cap == _CONSERVATIVE_DEFAULT_CAP

    @settings(max_examples=100)
    @given(
        m=st.integers(min_value=1, max_value=_MAX_MEM_BYTES),
        bad_kbs=st.one_of(st.none(), st.integers(max_value=0)),
    )
    def test_abnormal_active_kbs_degrades_to_conservative_default(self, m, bad_kbs):
        """6d-2: 活跃库数 < 1 或 None → 保守默认推荐，绝不抛错。

        **Validates: Requirements 5.5 — Property 6**
        """
        cap = _recommended_cap_for(m, bad_kbs)
        assert cap == _CONSERVATIVE_DEFAULT_CAP

    @settings(max_examples=100)
    @given(detected=st.integers(min_value=0, max_value=_MAX_MEM_BYTES))
    def test_public_api_shape_and_consistency(self, detected):
        """6e: 公开 API 返回结构完整、字段一致、检测失败(0)兜底保守默认。

        **Validates: Requirements 5.2, 5.5 — Property 6**
        """
        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=detected):
            result = recommend_kb_chunk_cap()

        assert set(result) == {
            "detected_memory_gb",
            "recommended_kb_chunk_cap",
            "safety_factor",
            "active_kbs_assumption",
            "assumption",
        }
        assert result["safety_factor"] == _SAFETY_FACTOR
        assert result["active_kbs_assumption"] == _DEFAULT_ACTIVE_KBS
        # recommended 与纯函数核心一致
        assert result["recommended_kb_chunk_cap"] == _recommended_cap_for(
            detected, _DEFAULT_ACTIVE_KBS
        )
        if detected <= 0:
            assert result["detected_memory_gb"] == 0.0


# ============================================================
# 单元测试：_recommended_cap_for 边界与示例
# ============================================================


class TestRecommendedCapForExamples:
    """纯函数推荐核心的具体示例与边界。"""

    def test_typical_value(self):
        """32 GiB、2 活跃库 → 约 720 万 * 0.35 / 2，向下取整到千粒度。"""
        detected = 32 * (1024 ** 3)
        expected_raw = detected * _SAFETY_FACTOR / _CHUNK_BYTES / 2
        cap = _recommended_cap_for(detected, 2)
        assert cap == math.floor(expected_raw) - (math.floor(expected_raw) % _ROUND_GRANULARITY)
        assert cap <= expected_raw

    def test_caps_at_hard_max(self):
        """极大内存 → 推荐值封顶在 _KB_CHUNK_CAP_MAX。"""
        cap = _recommended_cap_for(1 << 50, 1)
        assert cap == _KB_CHUNK_CAP_MAX

    def test_tiny_memory_floors_to_zero(self):
        """极小内存使 raw < 粒度 → 向下取整为 0（不为负）。"""
        cap = _recommended_cap_for(1, 1)
        assert cap == 0

    def test_string_inputs_coerced(self):
        """字符串数值可被强转（int/float），不抛错。"""
        cap = _recommended_cap_for("34359738368", "2")
        assert cap > 0


# ============================================================
# 单元测试：cgroup v2 读取（'max'、超大值、有效值）
# ============================================================


class TestReadCgroupV2:
    """_read_cgroup_v2: 容器 cgroup v2 memory.max 读取。"""

    def test_valid_value(self):
        """有效字节数 → 原样返回。"""
        with patch.object(builtins, "open", mock_open(read_data="2147483648")):
            assert _read_cgroup_v2() == 2147483648

    def test_max_sentinel_returns_none(self):
        """内容为 'max'（未设限）→ None，交由上层回退。"""
        with patch.object(builtins, "open", mock_open(read_data="max")):
            assert _read_cgroup_v2() is None

    def test_oversized_value_returns_none(self):
        """超大值（≥ 未设限阈值）→ None。"""
        oversized = str(_CGROUP_UNLIMITED_THRESHOLD + 1)
        with patch.object(builtins, "open", mock_open(read_data=oversized)):
            assert _read_cgroup_v2() is None

    def test_empty_returns_none(self):
        """空内容 → None。"""
        with patch.object(builtins, "open", mock_open(read_data="")):
            assert _read_cgroup_v2() is None

    def test_non_numeric_returns_none(self):
        """非数字内容 → None。"""
        with patch.object(builtins, "open", mock_open(read_data="not-a-number")):
            assert _read_cgroup_v2() is None

    def test_zero_returns_none(self):
        """0 字节（非正）→ None。"""
        with patch.object(builtins, "open", mock_open(read_data="0")):
            assert _read_cgroup_v2() is None

    def test_file_missing_returns_none(self):
        """文件不存在（OSError）→ None。"""
        with patch.object(builtins, "open", side_effect=FileNotFoundError):
            assert _read_cgroup_v2() is None

    def test_strips_whitespace(self):
        """带换行/空白的有效值 → 正确解析。"""
        with patch.object(builtins, "open", mock_open(read_data="  4294967296\n")):
            assert _read_cgroup_v2() == 4294967296


# ============================================================
# 单元测试：cgroup v1 读取（哨兵、有效值）
# ============================================================


class TestReadCgroupV1:
    """_read_cgroup_v1: 容器 cgroup v1 memory.limit_in_bytes 读取。"""

    def test_valid_value(self):
        """有效字节数 → 原样返回。"""
        with patch.object(builtins, "open", mock_open(read_data="1073741824")):
            assert _read_cgroup_v1() == 1073741824

    def test_unlimited_sentinel_returns_none(self):
        """达到/超过未设限哨兵阈值 → None。"""
        sentinel = str(_CGROUP_UNLIMITED_THRESHOLD)
        with patch.object(builtins, "open", mock_open(read_data=sentinel)):
            assert _read_cgroup_v1() is None

    def test_real_world_v1_unlimited_sentinel(self):
        """真实 v1 未设限常见哨兵（约 9.2e18）→ None。"""
        with patch.object(builtins, "open", mock_open(read_data="9223372036854771712")):
            assert _read_cgroup_v1() is None

    def test_non_numeric_returns_none(self):
        """非数字内容 → None。"""
        with patch.object(builtins, "open", mock_open(read_data="garbage")):
            assert _read_cgroup_v1() is None

    def test_zero_returns_none(self):
        """0 字节（非正）→ None。"""
        with patch.object(builtins, "open", mock_open(read_data="0")):
            assert _read_cgroup_v1() is None

    def test_file_missing_returns_none(self):
        """文件不存在（OSError）→ None。"""
        with patch.object(builtins, "open", side_effect=FileNotFoundError):
            assert _read_cgroup_v1() is None


# ============================================================
# 单元测试：detect_memory_limit_bytes 探测顺序（v2 → v1 → psutil）
# ============================================================


class TestDetectMemoryLimitBytes:
    """detect_memory_limit_bytes: 探测优先级与安全降级。"""

    def test_v2_takes_priority(self):
        """v2 有效 → 直接返回 v2，不查 v1/psutil。"""
        with patch(f"{MODULE}._read_cgroup_v2", return_value=999) as m_v2, \
             patch(f"{MODULE}._read_cgroup_v1") as m_v1:
            assert detect_memory_limit_bytes() == 999
            m_v2.assert_called_once()
            m_v1.assert_not_called()

    def test_falls_back_to_v1(self):
        """v2 不可用、v1 有效 → 返回 v1。"""
        with patch(f"{MODULE}._read_cgroup_v2", return_value=None), \
             patch(f"{MODULE}._read_cgroup_v1", return_value=512):
            assert detect_memory_limit_bytes() == 512

    def test_falls_back_to_psutil(self):
        """v2/v1 均不可用 → 回退 psutil 物理内存。"""
        fake_psutil = MagicMock()
        fake_psutil.virtual_memory.return_value = MagicMock(total=8 * (1024 ** 3))
        with patch(f"{MODULE}._read_cgroup_v2", return_value=None), \
             patch(f"{MODULE}._read_cgroup_v1", return_value=None), \
             patch.dict("sys.modules", {"psutil": fake_psutil}):
            assert detect_memory_limit_bytes() == 8 * (1024 ** 3)

    def test_psutil_zero_total_returns_zero(self):
        """psutil 返回 total=0（异常值）→ 最终返回 0（交由推荐侧兜底）。"""
        fake_psutil = MagicMock()
        fake_psutil.virtual_memory.return_value = MagicMock(total=0)
        with patch(f"{MODULE}._read_cgroup_v2", return_value=None), \
             patch(f"{MODULE}._read_cgroup_v1", return_value=None), \
             patch.dict("sys.modules", {"psutil": fake_psutil}):
            assert detect_memory_limit_bytes() == 0

    def test_psutil_raises_returns_zero(self):
        """psutil 抛错 → 安全降级返回 0，不向上抛。"""
        fake_psutil = MagicMock()
        fake_psutil.virtual_memory.side_effect = RuntimeError("boom")
        with patch(f"{MODULE}._read_cgroup_v2", return_value=None), \
             patch(f"{MODULE}._read_cgroup_v1", return_value=None), \
             patch.dict("sys.modules", {"psutil": fake_psutil}):
            assert detect_memory_limit_bytes() == 0

    def test_all_unavailable_returns_zero(self):
        """全部检测路径不可用（含 psutil 导入失败）→ 返回 0，不抛错。"""
        import builtins as _b

        real_import = _b.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kwargs)

        with patch(f"{MODULE}._read_cgroup_v2", return_value=None), \
             patch(f"{MODULE}._read_cgroup_v1", return_value=None), \
             patch.object(_b, "__import__", side_effect=_fake_import):
            assert detect_memory_limit_bytes() == 0


# ============================================================
# 单元测试：recommend_kb_chunk_cap 在检测失败时安全降级
# ============================================================


class TestRecommendKbChunkCapDegradation:
    """recommend_kb_chunk_cap: 检测失败/异常时安全降级，不阻塞配置页。"""

    def test_detection_zero_uses_conservative_default(self):
        """检测内存为 0（全部失败）→ 推荐保守默认且 detected_memory_gb=0.0。"""
        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=0):
            result = recommend_kb_chunk_cap()
        assert result["recommended_kb_chunk_cap"] == _CONSERVATIVE_DEFAULT_CAP
        assert result["detected_memory_gb"] == 0.0

    def test_detection_raises_is_swallowed(self):
        """detect 内部异常 → recommend 兜一层，返回保守默认，不抛错。"""
        with patch(f"{MODULE}.detect_memory_limit_bytes", side_effect=RuntimeError("x")):
            result = recommend_kb_chunk_cap()
        assert result["recommended_kb_chunk_cap"] == _CONSERVATIVE_DEFAULT_CAP
        assert result["detected_memory_gb"] == 0.0

    def test_active_kbs_below_one_falls_back_in_assumption(self):
        """active_kbs < 1 → 假设字段回退默认值（不破坏返回结构）。"""
        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=16 * (1024 ** 3)):
            result = recommend_kb_chunk_cap(0)
        # 推荐核心对 active_kbs<1 直接走保守默认
        assert result["recommended_kb_chunk_cap"] == _CONSERVATIVE_DEFAULT_CAP
        assert result["active_kbs_assumption"] == _DEFAULT_ACTIVE_KBS

    def test_does_not_auto_write_config(self):
        """仅返回 dict（信息性建议），无副作用写库（Req 5.4）。"""
        with patch(f"{MODULE}.detect_memory_limit_bytes", return_value=16 * (1024 ** 3)):
            result = recommend_kb_chunk_cap()
        assert isinstance(result, dict)
        assert "recommended_kb_chunk_cap" in result
