"""ATR 与止损事件（计划 Task 4）。

研报口径（计划 Strategy Summary）：

- `TR = max(high - low, |high - previous_close|, |low - previous_close|)`；
- ATR 在 15 分钟 K 线上取 16 根窗口；
- 反向信号止损：多头 5 根严格递减的 high，空头 3 根严格递增的 low；
- 吊灯止损：多头 `close < 入场后最高价 - 2.5 * ATR`，空头 `close > 入场后最低价 + 2.0 * ATR`；
- **每根 bar 最多产生一次减仓事件**，即使两族止损同时触发。

期望值全部手算，不由被测代码推导。
"""

import pytest

from index_open_momentum.risk import (
    atr_series,
    long_chandelier_stop,
    long_reverse_stop,
    short_chandelier_stop,
    short_reverse_stop,
    stop_event,
    true_range,
)
from index_open_momentum.risk import Direction, StopReason
from index_open_momentum.types import Bar


def test_true_range_when_the_bar_range_itself_dominates():
    bar = Bar(open=4010.0, high=4030.0, low=4000.0, close=4020.0)
    # max(4030-4000=30, |4030-4012|=18, |4000-4012|=12) = 30
    assert true_range(bar, previous_close=4012.0) == 30.0


def test_true_range_when_the_gap_up_from_previous_close_dominates():
    bar = Bar(open=4010.0, high=4030.0, low=4000.0, close=4020.0)
    # max(30, |4030-3980|=50, |4000-3980|=20) = 50
    assert true_range(bar, previous_close=3980.0) == 50.0


def test_true_range_when_the_gap_down_from_previous_close_dominates():
    bar = Bar(open=4010.0, high=4030.0, low=4000.0, close=4020.0)
    # max(30, |4030-4045|=15, |4000-4045|=45) = 45
    assert true_range(bar, previous_close=4045.0) == 45.0


def test_true_range_of_the_first_bar_has_no_previous_close():
    bar = Bar(open=4010.0, high=4030.0, low=4000.0, close=4020.0)
    # 没有前收，只剩 high - low
    assert true_range(bar, previous_close=None) == 30.0


def _bar_with_half_range(half_range: float) -> Bar:
    """造一根 TR 恰好等于 ``2 * half_range`` 的 bar。

    收盘固定在 4000，且 4000 落在 ``[low, high]`` 正中 —— 于是前收也落在区间内，
    `|high - prev_close|` 与 `|low - prev_close|` 都等于 half_range，永远不会
    超过 `high - low = 2 * half_range`。TR 因此只由 half_range 决定，与相邻
    bar 无关，均值可以手算。
    """
    return Bar(
        open=4000.0,
        high=4000.0 + half_range,
        low=4000.0 - half_range,
        close=4000.0,
    )


def test_atr_needs_a_full_sixteen_bar_window_before_it_reports():
    bars = [_bar_with_half_range(i + 1) for i in range(15)]  # 只有 15 根
    assert atr_series(bars) == [None] * 15


def test_atr_is_the_mean_of_the_last_sixteen_true_ranges():
    bars = [_bar_with_half_range(i + 1) for i in range(17)]
    # TR_i = 2*(i+1) → 2, 4, 6, ..., 34
    series = atr_series(bars)
    assert series[:15] == [None] * 15
    # index 15: mean(2,4,...,32) = 2 * mean(1..16) = 2 * 8.5
    assert series[15] == 17.0
    # index 16: mean(4,6,...,34) = 2 * mean(2..17) = 2 * 9.5
    assert series[16] == 19.0


def test_atr_window_is_sixteen_not_some_neighbouring_length():
    """用一个"前 16 根平、第 17 根跳"的序列钉死窗口长度。"""
    bars = [_bar_with_half_range(5.0) for _ in range(16)] + [_bar_with_half_range(85.0)]
    series = atr_series(bars)
    # 前 16 根 TR 全是 10 → index 15 的 ATR = 10.0
    assert series[15] == 10.0
    # index 16 的窗口 = TR_1..TR_16 = 十五个 10 加一个 170 → (15*10 + 170)/16 = 20.0
    assert series[16] == 20.0


