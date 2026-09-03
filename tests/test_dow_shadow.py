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
        [
            100.0 + 0.35 * index + 7.0 * math.sin(2 * math.pi * index / 25.0)
            for index in range(count)
        ]
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
        [
            pd.Timestamp(datetime.combine(day.date(), time(14, 45), tzinfo=TZ))
            for day in days
        ]
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
                "trade_date",
                "product",
                "old_contract",
                "new_contract",
                "fill_time",
                "old_price",
                "new_price",
                "old_pricing_basis",
                "new_pricing_basis",
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
    bars, rolls = _dow_panel(
        **{k: v for k, v in kwargs.items() if k in {"count", "roll_at"}}
    )
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

    after = rolled.trades.loc[
        rolled.trades["entry_date"] > pd.Timestamp("2024-03-01").date()
    ]
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
    "signal_close",
    "macd",
    "signal_line",
    "macd_diff",
    "cumulative",
    "atr_adjusted",
    "trend",
    "trend_changed",
    "turning_valid",
    "dow_resonance",
    "close_breakout",
    "enough_history",
    "segment_high",
    "segment_low",
    "last_up_high_1",
    "last_down_low_1",
    "action",
    "target_weight",
]


def _zigzag(count: int, base: float) -> np.ndarray:
    return np.array(
        [base + 0.35 * i + 7.0 * math.sin(2 * math.pi * i / 25.0) for i in range(count)]
    )


