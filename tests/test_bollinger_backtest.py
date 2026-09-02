"""The selected Bollinger portfolio: capital, volatility, causality, rolls."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from common.commodity.bundle import PanelBundle
from common.commodity.execution import ROLL_COLUMNS
from cta_bollinger.backtest import BacktestResult, run_backtest
from cta_bollinger.shadow import ShadowResult, run_shadow_product


TZ = ZoneInfo("Asia/Shanghai")

#: Small enough that a four-month fixture reaches every month boundary.
SHADOW_KWARGS = {"band_length": 5, "atr_window": 2, "oi_short": 2, "oi_long": 3}
SELECTION_OBSERVATIONS = 5
VOL_OBSERVATIONS = 15  # longer than the 12-bar price cycle, so no window is flat

HOLD_FROM = date(2024, 2, 15)
CU_ROLL_DATE = date(2024, 3, 15)


def _trade_dates() -> list[date]:
    return [stamp.date() for stamp in pd.bdate_range("2023-12-01", "2024-03-29")]


def _oscillating(dates: list[date]) -> list[float]:
    """Flat at 100 with a periodic spike, then a step that is held.

    The spike crosses the upper band and the following bar crosses back
    through the middle, so the product completes a trade every cycle. From
    ``HOLD_FROM`` the close steps up and stays there, which enters once and
    never exits -- the state a product must be in to test what happens when it
    joins or leaves the selected universe while holding.
    """
    closes: list[float] = []
    for index, day in enumerate(dates):
        if day >= HOLD_FROM:
            closes.append(104.0)
        elif index % 12 == 11:
            closes.append(104.0)
        else:
            closes.append(100.0)
    hold_index = next(i for i, day in enumerate(dates) if day >= HOLD_FROM)
    for index in range(max(0, hold_index - 6), hold_index):
        closes[index] = 100.0
    return closes


def _stepping(dates: list[date]) -> list[float]:
    """Flat until ``HOLD_FROM``, then one step up that is never given back."""
    return [104.0 if day >= HOLD_FROM else 100.0 for day in dates]


def _bars(
    product: str,
    dates: list[date],
    closes: list[float],
    *,
    contract_of=None,
    multiplier: int = 10,
) -> pd.DataFrame:
    rows = []
    for index, (day, close) in enumerate(zip(dates, closes)):
        slot_end = pd.Timestamp(datetime.combine(day, time(14, 45), tzinfo=TZ))
        rows.append(
            {
                "product": product,
                "contract": (
                    contract_of(day) if contract_of else f"{product}2405.SHF"
                ),
                "trade_date": day,
                "slot_end": slot_end,
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100.0,
                "open_interest": 1000.0 + index,
                "no_trade": False,
                "adj_factor": 1.0,
                "continuity_segment": 0,
                "fill_time": slot_end + pd.Timedelta(minutes=5),
                "fill_price": close,
                "fill_pending": False,
                "fill_unpriceable": False,
                "pricing_basis": "amount_vwap",
                "multiplier": multiplier,
            }
        )
    return pd.DataFrame(rows)


def _cu_contract(day: date) -> str:
    return "CU2406.SHF" if day >= CU_ROLL_DATE else "CU2404.SHF"


def _universe_rows(dates: list[date]) -> pd.DataFrame:
    """RB leaves the liquidity universe in March; CU only joins in March."""
    months = sorted({day.replace(day=1) for day in dates})
    rows = []
    for month in months:
        products = ["TA"] + (["CU"] if month.month == 3 else ["RB"])
        for product in sorted(products):
            rows.append({"month_start": month, "product": product})
    return pd.DataFrame(rows)


def _forced_scores(product: str, dates: list[date]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A daily/trade history that clears the eligibility gates every month.

    Selection is not what these tests are about, so the score inputs are
    supplied directly: an alternating return with positive drift (so Sharpe
    and Calmar are both positive) and two closed trades per day (so the
    five-trade gate passes in any trailing window).
    """
    score_dates = [stamp.date() for stamp in pd.bdate_range("2023-11-01", "2024-03-29")]
    daily = pd.DataFrame(
        {
            "product": product,
            "trade_date": score_dates,
            "net_return": [
                0.002 if index % 2 == 0 else -0.001
                for index in range(len(score_dates))
            ],
        }
    )
    trades = pd.DataFrame(
        [
            {
                "product": product,
                "trade_id": f"{product}-{index:04d}-{leg}",
                "exit_date": day,
            }
            for index, day in enumerate(score_dates)
            for leg in ("a", "b")
        ]
    )
    return daily, trades