def _bar(high: float, low: float) -> Bar:
    """反向止损只看 high / low；open 与 close 取区间中点，不影响判据。"""
    mid = (high + low) / 2
    return Bar(open=mid, high=high, low=low, close=mid)


def test_five_strictly_falling_highs_trip_the_long_reverse_stop():
    bars = [_bar(4050.0, 4000.0), _bar(4040.0, 4000.0), _bar(4030.0, 4000.0),
            _bar(4020.0, 4000.0), _bar(4010.0, 4000.0)]
    assert long_reverse_stop(bars) is True


def test_a_tie_among_the_highs_does_not_trip_the_long_reverse_stop():
    bars = [_bar(4050.0, 4000.0), _bar(4040.0, 4000.0), _bar(4040.0, 4000.0),
            _bar(4020.0, 4000.0), _bar(4010.0, 4000.0)]
    # 第二、三根 high 都是 4040，持平不算严格递减
    assert long_reverse_stop(bars) is False


def test_four_falling_highs_are_not_yet_enough_for_the_long_reverse_stop():
    bars = [_bar(4040.0, 4000.0), _bar(4030.0, 4000.0), _bar(4020.0, 4000.0),
            _bar(4010.0, 4000.0)]
    assert long_reverse_stop(bars) is False


def test_only_the_trailing_five_bars_matter_for_the_long_reverse_stop():
    earlier = [_bar(4000.0, 3900.0), _bar(4100.0, 3900.0)]  # 先涨，与判据无关
    falling = [_bar(4050.0, 4000.0), _bar(4040.0, 4000.0), _bar(4030.0, 4000.0),
               _bar(4020.0, 4000.0), _bar(4010.0, 4000.0)]
    assert long_reverse_stop([*earlier, *falling]) is True


def test_three_strictly_rising_lows_trip_the_short_reverse_stop():
    bars = [_bar(4100.0, 4000.0), _bar(4100.0, 4010.0), _bar(4100.0, 4020.0)]
    assert short_reverse_stop(bars) is True


def test_a_tie_among_the_lows_does_not_trip_the_short_reverse_stop():
    bars = [_bar(4100.0, 4000.0), _bar(4100.0, 4010.0), _bar(4100.0, 4010.0)]
    assert short_reverse_stop(bars) is False


def test_two_rising_lows_are_not_yet_enough_for_the_short_reverse_stop():
    assert short_reverse_stop([_bar(4100.0, 4000.0), _bar(4100.0, 4010.0)]) is False


def test_the_two_reverse_stops_read_different_columns_and_different_lengths():
    """多头看 high 且要 5 根，空头看 low 且要 3 根 —— 用一组只满足其一的 bar 钉住。"""
    # low 严格递增 3 根（空头成立），high 恒定（多头的 5 根递减不成立）
    rising_lows = [_bar(4100.0, 4000.0), _bar(4100.0, 4010.0), _bar(4100.0, 4020.0)]
    assert short_reverse_stop(rising_lows) is True
    assert long_reverse_stop(rising_lows) is False

    # high 严格递减 3 根 —— 对空头无意义，对多头也不够长
    three_falling_highs = [_bar(4050.0, 4000.0), _bar(4040.0, 4000.0), _bar(4030.0, 4000.0)]
    assert long_reverse_stop(three_falling_highs) is False
    assert short_reverse_stop(three_falling_highs) is False

    # 上面三组多头断言全部在长度守卫处就返回了，走不到"读哪一列"。
    # 补一组够长(5 根)、low 严格递减而 high 恒定的 bar，才真正压住多头读的是 high。
    five_falling_lows_flat_highs = [
        _bar(4100.0, 4040.0), _bar(4100.0, 4030.0), _bar(4100.0, 4020.0),
        _bar(4100.0, 4010.0), _bar(4100.0, 4000.0),
    ]
    assert long_reverse_stop(five_falling_lows_flat_highs) is False