def _dow_bars(closes: np.ndarray, contract: str, segment: int, start) -> pd.DataFrame:
    count = len(closes)
    days = pd.bdate_range(start, periods=count)
    slot_end = pd.DatetimeIndex(
        [
            pd.Timestamp(datetime.combine(d.date(), time(14, 45), tzinfo=TZ))
            for d in days
        ]
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
            "trade_date",
            "product",
            "old_contract",
            "new_contract",
            "fill_time",
            "old_price",
            "new_price",
            "old_pricing_basis",
            "new_pricing_basis",
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


def test_a_dow_switch_without_a_fill_closes_on_the_old_contract() -> None:
    """同一个连续段里换了合约，但那次换月定不出价 —— 没人能执行的转移。

    面板对「成交窗口零成交」的换月不发成交单，所以这里不能再要求必有成交单；
    与断代同样处理：切换前那一根强制平仓，新合约上重新开始。
    """
    first = _dow_bars(_zigzag(60, 100.0), "RB2405.SHF", 0, "2017-06-01")
    second = _dow_bars(_zigzag(200, 300.0), "RB2410.SHF", 0, "2017-09-01")

    result = run_shadow_product(
        pd.concat([first, second], ignore_index=True),
        product="RB",
        roll_fills=_empty_rolls(),
    )

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    assert closed.iloc[0]["exit_contract"] == "RB2405.SHF"
    assert closed.iloc[0]["exit_date"] == first["trade_date"].iloc[-1]
    assert result.signals["roll_new_contract"].isna().all()


def test_a_signal_whose_fill_window_never_traded_is_cancelled() -> None:
    """那五分钟根本没人成交 ⇒ 这一笔没有对手盘，信号作废（用户 2026-09-01 裁决）。

    仓位与状态都不动，下一根重新判；这一根标成 `fill_unavailable` 交给报告层计数。
    原先是直接中止整跑 —— 菜籽油 2012-12-26 那种死盘日（全天仍成交 196 手，只是
    开盘那一窗无人）实测每个探针窗口都有 1–2 个品种撞上，全历史验收因此跑不出来。
    """
    plain = _run()
    entries = plain.signals.loc[plain.signals["action"] == "dow_entry"]
    assert not entries.empty
    index = int(entries.index[0])

    bars, rolls = _dow_panel()
    bars.loc[index, ["fill_price", "fill_unpriceable"]] = [np.nan, True]
    result = run_shadow_product(bars, product="RB", roll_fills=rolls)

    row = result.signals.iloc[index]
    assert row["action"] == "fill_unavailable"
    assert bool(row["action_changed"]) is False
    assert row["state_position"] == result.signals.iloc[index - 1]["state_position"]


def test_a_dow_fill_missing_without_being_declared_is_still_fatal() -> None:
    """成交价该有却没有、又没标成"没人成交" —— 那是面板自相矛盾，仍然硬失败。"""
    plain = _run()
    index = int(plain.signals.loc[plain.signals["action"] == "dow_entry"].index[0])
    bars, rolls = _dow_panel()
    bars.loc[index, "fill_time"] = pd.NaT

    with pytest.raises(ValueError, match="shadow_fill_time: timestamp is required"):
        run_shadow_product(bars, product="RB", roll_fills=rolls)


def test_a_roll_and_the_pending_fill_it_lands_on_are_one_trade() -> None:
    """换月与前一日收盘信号的成交撞在同一时刻 —— 它们本就是同一笔。

    真实面板里两者由构造决定都是当日开盘第 5 分钟：换月成交单来自 `roll_fills`，
    而前一日最后一根的 `fill_price` 由**当日**（也就是换月后的新合约）行情解出 ——
    实测 RU 三次换月，前一日最后一根的 fill_price 与换月 new_price 逐位相同。
    分开记两笔既会撞上账本「时间戳严格递增」，又会把新合约的价记到旧合约上。
    """
    plain = _run()
    entries = plain.signals.loc[plain.signals["action"] == "dow_entry"].index
    index = int(entries[entries > 250][0])

    bars, rolls = _dow_panel(roll_at=index + 1)
    roll = rolls.iloc[0]
    bars.loc[index, "fill_time"] = roll["fill_time"]
    bars.loc[index, "fill_price"] = float(roll["new_price"])

    result = run_shadow_product(bars, product="RB", roll_fills=rolls)

    opened = result.trades.loc[result.trades["entry_time"] == roll["fill_time"]]
    assert len(opened) == 1
    assert opened.iloc[0]["entry_contract"] == "RB2410.SHF"
    assert opened.iloc[0]["entry_price"] == pytest.approx(float(roll["new_price"]))


def test_a_pending_fill_that_disagrees_with_the_roll_price_is_fatal() -> None:
    """同一时刻、同一张合约、同一段窗口，两个价对不上 ⇒ 面板自相矛盾，不许合并。"""
    plain = _run()
    entries = plain.signals.loc[plain.signals["action"] == "dow_entry"].index
    index = int(entries[entries > 250][0])

    bars, rolls = _dow_panel(roll_at=index + 1)
    roll = rolls.iloc[0]
    bars.loc[index, "fill_time"] = roll["fill_time"]
    bars.loc[index, "fill_price"] = float(roll["new_price"]) * 1.05

    with pytest.raises(ValueError, match="roll_fill_price"):
        run_shadow_product(bars, product="RB", roll_fills=rolls)


def _first_day_roll(bars: pd.DataFrame) -> pd.DataFrame:
    """保留区间首日的换月成交单 —— 旧主力在窗口之外。"""
    first = bars.iloc[0]
    return pd.DataFrame(
        [
            {
                "trade_date": first["trade_date"],
                "product": "RB",
                "old_contract": "RB2401.SHF",
                "new_contract": str(first["contract"]),
                "fill_time": first["slot_end"] - pd.Timedelta(minutes=5),
                "old_price": float(first["close"]) * 0.99,
                "new_price": float(first["close"]),
                "old_pricing_basis": "amount_vwap",
                "new_pricing_basis": "amount_vwap",
            }
        ]
    )


def test_a_roll_fill_on_the_products_first_panel_day_is_not_an_unused_fill() -> None:
    """首日那笔换月成交单没人能消费 —— 它指向窗口之外的主力，不是"漏用"。

    bundle 自己就允许这种形态（`first_keys`：保留区间首日的成交单可以指向窗口外的
    主力），而影子层的完整性检查原先要求每一笔都被某次合约切换用掉，于是整跑中止。
    实测 PF 2024-01-05（PF402→PF403）就落在 PF 进面板的第一天；全历史里每个品种的
    起点都是它自己的影子回看起点，必然还会撞上。
    """
    bars, _rolls = _dow_panel()

    result = run_shadow_product(bars, product="RB", roll_fills=_first_day_roll(bars))

    assert not result.signals.empty
    assert result.signals["roll_event"].sum() == 0


def test_a_later_fill_for_the_same_contract_is_still_an_unused_fill() -> None:
    """首日那条只赦免**首日那一笔** —— 同一张合约、别的日子的成交单仍然是漏用。"""
    bars, _rolls = _dow_panel()
    stray = _first_day_roll(bars)
    later = bars.iloc[100]
    stray.loc[0, "trade_date"] = later["trade_date"]
    stray.loc[0, "fill_time"] = later["slot_end"] - pd.Timedelta(minutes=5)

    with pytest.raises(ValueError, match="roll_mismatch"):
        run_shadow_product(bars, product="RB", roll_fills=stray)


def _dow_break_with_no_fill() -> pd.DataFrame:
    first = _dow_bars(_zigzag(60, 100.0), "RB1804.SHF", 0, "2017-06-01")
    last = first.index[-1]
    first.loc[last, ["fill_price", "fill_unpriceable"]] = [np.nan, True]
    second = _dow_bars(_zigzag(200, 300.0), "RB1901.SHF", 1, "2018-07-16")
    return pd.concat([first, second], ignore_index=True)


def test_a_dow_forced_exit_with_no_fill_is_priced_at_the_bars_own_close() -> None:
    """用户 2026-09-02 裁决 B：断代平仓没有对手盘时按该 bar 的收盘价平掉。"""
    frame = _dow_break_with_no_fill()

    result = run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    assert closed.iloc[0]["exit_contract"] == "RB1804.SHF"
    assert closed.iloc[0]["exit_price"] == frame.loc[59, "close"]
    assert closed.iloc[0]["exit_time"] == frame.loc[59, "slot_end"]
    assert result.signals.iloc[59]["fill_time"] == frame.loc[59, "fill_time"]


def test_a_dow_close_priced_forced_exit_reports_the_price_it_used() -> None:
    frame = _dow_break_with_no_fill()

    result = run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())

    assert result.signals.iloc[59]["fill_price"] == frame.loc[59, "close"]


