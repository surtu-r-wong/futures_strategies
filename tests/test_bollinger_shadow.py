from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from cta_bollinger.shadow import ShadowResult, run_shadow_product


TZ = ZoneInfo("Asia/Shanghai")


def _panel(
    closes: list[float],
    *,
    product: str = "RB",
    adj_factor: float = 2.0,
    open_interest: list[float] | None = None,
    contracts: list[str] | None = None,
    no_trade: list[bool] | None = None,
    segments: list[int] | None = None,
) -> pd.DataFrame:
    count = len(closes)
    days = pd.bdate_range("2023-01-02", periods=count)
    slot_end = (days + pd.Timedelta(hours=14, minutes=45)).tz_localize(TZ)
    fill_time = slot_end + pd.Timedelta(minutes=5)
    oi = open_interest or ([100.0] * 150 + [200.0] * max(0, count - 150))
    if len(oi) != count:
        raise AssertionError("test fixture OI length mismatch")
    contract_values = contracts or ["RB2405.SHF"] * count
    no_trade_values = no_trade or [False] * count
    frame = pd.DataFrame(
        {
            "product": pd.Series([product] * count, dtype="string"),
            "contract": pd.Series(contract_values, dtype="string"),
            "trade_date": days,
            "slot_end": slot_end,
            "open": np.asarray(closes, dtype="float64"),
            "high": np.asarray(closes, dtype="float64") + 1.0,
            "low": np.asarray(closes, dtype="float64") - 1.0,
            "close": np.asarray(closes, dtype="float64"),
            "volume": np.ones(count, dtype="float64"),
            "open_interest": np.asarray(oi, dtype="float64"),
            "no_trade": np.asarray(no_trade_values, dtype="bool"),
            "adj_factor": np.full(count, adj_factor, dtype="float64"),
            "continuity_segment": np.asarray(
                segments if segments is not None else [0] * count, dtype="int64"
            ),
            "fill_time": fill_time,
            "fill_price": np.asarray(closes, dtype="float64"),
            "fill_pending": np.zeros(count, dtype="bool"),
            "fill_unpriceable": np.zeros(count, dtype="bool"),
            "pricing_basis": pd.Series(["amount_vwap"] * count, dtype="string"),
            "multiplier": np.full(count, 10, dtype="int64"),
        }
    )
    return frame


def _long_panel() -> pd.DataFrame:
    closes = [99.0, 101.0] * 150 + [103.0, 99.0]
    frame = _panel(closes)
    frame.loc[300, "fill_price"] = 200.0
    frame.loc[301, "fill_price"] = 220.0
    return frame


def _short_panel() -> pd.DataFrame:
    closes = [101.0, 99.0] * 150 + [97.0, 101.0]
    frame = _panel(closes)
    frame.loc[300, "fill_price"] = 200.0
    frame.loc[301, "fill_price"] = 180.0
    return frame


def test_shadow_uses_adjusted_signal_path_once_and_raw_fills_for_returns() -> None:
    panel = _long_panel()

    result = run_shadow_product(panel, product="RB")

    entry = result.signals.loc[result.signals["action"] == "upper_cross"].iloc[0]
    trade = result.trades.iloc[0]
    assert entry["raw_close"] == 103.0
    assert entry["signal_close"] == 206.0
    assert entry["signal_high"] == 208.0
    assert entry["fill_price"] == 200.0
    assert trade["entry_price"] == 200.0
    assert trade["exit_price"] == 220.0
    assert trade["direction"] == 1
    assert trade["oi_scale"] == 1.0

    expected_target = 0.005 * 103.0 / 3.0
    expected = (1.0 - expected_target * 1.3 / 10_000.0) * (
        1.0 + expected_target * (220.0 / 200.0 - 1.0) - expected_target * 1.3 / 10_000.0
    ) - 1.0
    assert trade["target_magnitude"] == pytest.approx(expected_target)
    assert trade["cost"] == pytest.approx(2.0 * expected_target * 1.3 / 10_000.0)
    assert trade["net_return"] == pytest.approx(expected)


