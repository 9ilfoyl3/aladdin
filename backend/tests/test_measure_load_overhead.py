"""measure_load_overhead 逻辑单元测试（任务 13.1，可选）

验证一次性测量脚本的纯逻辑，不连真实 Milvus：
- 丢弃首次冷调用，取后续样本中位数作为 Load_Overhead
- 判定分支：>50ms 采纳 / <5ms 不采纳 / 5~50ms 灰色区间
- n < 2 抛 ValueError

参考 test_milvus_ef.py 的 pymilvus mock 模式。
"""

import sys
from unittest.mock import MagicMock, patch

# 模拟 pymilvus 模块以避免导入依赖问题
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

from app.scripts.measure_load_overhead import (  # noqa: E402
    _RECOMMEND_ADOPT,
    _RECOMMEND_GRAY,
    _RECOMMEND_REJECT,
    _recommendation_for,
    measure_load_overhead,
)


class TestRecommendationBranches:
    """判定分支：阈值来自 Req 13.2 / 13.3。"""

    def test_above_50ms_recommends_adopt(self):
        assert _recommendation_for(50.1) == _RECOMMEND_ADOPT

    def test_below_5ms_recommends_reject(self):
        assert _recommendation_for(4.9) == _RECOMMEND_REJECT

    def test_gray_zone_recommends_weigh(self):
        assert _recommendation_for(5.0) == _RECOMMEND_GRAY
        assert _recommendation_for(30.0) == _RECOMMEND_GRAY
        assert _recommendation_for(50.0) == _RECOMMEND_GRAY


class TestMeasureLoadOverhead:
    """measure_load_overhead 的丢弃首次 + 中位数 + 判定。"""

    def _run_with_perf_sequence(self, perf_values, n):
        """在受控 perf_counter 序列下执行 measure_load_overhead。"""
        mock_settings = MagicMock(milvus_host="localhost", milvus_port=19530)

        with patch("app.scripts.measure_load_overhead.perf_counter", side_effect=perf_values), \
             patch("app.config.get_settings", return_value=mock_settings), \
             patch("app.storage.milvus.MilvusClient"), \
             patch("pymilvus.Collection", return_value=MagicMock()):
            return measure_load_overhead("kb-x", n=n)

    def test_discards_first_and_takes_median(self):
        """丢弃首次冷调用，取后续 (N-1) 次中位数。"""
        # 每次迭代消费两个 perf_counter 值 (t0, t1)，单位秒。
        # iter0 冷 100ms（丢弃），后续 10/20/30ms → 中位数 20ms。
        perf_values = [0.0, 0.1, 0.1, 0.11, 0.11, 0.13, 0.13, 0.16]
        result = self._run_with_perf_sequence(perf_values, n=4)

        assert len(result.samples) == 4
        assert result.samples[0] == pytest.approx(100.0, abs=1e-6)
        # 统计基于丢弃首次后的 [10, 20, 30]
        assert result.median == pytest.approx(20.0, abs=1e-6)
        assert result.min == pytest.approx(10.0, abs=1e-6)
        assert result.max == pytest.approx(30.0, abs=1e-6)
        assert result.mean == pytest.approx(20.0, abs=1e-6)
        assert result.kb_id == "kb-x"
        assert result.iterations == 4

    def test_recommendation_reflects_median(self):
        """中位数落在不采纳区间时给出对应建议。"""
        # iter0 冷 100ms（丢弃），后续均 2ms → 中位数 2ms < 5ms。
        perf_values = [0.0, 0.1, 0.1, 0.102, 0.102, 0.104, 0.104, 0.106]
        result = self._run_with_perf_sequence(perf_values, n=4)

        assert result.median == pytest.approx(2.0, abs=1e-6)
        assert result.recommendation == _RECOMMEND_REJECT

    def test_n_less_than_two_raises(self):
        """n < 2 无法在丢弃首次后保留样本，应拒绝。"""
        with pytest.raises(ValueError):
            measure_load_overhead("kb-x", n=1)
