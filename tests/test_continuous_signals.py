"""涨跌概率递推与开仓闸（计划 Task 5）。

研报 §3.2 的递推（正文一个字都没描述，全在公式图里）：

    UpProb_t   = UpProb_{t−1}   + 0.5 × (DownProb_{t−1} × Long_t − UpProb_{t−1}   × Short_t)
    DownProb_t = DownProb_{t−1} + 0.5 × (UpProb_{t−1}   × Short_t − DownProb_{t−1} × Long_t)

起点 0.5 / 0.5，`U2P = Up − Down`。图 13 印出了四步的数值，是本层的硬验收。
"""

import pytest

from cta_continuous.signals import (
    Direction,
    position_path,
    position_signal,
    up_down_prob,
)


def test_up_down_prob_reproduces_figure_13():
    """研报图 13 的四步。期望值是研报印出来的数字，不是被测代码算的。"""
    result = up_down_prob(
        long_flags=[True, True, False, False],
        short_flags=[False, False, True, True],
    )
    assert [round(v, 5) for v in result.up] == [0.75, 0.875, 0.4375, 0.21875]
    assert [round(v, 5) for v in result.down] == [0.25, 0.125, 0.5625, 0.78125]


def test_figure_13_percentages_to_two_decimals():
    """图 13 印的是 75.00 / 87.50 / 43.75 / 21.88 与 25.00 / 12.50 / 56.25 / 78.12。"""
    result = up_down_prob(
        long_flags=[True, True, False, False],
        short_flags=[False, False, True, True],
    )
    assert [f"{v * 100:.2f}" for v in result.up] == ["75.00", "87.50", "43.75", "21.88"]
    assert [f"{v * 100:.2f}" for v in result.down] == [
        "25.00", "12.50", "56.25", "78.12",
    ]


def test_probabilities_hold_still_when_neither_side_fires():
    """图 13 的横向段：两侧都没触发时概率不动。"""
    result = up_down_prob(
        long_flags=[True, False, False], short_flags=[False, False, False]
    )
    assert result.up[1] == pytest.approx(0.75)
    assert result.up[2] == pytest.approx(0.75)


def test_probabilities_always_sum_to_one():
    result = up_down_prob(
        long_flags=[True, False, True, True, False],
        short_flags=[False, True, False, False, True],
    )
    for up, down in zip(result.up, result.down):
        assert up + down == pytest.approx(1.0)


def test_probabilities_never_leave_the_unit_interval():
    result = up_down_prob(long_flags=[True] * 40, short_flags=[False] * 40)
    assert max(result.up) < 1.0 and min(result.down) > 0.0


def test_both_sides_firing_at_once_is_rejected():
    """多空同时触发是上游算错了，不是一种可以求和的状态。"""
    with pytest.raises(ValueError, match="both_sides"):
        up_down_prob(long_flags=[True], short_flags=[True])


def test_u2p_is_the_difference():
    result = up_down_prob(long_flags=[True], short_flags=[False])
    assert result.u2p[0] == pytest.approx(0.75 - 0.25)


# --- 开仓闸 -----------------------------------------------------------------

def test_long_needs_every_gate_including_the_u2p_threshold():
    assert (
        position_signal(
            short_above_long=True, widening=True, atr_leverage=1.2,
            delta_tnr=0.01, u2p=0.5,
        ).direction
        is Direction.LONG
    )


def test_a_failing_gate_means_flat_not_hold():
    """D6：任一闸不过就平仓，不是持有到反向信号。"""
    for kwargs in (
        {"widening": False},
        {"atr_leverage": 0.9},
        {"delta_tnr": -0.01},
        {"u2p": 0.19},
    ):
        base = {
            "short_above_long": True, "widening": True, "atr_leverage": 1.2,
            "delta_tnr": 0.01, "u2p": 0.5,
        }
        assert position_signal(**{**base, **kwargs}).direction is Direction.FLAT


def test_short_mirrors_long():
    assert (
        position_signal(
            short_above_long=False, widening=True, atr_leverage=1.2,
            delta_tnr=0.01, u2p=-0.5,
        ).direction
        is Direction.SHORT
    )


def test_long_signal_value_is_up_prob_and_short_is_negative_down_prob():
    """§5.1『信号调整』：多头取上涨概率，空头取下跌概率（带负号表方向）。"""
    long_side = position_signal(
        short_above_long=True, widening=True, atr_leverage=1.2,
        delta_tnr=0.01, u2p=0.5,
    )
    short_side = position_signal(
        short_above_long=False, widening=True, atr_leverage=1.2,
        delta_tnr=0.01, u2p=-0.5,
    )
    # U2P = 0.5 且两概率和为 1 ⇒ Up = 0.75、Down = 0.25
    assert long_side.value == pytest.approx(0.75)
    assert short_side.value == pytest.approx(-0.75)