def _scenario() -> tuple[PanelBundle, dict[str, ShadowResult]]:
    dates = _trade_dates()
    frames = {
        "RB": _bars("RB", dates, _oscillating(dates)),
        "TA": _bars("TA", dates, [200.0] * len(dates), multiplier=5),
        "CU": _bars("CU", dates, _stepping(dates), contract_of=_cu_contract),
    }
    bars = pd.concat(frames.values(), ignore_index=True)
    roll_slot = pd.Timestamp(datetime.combine(CU_ROLL_DATE, time(14, 40), tzinfo=TZ))
    roll_fills = pd.DataFrame(
        [
            {
                "trade_date": CU_ROLL_DATE,
                "product": "CU",
                "old_contract": "CU2404.SHF",
                "new_contract": "CU2406.SHF",
                "fill_time": roll_slot,
                "old_price": 104.0,
                "new_price": 104.0,
                "old_pricing_basis": "amount_vwap",
                "new_pricing_basis": "amount_vwap",
            }
        ]
    )
    dominants = pd.DataFrame(
        [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "oi": 1000,
                "volume": 900,
                "selected_from": row.trade_date,
                "adj_factor": 1.0,
            }
            for row in bars.itertuples(index=False)
        ]
    )
    bundle = PanelBundle(
        bars=bars,
        universes=_universe_rows(dates),
        dominants=dominants,
        roll_fills=roll_fills,
        manifest={"bundle_version": 1},
    )

    shadows: dict[str, ShadowResult] = {}
    for product in sorted(frames):
        raw = run_shadow_product(
            bars,
            product=product,
            roll_fills=roll_fills,
            **SHADOW_KWARGS,
        )
        daily, trades = _forced_scores(product, dates)
        shadows[product] = ShadowResult(product, raw.signals, trades, daily)
    return bundle, shadows


@pytest.fixture
def scenario() -> tuple[PanelBundle, dict[str, ShadowResult]]:
    return _scenario()


def _run(bundle, shadows, **overrides) -> BacktestResult:
    options = {
        "bundle": bundle,
        "shadows": shadows,
        "selection_observations": SELECTION_OBSERVATIONS,
        "realized_vol_min_observations": VOL_OBSERVATIONS,
    }
    options.update(overrides)
    return run_backtest(**options)


def test_selected_but_flat_product_keeps_its_sleeve_in_cash(scenario) -> None:
    result = _run(*scenario)

    flat = result.positions.query("product == 'TA'")
    assert not flat.empty
    assert flat["universe_weight"].eq(0.5).all()
    assert flat["actual_weight"].eq(0.0).all()

    # The cash half must stay cash: an active sleeve is sized off 1/N, not off
    # the count of products that happen to have a signal (fidelity rule B3).
    active = result.positions.query("selected and direction != 0")
    assert not active.empty
    assert np.allclose(
        active["target_weight"].abs(),
        active["universe_weight"] * active["oi_scale"] * active["leverage"],
    )


def test_monthly_multiplier_targets_ten_percent(scenario) -> None:
    result = _run(*scenario)

    applied = result.positions["target_annual_vol"].dropna()
    assert not applied.empty
    assert applied.eq(0.10).all()


def test_the_volatility_multiplier_is_constant_inside_one_month(scenario) -> None:
    result = _run(*scenario)

    per_month = result.daily.dropna(subset=["vol_multiplier"]).groupby("month_start")
    assert per_month.ngroups >= 2
    assert per_month["vol_multiplier"].nunique().eq(1).all()