def test_a_dow_close_priced_forced_exit_declares_its_pricing_basis() -> None:
    result = run_shadow_product(
        _dow_break_with_no_fill(), product="RB", roll_fills=_empty_rolls()
    )

    assert result.signals.iloc[59]["action"] == "continuity_break_close"
    assert (result.signals["action"] == "continuity_break_close").sum() == 1


def test_a_dow_forced_exit_refuses_a_close_that_is_not_a_price() -> None:
    frame = _dow_break_with_no_fill()
    frame.loc[59, "close"] = 0.0

    with pytest.raises(ValueError, match="continuity break close"):
        run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())


def _dow_break_with_a_deferred_fill() -> pd.DataFrame:
    first = _dow_bars(_zigzag(60, 100.0), "RB1804.SHF", 0, "2017-06-01")
    second = _dow_bars(_zigzag(200, 300.0), "RB1901.SHF", 1, "2018-07-16")
    last = first.index[-1]
    first.loc[last, "fill_time"] = second["slot_end"].iloc[0] - pd.Timedelta(hours=5)
    first.loc[last, "fill_price"] = 300.0
    return pd.concat([first, second], ignore_index=True)


def test_a_dow_forced_exit_deferred_into_the_next_leg_uses_the_close() -> None:
    """裁决 B 的延用（与 Bollinger 同一条）：成交窗口落到旧腿已不在面板的那一天，
    面板给的价是后继合约的，按该 bar 自己的收盘价平。"""
    frame = _dow_break_with_a_deferred_fill()

    result = run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())

    closed = result.trades.loc[result.trades["exit_reason"] == "continuity_break"]
    assert len(closed) == 1
    assert closed.iloc[0]["exit_price"] == frame.loc[59, "close"]
    assert result.signals.iloc[59]["action"] == "continuity_break_close"


