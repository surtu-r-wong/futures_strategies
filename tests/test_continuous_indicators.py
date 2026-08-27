"""指标层：EMA / 距离扩大 / TNR / ΔTNR / ATR（计划 Task 4）。

⚠️ 本层只吃**有成交**的 bar（计划 D13）。空 K 线带的是前收结转价而非成交价，
让它进窗口会把 TR 压成 0、把 TNR 的分母压小。对齐回全局时间栅格是回测层的事。
"""

import math

import pytest

import common.commodity.indicators as shared
import cta_continuous.indicators as legacy
from cta_continuous.indicators import delta_tnr, gap_widening, tnr_series


def test_shared_indicators_are_direct_compatibility_re_exports():
    for name in ("_as_float_array", "atr_series", "ema", "true_range"):
        assert getattr(legacy, name) is getattr(shared, name)


def test_tnr_is_one_on_a_monotone_path():
    """无噪音时位移 = 路程（研报图 11 左，TNR=1）。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    assert tnr_series(closes, window=4)[4] == pytest.approx(1.0)


def test_tnr_falls_as_the_path_wanders():
    """同样的起点终点、更长的路程 ⇒ 更小的 TNR（图 11 中/右）。"""
    # |104-100| / (5+5+5+1) = 4/16
    closes = [100.0, 105.0, 100.0, 105.0, 104.0]
    assert tnr_series(closes, window=4)[4] == pytest.approx(0.25)


def test_tnr_is_undefined_before_the_window_fills():
    closes = [100.0, 101.0, 102.0]
    values = tnr_series(closes, window=4)
    assert all(math.isnan(v) for v in values)


def test_tnr_of_a_flat_path_is_undefined_not_zero():
    """路程为 0 ⇒ 分母为 0。研报没写这种情形；取 NaN 而不是 0 或 1。"""
    values = tnr_series([100.0] * 6, window=4)
    assert math.isnan(values[5])


def test_delta_tnr_uses_the_mean_of_the_last_k_including_now():
    """D7：ΔTNR = TNR_t − mean(TNR_t, TNR_{t−1}, TNR_{t−2})。

    手算：0.3 − (0.3 + 0.6 + 0.9)/3 = 0.3 − 0.6 = −0.3。
    """
    assert delta_tnr([0.9, 0.6, 0.3], k=3)[2] == pytest.approx(-0.3)


def test_delta_tnr_is_positive_when_noise_is_falling():
    """噪音减小 = TNR 上升 ⇒ ΔTNR > 0，这才是研报表 4 里赚钱的那一侧。"""
    assert delta_tnr([0.3, 0.6, 0.9], k=3)[2] > 0


def test_gap_widening_compares_absolute_distance_to_the_previous_bar():
    """『二者距离扩大』—— 距离取绝对值，扩大是与上一根比。"""
    widening = gap_widening([1.0, 2.0, 1.5], [0.0, 0.0, 0.0])
    assert widening[0] is False          # 没有上一根可比
    assert widening[1] is True           # 1 -> 2
    assert widening[2] is False          # 2 -> 1.5


def test_gap_widening_is_true_when_a_short_ma_pulls_further_below():
    """空头一侧距离也在扩大 —— 绝对值，不是带符号的差。"""
    widening = gap_widening([-1.0, -2.0], [0.0, 0.0])
    assert widening[1] is True
