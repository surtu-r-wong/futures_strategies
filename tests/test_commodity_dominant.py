"""商品期货主力合约选择。"""

from datetime import date

import pandas as pd

from common.commodity.dominant import choose_dominant_commodity


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


def test_a_contract_that_never_traded_is_not_the_volume_maximum():
    """挂牌但零成交的合约不能当主力 —— 「成交量最大」以成交为前提。

    真实数据里有：`OI1307.CZC` 2012 年 7 月逐日 `oi=0 volume=0`、收盘冻在 10052，
    却因为是当日唯一一张菜籽油合约而被选成主力，于是面板为一个没有交易的品种日
    索取时段规则。
    """
    frame = _pool(
        [
            (D[0], "OI1307.CZC", 0, 0),
            (D[1], "OI1307.CZC", 0, 0),
            (D[2], "OI1307.CZC", 0, 0),
        ]
    )

    assert choose_dominant_commodity(frame, products=("OI",)) == ()


def test_a_held_dominant_that_stops_trading_is_not_carried_onto_a_dead_day():
    """沿用只在旧主力**当日仍在交易**时成立。

    D11 的注释本来就是这个意思（「不能被伪造成 oi=volume=0 的可交易主力」），但它
    只拦了合约整行缺席，没拦行在、量为零。
    """
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 300),
            (D[1], "RB2405.SHF", 100, 300),
            (D[2], "RB2405.SHF", 100, 0),
            (D[3], "RB2405.SHF", 100, 0),
        ]
    )

    chosen = choose_dominant_commodity(frame, products=("RB",))

    assert [c.trade_date for c in chosen] == [D[1], D[2]]
    assert all(c.volume > 0 for c in chosen)


def test_a_traded_day_still_selects_normally():
    frame = _pool(
        [
            (D[0], "RB2405.SHF", 100, 300),
            (D[0], "RB2410.SHF", 90, 100),
            (D[1], "RB2405.SHF", 100, 300),
            (D[1], "RB2410.SHF", 90, 100),
        ]
    )

    chosen = choose_dominant_commodity(frame, products=("RB",))

    assert [c.contract for c in chosen] == ["RB2405.SHF"]