def test_changing_march_cannot_move_february_selection_or_multiplier(scenario) -> None:
    bundle, shadows = scenario
    baseline = _run(bundle, shadows)

    disturbed = {}
    for product, shadow in shadows.items():
        daily = shadow.daily.copy()
        march = daily["trade_date"] >= date(2024, 3, 1)
        daily.loc[march, "net_return"] = -0.25
        disturbed[product] = ShadowResult(
            product, shadow.signals, shadow.trades, daily
        )
    changed = _run(bundle, disturbed)

    for frame in (baseline.selection, changed.selection):
        frame.sort_values(["month_start", "product"], inplace=True)
    february = date(2024, 2, 1)
    pd.testing.assert_frame_equal(
        baseline.selection.query("month_start == @february").reset_index(drop=True),
        changed.selection.query("month_start == @february").reset_index(drop=True),
    )
    assert baseline.daily.query("month_start == @february")[
        "vol_multiplier"
    ].equals(changed.daily.query("month_start == @february")["vol_multiplier"])


def test_a_product_leaving_the_universe_is_closed_at_the_first_march_fill(
    scenario,
) -> None:
    result = _run(*scenario)

    last_february = date(2024, 2, 29)
    march_start = date(2024, 3, 1)
    february_end = result.positions.query(
        "product == 'RB' and trade_date == @last_february"
    )
    assert february_end["actual_weight"].abs().gt(0.0).all()

    march = result.positions.query(
        "product == 'RB' and month_start == @march_start"
    )
    assert march["selected"].eq(False).all()
    assert march["actual_weight"].eq(0.0).all()

    closing = result.trades.query(
        "product == 'RB' and reason == 'universe_exit'"
    )
    assert len(closing) == 1
    assert closing.iloc[0]["trade_date"] == march_start
    assert closing.iloc[0]["new_weight"] == 0.0


def test_a_product_joining_the_universe_aligns_to_its_shadow_position(scenario) -> None:
    result = _run(*scenario)

    entering = result.trades.query("product == 'CU' and reason == 'universe_entry'")
    assert len(entering) == 1
    assert entering.iloc[0]["trade_date"] == date(2024, 3, 1)
    assert entering.iloc[0]["new_weight"] > 0.0

    # CU is long in its shadow from mid-February but outside the universe, so
    # the portfolio must be carrying nothing until March aligns it.
    february_start = date(2024, 2, 1)
    february = result.positions.query(
        "product == 'CU' and month_start == @february_start"
    )
    assert february["actual_weight"].eq(0.0).all()
    assert february.iloc[-1]["direction"] == 1


def test_one_roll_event_executes_both_contract_legs(scenario) -> None:
    result = _run(*scenario)

    roll_date = CU_ROLL_DATE
    legs = result.trades.query("product == 'CU' and trade_date == @roll_date")
    assert set(legs["reason"]) == {"roll_old", "roll_new"}
    assert set(legs["contract"]) == {"CU2404.SHF", "CU2406.SHF"}
    assert legs["timestamp"].nunique() == 1
    old_leg = legs.query("reason == 'roll_old'").iloc[0]
    new_leg = legs.query("reason == 'roll_new'").iloc[0]
    assert old_leg["new_weight"] == 0.0
    assert new_leg["new_weight"] == pytest.approx(old_leg["old_weight"])


def test_a_target_without_a_priced_fill_is_rejected(scenario) -> None:
    bundle, shadows = scenario
    signals = shadows["CU"].signals.copy()
    entry = signals.index[signals["action_changed"]][0]
    signals.loc[entry, "fill_price"] = np.nan
    broken = dict(shadows)
    broken["CU"] = ShadowResult("CU", signals, shadows["CU"].trades, shadows["CU"].daily)

    with pytest.raises(ValueError, match="bollinger_backtest_fill"):
        _run(bundle, broken)


#: CU 换合约的那天，以及它前一天（那天最后一根的成交被推到换合约当天开盘）。
DEFERRED_SWITCH = date(2024, 2, 16)
DEFERRED_SIGNAL = date(2024, 2, 15)
#: 换月新腿的价，与旧腿 104.0 明显不同，好看出这一笔落在哪条腿上。
DEFERRED_NEW_PRICE = 110.0


def _deferred_fill_universe(dates: list[date]) -> pd.DataFrame:
    """三个品种全程在池：RB 从十二月起就在交易，二月的已实现波动率才有得算。"""
    months = sorted({day.replace(day=1) for day in dates})
    return pd.DataFrame(
        [
            {"month_start": month, "product": product}
            for month in months
            for product in ("CU", "RB", "TA")
        ]
    )


