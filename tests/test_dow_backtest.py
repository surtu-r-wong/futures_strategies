"""The Dow portfolio: only current positions share capital, at 15% target vol."""

from __future__ import annotations

from datetime import date, datetime, time
import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from common.commodity.bundle import PanelBundle
from cta_dow.backtest import BacktestResult, run_backtest
from cta_dow.shadow import ShadowResult, run_shadow_product


TZ = ZoneInfo("Asia/Shanghai")
BAND = 0.6
SELECTION_OBSERVATIONS = 5
VOL_OBSERVATIONS = 15

PRODUCTS = {"RB": 0.0, "TA": math.pi, "CU": math.pi / 2.0}


def _dates() -> list[date]:
    return [stamp.date() for stamp in pd.bdate_range("2023-01-02", periods=300)]


def _bars(product: str, phase: float, days: list[date], multiplier: int) -> pd.DataFrame:
    count = len(days)
    close = np.array(
        [
            100.0 + 0.35 * index + 7.0 * math.sin(2 * math.pi * index / 25.0 + phase)
            for index in range(count)
        ]
    )
    slot_end = pd.DatetimeIndex(
        [pd.Timestamp(datetime.combine(day, time(14, 45), tzinfo=TZ)) for day in days]
    )
    return pd.DataFrame(
        {
            "product": pd.Series([product] * count, dtype="string"),
            "contract": pd.Series([f"{product}2405.SHF"] * count, dtype="string"),
            "trade_date": days,
            "slot_end": slot_end,
            "open": close,
            "high": close + BAND,
            "low": close - BAND,
            "close": close,
            "volume": np.full(count, 100.0),
            "open_interest": np.arange(count, dtype="float64") + 1000.0,
            "no_trade": np.zeros(count, dtype="bool"),
            "adj_factor": np.ones(count),
            "continuity_segment": np.zeros(count, dtype="int64"),
            "fill_time": slot_end + pd.Timedelta(minutes=5),
            "fill_price": close,
            "fill_pending": np.zeros(count, dtype="bool"),
            "fill_unpriceable": np.zeros(count, dtype="bool"),
            "pricing_basis": pd.Series(["amount_vwap"] * count, dtype="string"),
            "multiplier": np.full(count, multiplier, dtype="int64"),
        }
    )


