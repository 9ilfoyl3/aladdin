"""知识库容量进度条（KBCapacityVO）核心逻辑单元测试（Task 12）。

覆盖 `_compute_capacity` 纯函数对 Req 7 的关键行为：
- 真实度量为 child chunk：total = 平台 KB_Chunk_Cap，used = 精确已用（Req 7.2）。
- percent = used / total，封顶 1.0（已满/超限同样封顶，Req 7.3）。
- approx_total_files = total // (upload_max_file_mb × CHUNK_DENSITY)，向下取整（Req 7.4）。
- approx_used_files = 已传文档数（精确，Req 7.4）。
- 边界与异常输入（零分母、负值）安全兜底不抛错。

Feature: session-file-upload
"""

import os
import sys
from unittest.mock import MagicMock

# 避免导入 app 时 pymilvus / settings 启动期失败（沿用现有测试模式）。
os.environ.setdefault("JWT_SECRET", "kb-capacity-test-secret-0123456789abcdef")
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.api.knowledge_base import CHUNK_DENSITY, KBCapacityVO, _compute_capacity  # noqa: E402


def test_chunk_density_is_conservative_constant():
    """CHUNK_DENSITY 为约 300 chunk/MB 的保守常量（Req 7.4）。"""
    assert CHUNK_DENSITY == 300


def test_basic_capacity_half_used():
    """已用为上限一半时 percent=0.5；文件数按密度向下取整。"""
    vo = _compute_capacity(
        used_chunks=500_000, used_files=10, kb_chunk_cap=1_000_000, upload_max_file_mb=10
    )
    assert isinstance(vo, KBCapacityVO)
    assert vo.used_chunks == 500_000
    assert vo.total_chunks == 1_000_000
    assert vo.percent == 0.5
    # 1_000_000 // (10 * 300) = 1_000_000 // 3000 = 333
    assert vo.approx_total_files == 333
    assert vo.approx_used_files == 10


def test_percent_capped_at_one_when_full():
    """已用恰好达上限时 percent=1.0（已满，Req 7.3）。"""
    vo = _compute_capacity(
        used_chunks=1_000_000, used_files=50, kb_chunk_cap=1_000_000, upload_max_file_mb=10
    )
    assert vo.percent == 1.0


def test_percent_capped_at_one_when_over_limit():
    """已用超过上限时 percent 仍封顶 1.0（不超过 1，Req 7.3）。"""
    vo = _compute_capacity(
        used_chunks=2_500_000, used_files=999, kb_chunk_cap=1_000_000, upload_max_file_mb=10
    )
    assert vo.percent == 1.0
    assert vo.used_chunks == 2_500_000  # used 仍如实透出


def test_empty_kb_percent_zero():
    """空库 used=0 → percent=0；文件数仍按上限估算。"""
    vo = _compute_capacity(
        used_chunks=0, used_files=0, kb_chunk_cap=1_000_000, upload_max_file_mb=20
    )
    assert vo.percent == 0.0
    assert vo.approx_used_files == 0
    # 1_000_000 // (20 * 300) = 1_000_000 // 6000 = 166
    assert vo.approx_total_files == 166


def test_approx_total_files_floor():
    """approx_total_files 向下取整（Req 7.4），不进位。"""
    # 100_000 // (1 * 300) = 333 (333.33 向下取整)
    vo = _compute_capacity(
        used_chunks=0, used_files=0, kb_chunk_cap=100_000, upload_max_file_mb=1
    )
    assert vo.approx_total_files == 333


def test_zero_total_chunks_no_div_by_zero():
    """分母为 0 时 percent 安全记 0，不抛除零错误。"""
    vo = _compute_capacity(
        used_chunks=100, used_files=1, kb_chunk_cap=0, upload_max_file_mb=10
    )
    assert vo.percent == 0.0
    assert vo.total_chunks == 0
    assert vo.approx_total_files == 0


def test_zero_upload_mb_no_div_by_zero():
    """单文件估算 chunk 为 0（upload_mb=0）时 approx_total_files 记 0，不抛错。"""
    vo = _compute_capacity(
        used_chunks=100, used_files=1, kb_chunk_cap=1_000_000, upload_max_file_mb=0
    )
    assert vo.approx_total_files == 0


def test_negative_inputs_clamped_to_non_negative():
    """负值输入按非负兜底，保证输出稳定不抛错。"""
    vo = _compute_capacity(
        used_chunks=-5, used_files=-3, kb_chunk_cap=-10, upload_max_file_mb=-1
    )
    assert vo.used_chunks == 0
    assert vo.total_chunks == 0
    assert vo.approx_used_files == 0
    assert vo.approx_total_files == 0
    assert vo.percent == 0.0


def test_percent_within_unit_interval_for_various_inputs():
    """percent 恒落在 [0, 1] 区间（Req 7.3 的封顶语义）。"""
    for used, total in [(0, 100), (50, 100), (100, 100), (200, 100), (1, 3)]:
        vo = _compute_capacity(
            used_chunks=used, used_files=0, kb_chunk_cap=total, upload_max_file_mb=10
        )
        assert 0.0 <= vo.percent <= 1.0
