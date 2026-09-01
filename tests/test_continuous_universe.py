"""成交额宇宙筛选（计划 Task 1）。

研报口径：过去半年中日均成交额超过 50 亿元的商品期货全品种。
"""

from datetime import date

import pandas as pd
import pytest

from cta_continuous.universe import (
    FINANCIAL_FUTURES,
    TURNOVER_THRESHOLD,
    canonical_contract,
    product_daily_turnover,
    universe_for_month,
)


def _daily(rows):
    return pd.DataFrame(rows, columns=["symbol", "trade_date", "turnover"])


def test_turnover_sums_every_contract_of_a_product():
    """成交额是**品种**级的：该品种全部合约加起来，不是只看主力。"""
    frame = _daily(
        [
            ("RB2410.SHF", date(2024, 3, 1), 30e8),
            ("RB2501.SHF", date(2024, 3, 1), 20e8),
            ("CU2404.SHF", date(2024, 3, 1), 70e8),
        ]
    )
    result = product_daily_turnover(frame).set_index(["product", "trade_date"])
    assert result.loc[("RB", date(2024, 3, 1)), "turnover"] == pytest.approx(50e8)
    assert result.loc[("CU", date(2024, 3, 1)), "turnover"] == pytest.approx(70e8)


def test_turnover_deduplicates_the_czce_three_and_four_digit_twins():
    """上游缺陷：2015-2017 郑商所同一合约被 3 位与 4 位代码各存一份、逐字段相同。

    去重键**不能**是 symbol —— 两份的 symbol 本来就不同。必须先把交割码归一到
    四位，否则该品种成交额直接翻倍（实测平均高估 18.3%、最高 50%）。
    """
    frame = _daily(
        [
            ("TA701.CZC", date(2016, 1, 18), 47.859e8),
            ("TA1701.CZC", date(2016, 1, 18), 47.859e8),
        ]
    )
    result = product_daily_turnover(frame)
    assert len(result) == 1
    assert result.iloc[0]["turnover"] == pytest.approx(47.859e8)


def test_canonical_contract_resolves_the_three_digit_delivery_code():
    assert canonical_contract("TA701.CZC", date(2016, 1, 18)) == "CZC:TA:1701"
    assert canonical_contract("TA1701.CZC", date(2016, 1, 18)) == "CZC:TA:1701"
    assert canonical_contract("RB2410.SHF", date(2024, 3, 1)) == "SHF:RB:2410"


def test_canonical_contract_returns_none_for_synthetic_codes():
    """`futures_daily` 里混着 Wind 的连续/主力合成码，它们不是可交易合约。"""
    assert canonical_contract("TA00.CZC", date(2016, 1, 18)) is None
    assert canonical_contract("RBL0.SHF", date(2024, 3, 1)) is None


def test_universe_excludes_financial_futures():
    """研报是商品期货全品种；股指与国债不在内。"""
    days = pd.date_range("2024-01-01", "2024-06-28", freq="B").date
    rows = []
    for day in days:
        rows.append(("RB2410.SHF", day, 200e8))
        rows.append(("IF2406.CFE", day, 900e8))
        rows.append(("T2406.CFE", day, 900e8))
    picked = universe_for_month(
        product_daily_turnover(_daily(rows)), month_start=date(2024, 7, 1)
    )
    assert picked == ("RB",)


def test_universe_window_stops_at_the_previous_month_end():
    """D10：窗口截止上一自然月末。当月自己的成交额一律不许进窗口，否则是前视。"""
    window_days = pd.date_range("2024-01-01", "2024-06-28", freq="B").date
    current_days = pd.date_range("2024-07-01", "2024-07-31", freq="B").date
    rows = [("RB2410.SHF", day, 10e8) for day in window_days]
    # 当月成交额爆炸性放大；只要它进了窗口，RB 就会入池。
    rows += [("RB2410.SHF", day, 100000e8) for day in current_days]
    picked = universe_for_month(
        product_daily_turnover(_daily(rows)), month_start=date(2024, 7, 1)
    )
    assert picked == ()


def test_universe_averages_over_observed_market_days_not_the_products_own_days():
    """D15：分母是窗口内**全市场**观测到的交易日数，品种缺席那天按 0 计。

    否则一个刚挂牌、只交易了三天但每天 200 亿的品种会被算成"日均 200 亿"入池。
    """
    days = list(pd.date_range("2024-01-01", "2024-06-28", freq="B").date)
    rows = [("CU2404.SHF", day, 100e8) for day in days]          # 撑起市场日历
    rows += [("XX2410.DCE", day, 200e8) for day in days[-3:]]    # 新品种只有三天
    picked = universe_for_month(
        product_daily_turnover(_daily(rows)), month_start=date(2024, 7, 1)
    )
    assert picked == ("CU",)


def test_universe_threshold_is_five_billion_yuan():
    assert TURNOVER_THRESHOLD == 5e9


def test_financial_exclusion_does_not_drift_from_the_other_commodity_path():
    """本仓另外两条商品路径用的是同一个排除集；两处分叉迟早出事。"""
    from cta_gtja.pg_source import FINANCIAL_FUTURES as gtja_set

    assert FINANCIAL_FUTURES == gtja_set