def _deferred_fill_scenario(
    *,
    switch: date = DEFERRED_SWITCH,
    signal_day: date = DEFERRED_SIGNAL,
    closes=_stepping,
    old_price: float = 104.0,
    new_price: float = DEFERRED_NEW_PRICE,
) -> tuple[PanelBundle, dict[str, ShadowResult]]:
    """换月与「前一日信号在次日开盘的成交」落在同一时刻。

    真实形态：2020-04-01 没有夜盘，FG 当日最后一根的成交被推到 04-02 09:04，
    而 FG005→FG009 的换月也落在 09:04；面板给那笔递延成交的价正是**新腿**的价
    （实测逐位相同）。影子层 `EventLedger.roll` 早已裁定两者本就是同一笔。
    """
    dates = _trade_dates()

    def contract_of(day: date) -> str:
        return "CU2406.SHF" if day >= switch else "CU2404.SHF"

    cu = _bars("CU", dates, closes(dates), contract_of=contract_of)
    signal_row = cu.index[cu["trade_date"] == signal_day][0]
    roll_slot = pd.Timestamp(datetime.combine(switch, time(9, 4), tzinfo=TZ))
    cu.loc[signal_row, "fill_time"] = roll_slot
    cu.loc[signal_row, "fill_price"] = new_price

    frames = {
        "CU": cu,
        "RB": _bars("RB", dates, _oscillating(dates)),
        "TA": _bars("TA", dates, [200.0] * len(dates), multiplier=5),
    }
    bars = pd.concat(frames.values(), ignore_index=True)
    roll_fills = pd.DataFrame(
        [
            {
                "trade_date": switch,
                "product": "CU",
                "old_contract": "CU2404.SHF",
                "new_contract": "CU2406.SHF",
                "fill_time": roll_slot,
                "old_price": old_price,
                "new_price": new_price,
                "old_pricing_basis": "amount_vwap",
                "new_pricing_basis": "amount_vwap",
            }
        ]
    )
    dominants = pd.DataFrame(
        [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "oi": 1000,
                "volume": 900,
                "selected_from": row.trade_date,
                "adj_factor": 1.0,
            }
            for row in bars.itertuples(index=False)
        ]
    )
    bundle = PanelBundle(
        bars=bars,
        universes=_deferred_fill_universe(dates),
        dominants=dominants,
        roll_fills=roll_fills,
        manifest={"bundle_version": 1},
    )

    shadows: dict[str, ShadowResult] = {}
    for product in sorted(frames):
        raw = run_shadow_product(
            bars, product=product, roll_fills=roll_fills, **SHADOW_KWARGS
        )
        daily, trades = _forced_scores(product, dates)
        shadows[product] = ShadowResult(product, raw.signals, trades, daily)
    return bundle, shadows


def test_a_roll_meeting_a_deferred_fill_puts_the_position_on_the_new_leg() -> None:
    """仓位先搬家，这一笔再作用在新腿上 —— 不能开在刚被换掉的那条腿上，
    也不能把新腿的价记到旧腿名下。"""
    result = _run(*_deferred_fill_scenario())

    roll_slot = pd.Timestamp(datetime.combine(DEFERRED_SWITCH, time(9, 4), tzinfo=TZ))
    legs = result.trades.query("product == 'CU' and timestamp == @roll_slot")
    assert not legs.empty
    opened = legs.query("new_weight != 0")
    assert list(opened["contract"]) == ["CU2406.SHF"]
    assert opened.iloc[0]["price"] == pytest.approx(DEFERRED_NEW_PRICE)


def test_a_roll_meeting_a_deferred_fill_keeps_the_fills_own_reason() -> None:
    """影子层把两者合并成一次调仓：旧腿记 roll_old，新腿记那笔成交自己的 reason。"""
    result = _run(*_deferred_fill_scenario())

    roll_slot = pd.Timestamp(datetime.combine(DEFERRED_SWITCH, time(9, 4), tzinfo=TZ))
    legs = result.trades.query("product == 'CU' and timestamp == @roll_slot")
    opened = legs.query("new_weight != 0").iloc[0]
    assert opened["reason"] == "upper_cross"