def test_shadow_supports_symmetric_short_path() -> None:
    result = run_shadow_product(_short_panel(), product="RB")

    assert result.signals.loc[300, "action"] == "lower_cross"
    trade = result.trades.iloc[0]
    assert trade["direction"] == -1
    assert trade["entry_price"] == 200.0
    assert trade["exit_price"] == 180.0
    assert trade["net_return"] > 0.0


def test_no_trade_bar_does_not_consume_or_pollute_indicator_history() -> None:
    closes = [99.0, 101.0] * 150 + [10_000.0, 103.0, 99.0]
    flags = [False] * 300 + [True, False, False]
    frame = _panel(closes, no_trade=flags)

    result = run_shadow_product(frame, product="RB")

    skipped = result.signals.iloc[300]
    assert skipped["action"] == "no_trade"
    assert np.isnan(skipped["middle"])
    assert result.signals.iloc[301]["action"] == "upper_cross"


def test_oi_scale_is_frozen_for_the_logical_trade() -> None:
    frame = _long_panel()
    frame.loc[301, "open_interest"] = 0.0

    result = run_shadow_product(frame, product="RB")

    assert result.trades.iloc[0]["oi_scale"] == 1.0
    assert result.signals.iloc[301]["state_oi_scale"] == 0.0


def test_roll_is_atomic_execution_but_one_continuous_logical_trade() -> None:
    frame = _long_panel()
    frame.loc[301:, "contract"] = "RB2410.SHF"
    roll = pd.DataFrame(
        {
            "trade_date": [frame.loc[301, "trade_date"]],
            "product": ["RB"],
            "old_contract": ["RB2405.SHF"],
            "new_contract": ["RB2410.SHF"],
            "fill_time": [frame.loc[301, "slot_end"] - pd.Timedelta(hours=1)],
            "old_price": [205.0],
            "new_price": [210.0],
            "old_pricing_basis": ["amount_vwap"],
            "new_pricing_basis": ["amount_vwap"],
        }
    )

    result = run_shadow_product(frame, product="RB", roll_fills=roll)

    trade = result.trades.iloc[0]
    exit_signal = result.signals.iloc[301]
    assert trade["entry_contract"] == "RB2405.SHF"
    assert trade["exit_contract"] == "RB2410.SHF"
    assert trade["roll_count"] == 1
    assert len(result.trades) == 1
    assert exit_signal["roll_old_contract"] == "RB2405.SHF"
    assert exit_signal["roll_new_contract"] == "RB2410.SHF"
    assert exit_signal["roll_execution_count"] == 2
    assert exit_signal["roll_turnover"] == pytest.approx(
        2.0 * trade["target_magnitude"]
    )
    assert trade["cost"] == pytest.approx(
        4.0 * trade["target_magnitude"] * 1.3 / 10_000.0
    )


def _unexecutable_switch_panel() -> pd.DataFrame:
    """同一个连续段里换了合约，但那次换月定不出价，所以没有换月成交单。"""
    first = [100.0] * 10 + [104.0] * 8
    second = _second_segment_closes()
    return _panel(
        first + second,
        open_interest=[100.0] * (len(first) + len(second)),
        contracts=["RB2405.SHF"] * len(first) + ["RB2410.SHF"] * len(second),
        segments=[0] * (len(first) + len(second)),
    )


def test_a_switch_without_a_fill_closes_on_the_old_contract() -> None:
    """面板对「成交窗口零成交」的换月不发成交单，所以这里不能再要求必有成交单。

    没人能执行的转移与断代同样处理：切换前那一根强制平仓，新合约上重新开始 ——
    仓位不会自己搬家，两张合约的原始价不可比，只有复权因子让**价格**连续。
    """
    result = run_shadow_product(
        _unexecutable_switch_panel(), product="RB", roll_fills=pd.DataFrame(), **_SMALL
    )

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    assert closed.iloc[0]["exit_contract"] == "RB2405.SHF"
    assert closed.iloc[0]["exit_date"] == result.signals.iloc[17]["trade_date"]
    assert result.signals["roll_new_contract"].isna().all()


