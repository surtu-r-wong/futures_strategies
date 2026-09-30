import numpy as np
import pandas as pd
import pytest

from scripts.research.alpha013_commodity import (
    alpha013,
    forward_returns,
    hac_t,
    quantile_weights,
    simulate_opens,
    load_panel,
)


def test_formula_matches_hand_covariance_and_does_not_look_forward():
    close = pd.DataFrame([[1, 2, 3], [2, 1, 3], [3, 2, 1], [2, 3, 1], [1, 3, 2], [9, 1, 5]])
    volume = pd.DataFrame([[3, 1, 2], [1, 3, 2], [2, 3, 1], [3, 1, 2], [1, 2, 3], [7, 9, 1]])
    scores, cov = alpha013(close, volume)
    x = close.iloc[:5].rank(axis=1, pct=True)
    y = volume.iloc[:5].rank(axis=1, pct=True)
    expected = pd.Series([np.cov(x[c], y[c], ddof=1)[0, 1] for c in close.columns])
    np.testing.assert_allclose(cov.iloc[4], expected, atol=1e-14)
    np.testing.assert_allclose(scores.iloc[4], -expected.rank(pct=True))
    changed = close.copy()
    changed.iloc[5] = [300, 500, 100]
    pd.testing.assert_frame_equal(alpha013(changed, volume)[0].iloc[:5], scores.iloc[:5])
    assert scores.iloc[:4].isna().all().all()


def test_constant_ranks_and_missing_sessions_do_not_create_signals():
    close = pd.DataFrame(np.tile([1.0, 2, 3, 4, 5, 6, 7], (9, 1)))
    volume = pd.DataFrame(np.random.default_rng(7).uniform(1, 100, (9, 7)))
    scores, cov = alpha013(close, volume)
    assert cov.iloc[4:].eq(0).all().all()
    assert scores.iloc[4:].nunique(axis=1).eq(1).all()
    volume.iloc[4, 0] = np.nan
    assert alpha013(close, volume)[0].iloc[4:9, 0].isna().all()


def test_ties_are_split_symmetrically_without_symbol_order_bias():
    scores = pd.DataFrame([[1, 1, 1, 1, 1], [1, 2, 3, 4, 5], [np.nan] * 5], columns=list("ABCDE"))
    groups = quantile_weights(scores)
    for group in groups:
        np.testing.assert_allclose(group.iloc[:2].sum(axis=1), 1)
        np.testing.assert_allclose(group.iloc[0], 0.2)
    np.testing.assert_allclose((groups[-1] - groups[0]).iloc[0], 0)
    assert groups[0].iloc[1].to_dict() == dict(zip("ABCDE", [1, 0, 0, 0, 0]))
    reversed_groups = quantile_weights(scores[scores.columns[::-1]])
    for left, right in zip(groups, reversed_groups):
        pd.testing.assert_frame_equal(left, right[left.columns])
    assert groups[0].iloc[2].eq(0).all()


def test_forward_labels_use_next_open_same_contract_and_global_calendar():
    dates = pd.date_range("2025-01-01", periods=4)
    bars = pd.DataFrame({"trade_date": list(dates) * 2,
                         "contract": ["A2505.X"] * 4 + ["A2509.X"] * 4,
                         "open": [90, 100, 110, 121, 900, 950, 1200, 1400], "volume": [1] * 8})
    panel = pd.DataFrame({"trade_date": dates, "product": ["A"] * 4,
                          "contract": ["A2505.X", "A2509.X", "A2509.X", "A2509.X"]}).set_index(["trade_date", "product"])
    out = forward_returns(panel, bars, 1)
    assert out.loc[dates[0], "A"] == pytest.approx(0.1)
    missing = bars.drop(index=2)
    assert pd.isna(forward_returns(panel, missing, 1).loc[dates[0], "A"])
    assert out.iloc[-2:].isna().all().all()


def test_hac_mean_t_at_zero_lag_matches_direct_population_formula():
    values = pd.Series([1.0, 2, 4, 6, 2])
    expected = values.mean() * np.sqrt(len(values)) / values.std(ddof=0)
    assert hac_t(values, 0) == pytest.approx(expected)

def test_event_accounting_rolls_both_legs_and_liquidates():
    dates = pd.date_range("2025-01-01", periods=4)
    w = pd.DataFrame({"A": [0.5, 0.5], "B": [-0.5, -0.5]}, index=dates[:2])
    c = pd.DataFrame({"A": ["A2505.X", "A2509.X"], "B": ["B2505.X", "B2509.X"]}, index=dates[:2])
    market = {d: {contract: 100 for contract in c.to_numpy().ravel()} for d in dates}
    out = simulate_opens(w, c, market, dates, 4)
    assert out.iloc[0].turnover == 1
    assert out.iloc[0].net == pytest.approx(-0.0004)
    assert out.iloc[1].turnover == pytest.approx(1 / (1 - 0.0004) + 1)
    assert out.iloc[-1].gross_exposure == 0
    assert out.iloc[-1].turnover > 0
    np.testing.assert_allclose(out.net, -out.cost, atol=1e-15)


