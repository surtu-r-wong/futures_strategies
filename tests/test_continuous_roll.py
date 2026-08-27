"""主力合约与后复权连续价（计划 Task 2）。

研报附录一：主力 = **成交量与持仓量均达到最大**的合约，且**主力不可逆**；
展期用后复权，`AdjFactor_i = AdjFactor_{i−1} × Close_{i−1,old} / Close_{i−1,new}`。
"""

from datetime import date

import pandas as pd
import pytest

from common.dominant import DominantChoice
from cta_continuous.continuous import (
    adjustment_factors,
    choose_dominant_commodity,
    continuous_close,
)


def _pool(rows):
    return pd.DataFrame(rows, columns=["trade_date", "symbol", "oi", "volume"])


D = [date(2024, 3, day) for day in (1, 4, 5, 6, 7, 8)]


def test_dominant_requires_both_volume_and_oi_max():
    """单项最大不算主力。研报写的是「均达到最大」。

    起手就没有双最大合约时，**没有主力可言** —— 不是退而求其次挑持仓量最大的那张。
    """
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 100),   # 持仓量最大
            (D[0], "RB2410.SHF", 90, 300),    # 成交量最大
            (D[1], "RB2405.SHF", 100, 100),
            (D[1], "RB2410.SHF", 90, 300),
        ]
    )
    assert choose_dominant_commodity(frame, products=("RB",)) == ()


def test_dominant_appears_on_the_first_session_with_a_both_max_contract():
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 100),
            (D[0], "RB2410.SHF", 90, 300),    # 谁都不是双最大
            (D[1], "RB2405.SHF", 100, 300),   # 这一天开始有了
            (D[1], "RB2410.SHF", 90, 100),
            (D[2], "RB2405.SHF", 100, 300),
            (D[2], "RB2410.SHF", 90, 100),
        ]
    )
    choices = choose_dominant_commodity(frame, products=("RB",))
    assert [(c.trade_date, c.contract) for c in choices] == [(D[2], "RB2405.SHF")]


def test_dominant_keeps_the_previous_contract_when_nobody_is_both_max():
    """D11：双最大不成立时不切换，沿用上一主力。"""
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 300),   # 双最大 → 主力
            (D[0], "RB2410.SHF", 90, 100),
            (D[1], "RB2405.SHF", 90, 300),    # 持仓量输了、成交量赢了
            (D[1], "RB2410.SHF", 100, 100),   # 反过来
            (D[2], "RB2405.SHF", 90, 300),
            (D[2], "RB2410.SHF", 100, 100),
        ]
    )
    choices = choose_dominant_commodity(frame, products=("RB",))
    assert [c.contract for c in choices] == ["RB2405.SHF", "RB2405.SHF"]


def test_dominant_goes_missing_when_the_held_contract_leaves_the_daily_pool():
    """D11 只允许沿用仍可交易的旧主力；合约消失后不得伪造零量主力日。"""
    days = [date(2024, 3, day) for day in (1, 4, 5, 6)]
    frame = _pool(
        [
            (days[0], "RB2405.SHF", 100, 300),
            (days[0], "RB2410.SHF", 90, 100),
            # 旧主力从池中消失，两个新合约又各占一个最大值：没有双最大。
            (days[1], "RB2410.SHF", 100, 100),
            (days[1], "RB2501.SHF", 90, 300),
            (days[2], "RB2410.SHF", 100, 300),
            (days[2], "RB2501.SHF", 90, 100),
            (days[3], "RB2410.SHF", 100, 300),
            (days[3], "RB2501.SHF", 90, 100),
        ]
    )

    choices = choose_dominant_commodity(frame, products=("RB",))

    assert [(c.trade_date, c.contract) for c in choices] == [
        (days[1], "RB2405.SHF"),
        (days[3], "RB2410.SHF"),
    ]


def test_dominant_never_rolls_backwards():
    """研报：主力不可逆，一经确定不可反复。交割月只许非递减。"""
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 90, 100),
            (D[0], "RB2410.SHF", 100, 300),   # 双最大 → 主力是 2410
            (D[1], "RB2405.SHF", 100, 300),   # 近月双最大，但那是回头路
            (D[1], "RB2410.SHF", 90, 100),
            (D[2], "RB2405.SHF", 100, 300),
            (D[2], "RB2410.SHF", 90, 100),
        ]
    )
    choices = choose_dominant_commodity(frame, products=("RB",))
    assert [c.contract for c in choices] == ["RB2410.SHF", "RB2410.SHF"]


