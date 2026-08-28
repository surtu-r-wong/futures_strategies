"""One product's Dow shadow: gates, rolls, causality, and the two modes."""

from __future__ import annotations

from datetime import datetime, time
import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from cta_dow.shadow import ShadowResult, run_shadow_product


TZ = ZoneInfo("Asia/Shanghai")

SIGNAL_COLUMNS = [
    "signal_close",
    "trend",
    "trend_changed",
    "turning_valid",
    "dow_resonance",
    "close_breakout",
    "enough_history",
    "segment_high",
    "segment_low",
    "last_up_high_1",
    "last_up_high_2",
    "last_down_low_1",
    "last_down_low_2",
    "action",
    "target_weight",
]


#: 单根 bar 的高低幅。它必须比一个 bar 的涨幅小，否则"收盘价突破本段之前的最高价"
#: 永远不成立 —— 上一根的最高价 = 上一根收盘 + BAND，一根都涨不过去。
BAND = 0.6


def _dow_panel(
    *, count: int = 400, roll_at: int | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """An up-drifting zigzag on the *adjusted* series, optionally rolled.

    The adjusted path is identical with and without the roll; only the raw
    contract prices and the adjustment factor differ. That makes "a roll moves
    execution but never the signal path" a checkable claim.
    """
    days = pd.bdate_range("2023-01-02", periods=count)
    adjusted_close = np.array(
        [100.0 + 0.35 * index + 7.0 * math.sin(2 * math.pi * index / 25.0)
         for index in range(count)]
    )
    factors = np.ones(count)
    contracts = ["RB2405.SHF"] * count
    if roll_at is not None:
        factors[roll_at:] = 1.5
        contracts[roll_at:] = ["RB2410.SHF"] * (count - roll_at)

    raw_close = adjusted_close / factors
    raw_high = (adjusted_close + BAND) / factors
    raw_low = (adjusted_close - BAND) / factors
    slot_end = pd.DatetimeIndex(
        [pd.Timestamp(datetime.combine(day.date(), time(14, 45), tzinfo=TZ)) for day in days]
    )
    bars = pd.DataFrame(
        {
            "product": pd.Series(["RB"] * count, dtype="string"),
            "contract": pd.Series(contracts, dtype="string"),
            "trade_date": [day.date() for day in days],
            "slot_end": slot_end,
            "open": raw_close,
            "high": raw_high,
            "low": raw_low,
            "close": raw_close,
            "volume": np.full(count, 100.0),
            "open_interest": np.arange(count, dtype="float64") + 1000.0,
            "no_trade": np.zeros(count, dtype="bool"),
            "adj_factor": factors,
            "continuity_segment": np.zeros(count, dtype="int64"),
            "fill_time": slot_end + pd.Timedelta(minutes=5),
            "fill_price": raw_close,
            "fill_pending": np.zeros(count, dtype="bool"),
            "fill_unpriceable": np.zeros(count, dtype="bool"),
            "pricing_basis": pd.Series(["amount_vwap"] * count, dtype="string"),
            "multiplier": np.full(count, 10, dtype="int64"),
        }
    )
    if roll_at is None:
        rolls = pd.DataFrame(
            columns=[
                "trade_date", "product", "old_contract", "new_contract", "fill_time",
                "old_price", "new_price", "old_pricing_basis", "new_pricing_basis",
            ]
        )
    else:
        rolls = pd.DataFrame(
            [
                {
                    "trade_date": days[roll_at].date(),
                    "product": "RB",
                    "old_contract": "RB2405.SHF",
                    "new_contract": "RB2410.SHF",
                    "fill_time": slot_end[roll_at] - pd.Timedelta(minutes=5),
                    "old_price": float(adjusted_close[roll_at - 1]),
                    "new_price": float(raw_close[roll_at]),
                    "old_pricing_basis": "amount_vwap",
                    "new_pricing_basis": "amount_vwap",
                }
            ]
        )
    return bars, rolls


def _run(**kwargs) -> ShadowResult:
    bars, rolls = _dow_panel(**{k: v for k, v in kwargs.items() if k in {"count", "roll_at"}})
    options = {k: v for k, v in kwargs.items() if k not in {"count", "roll_at"}}
    return run_shadow_product(bars, product="RB", roll_fills=rolls, **options)


def test_every_dow_entry_has_all_four_gates_open() -> None:
    result = _run()

    entries = result.signals.loc[result.signals["action"] == "dow_entry"]
    assert not entries.empty
    assert entries["turning_valid"].all()
    assert entries["dow_resonance"].all()
    assert entries["close_breakout"].all()
    assert entries["enough_history"].all()
    assert entries["trend"].isin(["up", "down"]).all()


def test_a_roll_moves_execution_but_never_the_signal_path() -> None:
    plain = _run()
    rolled = _run(roll_at=300)

    pd.testing.assert_frame_equal(
        plain.signals[SIGNAL_COLUMNS], rolled.signals[SIGNAL_COLUMNS]
    )
    # Execution did move: the raw contract price basis changed at the roll.
    assert rolled.signals.loc[310, "raw_close"] != plain.signals.loc[310, "raw_close"]
    assert rolled.signals["roll_event"].sum() == 1


def test_entries_fill_at_the_contract_price_not_the_continuous_price() -> None:
    rolled = _run(roll_at=300)

    after = rolled.trades.loc[rolled.trades["entry_date"] > pd.Timestamp("2024-03-01").date()]
    assert not after.empty
    for row in after.itertuples(index=False):
        assert row.entry_price == pytest.approx(row.entry_signal_close / 1.5)


def test_future_bars_do_not_change_earlier_output() -> None:
    short = _run(count=300)
    long = _run(count=400)

    pd.testing.assert_frame_equal(
        short.signals, long.signals.iloc[:300].reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(
        short.daily, long.daily.iloc[: len(short.daily)].reset_index(drop=True)
    )
    horizon = short.daily["trade_date"].iloc[-1]
    pd.testing.assert_frame_equal(
        short.trades.loc[short.trades["exit_date"] <= horizon].reset_index(drop=True),
        long.trades.loc[long.trades["exit_date"] <= horizon].reset_index(drop=True),
    )


def test_literal_mode_never_holds_without_a_fresh_breakout() -> None:
    latched = _run(signal_mode="latched")
    literal = _run(signal_mode="literal")

    assert (latched.signals["action"] == "latched_hold").any()
    assert not (literal.signals["action"] == "latched_hold").any()
    assert (literal.signals["action"] == "literal_gate_closed").any()


def test_the_warmup_prefix_cannot_trade() -> None:
    result = _run()

    warmup = result.signals.iloc[:19]
    assert not warmup["action_changed"].any()
    assert warmup["target_weight"].eq(0.0).all()


def test_an_unknown_signal_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_shadow_signal_mode"):
        _run(signal_mode="whatever")


_DOW_SEGMENT_COLUMNS = [
    "signal_close", "macd", "signal_line", "macd_diff", "cumulative",
    "atr_adjusted", "trend", "trend_changed", "turning_valid",
    "dow_resonance", "close_breakout", "enough_history",
    "segment_high", "segment_low", "last_up_high_1", "last_down_low_1",
    "action", "target_weight",
]


def _zigzag(count: int, base: float) -> np.ndarray:
    return np.array(
        [base + 0.35 * i + 7.0 * math.sin(2 * math.pi * i / 25.0) for i in range(count)]
    )


def _dow_bars(closes: np.ndarray, contract: str, segment: int, start) -> pd.DataFrame:
    count = len(closes)
    days = pd.bdate_range(start, periods=count)
    slot_end = pd.DatetimeIndex(
        [pd.Timestamp(datetime.combine(d.date(), time(14, 45), tzinfo=TZ)) for d in days]
    )
    return pd.DataFrame(
        {
            "product": pd.Series(["RB"] * count, dtype="string"),
            "contract": pd.Series([contract] * count, dtype="string"),
            "trade_date": [d.date() for d in days],
            "slot_end": slot_end,
            "open": closes,
            "high": closes + BAND,
            "low": closes - BAND,
            "close": closes,
            "volume": np.full(count, 100.0),
            "open_interest": np.arange(count, dtype="float64") + 1000.0,
            "no_trade": np.zeros(count, dtype="bool"),
            "adj_factor": np.ones(count),
            "continuity_segment": np.full(count, segment, dtype="int64"),
            "fill_time": slot_end + pd.Timedelta(minutes=5),
            "fill_price": closes,
            "fill_pending": np.zeros(count, dtype="bool"),
            "fill_unpriceable": np.zeros(count, dtype="bool"),
            "pricing_basis": pd.Series(["amount_vwap"] * count, dtype="string"),
            "multiplier": np.full(count, 10, dtype="int64"),
        }
    )


def _empty_rolls() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date", "product", "old_contract", "new_contract", "fill_time",
            "old_price", "new_price", "old_pricing_basis", "new_pricing_basis",
        ]
    )