def test_long_chandelier_trips_when_close_falls_more_than_two_and_a_half_atr():
    # 阈值 = 4100 - 2.5 * 20 = 4050
    assert long_chandelier_stop(close=4049.9, best_high_since_entry=4100.0, atr=20.0) is True


def test_long_chandelier_does_not_trip_exactly_at_the_threshold():
    """研报是"跌破"，等于不算 —— 阈值相等不触发。"""
    assert long_chandelier_stop(close=4050.0, best_high_since_entry=4100.0, atr=20.0) is False


def test_long_chandelier_does_not_trip_above_the_threshold():
    assert long_chandelier_stop(close=4050.1, best_high_since_entry=4100.0, atr=20.0) is False


def test_short_chandelier_trips_when_close_rises_more_than_two_atr():
    # 阈值 = 4000 + 2.0 * 20 = 4040
    assert short_chandelier_stop(close=4040.1, best_low_since_entry=4000.0, atr=20.0) is True


def test_short_chandelier_does_not_trip_exactly_at_the_threshold():
    assert short_chandelier_stop(close=4040.0, best_low_since_entry=4000.0, atr=20.0) is False


def test_the_two_chandelier_multiples_are_not_interchangeable():
    """多头 2.5、空头 2.0 —— 用一个只在正确乘数下不触发的收盘价钉住。

    best_high = best_low = 4100，ATR = 20：
    - 正确：多头阈值 4100-2.5*20 = 4050，空头阈值 4100+2.0*20 = 4140
    - 若两个乘数互换：多头阈值 4060，空头阈值 4150
    收盘 4055 落在两套阈值之间 —— 正确口径下多头不触发，互换后会触发。
    """
    assert long_chandelier_stop(close=4055.0, best_high_since_entry=4100.0, atr=20.0) is False
    assert short_chandelier_stop(close=4145.0, best_low_since_entry=4100.0, atr=20.0) is True


def test_a_chandelier_stop_cannot_be_evaluated_without_an_atr():
    """ATR 窗口未满时不许静默"不触发" —— 那是把风控失效伪装成没有风险。"""
    with pytest.raises(ValueError, match="ATR"):
        long_chandelier_stop(close=4000.0, best_high_since_entry=4100.0, atr=None)
    with pytest.raises(ValueError, match="ATR"):
        short_chandelier_stop(close=4200.0, best_low_since_entry=4100.0, atr=float("nan"))


_FALLING_HIGHS = [
    Bar(open=4090.0, high=4100.0, low=4000.0, close=4050.0),
    Bar(open=4080.0, high=4090.0, low=4000.0, close=4050.0),
    Bar(open=4070.0, high=4080.0, low=4000.0, close=4050.0),
    Bar(open=4060.0, high=4070.0, low=4000.0, close=4050.0),
    Bar(open=4050.0, high=4060.0, low=4000.0, close=4050.0),
]
# high 4100>4090>4080>4070>4060 严格递减 → 多头反向止损成立
# 收盘 4050；入场后最高 4100 ⇒ ATR=10 时吊灯阈值 4075（触发），ATR=30 时 4025（不触发）

_TIED_HIGHS = [
    _FALLING_HIGHS[0],
    Bar(open=4080.0, high=4100.0, low=4000.0, close=4050.0),  # high 抬到 4100，与上一根持平
    *_FALLING_HIGHS[2:],
]
# high 序列 4100, 4100, 4080, 4070, 4060 → 首对持平，不是严格递减 → 反向止损不成立；
# 收盘与入场后最高价都没变，吊灯不受影响