def _forced_scores(product: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score inputs that clear the Dow gates, so selection is not under test."""
    score_dates = [stamp.date() for stamp in pd.bdate_range("2022-11-01", "2024-03-29")]
    daily = pd.DataFrame(
        {
            "product": product,
            "trade_date": score_dates,
            "net_return": [
                0.002 if index % 2 == 0 else -0.001 for index in range(len(score_dates))
            ],
        }
    )
    trades = pd.DataFrame(
        [
            {"product": product, "trade_id": f"{product}-{index}-{leg}", "exit_date": day}
            for index, day in enumerate(score_dates)
            for leg in ("a", "b")
        ]
    )
    return daily, trades


def _scenario(*, drop_cu_after: date | None = None):
    days = _dates()
    frames = {
        product: _bars(product, phase, days, 10 if product != "TA" else 5)
        for product, phase in PRODUCTS.items()
    }
    bars = pd.concat(frames.values(), ignore_index=True)
    months = sorted({day.replace(day=1) for day in days})
    universe_rows = []
    for month in months:
        for product in sorted(PRODUCTS):
            if (
                product == "CU"
                and drop_cu_after is not None
                and month > drop_cu_after
            ):
                continue
            universe_rows.append({"month_start": month, "product": product})
    roll_fills = pd.DataFrame(
        columns=[
            "trade_date", "product", "old_contract", "new_contract", "fill_time",
            "old_price", "new_price", "old_pricing_basis", "new_pricing_basis",
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
        universes=pd.DataFrame(universe_rows),
        dominants=dominants,
        roll_fills=roll_fills,
        manifest={"bundle_version": 1},
    )
    shadows = {}
    for product in sorted(PRODUCTS):
        raw = run_shadow_product(bars, product=product, roll_fills=roll_fills)
        daily, trades = _forced_scores(product)
        shadows[product] = ShadowResult(
            product, raw.signal_mode, raw.signals, trades, daily
        )
    return bundle, shadows


@pytest.fixture(scope="module")
def scenario():
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


def test_only_current_positions_share_capital(scenario) -> None:
    result = _run(*scenario)

    paired = result.positions.query("active_products == 2 and direction != 0")
    assert not paired.empty
    assert paired["base_weight_abs"].eq(0.5).all()

    alone = result.positions.query("active_products == 1 and direction != 0")
    assert not alone.empty
    assert alone["base_weight_abs"].eq(1.0).all()


def test_a_flat_product_is_absent_from_the_active_denominator(scenario) -> None:
    result = _run(*scenario)

    flat = result.positions.query("direction == 0")
    assert not flat.empty
    assert flat["base_weight_abs"].eq(0.0).all()
    assert flat["universe_weight"].eq(0.0).all()
    assert flat["actual_weight"].eq(0.0).all()


def test_monthly_multiplier_targets_fifteen_percent(scenario) -> None:
    result = _run(*scenario)

    applied = result.positions["target_annual_vol"].dropna()
    assert not applied.empty
    assert applied.eq(0.15).all()


def test_an_entry_resizes_the_other_holders_at_their_own_next_window(scenario) -> None:
    result = _run(*scenario)

    resizes = result.trades.query("reason == 'allocation_resize'")
    assert not resizes.empty

    for row in resizes.itertuples(index=False):
        # Every resize must land on a fill window belonging to the product
        # being resized -- never on the price of whoever changed the
        # denominator.
        own_windows = result.signals.loc[
            (result.signals["product"] == row.product)
            & (result.signals["fill_time"] == row.timestamp)
        ]
        assert not own_windows.empty

    # And the cause has to come first: something else entered or left before
    # each resize, otherwise the denominator never moved.
    first_resize = resizes["timestamp"].min()
    causes = result.trades.loc[
        (result.trades["timestamp"] < first_resize)
        & (result.trades["reason"] != "allocation_resize")
    ]
    assert not causes.empty


def test_the_multiplier_is_constant_inside_one_month(scenario) -> None:
    result = _run(*scenario)

    per_month = result.daily.dropna(subset=["vol_multiplier"]).groupby("month_start")
    assert per_month.ngroups >= 2
    assert per_month["vol_multiplier"].nunique().eq(1).all()


def test_future_returns_cannot_move_an_earlier_multiplier(scenario) -> None:
    bundle, shadows = scenario
    baseline = _run(bundle, shadows)

    disturbed = {}
    for product, shadow in shadows.items():
        daily = shadow.daily.copy()
        later = daily["trade_date"] >= date(2023, 10, 1)
        daily.loc[later, "net_return"] = -0.4
        disturbed[product] = ShadowResult(
            product, shadow.signal_mode, shadow.signals, shadow.trades, daily
        )
    changed = _run(bundle, disturbed)

    horizon = date(2023, 9, 1)
    pd.testing.assert_series_equal(
        baseline.daily.loc[baseline.daily["month_start"] < horizon, "vol_multiplier"],
        changed.daily.loc[changed.daily["month_start"] < horizon, "vol_multiplier"],
    )


def test_a_product_leaving_the_universe_is_closed_at_the_next_month() -> None:
    bundle, shadows = _scenario(drop_cu_after=date(2023, 6, 1))
    result = _run(bundle, shadows)

    cutoff = date(2023, 7, 1)
    copper = result.positions.query("product == 'CU'")
    dropped = copper.loc[pd.to_datetime(copper["month_start"]).dt.date > cutoff]
    assert not dropped.empty
    assert dropped["selected"].eq(False).all()
    assert dropped["actual_weight"].eq(0.0).all()
    # The other two keep trading, so this is a selection exit and not an empty run.
    survivors = result.positions.query("product != 'CU' and direction != 0")
    assert not survivors.empty


def test_selected_allocation_reads_the_denominator_as_the_selected_universe(
    scenario,
) -> None:
    """D7 的另一种读法：「满足开仓条件品种等权分配资金」按 Bollinger 篇同一句
    （「经筛选后品种等权」）读 —— 分母是当月入选品种，无信号份额留现金，别人
    进出不再牵动我的目标。登记默认（只在持仓品种间等分）不动。"""
    result = _run(*scenario, allocation="selected")

    held = result.positions.query("direction != 0")
    assert not held.empty
    selected_count = (
        result.positions.query("selected").groupby("trade_date")["product"].nunique()
    )
    expected = held["trade_date"].map(selected_count)
    assert np.allclose(held["base_weight_abs"], 1.0 / expected)
    # 入选但无信号的品种也占一份分母（份额留现金）。
    chosen = result.positions.query("selected")
    assert np.allclose(
        chosen["universe_weight"], 1.0 / chosen["trade_date"].map(selected_count)
    )
    assert (chosen.query("direction == 0")["actual_weight"] == 0.0).all()

    assert result.trades.query("reason == 'allocation_resize'").empty


def test_the_default_allocation_is_still_active_only(scenario) -> None:
    default = _run(*scenario)
    explicit = _run(*scenario, allocation="active")
    assert default.trades.equals(explicit.trades)
    with pytest.raises(ValueError, match="allocation"):
        _run(*scenario, allocation="equal")