def test_no_dow_state_crosses_a_continuity_break() -> None:
    first = _dow_bars(_zigzag(200, 100.0), "RB1804.SHF", 0, "2017-06-01")
    second_closes = _zigzag(200, 300.0)
    second = _dow_bars(second_closes, "RB1901.SHF", 1, "2018-07-16")
    whole = run_shadow_product(
        pd.concat([first, second], ignore_index=True),
        product="RB",
        roll_fills=_empty_rolls(),
    )
    alone = run_shadow_product(
        _dow_bars(second_closes, "RB1901.SHF", 0, "2018-07-16"),
        product="RB",
        roll_fills=_empty_rolls(),
    )

    tail = whole.signals.tail(len(alone.signals)).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        tail[_DOW_SEGMENT_COLUMNS], alone.signals[_DOW_SEGMENT_COLUMNS]
    )


def test_a_dow_position_is_closed_at_the_last_bar_before_a_break() -> None:
    # 60 bars ends the first segment mid-hold, which is the case that matters:
    # a delisted contract cannot be carried into the relaunched one.
    first = _dow_bars(_zigzag(60, 100.0), "RB1804.SHF", 0, "2017-06-01")
    second = _dow_bars(_zigzag(200, 300.0), "RB1901.SHF", 1, "2018-07-16")
    result = run_shadow_product(
        pd.concat([first, second], ignore_index=True),
        product="RB",
        roll_fills=_empty_rolls(),
    )

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    assert closed.iloc[0]["exit_contract"] == "RB1804.SHF"
    assert closed.iloc[0]["exit_date"] == first["trade_date"].iloc[-1]


def test_a_dow_continuity_break_needs_no_roll_fill() -> None:
    first = _dow_bars(_zigzag(200, 100.0), "RB1804.SHF", 0, "2017-06-01")
    second = _dow_bars(_zigzag(200, 300.0), "RB1901.SHF", 1, "2018-07-16")

    result = run_shadow_product(
        pd.concat([first, second], ignore_index=True),
        product="RB",
        roll_fills=_empty_rolls(),
    )

    assert not result.signals["roll_event"].any()
