from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cta_gtja.data import CTADataSet
from cta_gtja.factors import price_volume_cta_factors
from cta_gtja.strategies import run_medium_equal_weight


PILOT_SYMBOLS = (
    "M",
    "RB",
    "CU",
    "AL",
    "TA",
    "PP",
    "MA",
    "BU",
    "RU",
)


@pytest.fixture
def complete_price_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", periods=320).date
    rows = []
    for symbol_position, symbol in enumerate(PILOT_SYMBOLS):
        for date_position, trade_date in enumerate(dates):
            trend = (symbol_position - 4) * date_position * 0.015
            cycle = np.sin(date_position / 17 + symbol_position) * 2.5
            close = 100 + symbol_position * 12 + trend + cycle
            rows.append(
                {
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "open": close * (
                        1
                        + 0.001
                        * np.cos(date_position / 13 + symbol_position)
                    ),
                    "close": close,
                    "volume": (
                        1000
                        + symbol_position * 100
                        + date_position * 0.5
                        + np.cos(date_position / 11) * 20
                    ),
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def complete_fundamental_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", periods=320).date
    rows = []
    for symbol_position, symbol in enumerate(PILOT_SYMBOLS):
        for date_position, trade_date in enumerate(dates):
            basis = (
                symbol_position - 4
            ) * 0.002 + np.sin(date_position / 19) * 0.005
            rows.append(
                {
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "spot": (
                        100
                        + symbol_position * 12
                        + date_position * 0.02
                    ),
                    "basis_rate": basis,
                    "inventory": (
                        500
                        + symbol_position * 25
                        + (4 - symbol_position) * date_position * 0.1
                    ),
                    "profit": (
                        30
                        + symbol_position * 3
                        + np.cos(date_position / 23 + symbol_position) * 5
                    ),
                }
            )
    return pd.DataFrame(rows)


def test_price_volume_control_is_independent_of_fundamental_values(
    complete_price_frame,
    complete_fundamental_frame,
):
    baseline_data = CTADataSet(
        prices=complete_price_frame,
        fundamentals=complete_fundamental_frame,
    )
    changed_fundamentals = complete_fundamental_frame.copy()
    columns = ["spot", "basis_rate", "inventory", "profit"]
    changed_fundamentals[columns] = (
        changed_fundamentals[columns] * 1000 + 777
    )
    changed_data = CTADataSet(
        prices=complete_price_frame.copy(),
        fundamentals=changed_fundamentals,
    )

    baseline = run_medium_equal_weight(
        baseline_data,
        symbols=list(PILOT_SYMBOLS),
        factors=price_volume_cta_factors(),
    )
    changed = run_medium_equal_weight(
        changed_data,
        symbols=list(PILOT_SYMBOLS),
        factors=price_volume_cta_factors(),
    )

    pd.testing.assert_frame_equal(baseline.weights, changed.weights)
    pd.testing.assert_series_equal(
        baseline.period_returns,
        changed.period_returns,
    )
    pd.testing.assert_series_equal(baseline.equity, changed.equity)


def test_future_fundamentals_cannot_change_past_six_factor_results(
    complete_price_frame,
    complete_fundamental_frame,
):
    dates = sorted(complete_fundamental_frame["trade_date"].unique())
    assert len(PILOT_SYMBOLS) == 9
    assert set(complete_fundamental_frame["symbol"].unique()) == set(PILOT_SYMBOLS)
    cutoff = dates[260]
    metadata = {
        "source": "standard",
        "pit_mode": "conservative",
        "materialized_daily": True,
    }
    baseline_data = CTADataSet(
        prices=complete_price_frame,
        fundamentals=complete_fundamental_frame,
        fundamental_metadata=metadata,
    )
    mutated_fundamentals = complete_fundamental_frame.copy()
    future = mutated_fundamentals["trade_date"] > cutoff
    columns = ["spot", "basis_rate", "inventory", "profit"]
    mutated_fundamentals.loc[future, columns] *= -1000.0
    # Scaling alone no longer trips the inventory-side check: that check now
    # reads the demeaned signal, which stays two-sided however the raw scores
    # are scaled or flipped. Blank most products' inventory after the cutoff so
    # the corruption is one the corrected gates actually catch.
    blackout = future & mutated_fundamentals["symbol"].isin(
        sorted(PILOT_SYMBOLS)[:7]
    )
    mutated_fundamentals.loc[blackout, "inventory"] = float("nan")
    mutated_data = CTADataSet(
        prices=complete_price_frame.copy(),
        fundamentals=mutated_fundamentals,
        fundamental_metadata=dict(metadata),
    )

    enforced_baseline = run_medium_equal_weight(
        baseline_data,
        symbols=list(PILOT_SYMBOLS),
    )
    # The corruption intentionally makes the fundamental audit fail after the
    # cutoff.  Run both comparison arms without raising, while the separate
    # baseline above proves the original fixture passes the enforced gate.
    baseline = run_medium_equal_weight(
        baseline_data,
        symbols=list(PILOT_SYMBOLS),
        enforce_coverage=False,
    )
    mutated = run_medium_equal_weight(
        mutated_data,
        symbols=list(PILOT_SYMBOLS),
        enforce_coverage=False,
    )

    assert not enforced_baseline.fundamental_coverage.empty
    assert enforced_baseline.fundamental_coverage["status"].eq("pass").all()
    inventory_sides = enforced_baseline.fundamental_coverage.loc[
        enforced_baseline.fundamental_coverage["check"].eq("inventory_sides")
    ]
    assert not inventory_sides.empty
    assert inventory_sides["long_candidates"].min() >= 2
    assert inventory_sides["short_candidates"].min() >= 2
    mutated_failures = mutated.fundamental_coverage.loc[
        mutated.fundamental_coverage["status"].eq("fail")
    ]
    assert not mutated_failures.empty
    assert (mutated_failures["trade_date"].dt.date > cutoff).all()

    pd.testing.assert_frame_equal(
        baseline.weights.loc[baseline.weights.index <= cutoff],
        mutated.weights.loc[mutated.weights.index <= cutoff],
    )
    pd.testing.assert_frame_equal(
        baseline.factor_returns.loc[baseline.factor_returns.index <= cutoff],
        mutated.factor_returns.loc[mutated.factor_returns.index <= cutoff],
    )
    pd.testing.assert_series_equal(
        baseline.period_returns.loc[baseline.period_returns.index <= cutoff],
        mutated.period_returns.loc[mutated.period_returns.index <= cutoff],
    )
    pd.testing.assert_series_equal(
        baseline.equity.loc[baseline.equity.index <= cutoff],
        mutated.equity.loc[mutated.equity.index <= cutoff],
    )
