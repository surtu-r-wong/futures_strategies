"""Shared deterministic fixtures for commodity minute-panel tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.sessions import (
    SessionRule,
    build_trading_slots,
    fifteen_minute_buckets,
)


TRADE_DATE = date(2024, 3, 5)
PREVIOUS_TRADE_DATE = date(2024, 3, 4)
CONTRACT = "RB2405.SHF"
SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def session():
    rule = SessionRule.day_only("SHFE", "RB", version="commodity-v1")
    slots = tuple(build_trading_slots(TRADE_DATE, PREVIOUS_TRADE_DATE, rule))
    return SimpleNamespace(
        rule=rule,
        slots=slots,
        buckets=tuple(fifteen_minute_buckets(slots, rule)),
    )


@pytest.fixture
def minute_frame(session):
    rows = []
    for index, slot in enumerate(session.slots[:20]):
        price = 100.0 + index
        volume = 1.0
        rows.append(
            {
                "trade_date": TRADE_DATE,
                "product": "RB",
                "daily_contract": CONTRACT,
                "bar_time": slot,
                "symbol": CONTRACT,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": volume,
                "open_interest": 100.0 + index,
                "amount": price * volume * 10,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def bundle_frames():
    """A complete two-product bundle with one causal dominant roll."""
    dates = pd.to_datetime(["2024-03-05", "2024-03-05", "2024-03-06", "2024-03-06"])
    slot_ends = pd.to_datetime(
        [
            "2024-03-05 09:15", "2024-03-05 09:15",
            "2024-03-06 09:15", "2024-03-06 09:15",
        ]
    ).tz_localize(SHANGHAI).astype("datetime64[ns, Asia/Shanghai]")
    fill_times = pd.to_datetime(
        [
            "2024-03-05 09:20", "2024-03-05 09:20",
            "2024-03-06 09:20", "2024-03-06 09:20",
        ]
    ).tz_localize(SHANGHAI).astype("datetime64[ns, Asia/Shanghai]")
    bars = pd.DataFrame(
        {
            "product": pd.Series(["RB", "TA", "RB", "TA"], dtype="string"),
            "contract": pd.Series(
                ["RB2405.SHF", "TA405.CZC", "RB2410.SHF", "TA405.CZC"],
                dtype="string",
            ),
            "trade_date": dates.astype("datetime64[ns]"),
            "slot_end": slot_ends,
            "open": pd.Series([100.0, 200.0, 110.0, 202.0], dtype="float64"),
            "high": pd.Series([102.0, 203.0, 112.0, 205.0], dtype="float64"),
            "low": pd.Series([99.0, 199.0, 109.0, 201.0], dtype="float64"),
            "close": pd.Series([101.0, 202.0, 111.0, 204.0], dtype="float64"),
            "volume": pd.Series([10.0, 20.0, 12.0, 22.0], dtype="float64"),
            "open_interest": pd.Series([1000.0, 2000.0, 1010.0, 2010.0], dtype="float64"),
            "no_trade": pd.Series([False, False, False, False], dtype="bool"),
            "adj_factor": pd.Series([1.0, 1.0, 0.95, 1.0], dtype="float64"),
            "fill_time": fill_times,
            "fill_price": pd.Series([101.5, 202.5, 111.5, 204.5], dtype="float64"),
            "fill_pending": pd.Series([False, False, False, False], dtype="bool"),
            "fill_unpriceable": pd.Series([False, False, False, False], dtype="bool"),
            "pricing_basis": pd.Series(
                ["amount_vwap", "ohlc_typical", "amount_vwap", "ohlc_typical"],
                dtype="string",
            ),
            "multiplier": pd.Series([10, 5, 10, 5], dtype="int64"),
        }
    )
    universes = pd.DataFrame(
        {
            "month_start": pd.to_datetime(["2024-03-01", "2024-03-01"]).astype(
                "datetime64[ns]"
            ),
            "product": pd.Series(["RB", "TA"], dtype="string"),
        }
    )
    dominants = pd.DataFrame(
        {
            "trade_date": dates.astype("datetime64[ns]"),
            "product": pd.Series(["RB", "TA", "RB", "TA"], dtype="string"),
            "contract": pd.Series(
                ["RB2405.SHF", "TA405.CZC", "RB2410.SHF", "TA405.CZC"],
                dtype="string",
            ),
            "oi": pd.Series([900, 1900, 1000, 2000], dtype="int64"),
            "volume": pd.Series([800, 1800, 900, 1900], dtype="int64"),
            "selected_from": pd.to_datetime(
                ["2024-03-04", "2024-03-04", "2024-03-05", "2024-03-05"]
            ).astype("datetime64[ns]"),
            "adj_factor": pd.Series([1.0, 1.0, 0.95, 1.0], dtype="float64"),
        }
    )
    roll_fills = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-03-06"]).astype("datetime64[ns]"),
            "product": pd.Series(["RB"], dtype="string"),
            "old_contract": pd.Series(["RB2405.SHF"], dtype="string"),
            "new_contract": pd.Series(["RB2410.SHF"], dtype="string"),
            "fill_time": pd.to_datetime(["2024-03-06 09:04"])
            .tz_localize(SHANGHAI)
            .astype("datetime64[ns, Asia/Shanghai]"),
            "old_price": pd.Series([109.5], dtype="float64"),
            "new_price": pd.Series([111.5], dtype="float64"),
            "old_pricing_basis": pd.Series(["amount_vwap"], dtype="string"),
            "new_pricing_basis": pd.Series(["amount_vwap"], dtype="string"),
        }
    )
    return {
        "bars": bars,
        "universes": universes,
        "dominants": dominants,
        "roll_fills": roll_fills,
    }