def test_daily_atr_frequency_sees_only_the_previous_days() -> None:
    """研报的 ATR 用「每日」波动幅度（原文），登记默认却是 15 分钟 bar 的 ATR。

    `atr_frequency="daily"` 下：一根 bar 一天的夹具里，日 TR 就是 bar TR，但 t 日的
    bar 只能用到 t−1 日为止的 20 个完成日 —— 恰是 15 分钟 ATR 往后错一根。"""
    from common.commodity.indicators import atr_series

    frame = _dow_bars(_zigzag(80, 100.0), "RB1804.SHF", 0, "2017-06-01")

    daily = run_shadow_product(
        frame, product="RB", roll_fills=_empty_rolls(), atr_frequency="daily"
    )
    bar = run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())

    got = daily.signals["atr_adjusted"].to_numpy(dtype="float64")
    trailing = atr_series(frame["high"], frame["low"], frame["close"], window=20)
    assert np.isnan(got[:20]).all()
    np.testing.assert_allclose(got[20:], trailing[19:-1])
    # 默认口径不动：仍是含当根在内的 20 根 bar。
    np.testing.assert_allclose(
        bar.signals["atr_adjusted"].to_numpy(dtype="float64")[19:], trailing[19:]
    )
    assert daily.signals["atr_raw"].iloc[25] == pytest.approx(got[25])


def test_shadow_rejects_an_unknown_atr_frequency() -> None:
    frame = _dow_bars(_zigzag(30, 100.0), "RB1804.SHF", 0, "2017-06-01")
    with pytest.raises(ValueError, match="atr_frequency"):
        run_shadow_product(
            frame, product="RB", roll_fills=_empty_rolls(), atr_frequency="weekly"
        )


def test_prior_extreme_breakout_reference_reaches_every_signal_row() -> None:
    """`breakout_reference="prior_extreme"` 下，signals 表里每一根上升段 bar 的突破判定都
    是「收盘 ≥ 第一高点」，下降段对称；没有上一同向段就没有突破。默认读法不受影响。"""
    frame = _dow_bars(_zigzag(120, 100.0), "RB1804.SHF", 0, "2017-06-01")

    prior = run_shadow_product(
        frame,
        product="RB",
        roll_fills=_empty_rolls(),
        breakout_reference="prior_extreme",
    )
    default = run_shadow_product(frame, product="RB", roll_fills=_empty_rolls())

    signals = prior.signals
    judged = signals["trend_changed"].eq(False) & signals["atr_adjusted"].notna()
    up = judged & signals["trend"].eq("up")
    down = judged & signals["trend"].eq("down")
    assert up.any() and down.any()
    expected_up = signals["signal_close"] >= signals["last_up_high_1"]
    expected_down = signals["signal_close"] <= signals["last_down_low_1"]
    assert (
        signals.loc[up, "close_breakout"].tolist()
        == expected_up[up].fillna(False).tolist()
    )
    assert (
        signals.loc[down, "close_breakout"].tolist()
        == expected_down[down].fillna(False).tolist()
    )
    assert not default.signals["close_breakout"].equals(signals["close_breakout"])


def test_shadow_rejects_an_unknown_breakout_reference() -> None:
    frame = _dow_bars(_zigzag(30, 100.0), "RB1804.SHF", 0, "2017-06-01")
    with pytest.raises(ValueError, match="breakout_reference"):
        run_shadow_product(
            frame, product="RB", roll_fills=_empty_rolls(), breakout_reference="moon"
        )
