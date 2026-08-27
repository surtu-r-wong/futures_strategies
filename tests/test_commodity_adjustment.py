"""商品期货后复权连续价。"""

from datetime import date

import pytest

from common.commodity.continuous import adjustment_factors, continuous_close
from common.dominant import DominantChoice

D = [date(2024, 3, day) for day in (1, 4, 5, 6, 7, 8)]


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
    closes.pop((D[3], "RB2501.SHF"))
    with pytest.raises(ValueError, match="roll_close_missing"):
        adjustment_factors(choices, closes=closes)


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
