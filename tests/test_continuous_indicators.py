"""指标层：EMA / 距离扩大 / TNR / ΔTNR / ATR（计划 Task 4）。

⚠️ 本层只吃**有成交**的 bar（计划 D13）。空 K 线带的是前收结转价而非成交价，
让它进窗口会把 TR 压成 0、把 TNR 的分母压小。对齐回全局时间栅格是回测层的事。
"""

import math

import pytest

from cta_continuous.indicators import (
    atr_series,
    delta_tnr,
    ema,
    gap_widening,
    tnr_series,
)


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


def test_delta_tnr_lag_mode_compares_with_k_periods_ago():
    """D7 的另一侧：研报**正文**说「当日与 3 日前」比，公式图说与近 k 期均值比。

    ⚠️ k=3 的滞后版在第 4 个值上才有定义（索引 3 减索引 0）。三个值时索引 2 要减
    索引 −1，那是 NaN —— `0.3 − 0.9` 是滞后 **2**，不是 3。

    手算（四个值）：滞后版 `0.2 − 0.9 = −0.7`；均值版
    `0.2 − (0.6 + 0.3 + 0.2)/3 = −1/6`。两条口径确实分得开。
    """
    lag = delta_tnr([0.9, 0.6, 0.3, 0.2], k=3, mode="lag")
    mean = delta_tnr([0.9, 0.6, 0.3, 0.2], k=3, mode="mean")
    assert math.isnan(lag[2])
    assert lag[3] == pytest.approx(-0.7)
    assert mean[3] == pytest.approx(-1 / 6)


def test_delta_tnr_lag_mode_needs_k_periods_of_history():
    values = delta_tnr([0.1, 0.2, 0.3, 0.4], k=3, mode="lag")
    assert all(math.isnan(value) for value in values[:3])
    assert values[3] == pytest.approx(0.3)


def test_delta_tnr_rejects_a_mode_it_does_not_know():
    with pytest.raises(ValueError) as caught:
        delta_tnr([0.1, 0.2], k=1, mode="ewm")
    assert str(caught.value).startswith("delta_tnr_mode:")


def test_delta_tnr_is_positive_when_noise_is_falling():
    """噪音减小 = TNR 上升 ⇒ ΔTNR > 0，这才是研报表 4 里赚钱的那一侧。"""
    assert delta_tnr([0.3, 0.6, 0.9], k=3)[2] > 0


def test_ema_uses_alpha_two_over_span_plus_one_without_adjustment():
    """D12：alpha = 2/(span+1)，adjust=False。span=2 ⇒ alpha=2/3。"""
    values = ema([1.0, 2.0], span=2)
    assert values[0] == pytest.approx(1.0)
    assert values[1] == pytest.approx(2.0 * (2 / 3) + 1.0 * (1 / 3))


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


def test_atr_averages_true_range_over_the_window():
    """TR = max(h−l, |h−前收|, |l−前收|)；ATR 是它的移动平均。

    手算三根 bar：
      #0 无前收        ⇒ TR = 110 − 100 = 10
      #1 前收 105      ⇒ TR = max(112−106=6, |112−105|=7, |106−105|=1) = 7
      #2 前收 110      ⇒ TR = max(118−110=8, |118−110|=8, |110−110|=0) = 8
    window=2 ⇒ 第三根的 ATR = (7 + 8)/2 = 7.5。
    """
    high = [110.0, 112.0, 118.0]
    low = [100.0, 106.0, 110.0]
    close = [105.0, 110.0, 112.0]
    values = atr_series(high, low, close, window=2)
    assert values[2] == pytest.approx(7.5)
    assert values[1] == pytest.approx((10.0 + 7.0) / 2)


def test_first_bar_true_range_falls_back_to_the_bar_range():
    """没有前收时 TR 只能是 h−l。"""
    values = atr_series([110.0, 111.0], [100.0, 109.0], [105.0, 110.0], window=1)
    assert values[0] == pytest.approx(10.0)
