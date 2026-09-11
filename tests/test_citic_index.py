from datetime import date

import pandas as pd
import pytest

from citic_index.index import accumulate, blended_returns


def _weights(rows):
    """rows: (day, product, weight)."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "product": [r[1] for r in rows],
            "weight": [r[2] for r in rows],
        }
    )


def _returns(rows):
    """rows: (day, product, product_return)."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "product": [r[1] for r in rows],
            "product_return": [r[2] for r in rows],
        }
    )


def test_index_compounds_the_weighted_cross_section_from_the_base():
    # w = [-0.5, +0.5] against r = [-0.02, +0.04] is 0.01 + 0.02 = 0.03.
    weights = _weights([("2010-01-04", "A", -0.5), ("2010-01-04", "B", 0.5)])
    returns = _returns([("2010-01-05", "A", -0.02), ("2010-01-05", "B", 0.04)])
    out = accumulate(weights, returns, base_date=date(2010, 1, 4), base_value=1000.0)
    series = out.set_index("trade_date")["index_value"]
    assert series[date(2010, 1, 4)] == pytest.approx(1000.0)
    assert series[date(2010, 1, 5)] == pytest.approx(1030.0)
    assert out.set_index("trade_date")["daily_return"][date(2010, 1, 5)] == pytest.approx(0.03)


def test_the_weights_struck_on_a_day_are_applied_to_the_next_days_returns():
    # 3.5: "假设 T 日的指数值为 MoMI_T，计算 T+1 日的指数值" -- the weights come out
    # of T's data and earn T+1's returns.  Using the same day's return would be
    # a one-day lookahead and would show up as a lag-one correlation later.
    weights = _weights(
        [
            ("2010-01-04", "A", 1.0),
            ("2010-01-05", "A", 0.0),
        ]
    )
    returns = _returns(
        [
            ("2010-01-04", "A", 0.99),   # must never be earned: it precedes the strike
            ("2010-01-05", "A", 0.02),   # earned by 01-04's weight of 1.0
            ("2010-01-06", "A", 0.50),   # 01-05 struck 0.0, so this earns nothing
        ]
    )
    out = accumulate(weights, returns, base_date=date(2010, 1, 4), base_value=1000.0)
    series = out.set_index("trade_date")["index_value"]
    assert series[date(2010, 1, 4)] == pytest.approx(1000.0)
    assert series[date(2010, 1, 5)] == pytest.approx(1020.0)
    assert series[date(2010, 1, 6)] == pytest.approx(1020.0)


def test_a_product_without_a_return_that_day_simply_does_not_contribute():
    weights = _weights([("2010-01-04", "A", -0.5), ("2010-01-04", "B", 0.5)])
    returns = _returns([("2010-01-05", "B", 0.04)])
    out = accumulate(weights, returns, base_date=date(2010, 1, 4), base_value=1000.0)
    # Only B's leg is earned: 0.5 * 0.04 = 0.02.
    assert out.set_index("trade_date")["index_value"][date(2010, 1, 5)] == pytest.approx(1020.0)


def _roll_prices():
    # OLD runs 300 -> 312 (+4%), NEW runs 100 -> 108 (+8%), and the chain
    # switches to NEW on 01-05.
    return pd.DataFrame(
        {
            "trade_date": [date(2010, 1, 4)] * 2 + [date(2010, 1, 5)] * 2,
            "contract": ["M2001.DCE", "M2005.DCE", "M2001.DCE", "M2005.DCE"],
            "close": [300.0, 100.0, 312.0, 108.0],
        }
    )


def _roll_chain():
    return pd.DataFrame(
        {
            "trade_date": [date(2010, 1, 4), date(2010, 1, 5)],
            "product": "M",
            "chain_contract": ["M2001.DCE", "M2005.DCE"],
        }
    )


def test_a_roll_day_blends_the_two_contracts_by_their_value_share():
    # 3.5 step 4: r = w_bar * r_new + (1 - w_bar) * r_old, w_bar the new
    # contract's share of contract value.  Value at the start of the day is
    # 100 against 300, so w_bar = 0.25 and r = 0.25*0.08 + 0.75*0.04 = 0.05.
    out = blended_returns(_roll_prices(), _roll_chain(), roll_blend=True)
    row = out.loc[out["trade_date"] == date(2010, 1, 5)].iloc[0]
    assert row["product_return"] == pytest.approx(0.05)
    assert row["value_share_new"] == pytest.approx(0.25)
    assert bool(row["is_roll"]) is True


def test_without_blending_a_roll_day_earns_the_contract_held_into_it():
    # Deviation 7: the production chain prices the contract it held at
    # yesterday's close, so a roll costs nothing and the day earns 4%.
    out = blended_returns(_roll_prices(), _roll_chain(), roll_blend=False)
    row = out.loc[out["trade_date"] == date(2010, 1, 5)].iloc[0]
    assert row["product_return"] == pytest.approx(0.04)


def test_an_ordinary_day_is_just_the_held_contracts_return():
    prices = pd.DataFrame(
        {
            "trade_date": [date(2010, 1, 4), date(2010, 1, 5)],
            "contract": "M2001.DCE",
            "close": [300.0, 312.0],
        }
    )
    chain = pd.DataFrame(
        {
            "trade_date": [date(2010, 1, 4), date(2010, 1, 5)],
            "product": "M",
            "chain_contract": "M2001.DCE",
        }
    )
    out = blended_returns(prices, chain, roll_blend=True)
    row = out.loc[out["trade_date"] == date(2010, 1, 5)].iloc[0]
    assert row["product_return"] == pytest.approx(0.04)
    assert bool(row["is_roll"]) is False
    # No roll means no share to report: the column is a roll diagnostic, and a
    # 0.5 sitting there on every ordinary day would make it useless as one.
    assert pd.isna(row["value_share_new"])


def test_a_roll_into_a_contract_with_no_prior_close_falls_back_to_the_old_leg():
    # The new contract listed today, so it has no return to blend.  Earning the
    # old leg is the only honest answer; synthesising one is not.
    prices = _roll_prices()
    prices = prices.loc[
        ~(
            (prices["trade_date"] == date(2010, 1, 4))
            & (prices["contract"] == "M2005.DCE")
        )
    ]
    out = blended_returns(prices, _roll_chain(), roll_blend=True)
    row = out.loc[out["trade_date"] == date(2010, 1, 5)].iloc[0]
    assert row["product_return"] == pytest.approx(0.04)
    assert pd.isna(row["value_share_new"])


def test_the_index_compounds_rather_than_adding_up_the_returns():
    # Two 10% days are 1.21, not 1.20.  With a single non-zero day the two are
    # the same number, which is why every earlier test here would pass against
    # an implementation that just added the returns up.
    weights = _weights(
        [("2010-01-04", "A", 1.0), ("2010-01-05", "A", 1.0)]
    )
    returns = _returns(
        [("2010-01-05", "A", 0.10), ("2010-01-06", "A", 0.10)]
    )
    out = accumulate(weights, returns, base_date=date(2010, 1, 4), base_value=1000.0)
    series = out.set_index("trade_date")["index_value"]
    assert series[date(2010, 1, 5)] == pytest.approx(1100.0)
    assert series[date(2010, 1, 6)] == pytest.approx(1210.0)