def test_the_u2p_threshold_pins_the_signal_magnitude_above_zero_point_six():
    """|U2P| > 0.2 且两概率和为 1 ⇒ 信号绝对值恒在 0.6 以上。"""
    just_inside = position_signal(
        short_above_long=True, widening=True, atr_leverage=1.2,
        delta_tnr=0.01, u2p=0.2001,
    )
    assert just_inside.value > 0.6


def test_tnr_sign_switch_flips_the_noise_gate():
    """D3：§5.1 汇总框写的是 ΔTNR<0，与正文和表 4 相反。开关必须存在。"""
    kwargs = {
        "short_above_long": True, "widening": True, "atr_leverage": 1.2,
        "delta_tnr": -0.01, "u2p": 0.5,
    }
    assert position_signal(**kwargs).direction is Direction.FLAT
    assert (
        position_signal(**kwargs, tnr_sign="negative").direction is Direction.LONG
    )


def test_ma_orientation_switch_flips_the_long_side():
    """D2：§5.1 汇总框把多空方向写反了。开关必须存在。"""
    kwargs = {
        "short_above_long": False, "widening": True, "atr_leverage": 1.2,
        "delta_tnr": 0.01, "u2p": 0.5,
    }
    assert position_signal(**kwargs).direction is Direction.FLAT
    assert (
        position_signal(**kwargs, ma_orientation="reversed").direction is Direction.LONG
    )


def test_gate_flags_take_no_u2p_argument():
    """D4：递推的输入闸不含 U2P，否则自指。签名里根本不该有这个参数。"""
    import inspect

    from cta_continuous.signals import gate_flags

    assert "u2p" not in inspect.signature(gate_flags).parameters


def test_gate_flags_are_mutually_exclusive():
    """闸给出的多空标志不可能同时为真 —— 递推靠这条前提。"""
    from cta_continuous.signals import gate_flags

    for short_above_long in (True, False):
        is_long, is_short = gate_flags(
            short_above_long=short_above_long, widening=True,
            atr_leverage=1.2, delta_tnr=0.01,
        )
        assert not (is_long and is_short)


def test_gate_flags_are_both_false_when_a_filter_blocks():
    from cta_continuous.signals import gate_flags

    assert gate_flags(
        short_above_long=True, widening=True, atr_leverage=0.9, delta_tnr=0.01
    ) == (False, False)


def test_atr_leverage_gate_is_strict():
    """研报写的是 `Lev_ATR > 1`，恰好等于 1 不算通过。"""
    from cta_continuous.signals import gate_flags

    assert gate_flags(
        short_above_long=True, widening=True, atr_leverage=1.0, delta_tnr=0.01
    ) == (False, False)


# --- D21：离场用哪几道闸 -----------------------------------------------------

def _bars(count, overrides=None):
    """一串默认「四闸全过、多头」的 bar，按需覆盖某几根。"""
    bars = [
        {
            "short_above_long": True,
            "widening": True,
            "atr_leverage": 2.0,
            "delta_tnr": 0.1,
            "u2p": 0.5,
        }
        for _ in range(count)
    ]
    for index, patch in (overrides or {}).items():
        bars[index].update(patch)
    return bars


def _path(bars, **kwargs):
    return position_path(
        short_above_long=[b["short_above_long"] for b in bars],
        widening=[b["widening"] for b in bars],
        atr_leverage=[b["atr_leverage"] for b in bars],
        delta_tnr=[b["delta_tnr"] for b in bars],
        u2p=[b["u2p"] for b in bars],
        **kwargs,
    )


def test_wide_exit_is_pointwise_the_stateless_reading():
    """`exit_gates="wide"` 必须与逐 bar 无状态判定**逐点相同** —— 它就是 D6 原样。"""
    bars = _bars(8, {2: {"widening": False}, 4: {"delta_tnr": -0.1}, 6: {"atr_leverage": 0.5}})

    path = _path(bars, exit_gates="wide")
    stateless = [position_signal(**bar) for bar in bars]

    assert list(path) == stateless


def test_narrow_exit_holds_a_position_through_a_noise_gate_dip():
    """D6 的原文只点名 `Lev_ATR<1`；ΔTNR 转负不该把仓位打掉。"""
    bars = _bars(5, {2: {"delta_tnr": -0.3}, 3: {"widening": False}})

    path = _path(bars, exit_gates="narrow")

    assert [signal.direction for signal in path] == [Direction.LONG] * 5


def test_narrow_exit_closes_when_atr_leverage_falls_below_one():
    """§3.1：「当开仓杠杆率 Lev_ATR<1 …… 即使此时传统信号为 1，我们依然平仓操作」。"""
    bars = _bars(5, {2: {"atr_leverage": 0.5}})

    path = _path(bars, exit_gates="narrow")

    assert path[1].direction is Direction.LONG
    assert path[2].direction is Direction.FLAT
    # 杠杆恢复、四闸重新全过 ⇒ 可以再进场。
    assert path[3].direction is Direction.LONG


