"""Shared deterministic fixtures for commodity minute-panel tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

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
