"""商品策略共用指标：EMA / TR / ATR（计划 Shared Core Task 4）。"""

import numpy as np
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


def test_daily_atr_by_bar_uses_only_completed_days():
    """道氏研报的 ATR 是「每日」的波动幅度：先把当天的 bar 聚成日 H/L/C 算 TR，
    再对**已完成**的日取均值；当天的 bar 只能看到前一日为止的值。

    两根 bar 一天、四天、窗口 2，手算：
    day1 H11 L8 C10  → TR 3（无前收，取 H−L）
    day2 H16 L10 C12 → TR max(6, |16−10|, |10−10|) = 6
    day3 H19 L12 C14 → TR max(7, |19−12|, |12−12|) = 7
    day4 H20 L10 C15 → 不参与（当天未完成）
    day3 的 bar = mean(3, 6) = 4.5；day4 的 bar = mean(6, 7) = 6.5；前两天不足两个完成日。
    """
    from common.commodity.indicators import daily_atr_by_bar

    high = [10.0, 11.0, 12.0, 16.0, 14.0, 19.0, 20.0, 16.0]
    low = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 10.0, 14.0]
    close = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 15.0]
    days = ["d1", "d1", "d2", "d2", "d3", "d3", "d4", "d4"]

    result = daily_atr_by_bar(high, low, close, days, window=2)

    assert np.isnan(result[:4]).all()
    assert result[4:6].tolist() == [4.5, 4.5]
    assert result[6:8].tolist() == [6.5, 6.5]


def test_daily_atr_by_bar_rejects_unsorted_days():
    from common.commodity.indicators import daily_atr_by_bar

    with pytest.raises(ValueError, match="daily_atr_days"):
        daily_atr_by_bar(
            [1.0, 1.0, 1.0],
            [0.0, 0.0, 0.0],
            [0.5, 0.5, 0.5],
            ["d2", "d1", "d2"],
            window=1,
        )