#: 递延成交是**平仓**的那一变体：换月当刻旧腿真的被平掉，价才看得见。
DEFERRED_EXIT_SWITCH = date(2024, 2, 8)
DEFERRED_EXIT_SIGNAL = date(2024, 2, 7)
DEFERRED_EXIT_OLD_PRICE = 96.0


def test_a_roll_meeting_a_deferred_fill_closes_the_old_leg_at_its_own_price() -> None:
    """旧腿按换月单的 `old_price` 平 —— 把新腿的价记到旧腿名下会无声算错 P&L。"""
    result = _run(
        *_deferred_fill_scenario(
            switch=DEFERRED_EXIT_SWITCH,
            signal_day=DEFERRED_EXIT_SIGNAL,
            closes=_oscillating,
            old_price=DEFERRED_EXIT_OLD_PRICE,
        )
    )

    roll_slot = pd.Timestamp(
        datetime.combine(DEFERRED_EXIT_SWITCH, time(9, 4), tzinfo=TZ)
    )
    legs = result.trades.query("product == 'CU' and timestamp == @roll_slot")
    closed = legs.query("contract == 'CU2404.SHF'")
    assert len(closed) == 1
    assert closed.iloc[0]["new_weight"] == 0.0
    assert closed.iloc[0]["price"] == pytest.approx(DEFERRED_EXIT_OLD_PRICE)


#: 断代平仓落在没人成交的那一根：面板给的价是 NaN，策略按该 bar 的收盘价平掉。
UNFILLABLE_BREAK_DAY = date(2024, 3, 14)
UNFILLABLE_BREAK_CLOSE = 107.0


def _unfillable_break_scenario() -> tuple[PanelBundle, dict[str, ShadowResult]]:
    """CU 换合约但没有换月成交单 ⇒ 断代平仓，而那一根的成交窗没人成交。

    真实形态：`FU 2025-08-29 FU2509.SHF`（w2526）与 `AU 2019-12-16`（w1920）。
    """
    dates = _trade_dates()
    closes = _stepping(dates)
    break_index = dates.index(UNFILLABLE_BREAK_DAY)
    closes[break_index] = UNFILLABLE_BREAK_CLOSE

    cu = _bars("CU", dates, closes, contract_of=_cu_contract)
    cu.loc[break_index, ["fill_price", "fill_unpriceable"]] = [np.nan, True]

    frames = {
        "CU": cu,
        "RB": _bars("RB", dates, _oscillating(dates)),
        "TA": _bars("TA", dates, [200.0] * len(dates), multiplier=5),
    }
    bars = pd.concat(frames.values(), ignore_index=True)
    roll_fills = pd.DataFrame(columns=list(ROLL_COLUMNS))
    dominants = pd.DataFrame(
        [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "oi": 1000,
                "volume": 900,
                "selected_from": row.trade_date,
                "adj_factor": 1.0,
            }
            for row in bars.itertuples(index=False)
        ]
    )
    bundle = PanelBundle(
        bars=bars,
        universes=_universe_rows(dates),
        dominants=dominants,
        roll_fills=roll_fills,
        manifest={"bundle_version": 1},
    )

    shadows: dict[str, ShadowResult] = {}
    for product in sorted(frames):
        raw = run_shadow_product(
            bars, product=product, roll_fills=roll_fills, **SHADOW_KWARGS
        )
        daily, trades = _forced_scores(product, dates)
        shadows[product] = ShadowResult(product, raw.signals, trades, daily)
    return bundle, shadows


def test_a_close_priced_forced_exit_reaches_the_portfolio() -> None:
    """影子层用掉的价必须报进 signals 行 —— 组合层见 `action_changed` 就要一个价。

    影子层的单元测试全绿，接线仍在这一根上硬失败（实测 FU 2025-08-29 / AU
    2019-12-16）：只改账本执行、不改这一行报出去的东西，缺口只有串起来才现形。
    """
    result = _run(*_unfillable_break_scenario())

    exits = result.trades.query(
        "product == 'CU' and trade_date == @UNFILLABLE_BREAK_DAY"
    )
    assert len(exits) == 1
    assert exits.iloc[0]["new_weight"] == 0.0
    assert exits.iloc[0]["price"] == pytest.approx(UNFILLABLE_BREAK_CLOSE)


