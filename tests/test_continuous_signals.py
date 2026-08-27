"""涨跌概率递推与开仓闸（计划 Task 5）。

研报 §3.2 的递推（正文一个字都没描述，全在公式图里）：

    UpProb_t   = UpProb_{t−1}   + 0.5 × (DownProb_{t−1} × Long_t − UpProb_{t−1}   × Short_t)
    DownProb_t = DownProb_{t−1} + 0.5 × (UpProb_{t−1}   × Short_t − DownProb_{t−1} × Long_t)

起点 0.5 / 0.5，`U2P = Up − Down`。图 13 印出了四步的数值，是本层的硬验收。
"""

import pytest

from cta_continuous.signals import Direction, position_signal, up_down_prob


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