def test_roll_fill_pricing_bases_must_match_both_panel_legs() -> None:
    frame = _long_panel()
    frame.loc[301:, "contract"] = "RB2410.SHF"
    roll = pd.DataFrame(
        {
            "trade_date": [frame.loc[301, "trade_date"]],
            "product": ["RB"],
            "old_contract": ["RB2405.SHF"],
            "new_contract": ["RB2410.SHF"],
            "fill_time": [frame.loc[301, "slot_end"] - pd.Timedelta(hours=1)],
            "old_price": [205.0],
            "new_price": [210.0],
            "old_pricing_basis": ["amount_vwap"],
            "new_pricing_basis": ["ohlc_typical"],
        }
    )

    with pytest.raises(ValueError, match="roll.*pricing_basis"):
        run_shadow_product(frame, product="RB", roll_fills=roll)


def test_flat_roll_has_no_turnover() -> None:
    frame = _panel([100.0] * 302)
    frame.loc[301:, "contract"] = "RB2410.SHF"
    roll = pd.DataFrame(
        {
            "trade_date": [frame.loc[301, "trade_date"]],
            "product": ["RB"],
            "old_contract": ["RB2405.SHF"],
            "new_contract": ["RB2410.SHF"],
            "fill_time": [frame.loc[301, "slot_end"] - pd.Timedelta(hours=1)],
            "old_price": [100.0],
            "new_price": [101.0],
            "old_pricing_basis": ["amount_vwap"],
            "new_pricing_basis": ["amount_vwap"],
        }
    )

    result = run_shadow_product(frame, product="RB", roll_fills=roll)

    assert result.signals.iloc[301]["roll_execution_count"] == 0
    assert result.signals.iloc[301]["roll_turnover"] == 0.0
    assert result.trades.empty


def test_first_retained_date_boundary_roll_is_consumed_while_flat() -> None:
    frame = _panel([100.0] * 302)
    frame["contract"] = "RB2410.SHF"
    roll = pd.DataFrame(
        {
            "trade_date": [frame.loc[0, "trade_date"]],
            "product": ["RB"],
            "old_contract": ["RB2405.SHF"],
            "new_contract": ["RB2410.SHF"],
            "fill_time": [frame.loc[0, "slot_end"] - pd.Timedelta(hours=1)],
            "old_price": [99.0],
            "new_price": [100.0],
            "old_pricing_basis": ["amount_vwap"],
            "new_pricing_basis": ["amount_vwap"],
        }
    )

    result = run_shadow_product(frame, product="RB", roll_fills=roll)

    first = result.signals.iloc[0]
    assert first["roll_old_contract"] == "RB2405.SHF"
    assert first["roll_new_contract"] == "RB2410.SHF"
    assert first["roll_execution_count"] == 0
    assert first["roll_turnover"] == 0.0
    mismatched = roll.copy()
    mismatched.loc[0, "new_contract"] = "RB2501.SHF"
    with pytest.raises(ValueError, match="roll_mismatch"):
        run_shadow_product(frame, product="RB", roll_fills=mismatched)


