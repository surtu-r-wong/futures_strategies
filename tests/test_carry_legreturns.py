from datetime import date

import pandas as pd
import pytest

from cta_carry.legreturns import build_leg_returns, forward_only_chain


def _prices(rows):
    return pd.DataFrame(
        rows,
        columns=["trade_date", "product", "contract", "delivery_yyyymm", "close"],
    )


def test_forward_only_chain_refuses_to_step_back_to_an_earlier_delivery():
    picks = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405),
            (date(2024, 1, 3), "RB", "RB2403.SHF", 202403),  # OI 抖回近月
            (date(2024, 1, 4), "RB", "RB2410.SHF", 202410),
        ],
        columns=["trade_date", "product", "contract", "delivery_yyyymm"],
    )
    chain = forward_only_chain(picks)
    assert chain["chain_contract"].tolist() == [
        "RB2405.SHF",
        "RB2405.SHF",  # 不回头
        "RB2410.SHF",
    ]


def test_leg_return_prices_the_contract_held_into_today_not_todays_pick():
    prices = _prices(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405, 100.0),
            (date(2024, 1, 3), "RB", "RB2405.SHF", 202405, 110.0),
            (date(2024, 1, 3), "RB", "RB2410.SHF", 202410, 300.0),
        ]
    )
    chain = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF"),
            (date(2024, 1, 3), "RB", "RB2410.SHF"),  # 今天换月
        ],
        columns=["trade_date", "product", "chain_contract"],
    )
    returns = build_leg_returns(prices, chain)
    row = returns.loc[returns["trade_date"] == date(2024, 1, 3)].iloc[0]
    # 换月当天赚的是昨天那张合约的钱，不是新旧两张的价差
    assert row["leg_return"] == pytest.approx(0.10)


def test_leg_return_is_dropped_when_the_held_contract_has_no_bar_today():
    prices = _prices(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405, 100.0),
            (date(2024, 1, 3), "RB", "RB2410.SHF", 202410, 300.0),
        ]
    )
    chain = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF"),
            (date(2024, 1, 3), "RB", "RB2410.SHF"),
        ],
        columns=["trade_date", "product", "chain_contract"],
    )
    returns = build_leg_returns(prices, chain)
    # RB2405 今天没有 K 线 -> 该 product-day 无收益，绝不用别的合约顶上
    assert returns.loc[returns["trade_date"] == date(2024, 1, 3)].empty