CROSS_LEG_PANEL_FILL = 150.0


def _cross_leg_break_scenario() -> tuple[PanelBundle, dict[str, ShadowResult]]:
    """CU 断代平仓的成交窗口落到次日 —— 那时旧腿已不在面板里，面板给的价是后继合约的。

    真实形态：`CS 2021-11-02` / `SM 2016-09-01` / `ZC 2022-05-05`（全历史 Bollinger
    在 SM 上持仓，组合层对齐检查拦下 `fill_price differs from the bundle`）。
    """
    dates = _trade_dates()
    closes = _stepping(dates)
    break_index = dates.index(UNFILLABLE_BREAK_DAY)
    closes[break_index] = UNFILLABLE_BREAK_CLOSE

    cu = _bars("CU", dates, closes, contract_of=_cu_contract)
    next_day = dates[break_index + 1]
    cu.loc[break_index, "fill_time"] = pd.Timestamp(
        datetime.combine(next_day, time(9, 4), tzinfo=TZ)
    )
    cu.loc[break_index, "fill_price"] = CROSS_LEG_PANEL_FILL

    frames = {
        "CU": cu,
        "RB": _bars("RB", dates, _oscillating(dates)),
        "TA": _bars("TA", dates, [200.0] * len(dates), multiplier=5),
    }
    bars = pd.concat(frames.values(), ignore_index=True)
    roll_fills = pd.DataFrame(columns=list(ROLL_COLUMNS))
    dominants = pd.DataFrame(
        [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "oi": 1000,
                "volume": 900,
                "selected_from": row.trade_date,
                "adj_factor": 1.0,
            }
            for row in bars.itertuples(index=False)
        ]
    )
    bundle = PanelBundle(
        bars=bars,
        universes=_universe_rows(dates),
        dominants=dominants,
        roll_fills=roll_fills,
        manifest={"bundle_version": 1},
    )

    shadows: dict[str, ShadowResult] = {}
    for product in sorted(frames):
        raw = run_shadow_product(
            bars, product=product, roll_fills=roll_fills, **SHADOW_KWARGS
        )
        daily, trades = _forced_scores(product, dates)
        shadows[product] = ShadowResult(product, raw.signals, trades, daily)
    return bundle, shadows


def test_a_forced_exit_priced_off_the_panel_still_aligns_with_the_bundle() -> None:
    """裁决 B 延用到跨腿：那一行申报了 `continuity_break_close`，它的成交价按裁决
    替换成破口 bar 的收盘价，与面板给的（后继合约的）价不同是**应当**的。

    对齐检查要认得这份申报：只放过申报行的成交价，其余逐点照比。"""
    bundle, shadows = _cross_leg_break_scenario()
    signals = shadows["CU"].signals
    declared = signals.loc[signals["action"] == "continuity_break_close"]
    assert len(declared) == 1
    assert declared.iloc[0]["fill_price"] == UNFILLABLE_BREAK_CLOSE

    result = _run(bundle, shadows)

    exits = result.trades.query("product == 'CU' and new_weight == 0.0")
    assert len(exits) == 1
    assert exits.iloc[0]["contract"] == "CU2404.SHF"
    assert exits.iloc[0]["price"] == pytest.approx(UNFILLABLE_BREAK_CLOSE)


def test_an_undeclared_fill_price_still_fails_alignment() -> None:
    """放过的只有申报行：普通一行的成交价与面板不同，仍是「不是这份面板产的影子」。"""
    bundle, shadows = _scenario()
    signals = shadows["RB"].signals.copy()
    ordinary = signals.index[
        (signals["action"] != "continuity_break_close") & signals["fill_price"].notna()
    ][0]
    bumped = float(signals.loc[ordinary, "fill_price"]) + 1.0
    signals.loc[ordinary, "fill_price"] = bumped
    shadows["RB"] = ShadowResult(
        "RB", signals, shadows["RB"].trades, shadows["RB"].daily
    )

    with pytest.raises(ValueError, match="RB: fill_price differs from the bundle"):
        _run(bundle, shadows)
