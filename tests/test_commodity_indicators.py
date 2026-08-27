"""商品策略共用指标：EMA / TR / ATR（计划 Shared Core Task 4）。"""

import pytest

from common.commodity.indicators import atr_series, ema, true_range


def test_ema_uses_alpha_two_over_span_plus_one_without_adjustment():
    """D12：alpha = 2/(span+1)，adjust=False。span=2 ⇒ alpha=2/3。"""
    values = ema([1.0, 2.0], span=2)
    assert values[0] == pytest.approx(1.0)
    assert values[1] == pytest.approx(2.0 * (2 / 3) + 1.0 * (1 / 3))


def test_true_range_includes_the_gap_from_the_previous_close():
    assert true_range(112.0, 106.0, 105.0) == pytest.approx(7.0)


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
