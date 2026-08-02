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