def test_dominant_decides_today_from_the_previous_session():
    """持仓量当日收盘才知道；拿当日的量决定当日交易哪张合约是回看。"""
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 300),
            (D[0], "RB2410.SHF", 90, 100),
            (D[1], "RB2405.SHF", 90, 100),
            (D[1], "RB2410.SHF", 100, 300),   # 换月发生在 D[1] 收盘
            (D[2], "RB2405.SHF", 90, 100),
            (D[2], "RB2410.SHF", 100, 300),
        ]
    )
    choices = choose_dominant_commodity(frame, products=("RB",))
    assert choices[0].trade_date == D[1] and choices[0].contract == "RB2405.SHF"
    assert choices[0].selected_from == D[0]
    assert choices[1].trade_date == D[2] and choices[1].contract == "RB2410.SHF"


def test_czce_three_digit_codes_compare_as_the_same_delivery_month():
    """不可逆判据要比交割月；郑商所三位码不归一就比不出 TA701 与 TA1701 是同一个月。"""
    frame = _pool(
        [
            (D[0], "TA701.CZC", 100, 300),
            (D[0], "TA705.CZC", 90, 100),
            (D[1], "TA1701.CZC", 100, 300),
            (D[1], "TA1705.CZC", 90, 100),
        ]
    )
    choices = choose_dominant_commodity(frame, products=("TA",))
    assert [c.contract for c in choices] == ["TA701.CZC"]


# --- 后复权 ---------------------------------------------------------------

def _chain():
    """两次展期：D0/D1 用 C1，D2/D3 用 C2，D4 起用 C3。"""
    return (
        [
            _choice(D[0], "RB2405.SHF"),
            _choice(D[1], "RB2405.SHF"),
            _choice(D[2], "RB2410.SHF"),
            _choice(D[3], "RB2410.SHF"),
            _choice(D[4], "RB2501.SHF"),
        ],
        {
            (D[0], "RB2405.SHF"): 90.0,
            (D[1], "RB2405.SHF"): 100.0,
            (D[1], "RB2410.SHF"): 125.0,
            (D[2], "RB2410.SHF"): 130.0,
            (D[3], "RB2410.SHF"): 200.0,
            (D[3], "RB2501.SHF"): 250.0,
            (D[4], "RB2501.SHF"): 260.0,
        },
    )


def _choice(trade_date, contract):
    return DominantChoice(
        trade_date=trade_date,
        product="RB",
        contract=contract,
        oi=1,
        volume=1,
        selected_from=trade_date,
    )


def test_adjust_factor_chains_across_two_rolls():
    """手算：第一段 1；第二段 100/125 = 0.8；第三段 0.8 × 200/250 = 0.64。"""
    choices, closes = _chain()
    factors = adjustment_factors(choices, closes=closes)
    assert list(factors["adj_factor"]) == pytest.approx([1.0, 1.0, 0.8, 0.8, 0.64])
    assert list(factors["continuity_segment"]) == [0, 0, 0, 0, 0]


def test_first_dominant_gets_factor_one():
    choices, closes = _chain()
    factors = adjustment_factors(choices, closes=closes)
    assert factors.iloc[0]["adj_factor"] == 1.0


def test_adjusted_series_has_no_roll_gap():
    """展期当日的连续收益率 = 新合约自己的收益率，不是两张合约之间的跳空。"""
    choices, closes = _chain()
    factors = adjustment_factors(choices, closes=closes)
    series = continuous_close(factors, closes=closes)
    before = series.loc[series["trade_date"] == D[1], "close"].iloc[0]
    after = series.loc[series["trade_date"] == D[2], "close"].iloc[0]
    own_return = 130.0 / 125.0 - 1.0            # 新合约自己走的那一段
    assert after / before - 1.0 == pytest.approx(own_return)


