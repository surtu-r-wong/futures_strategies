from datetime import date

import pandas as pd
import pytest

from citic_index.universe import NAMED_37, pool_membership


def _flat_product(product, days, *, close=3000.0, volume=1000.0, oi=100000.0, multiplier=10.0):
    """One contract a day, every number constant, so the pool maths is checkable by hand."""
    return pd.DataFrame(
        {
            "trade_date": [d.date() for d in days],
            "product": product,
            "contract": f"{product}2405.DCE",
            "close": close,
            "volume": volume,
            "oi": oi,
            "turnover": close * volume * multiplier,
        }
    )


def test_named_universe_is_the_thirty_seven_from_section_3_2():
    assert len(NAMED_37) == 37
    # 3.2 names the metals explicitly; the production carry config excludes them,
    # which is deviation 5 in the design doc.
    assert {"AL", "AU", "AG", "CU", "ZN", "NI"} <= NAMED_37
    # Products that joined the market after the list was written are not on it.
    assert "SA" not in NAMED_37
    assert "LC" not in NAMED_37


def test_liquidity_is_open_interest_value_against_the_two_billion_threshold():
    days = pd.bdate_range("2024-01-02", periods=30)
    # oi * close * multiplier = 100000 * 3000 * 10 = 3.0e9, over the 2e9 threshold.
    rich = pool_membership(
        _flat_product("M", days),
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=0,
    )
    assert rich["liquidity_mean"].dropna().unique().tolist() == [3.0e9]
    assert rich.loc[rich["trade_date"] == days[19].date(), "in_pool"].item() is True

    # Same book at a tenth of the open interest is 3.0e8 and stays out.
    poor = pool_membership(
        _flat_product("M", days, oi=10000.0),
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=0,
    )
    assert poor["liquidity_mean"].dropna().unique().tolist() == [3.0e8]
    assert not poor["in_pool"].any()


def test_the_liquidity_window_is_twenty_trading_days():
    days = pd.bdate_range("2024-01-02", periods=30)
    pool = pool_membership(
        _flat_product("M", days),
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=0,
    )
    # Nineteen days is not a window: the mean is undefined and the product is out.
    assert pool["liquidity_mean"].isna().sum() == 19
    assert not pool.loc[pool["trade_date"] == days[18].date(), "in_pool"].item()
    assert pool.loc[pool["trade_date"] == days[19].date(), "in_pool"].item()


def test_a_product_needs_three_months_of_listing():
    days = pd.bdate_range("2024-01-02", periods=70)
    pool = pool_membership(
        _flat_product("M", days),
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=90,
    )
    first = pool.loc[pool["in_pool"], "trade_date"].min()
    # 2024-01-02 plus 90 calendar days is 2024-04-01 (2024 is a leap year), and
    # that Monday is a trading day, so it is the first day the gate lets M in.
    assert first == date(2024, 4, 1)
    assert (first - date(2024, 1, 2)).days == 90


def test_restricting_to_the_named_list_drops_everything_else():
    days = pd.bdate_range("2024-01-02", periods=30)
    prices = pd.concat(
        [_flat_product("M", days), _flat_product("SA", days)], ignore_index=True
    )
    restricted = pool_membership(
        prices,
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=0,
        restrict_to_named=True,
    )
    assert set(restricted.loc[restricted["in_pool"], "product"]) == {"M"}
    assert (
        restricted.loc[
            (restricted["product"] == "SA") & restricted["trade_date"].eq(days[19].date()),
            "reason",
        ].item()
        == "not_in_named_universe"
    )

    # The switch exists so the attribution task can price deviation 5.
    open_universe = pool_membership(
        prices,
        liquidity_window=20,
        threshold=2e9,
        min_listing_calendar_days=0,
        restrict_to_named=False,
    )
    assert set(open_universe.loc[open_universe["in_pool"], "product"]) == {"M", "SA"}


def test_the_multiplier_is_an_expanding_median_so_it_never_looks_ahead():
    days = pd.bdate_range("2024-01-02", periods=25)
    prices = _flat_product("M", days)
    # The exchange doubles the contract size from day 11 on, so the sample is
    # [10]*10 + [20]*15.  Its whole-sample median is 20 -- if the estimate ever
    # reads 20 on day one, it has seen a change that had not happened yet.
    changed = prices["trade_date"] >= days[10].date()
    prices.loc[changed, "turnover"] *= 2.0
    pool = pool_membership(
        prices, liquidity_window=20, threshold=2e9, min_listing_calendar_days=0
    )
    multiplier = pool["multiplier"].tolist()
    assert multiplier[0] == pytest.approx(10.0)
    # Days 1-19 hold a majority of 10s, day 20 is the even split of 10 and 20,
    # and from day 21 the 20s are the majority.
    assert multiplier[18] == pytest.approx(10.0)
    assert multiplier[19] == pytest.approx(15.0)
    assert multiplier[20] == pytest.approx(20.0)
    assert multiplier[-1] == pytest.approx(20.0)
