"""开盘趋势信号（研报口径）—— 计划 Task 3。

论文规定（`docs/superpowers/plans/2026-07-09-guosen-open-momentum.md` Strategy Summary）：

- 多头信号：开盘后前 3 根 15 分钟 bar 的 `open`、`low`、`close` **各自严格递增**；
- 空头信号：前 3 根 bar 的 `open`、`high`、`close` **各自严格递减**；
- 两者都不成立则当日不交易该合约。

本文件的每个期望值都是手算字面量，不由被测代码推导。
"""

from index_open_momentum.signals import OpeningSignal, opening_signal
from index_open_momentum.types import Bar


def test_all_three_series_strictly_rising_is_a_long_signal():
    bars = [
        Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=4020.0, low=4001.0, close=4015.0),
        Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
    ]
    # open 4000<4006<4016, low 3995<4001<4011, close 4005<4015<4025 —— 三条都严格递增
    assert opening_signal(bars) is OpeningSignal.LONG


def test_all_three_series_strictly_falling_is_a_short_signal():
    bars = [
        Bar(open=4025.0, high=4030.0, low=4011.0, close=4016.0),
        Bar(open=4015.0, high=4020.0, low=4001.0, close=4006.0),
        Bar(open=4005.0, high=4010.0, low=3995.0, close=4000.0),
    ]
    # open 4025>4015>4005, high 4030>4020>4010, close 4016>4006>4000 —— 三条都严格递减
    assert opening_signal(bars) is OpeningSignal.SHORT


def test_short_needs_high_not_low_to_fall():
    """空头看 high，多头看 low —— 拿 low 递减但 high 不递减的一组钉住这个不对称。"""
    bars = [
        Bar(open=4025.0, high=4030.0, low=4011.0, close=4016.0),
        Bar(open=4015.0, high=4031.0, low=4001.0, close=4006.0),
        Bar(open=4005.0, high=4032.0, low=3995.0, close=4000.0),
    ]
    # open 与 close 严格递减、low 也递减，但 high 4030<4031<4032 是递增的 → 不成空头
    assert opening_signal(bars) is OpeningSignal.NEUTRAL


def test_a_tie_in_one_series_breaks_the_strict_condition():
    """研报要求"严格"递增 —— 持平不算。"""
    bars = [
        Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=4020.0, low=3995.0, close=4015.0),
        Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
    ]
    # open 与 close 严格递增，但 low 3995 == 3995 持平 → 不成多头
    assert opening_signal(bars) is OpeningSignal.NEUTRAL


def test_two_opening_bars_are_not_enough():
    """不足三根就没有判据 —— 两根严格递增也不许出多头。"""
    bars = [
        Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=4020.0, low=4001.0, close=4015.0),
    ]
    assert opening_signal(bars) is OpeningSignal.NEUTRAL


def test_no_opening_bars_at_all_is_neutral():
    assert opening_signal([]) is OpeningSignal.NEUTRAL


def test_a_fourth_bar_cannot_change_the_verdict():
    """判据只看前三根；第四根即使掉头也不影响开盘信号。"""
    rising_three = [
        Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=4020.0, low=4001.0, close=4015.0),
        Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
    ]
    collapse = Bar(open=4024.0, high=4026.0, low=3900.0, close=3910.0)
    assert opening_signal([*rising_three, collapse]) is OpeningSignal.LONG


def test_a_bar_with_a_non_finite_price_is_not_a_valid_opening_bar():
    """坏 bar 不许出信号 —— 即使坏的那一列不在本方向的判据里。

    多头判据只看 open/low/close，所以一根 ``high`` 为 NaN 的 bar 会**沿着判据的
    盲区**溜过去。研报口径是"没有信号就不交易"，一根价格不完整的 bar 不足以
    支撑当日建仓，因此判定必须落到 NEUTRAL 而不是 LONG。
    """
    bars = [
        Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=float("nan"), low=4001.0, close=4015.0),
        Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
    ]
    # open 4000<4006<4016、low 3995<4001<4011、close 4005<4015<4025 三条都成立
    assert opening_signal(bars) is OpeningSignal.NEUTRAL
