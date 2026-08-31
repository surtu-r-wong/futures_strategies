"""Shared shadow-execution helpers: continuity segments and their guards."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from common.commodity.execution import (
    prepare_bars,
    segment_slices,
    unexecutable_transitions,
)


TZ = ZoneInfo("Asia/Shanghai")


def _bars(segments: list[int]) -> pd.DataFrame:
    count = len(segments)
    days = pd.bdate_range("2024-01-02", periods=count)
    slot_end = pd.DatetimeIndex(
        [pd.Timestamp(datetime.combine(d.date(), time(14, 45), tzinfo=TZ)) for d in days]
    )
    close = np.full(count, 100.0)
    return pd.DataFrame(
        {
            "product": pd.Series(["RB"] * count, dtype="string"),
            "contract": pd.Series(["RB2405.SHF"] * count, dtype="string"),
            "trade_date": [d.date() for d in days],
            "slot_end": slot_end,
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": np.full(count, 10.0),
            "open_interest": np.full(count, 100.0),
            "no_trade": np.zeros(count, dtype="bool"),
            "adj_factor": np.ones(count),
            "continuity_segment": np.asarray(segments, dtype="int64"),
            "fill_time": slot_end + pd.Timedelta(minutes=5),
            "fill_price": close,
            "fill_pending": np.zeros(count, dtype="bool"),
            "fill_unpriceable": np.zeros(count, dtype="bool"),
            "pricing_basis": pd.Series(["amount_vwap"] * count, dtype="string"),
            "multiplier": np.full(count, 10, dtype="int64"),
        }
    )


def test_segment_slices_returns_contiguous_runs() -> None:
    assert segment_slices(np.array([0, 0, 0, 1, 1, 2])) == [
        slice(0, 3),
        slice(3, 5),
        slice(5, 6),
    ]


def test_segment_slices_of_one_segment_is_one_run() -> None:
    assert segment_slices(np.array([0, 0, 0])) == [slice(0, 3)]


def test_segment_slices_of_nothing_is_nothing() -> None:
    assert segment_slices(np.array([], dtype="int64")) == []


def test_prepare_bars_requires_the_segment_column() -> None:
    frame = _bars([0, 0]).drop(columns=["continuity_segment"])

    with pytest.raises(ValueError, match="shadow_bars_columns"):
        prepare_bars(frame, "RB")


def test_a_segment_that_goes_backwards_is_rejected() -> None:
    # Segments only ever open; going back would mean an old price basis
    # silently returned, and every indicator would span the break.
    with pytest.raises(ValueError, match="shadow_continuity_segment"):
        prepare_bars(_bars([0, 1, 0]), "RB")


def test_a_negative_segment_is_rejected() -> None:
    with pytest.raises(ValueError, match="shadow_continuity_segment"):
        prepare_bars(_bars([0, -1]), "RB")


def test_a_well_ordered_segment_column_survives_preparation() -> None:
    frame, _ = prepare_bars(_bars([0, 0, 1]), "RB")

    assert frame["continuity_segment"].tolist() == [0, 0, 1]


def test_a_switch_without_a_fill_marks_the_bar_before_it_as_a_break():
    """换了合约却没有换月成交单 —— 与断代同样处理：上一根平仓，新合约重新开始。"""
    traded = pd.DataFrame(
        {
            "trade_date": [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)],
            "contract": ["RB2405.SHF", "RB2405.SHF", "RB2410.SHF"],
        },
        index=[10, 11, 12],
    )

    break_bars, switch_bars = unexecutable_transitions(traded, pd.DataFrame())

    assert break_bars == frozenset({11})
    assert switch_bars == frozenset({12})


def test_a_switch_with_its_fill_is_not_a_break():
    traded = pd.DataFrame(
        {
            "trade_date": [date(2024, 3, 5), date(2024, 3, 6)],
            "contract": ["RB2405.SHF", "RB2410.SHF"],
        },
        index=[11, 12],
    )
    rolls = pd.DataFrame(
        {
            "trade_date": [date(2024, 3, 6)],
            "old_contract": ["RB2405.SHF"],
            "new_contract": ["RB2410.SHF"],
        }
    )

    assert unexecutable_transitions(traded, rolls) == (frozenset(), frozenset())