def test_narrow_exit_closes_on_a_moving_average_reversal():
    bars = _bars(5, {2: {"short_above_long": False}, 3: {"short_above_long": False},
                       4: {"short_above_long": False}})

    path = _path(bars, exit_gates="narrow")

    assert path[1].direction is Direction.LONG
    # 均线反向：多头必须离场；能不能立刻反手，取决于空头那侧的 U2P。
    assert path[2].direction is not Direction.LONG


def test_narrow_exit_can_reverse_straight_into_the_other_side():
    bars = _bars(4)
    for index in (2, 3):
        bars[index].update({"short_above_long": False, "u2p": -0.5})

    path = _path(bars, exit_gates="narrow")

    assert path[1].direction is Direction.LONG
    assert path[2].direction is Direction.SHORT


def test_narrow_exit_still_needs_all_four_gates_to_enter():
    """收窄的是**离场**，不是入场。入场照旧要四道闸全过。"""
    bars = _bars(4, {0: {"delta_tnr": -0.1}, 1: {"widening": False},
                       2: {"u2p": 0.1}})

    path = _path(bars, exit_gates="narrow")

    assert [signal.direction for signal in path[:3]] == [Direction.FLAT] * 3
    assert path[3].direction is Direction.LONG


def test_position_path_rejects_an_exit_rule_it_does_not_know():
    with pytest.raises(ValueError) as caught:
        _path(_bars(2), exit_gates="trailing")
    assert str(caught.value).startswith("exit_gates:")


# --- 研报基线档：EMA 均线穿越（§2.1） ---------------------------------------
#
# 「当短均线上穿长均线且二者距离走阔时，我们即选择开多仓（Signal=1）；当短均线下穿
# 长均线且二者距离走阔时，即选择开空仓（Signal=-1）。策略没有空仓状态，在开仓之后，
# 直到反向信号出现，则反手开仓。」——「每次均线穿越的过程不加以区分，均满仓开仓。」
#
# 这一档是研报**自报 13.06% / 夏普 1.03** 的对照基准，不含它自称的三级改进，所以
# 也不该有 Lev_ATR 闸、ΔTNR 闸与 U2P 强弱。


def _crossover(bars, **kwargs):
    from cta_continuous.signals import crossover_path

    return crossover_path(
        short_above_long=[b["short_above_long"] for b in bars],
        widening=[b["widening"] for b in bars],
        **kwargs,
    )


def test_crossover_waits_for_the_gap_to_widen_before_the_first_entry():
    bars = _bars(3, {0: {"widening": False}, 1: {"widening": False}})

    path = _crossover(bars)

    assert [signal.direction for signal in path] == [
        Direction.FLAT, Direction.FLAT, Direction.LONG,
    ]


def test_crossover_opens_every_position_at_full_size():
    """§2.1：「每次均线穿越的过程不加以区分，均满仓开仓」—— 没有信号强弱。"""
    path = _crossover(_bars(3))

    assert [signal.value for signal in path] == [1.0, 1.0, 1.0]


def test_crossover_holds_through_a_narrowing_gap():
    """「策略没有空仓状态」：走阔只是开仓触发，不走阔不构成离场。"""
    bars = _bars(4, {2: {"widening": False}, 3: {"widening": False}})

    path = _crossover(bars)

    assert [signal.direction for signal in path] == [Direction.LONG] * 4


def test_crossover_needs_a_widening_gap_to_flip():
    """反手要的是**反向信号**，而反向信号同样要求距离走阔。"""
    bars = _bars(3, {1: {"short_above_long": False, "widening": False},
                     2: {"short_above_long": False}})

    path = _crossover(bars)

    assert path[0].direction is Direction.LONG
    assert path[1].direction is Direction.LONG
    assert path[2].direction is Direction.SHORT


def test_crossover_reverses_straight_into_a_full_short():
    bars = _bars(2, {1: {"short_above_long": False}})

    path = _crossover(bars)

    assert path[1].direction is Direction.SHORT
    assert path[1].value == -1.0


def test_crossover_takes_no_atr_or_noise_gate_arguments():
    """基线不含研报自称的任何改进；那两道闸在签名里根本不该出现。"""
    import inspect

    from cta_continuous.signals import crossover_path

    parameters = inspect.signature(crossover_path).parameters
    assert "atr_leverage" not in parameters
    assert "delta_tnr" not in parameters
    assert "u2p" not in parameters


def test_crossover_respects_the_ma_orientation_switch():
    """D2 的对照口径对基线一样要能跑到。"""
    path = _crossover(_bars(2), ma_orientation="reversed")

    assert [signal.direction for signal in path] == [Direction.SHORT] * 2