def test_future_session_entry_and_exit_fill_on_actual_dates() -> None:
    frame = _panel(
        [99.0, 101.0, 100.0, 105.0, 99.0],
        open_interest=[100.0, 100.0, 100.0, 200.0, 200.0],
    )
    days = pd.to_datetime(
        ["2024-02-21", "2024-02-22", "2024-02-23", "2024-02-26", "2024-02-27"]
    )
    frame["trade_date"] = days
    frame["slot_end"] = (days + pd.Timedelta(hours=14, minutes=45)).tz_localize(TZ)
    frame["fill_time"] = frame["slot_end"] + pd.Timedelta(minutes=5)
    frame.loc[3, "fill_time"] = pd.Timestamp("2024-02-27 09:05", tz=TZ)
    frame.loc[3, "fill_price"] = 100.0
    frame.loc[4, "fill_time"] = pd.Timestamp("2024-02-28 09:05", tz=TZ)
    frame.loc[4, "fill_price"] = 110.0

    result = run_shadow_product(
        frame,
        product="RB",
        band_length=3,
        atr_window=1,
        oi_short=1,
        oi_long=3,
        beta=1.0,
    )

    trade = result.trades.iloc[0]
    assert trade["entry_date"] == pd.Timestamp("2024-02-27").date()
    assert trade["exit_date"] == pd.Timestamp("2024-02-28").date()
    daily = result.daily.set_index("trade_date")
    assert daily.loc[pd.Timestamp("2024-02-26").date(), "net_return"] == 0.0
    target = trade["target_magnitude"]
    one_way_cost = target * 1.3 / 10_000.0
    entry_day = pd.Timestamp("2024-02-27").date()
    exit_day = pd.Timestamp("2024-02-28").date()
    assert daily.loc[entry_day, "turnover"] == pytest.approx(target)
    assert daily.loc[exit_day, "turnover"] == pytest.approx(target)
    assert daily.loc[entry_day, "net_return"] == pytest.approx(
        (1.0 - one_way_cost) * (1.0 + target * (99.0 / 100.0 - 1.0)) - 1.0
    )
    assert daily.loc[exit_day, "net_return"] == pytest.approx(
        target * (110.0 / 99.0 - 1.0) - one_way_cost
    )
    assert daily.index.is_monotonic_increasing


def test_daily_output_has_every_day_in_unique_chronological_order() -> None:
    frame = _long_panel().iloc[::-1].reset_index(drop=True)

    result = run_shadow_product(frame, product="RB")

    assert len(result.daily) == 302
    assert result.daily["trade_date"].is_monotonic_increasing
    assert not result.daily.duplicated(["product", "trade_date"]).any()
    assert result.daily.iloc[:299]["net_return"].eq(0.0).all()


def test_all_no_trade_days_keep_shadow_equity_at_one() -> None:
    frame = _panel(
        [100.0, 100.0, 100.0],
        open_interest=[100.0, 100.0, 100.0],
        no_trade=[True, True, True],
    )

    result = run_shadow_product(frame, product="RB")

    assert result.daily["net_return"].eq(0.0).all()
    assert result.daily["gross_equity"].eq(1.0).all()
    assert result.daily["equity"].eq(1.0).all()


def test_last_pending_fill_cannot_create_an_execution_or_trade() -> None:
    frame = _long_panel().iloc[:301].copy()
    frame.loc[300, ["fill_time", "fill_price"]] = [pd.NaT, np.nan]
    frame.loc[300, "fill_pending"] = True

    result = run_shadow_product(frame, product="RB")

    assert result.signals.iloc[-1]["action"] == "fill_pending"
    assert result.trades.empty
    assert result.daily.iloc[-1]["net_return"] == 0.0


def test_required_unpriceable_fill_fails() -> None:
    frame = _long_panel()
    frame.loc[300, ["fill_price", "fill_unpriceable"]] = [np.nan, True]

    with pytest.raises(ValueError, match="required fill"):
        run_shadow_product(frame, product="RB")


