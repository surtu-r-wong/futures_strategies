from datetime import date

import pandas as pd
import pytest

from citic_index.index import accumulate


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