def test_halt_keeps_position_defers_roll_and_books_entire_gap():
    dates = pd.date_range("2025-01-01", periods=5)
    w = pd.DataFrame({"A": [0.5] * 3, "B": [-0.5] * 3}, index=dates[:3])
    c = pd.DataFrame({"A": ["A2505.X", "A2509.X", "A2509.X"],
                      "B": ["B2505.X"] * 3}, index=dates[:3])
    market = {d: {"A2505.X": 100, "A2509.X": 100, "B2505.X": 100} for d in dates}
    market[dates[2]].pop("A2505.X")
    for d in dates[3:]:
        market[d]["A2505.X"] = 120
        market[d]["A2509.X"] = 120
    out = simulate_opens(w, c, market, dates, 0)
    assert out.loc[dates[2], "unquoted_holdings"] == 1
    assert out.loc[dates[2], "skipped_targets"] == 1
    assert out.loc[dates[2], "turnover"] == 0
    assert out.loc[dates[3], "gross"] == pytest.approx(0.1)
    assert (1 + out.net).prod() == pytest.approx(1.1)
    assert out.iloc[-1].gross_exposure == 0


def test_loader_excludes_special_contracts_and_uses_lagged_liquidity(tmp_path):
    dates = pd.date_range("2025-01-01", periods=65)
    bars = pd.DataFrame({"trade_date": dates, "contract": "A2512.X", "open": 100,
                         "high": 101, "low": 99, "close": 100, "volume": 1000,
                         "oi": 1000, "turnover": 2e8})
    special = bars.iloc[:2].copy()
    special["contract"] = ["SC2512TAS.INE", "L2512F.DCE"]
    path = tmp_path / "prices.csv"
    pd.concat([bars, special]).to_csv(path, index=False)
    panel, _, audit = load_panel(path)
    assert audit["excluded_nonstandard_contract_rows"] == 2
    assert panel.iloc[:60].eligible.eq(False).all()
    assert panel.iloc[60:].eligible.eq(True).all()

def test_halted_day_zero_turnover_remains_in_liquidity_window(tmp_path):
    dates = pd.date_range("2025-01-01", periods=65)
    bars = pd.DataFrame({"trade_date": dates, "contract": "A2512.X", "open": 100.0,
                         "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000,
                         "oi": 1000, "turnover": 2e8})
    bars.loc[50, ["open", "high", "low"]] = np.nan
    bars.loc[50, ["turnover", "volume"]] = 0
    path = tmp_path / "prices.csv"
    bars.to_csv(path, index=False)
    panel, _, _ = load_panel(path)
    assert panel.loc[(dates[60], "A"), "eligible"]


def test_five_session_schedule_does_not_rebalance_between_decisions():
    dates = pd.date_range("2025-01-01", periods=8)
    w = pd.DataFrame({"A": [1, -1, -1, -1, -1, -1]}, index=dates[:6])
    c = pd.DataFrame({"A": "A2512.X"}, index=dates[:6])
    market = {d: {"A2512.X": 100} for d in dates}
    out = simulate_opens(w, c, market, dates, 0, rebalance_every=5)
    assert out.iloc[0].turnover == 1
    assert out.iloc[1:5].turnover.eq(0).all()
    assert out.iloc[5].turnover == 2
    assert out.iloc[6].turnover == 1

def test_ic_missing_if_future_open_has_no_trades():
    dates = pd.date_range("2025-01-01", periods=3)
    bars = pd.DataFrame({"trade_date": dates, "contract": "A2512.X",
                         "open": [100, 101, 102], "volume": [1, 1, 0]})
    panel = pd.DataFrame({"trade_date": dates, "product": "A",
                          "contract": "A2512.X"}).set_index(["trade_date", "product"])
    assert pd.isna(forward_returns(panel, bars, 1).iloc[0, 0])


def test_five_session_allocation_still_rolls_main_contract_daily():
    dates = pd.date_range("2025-01-01", periods=5)
    weights = pd.DataFrame({"A": [1.0, -1, -1]}, index=dates[:3])
    contracts = pd.DataFrame({"A": ["A2505.X", "A2509.X", "A2509.X"]}, index=dates[:3])
    market = {d: {"A2505.X": 100, "A2509.X": 200} for d in dates}
    market[dates[-1]].pop("A2505.X")
    out = simulate_opens(weights, contracts, market, dates, 0, rebalance_every=5)
    assert out.iloc[1].turnover == 2
    assert out.iloc[1].gross_exposure == 1
    assert out.iloc[2].turnover == 0
    assert out.iloc[-1].turnover == 1
    assert out.iloc[-1].gross_exposure == 0
