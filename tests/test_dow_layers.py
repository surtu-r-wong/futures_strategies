"""One product, three layers: the paper's single-product tables, reproduced by reading."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from cta_dow.layers import (
    LAYER_COLUMNS,
    bar_returns,
    layer_daily_returns,
    layer_positions,
    paper_annual_table,
)


def _signals(rows: list[dict[str, object]]) -> pd.DataFrame:
    defaults = {
        "trade_date": date(2020, 1, 2),
        "no_trade": False,
        "continuity_segment": 0,
        "signal_close": 100.0,
        "trend": "up",
        "turning_valid": True,
        "dow_resonance": True,
        "close_breakout": False,
        "enough_history": True,
        "last_up_high_1": np.nan,
        "last_up_high_2": np.nan,
        "last_down_low_1": 90.0,
        "last_down_low_2": 80.0,
        "state_position": "flat",
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


def test_layer_one_holds_the_preliminary_trend_direction():
    frame = _signals(
        [{"trend": "neutral"}, {"trend": "up"}, {"trend": "down"}, {"trend": "up"}]
    )
    got = layer_positions(frame)
    assert list(got.columns) == list(LAYER_COLUMNS)
    assert got["l1_trend"].tolist() == [0, 1, -1, 1]


def test_layer_two_stand_down_reading_goes_flat_when_the_pullback_fails():
    frame = _signals(
        [
            {"trend": "up", "turning_valid": True},
            {"trend": "up", "turning_valid": False},
            {"trend": "down", "turning_valid": False, "last_up_high_1": 120.0},
        ]
    )
    got = layer_positions(frame)
    assert got["l2_stand_down"].tolist() == [1, 0, 0]


def test_layer_two_reversal_reading_flips_and_keeps_the_trend_without_history():
    frame = _signals(
        [
            # 上升段，前一下降段的低点存在且未被跌破：跟随趋势。
            {"trend": "up", "turning_valid": True},
            # 跌破前低：研报原文「修正为下降趋势」，反手。
            {"trend": "up", "turning_valid": False},
            # 第一个下降段，没有前一上升段的高点可比 ⇒ 趋势照旧成立。
            {
                "trend": "down",
                "turning_valid": False,
                "last_up_high_1": np.nan,
                "last_down_low_1": np.nan,
            },
            {"trend": "neutral", "turning_valid": False},
        ]
    )
    got = layer_positions(frame)
    assert got["l2_reversal"].tolist() == [1, -1, -1, 0]


def test_layer_three_registered_copies_the_shadow_state():
    frame = _signals(
        [
            {"state_position": "flat"},
            {"state_position": "long"},
            {"state_position": "short"},
        ]
    )
    got = layer_positions(frame)
    assert got["l3_registered"].tolist() == [0, 1, -1]


def test_layer_three_opposite_only_flat_keeps_the_corrected_position_when_undetermined():
    frame = _signals(
        [
            # 道氏上升（第一低点 > 第二低点）：与上升趋势共振，持多。
            {"trend": "up", "last_down_low_1": 90.0, "last_down_low_2": 80.0},
            # 第一低点 < 第二低点：道氏不是上升；但高点历史不足，道氏也不是下降 ⇒ 未定，
            # 「相反时空仓」不触发，沿用修正后趋势。
            {"trend": "up", "last_down_low_1": 70.0, "last_down_low_2": 80.0},
            # 高点依次下降 ⇒ 道氏下降，与上升趋势相反 ⇒ 空仓。
            {
                "trend": "up",
                "last_down_low_1": 70.0,
                "last_down_low_2": 80.0,
                "last_up_high_1": 110.0,
                "last_up_high_2": 120.0,
            },
            # 拐点修正失效：这条读法建立在反手读法之上；低点未依次上升，道氏不与
            # 修正后的下降趋势相反 ⇒ 持空。
            {
                "trend": "up",
                "turning_valid": False,
                "last_down_low_1": 70.0,
                "last_down_low_2": 80.0,
                "last_up_high_1": np.nan,
            },
        ]
    )
    got = layer_positions(frame)
    assert got["l3_opposite_flat"].tolist() == [1, 1, 0, -1]


def test_untraded_bars_carry_the_previous_position_in_every_layer():
    frame = _signals(
        [
            {
                "trend": "up",
                "state_position": "long",
                "close_breakout": True,
                "last_up_high_1": 90.0,
            },
            {
                "trend": "neutral",
                "no_trade": True,
                "signal_close": np.nan,
                "turning_valid": False,
                "state_position": "long",
            },
            {"trend": "up", "state_position": "long"},
        ]
    )
    got = layer_positions(frame)
    for column in LAYER_COLUMNS:
        assert got[column].tolist() == [1, 1, 1], column


def test_layer_three_breakout_latched_enters_on_the_breakout_and_holds():
    frame = _signals(
        [
            {"trend": "up", "close_breakout": False},
            {"trend": "up", "close_breakout": True},
            {"trend": "up", "close_breakout": False},
            # 道氏趋势转为相反：空仓，即使锁存中。
            {
                "trend": "up",
                "close_breakout": False,
                "last_down_low_1": 70.0,
                "last_down_low_2": 80.0,
                "last_up_high_1": 110.0,
                "last_up_high_2": 120.0,
            },
            {"trend": "down", "last_up_high_1": 120.0, "last_up_high_2": 110.0},
        ]
    )
    got = layer_positions(frame)
    assert got["l3_breakout_latched"].tolist() == [0, 1, 1, 0, 0]


def test_bar_returns_reset_across_a_continuity_break_and_skip_untraded_bars():
    frame = _signals(
        [
            {"signal_close": 100.0},
            {"signal_close": 110.0},
            {"signal_close": np.nan, "no_trade": True},
            {"signal_close": 121.0},
            {"signal_close": 50.0, "continuity_segment": 1},
            {"signal_close": 55.0, "continuity_segment": 1},
        ]
    )
    got = bar_returns(frame)
    expected = [np.nan, 0.1, np.nan, 0.1, np.nan, 0.1]
    assert len(got) == 6
    for value, want in zip(got.tolist(), expected):
        if np.isnan(want):
            assert np.isnan(value)
        else:
            assert value == pytest.approx(want)


def test_daily_returns_compound_within_the_day_and_charge_cost_on_position_changes():
    frame = _signals(
        [
            {"trade_date": date(2020, 1, 2), "signal_close": 100.0, "trend": "up"},
            {"trade_date": date(2020, 1, 2), "signal_close": 110.0, "trend": "up"},
            {"trade_date": date(2020, 1, 3), "signal_close": 99.0, "trend": "down"},
            {"trade_date": date(2020, 1, 3), "signal_close": 108.9, "trend": "down"},
        ]
    )
    positions = layer_positions(frame)
    got = layer_daily_returns(
        frame, positions["l1_trend"], returns=bar_returns(frame), cost_bps=100.0
    )
    # 第一根建仓（换手 1，成本 1%），第二根 +10% 持多：(1.10)(1 - 0.01) - 1。
    # 第二日第一根 -10% 仍持多、随后反手（换手 2，成本 2%）；第二根 +10% 持空。
    assert got.index.tolist() == [date(2020, 1, 2), date(2020, 1, 3)]
    assert got.iloc[0] == pytest.approx(1.10 * 0.99 - 1.0)
    assert got.iloc[1] == pytest.approx(0.90 * 0.98 * 0.90 - 1.0)


def test_paper_annual_table_uses_return_over_volatility_and_return_over_drawdown():
    days = pd.bdate_range("2021-01-01", "2022-12-31")
    rng = np.random.default_rng(7)
    daily = pd.Series(rng.normal(0.001, 0.01, len(days)), index=days.date)
    table = paper_annual_table(daily)
    assert table.index.tolist() == ["2021", "2022", "full_sample"]
    year = daily[[d.year == 2021 for d in daily.index]]
    equity = (1.0 + year).cumprod()
    total = equity.iloc[-1] - 1.0
    vol = year.std(ddof=1) * np.sqrt(252)
    drawdown = (equity / equity.cummax() - 1.0).min()
    months = year.groupby([d.month for d in year.index]).apply(
        lambda r: (1.0 + r).prod() - 1.0
    )
    row = table.loc["2021"]
    assert row["return"] == pytest.approx(total)
    assert row["volatility"] == pytest.approx(vol)
    assert row["sharpe"] == pytest.approx(total / vol)
    assert row["max_drawdown"] == pytest.approx(-drawdown)
    assert row["calmar"] == pytest.approx(total / -drawdown)
    assert row["monthly_win_rate"] == pytest.approx((months > 0).mean())
    assert row["trading_days"] == len(year)
    full = table.loc["full_sample"]
    all_equity = (1.0 + daily).cumprod()
    cagr = all_equity.iloc[-1] ** (252 / len(daily)) - 1.0
    assert full["return"] == pytest.approx(cagr)
    assert full["sharpe"] == pytest.approx(cagr / (daily.std(ddof=1) * np.sqrt(252)))


def test_layer_report_restricts_every_table_to_the_window():
    from cta_dow.layers import layer_report

    frame = _signals(
        [
            {"trade_date": date(2019, 12, 31), "signal_close": 100.0},
            {"trade_date": date(2020, 1, 2), "signal_close": 110.0},
            {"trade_date": date(2020, 1, 3), "signal_close": 121.0},
            {"trade_date": date(2020, 1, 6), "signal_close": 133.1},
        ]
    )
    shadow_daily = pd.DataFrame(
        {
            "trade_date": [
                date(2019, 12, 31),
                date(2020, 1, 2),
                date(2020, 1, 3),
                date(2020, 1, 6),
            ],
            "net_return": [0.5, 0.01, 0.02, 0.03],
        }
    )
    report = layer_report(
        frame,
        shadow_daily=shadow_daily,
        start=date(2020, 1, 1),
        end=date(2020, 1, 3),
        cost_bps=0.0,
    )
    assert set(report) == set(LAYER_COLUMNS) | {"shadow_registered"}
    for table in report.values():
        assert table.index.tolist() == ["2020", "full_sample"]
        assert table.loc["2020", "trading_days"] == 2
    # l1 持多：01-02 的 bar 回报 +10% 归 01-02，01-03 +10%；12-31 那天被裁掉。
    assert report["l1_trend"].loc["2020", "return"] == pytest.approx(1.1 * 1.1 - 1.0)
    assert report["shadow_registered"].loc["2020", "return"] == pytest.approx(
        1.01 * 1.02 - 1.0
    )


def test_layer_three_resonance_hold_holds_whenever_the_dow_trend_agrees():
    frame = _signals(
        [
            # 道氏上升与上升趋势共振：持多，不要求突破。
            {"trend": "up", "last_down_low_1": 90.0, "last_down_low_2": 80.0},
            # 低点未依次上升：共振不成立 ⇒ 空仓。
            {"trend": "up", "last_down_low_1": 70.0, "last_down_low_2": 80.0},
            # 下降趋势且高点依次下降：持空。
            {"trend": "down", "last_up_high_1": 110.0, "last_up_high_2": 120.0},
        ]
    )
    got = layer_positions(frame)
    assert got["l3_resonance_hold"].tolist() == [1, 0, -1]


def test_atr_sizing_fixes_the_magnitude_at_the_bar_the_direction_changes():
    from cta_dow.layers import atr_magnitudes, sized_positions

    frame = _signals(
        [
            {"raw_close": 1000.0, "atr_raw": 10.0},
            {"raw_close": 1000.0, "atr_raw": 20.0},
            {"raw_close": 1000.0, "atr_raw": np.nan},
            {"raw_close": 1000.0, "atr_raw": 1.0},
        ]
    )
    magnitude = atr_magnitudes(frame)
    # 0.005 * 1000 / 10 = 0.5；ATR 缺失取 0；0.005 * 1000 / 1 = 5 截到 4。
    assert magnitude.tolist() == [0.5, 0.25, 0.0, 4.0]
    direction = pd.Series([1, 1, -1, -1], index=frame.index)
    got = sized_positions(direction, magnitude)
    # 建仓那根定量，方向不变就一直持有那个量；换向那根 ATR 缺失 ⇒ 0。
    assert got.tolist() == [0.5, 0.5, 0.0, 0.0]


def test_layer_report_can_size_every_layer_by_atr():
    from cta_dow.layers import layer_report

    frame = _signals(
        [
            {
                "trade_date": date(2020, 1, 2),
                "signal_close": 100.0,
                "raw_close": 100.0,
                "atr_raw": 1.0,
            },
            {
                "trade_date": date(2020, 1, 3),
                "signal_close": 110.0,
                "raw_close": 110.0,
                "atr_raw": 1.0,
            },
        ]
    )
    shadow_daily = pd.DataFrame(
        {"trade_date": [date(2020, 1, 2), date(2020, 1, 3)], "net_return": [0.0, 0.0]}
    )
    report = layer_report(
        frame,
        shadow_daily=shadow_daily,
        start=date(2020, 1, 1),
        end=date(2020, 1, 3),
        cost_bps=0.0,
        sizing="atr",
    )
    # 0.005 * 100 / 1 = 0.5 倍：+10% 的 bar 只赚 5%。
    assert report["l1_trend"].loc["2020", "return"] == pytest.approx(0.05)


def test_layer_three_prior_high_reading_enters_when_the_close_clears_the_last_up_high():
    # 研报公式块在上升趋势下定义了 lastmax_1（上一上升段最高价），正文条件却没用它；
    # 图 13 标注的也是「第一高点」与「临时高点」。这条读法把入场改成收盘越过第一高点。
    frame = _signals(
        [
            {"trend": "up", "signal_close": 115.0, "last_up_high_1": 120.0},
            {"trend": "up", "signal_close": 121.0, "last_up_high_1": 120.0},
            # 锁存：回落到第一高点之下仍持有。
            {"trend": "up", "signal_close": 118.0, "last_up_high_1": 120.0},
            # 拐点修正失效：平仓。
            {
                "trend": "up",
                "signal_close": 118.0,
                "last_up_high_1": 120.0,
                "turning_valid": False,
            },
            # 下降趋势：高点依次下降且收盘跌破第一低点 ⇒ 持空。
            {
                "trend": "down",
                "signal_close": 85.0,
                "last_up_high_1": 110.0,
                "last_up_high_2": 120.0,
                "last_down_low_1": 90.0,
            },
        ]
    )
    got = layer_positions(frame)
    assert got["l3_prior_high_breakout"].tolist() == [0, 1, 1, 0, -1]