def test_adjustment_refuses_to_default_a_missing_roll_close():
    """缺前一日收盘价时必须报错。悄悄取 1.0 会造出一个假的无跳空序列。"""
    choices, closes = _chain()
    closes[(D[0], "RB2501.SHF")] = 249.0
    closes.pop((D[3], "RB2501.SHF"))
    with pytest.raises(ValueError) as exc_info:
        adjustment_factors(choices, closes=closes)
    message = str(exc_info.value)
    assert "roll_close_missing" in message
    assert f"not_after={choices[-1].selected_from}" in message
    assert choices[-2].contract in message
    assert choices[-1].contract in message


def test_adjustment_starts_a_new_segment_for_disjoint_contract_histories():
    old = DominantChoice(
        date(2018, 3, 30), "FU", "FU1804.SHF", 1, 1, date(2018, 3, 29)
    )
    new = DominantChoice(
        date(2018, 7, 17), "FU", "FU1901.SHF", 1, 1, date(2018, 7, 16)
    )
    factors = adjustment_factors(
        (old, new),
        closes={
            (date(2018, 3, 30), "FU1804.SHF"): 3200.0,
            (date(2018, 7, 16), "FU1901.SHF"): 2800.0,
        },
    )
    assert list(factors["adj_factor"]) == [1.0, 1.0]
    assert list(factors["continuity_segment"]) == [0, 1]


def test_adjustment_resets_segment_for_each_product_and_returns_exact_columns():
    choices = (
        DominantChoice(
            date(2018, 3, 30), "FU", "FU1804.SHF", 1, 1, date(2018, 3, 29)
        ),
        DominantChoice(
            date(2018, 7, 17), "FU", "FU1901.SHF", 1, 1, date(2018, 7, 16)
        ),
        DominantChoice(
            date(2018, 3, 30), "RB", "RB1810.SHF", 1, 1, date(2018, 3, 29)
        ),
    )
    factors = adjustment_factors(
        choices,
        closes={
            (date(2018, 3, 30), "FU1804.SHF"): 3200.0,
            (date(2018, 7, 16), "FU1901.SHF"): 2800.0,
            (date(2018, 3, 30), "RB1810.SHF"): 3700.0,
        },
    )

    assert list(factors["product"]) == ["FU", "FU", "RB"]
    assert list(factors["continuity_segment"]) == [0, 1, 0]
    assert list(factors.columns) == [
        "product",
        "trade_date",
        "contract",
        "adj_factor",
        "continuity_segment",
    ]


def test_adjustment_uses_the_latest_common_close_before_a_dominant_gap():
    """旧主力退市造成空档时，以换月前最近的新旧同日收盘衔接，不拿 1.0 顶替。"""
    old_day = date(2019, 12, 16)
    old_choice = DominantChoice(
        trade_date=date(2019, 12, 17),
        product="AU",
        contract="AU1912.SHF",
        oi=1,
        volume=1,
        selected_from=old_day,
    )
    new_choice = DominantChoice(
        trade_date=date(2019, 12, 30),
        product="AU",
        contract="AU2006.SHF",
        oi=1,
        volume=1,
        selected_from=date(2019, 12, 27),
    )

    factors = adjustment_factors(
        (old_choice, new_choice),
        closes={
            (date(2019, 12, 13), "AU1912.SHF"): 331.0,
            (date(2019, 12, 13), "AU2006.SHF"): 334.62,
            (old_day, "AU1912.SHF"): 333.0,
            (old_day, "AU2006.SHF"): 338.24,
        },
    )

    assert list(factors["adj_factor"]) == pytest.approx([1.0, 333.0 / 338.24])


def test_czce_symbol_alias_change_is_not_a_roll():
    """TA701 与 TA1701 是同一合约；代码位数变化不能产生虚假复权跳点。"""
    choices = (
        DominantChoice(date(2016, 1, 18), "TA", "TA701.CZC", 1, 1, date(2016, 1, 15)),
        DominantChoice(date(2016, 1, 19), "TA", "TA1701.CZC", 1, 1, date(2016, 1, 18)),
    )

    factors = adjustment_factors(choices, closes={})

    assert list(factors["adj_factor"]) == [1.0, 1.0]
    assert list(factors["continuity_segment"]) == [0, 0]