def test_both_stop_families_on_one_bar_still_produce_a_single_event():
    """研报：每根 bar 最多减一档，即使两族同时触发。"""
    event = stop_event(
        Direction.LONG, _FALLING_HIGHS, best_high_since_entry=4100.0, atr=10.0
    )
    assert event is not None
    assert event.triggered == (StopReason.CHANDELIER, StopReason.REVERSE)
    # 事件是单数：两族都响，减的仍然只是一档
    assert event.scale_down_steps == 1


def test_only_the_reverse_family_trips_when_atr_is_wide():
    event = stop_event(
        Direction.LONG, _FALLING_HIGHS, best_high_since_entry=4100.0, atr=30.0
    )
    assert event is not None
    assert event.triggered == (StopReason.REVERSE,)
    assert event.scale_down_steps == 1


def test_only_the_chandelier_family_trips_when_the_highs_tie():
    event = stop_event(
        Direction.LONG, _TIED_HIGHS, best_high_since_entry=4100.0, atr=10.0
    )
    assert event is not None
    assert event.triggered == (StopReason.CHANDELIER,)


def test_no_event_when_neither_family_trips():
    assert stop_event(
        Direction.LONG, _TIED_HIGHS, best_high_since_entry=4100.0, atr=30.0
    ) is None


def test_a_short_position_is_judged_by_the_short_family():
    rising_lows = [
        Bar(open=4050.0, high=4100.0, low=4000.0, close=4150.0),
        Bar(open=4050.0, high=4100.0, low=4010.0, close=4150.0),
        Bar(open=4050.0, high=4100.0, low=4020.0, close=4150.0),
    ]
    # low 4000<4010<4020 严格递增 → 空头反向止损成立
    # 收盘 4150；入场后最低 4000，ATR=10 ⇒ 吊灯阈值 4020，4150 > 4020 也成立
    event = stop_event(
        Direction.SHORT, rising_lows, best_low_since_entry=4000.0, atr=10.0
    )
    assert event is not None
    assert event.triggered == (StopReason.CHANDELIER, StopReason.REVERSE)
    assert event.scale_down_steps == 1

    # 同一组 bar 按多头判：high 恒定 4100 不递减，吊灯用 best_high 也不成立
    assert stop_event(
        Direction.LONG, rising_lows, best_high_since_entry=4200.0, atr=100.0
    ) is None


# --------------------------------------------------------------------------
# no-trade bar：ATR 序列必须能穿过它
# --------------------------------------------------------------------------
#
# ⚠️ 这一节是端到端真跑时炸出来的。先前按"零成交桶"统计得出"从未出现"的结论，
# 只数了**存在的行**——整桶**缺行**（档案里那几分钟根本没有）不在统计里。
# 2016 年 1~3 月的 IF 主力就有。


def test_the_atr_series_walks_through_an_absent_bar():
    """`None` 表示这根没成交：不贡献 TR，也不更新"上一根收盘"。"""
    from index_open_momentum.risk import atr_series

    bars = [Bar(open=100.0, high=101.0, low=99.0, close=100.0) for _ in range(20)]
    bars[5] = None

    out = atr_series(bars, window=3)

    assert out[5] is None
    assert out[6] is not None


def test_an_absent_bar_does_not_fabricate_a_gap_in_true_range():
    """跳过那根，而不是拿它前后两根算一个假跳空。

    第 6 根的 TR 应当仍以**第 5 根**（最后一根有成交的）收盘为参照。
    """
    from index_open_momentum.risk import atr_series, true_range

    bars = [Bar(open=100.0, high=101.0, low=99.0, close=100.0) for _ in range(6)]
    bars.append(None)
    bars.append(Bar(open=120.0, high=121.0, low=119.0, close=120.0))

    out = atr_series(bars, window=1)
    expected = true_range(bars[7], bars[5].close)

    assert out[7] == pytest.approx(expected)


def test_a_series_of_only_absent_bars_has_no_atr_anywhere():
    from index_open_momentum.risk import atr_series

    assert atr_series([None, None, None], window=1) == [None, None, None]