def test_shadow_filters_exact_product_and_rejects_causal_duplicates() -> None:
    rb = _long_panel()
    cu = _panel([100.0] * 302, product="CU")
    mixed = pd.concat([cu, rb], ignore_index=True)

    result = run_shadow_product(mixed, product="RB")

    assert result.product == "RB"
    assert result.signals["product"].eq("RB").all()
    duplicate = pd.concat([rb, rb.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        run_shadow_product(duplicate, product="RB")


def test_shadow_validates_aware_time_and_raw_traded_inputs() -> None:
    naive = _long_panel()
    naive["slot_end"] = naive["slot_end"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="slot_end.*timezone-aware"):
        run_shadow_product(naive, product="RB")

    bad_oi = _long_panel()
    bad_oi.loc[10, "open_interest"] = np.nan
    with pytest.raises(ValueError, match="open_interest.*finite"):
        run_shadow_product(bad_oi, product="RB")

    bad_factor = _long_panel()
    bad_factor.loc[10, "adj_factor"] = 0.0
    with pytest.raises(ValueError, match="adj_factor.*positive"):
        run_shadow_product(bad_factor, product="RB")


def test_shadow_requires_raw_fill_on_nonpending_bars_and_factor_on_grid() -> None:
    missing_fill = _long_panel()
    missing_fill.loc[10, "fill_price"] = np.nan
    with pytest.raises(ValueError, match="fill_price.*finite"):
        run_shadow_product(missing_fill, product="RB")

    bad_grid_factor = _long_panel()
    bad_grid_factor.loc[10, "no_trade"] = True
    bad_grid_factor.loc[10, "adj_factor"] = 0.0
    with pytest.raises(ValueError, match="adj_factor.*positive"):
        run_shadow_product(bad_grid_factor, product="RB")


def test_shadow_result_is_frozen_and_defensively_copies_frames() -> None:
    signals = pd.DataFrame({"x": [1]})
    trades = pd.DataFrame({"x": [2]})
    daily = pd.DataFrame({"x": [3]})

    result = ShadowResult("RB", signals, trades, daily)
    signals.loc[0, "x"] = 99

    assert result.signals.loc[0, "x"] == 1
    with pytest.raises(FrozenInstanceError):
        result.product = "CU"


def _night_panel(closes: list[float]) -> pd.DataFrame:
    """Four slots per trade date, with the night session on the prior evening.

    ``common.minute.sessions._slot_timestamp`` puts a trade date's night
    session on ``previous_trade_date``, so trade date D's 21:00 bar carries a
    wall-clock timestamp on calendar date D-1.  A bar inside a session fills
    five minutes later; the last bar of a session fills in the first five
    minutes of the next one, which for a 15:00 bar is 21:00 on the same
    calendar date -- a window that belongs to trade date D+1.
    """
    trade_dates = [date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7), date(2024, 3, 8)]
    if len(closes) != 4 * len(trade_dates):
        raise AssertionError("night fixture expects four bars per trade date")

    rows: list[dict[str, object]] = []
    for day in trade_dates:
        previous = day - timedelta(days=1)
        slots = (
            (datetime.combine(previous, time(21, 15), tzinfo=TZ),
             datetime.combine(previous, time(21, 20), tzinfo=TZ)),
            (datetime.combine(previous, time(21, 30), tzinfo=TZ),
             datetime.combine(day, time(9, 5), tzinfo=TZ)),
            (datetime.combine(day, time(9, 15), tzinfo=TZ),
             datetime.combine(day, time(9, 20), tzinfo=TZ)),
            (datetime.combine(day, time(15, 0), tzinfo=TZ),
             datetime.combine(day, time(21, 5), tzinfo=TZ)),
        )
        for slot_end, fill_time in slots:
            close = closes[len(rows)]
            rows.append(
                {
                    "product": "RB",
                    "contract": "RB2405.SHF",
                    "trade_date": day,
                    "slot_end": pd.Timestamp(slot_end),
                    "open": close,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 1.0,
                    "open_interest": 1000.0 + len(rows),
                    "no_trade": False,
                    "adj_factor": 1.0,
                    "continuity_segment": 0,
                    "fill_time": pd.Timestamp(fill_time),
                    "fill_price": close,
                    "fill_pending": False,
                    "fill_unpriceable": False,
                    "pricing_basis": "amount_vwap",
                    "multiplier": 10,
                }
            )
    frame = pd.DataFrame(rows)
    for column in ("product", "contract", "pricing_basis"):
        frame[column] = frame[column].astype("string")
    frame["multiplier"] = frame["multiplier"].astype("int64")
    return frame


def _night_kwargs() -> dict[str, int]:
    return {"band_length": 5, "atr_window": 2, "oi_short": 2, "oi_long": 3}


def test_a_night_bar_can_trade_after_the_previous_trade_date_closed() -> None:
    # The cross lands on trade date 2024-03-07's first night bucket and fills
    # at 21:20, both on calendar date 2024-03-06 -- before any calendar-end
    # stamp that 03-06 could carry.
    panel = _night_panel([100.0] * 8 + [101.0] * 8)

    result = run_shadow_product(panel, product="RB", **_night_kwargs())

    entry = result.signals.loc[result.signals["action"] == "upper_cross"]
    assert len(entry) == 1
    assert entry.iloc[0]["trade_date"] == date(2024, 3, 7)
    assert entry.iloc[0]["slot_end"] == pd.Timestamp("2024-03-06 21:15", tz=TZ)
    assert result.daily.set_index("trade_date").loc[date(2024, 3, 7), "turnover"] > 0.0


def test_a_fill_in_the_next_session_belongs_to_the_next_trade_date() -> None:
    # The cross lands on 2024-03-07's 15:00 bar; its fill window is 21:00 on
    # 2024-03-07, the first five minutes of trade date 2024-03-08's session.
    panel = _night_panel([100.0] * 11 + [101.0] * 5)

    result = run_shadow_product(panel, product="RB", **_night_kwargs())

    entry = result.signals.loc[result.signals["action"] == "upper_cross"]
    assert len(entry) == 1
    assert entry.iloc[0]["fill_time"] == pd.Timestamp("2024-03-07 21:05", tz=TZ)

    daily = result.daily.set_index("trade_date")
    assert daily.loc[date(2024, 3, 7), "turnover"] == 0.0
    assert daily.loc[date(2024, 3, 8), "turnover"] > 0.0


_SEGMENT_SIGNAL_COLUMNS = [
    "signal_close", "middle", "std", "upper", "lower",
    "atr_adjusted", "atr_raw", "oi_short", "oi_long",
    "action", "state_position", "target_weight",
]

_SMALL = {"band_length": 5, "atr_window": 2, "oi_short": 2, "oi_long": 3}


def _second_segment_closes() -> list[float]:
    return [200.0] * 10 + [208.0] * 8


def _broken_panel() -> pd.DataFrame:
    """One product whose contract is delisted and relaunched: two segments."""
    first = [100.0] * 10 + [104.0] * 8
    second = _second_segment_closes()
    closes = first + second
    count = len(closes)
    oi = [100.0] * len(first) + [100.0] * len(second)
    frame = _panel(
        closes,
        open_interest=oi,
        contracts=["RB1804.SHF"] * len(first) + ["RB1901.SHF"] * len(second),
        segments=[0] * len(first) + [1] * len(second),
    )
    return frame


def test_no_price_state_crosses_a_continuity_break() -> None:
    whole = run_shadow_product(_broken_panel(), product="RB", **_SMALL)
    alone = run_shadow_product(
        _panel(_second_segment_closes(), open_interest=[100.0] * 18),
        product="RB",
        **_SMALL,
    )

    tail = whole.signals.tail(len(alone.signals)).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        tail[_SEGMENT_SIGNAL_COLUMNS], alone.signals[_SEGMENT_SIGNAL_COLUMNS]
    )


def test_a_position_is_closed_at_the_last_bar_before_a_continuity_break() -> None:
    result = run_shadow_product(_broken_panel(), product="RB", **_SMALL)

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    # The break is a delisting: it is closed on the old contract, at the last
    # bar that contract ever traded.
    assert closed.iloc[0]["exit_contract"] == "RB1804.SHF"
    assert closed.iloc[0]["exit_date"] == result.signals.iloc[17]["trade_date"]


def test_a_continuity_break_needs_no_roll_fill() -> None:
    # The contracts never traded together, so no roll fill can exist. Demanding
    # one would make every relaunched product unbacktestable.
    result = run_shadow_product(_broken_panel(), product="RB", **_SMALL)

    assert result.signals["roll_new_contract"].isna().all()
