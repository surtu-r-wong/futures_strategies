from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

import cta_gtja.__main__ as cta_main
from cta_gtja.__main__ import _data_quality_summary
from cta_gtja.backtest import write_cta_outputs
from cta_gtja.coverage import FundamentalCoverageError
from cta_gtja.data import CTADataSet
from cta_gtja.factors import (
    BasisFactor,
    LongCrossSectionMomentumFactor,
    cta_factors_for_set,
    default_cta_factors,
)
from cta_gtja.pg_source import PILOT_FUNDAMENTAL_SYMBOLS
from cta_gtja.strategies import build_factor_sleeves, run_high_composite, run_medium_equal_weight


def _single_symbol_data(closes: np.ndarray, symbol: str = "CU") -> CTADataSet:
    dates = pd.bdate_range("2020-01-01", periods=len(closes)).date
    prices = pd.DataFrame(
        {"trade_date": dates, "symbol": symbol, "open": closes, "close": closes}
    )
    return CTADataSet(
        prices=prices,
        fundamentals=pd.DataFrame(columns=["trade_date", "symbol"]),
    )


def _sample_cta_data(n: int = 320) -> CTADataSet:
    dates = pd.bdate_range("2020-01-01", periods=n).date
    symbols = ["CU", "AL", "RB", "TA"]
    rows = []
    fund_rows = []
    for s_idx, symbol in enumerate(symbols):
        base = 100 + s_idx * 15
        trend = np.linspace(0, (s_idx - 1.5) * 18, n)
        cycle = np.sin(np.linspace(0, 8, n) + s_idx) * 4
        close = base + trend + cycle
        open_px = close * (1 + 0.001 * np.cos(np.linspace(0, 6, n) + s_idx))
        volume = 1000 + s_idx * 200 + np.linspace(0, 100, n) + np.cos(np.linspace(0, 9, n)) * 50
        basis = 0.02 * np.sin(np.linspace(0, 5, n) + s_idx)
        inventory = 100 + s_idx * 10 + np.linspace(0, (1.5 - s_idx) * 20, n)
        profit = 30 + s_idx * 5 + np.sin(np.linspace(0, 10, n) + s_idx) * 8
        for i, d in enumerate(dates):
            rows.append({
                "trade_date": d,
                "symbol": symbol,
                "open": open_px[i],
                "close": close[i],
                "volume": volume[i],
            })
            fund_rows.append({
                "trade_date": d,
                "symbol": symbol,
                "spot": close[i] * (1 + basis[i]),
                "basis_rate": basis[i],
                "inventory": inventory[i],
                "profit": profit[i],
            })
    return CTADataSet(
        prices=pd.DataFrame(rows),
        fundamentals=pd.DataFrame(fund_rows),
    )


def test_cli_defaults_to_price_volume_factor_set():
    args = cta_main.build_parser().parse_args([])

    assert args.factor_set == "price_volume"


def test_file_six_factor_requires_finite_fundamentals():
    incomplete = _single_symbol_data(np.linspace(100.0, 120.0, 80))

    with pytest.raises(SystemExit, match="basis.*inventory.*profit"):
        cta_main._validate_six_factor_request(
            source="files",
            factor_set="six_factor",
            data=incomplete,
        )

    complete = _sample_cta_data()
    cta_main._validate_six_factor_request(
        source="files",
        factor_set="six_factor",
        data=complete,
    )


def test_basis_factor_falls_back_to_spot_when_basis_rate_nonfinite():
    sample = _sample_cta_data()
    fundamentals = sample.fundamentals.copy()
    fundamentals["basis_rate"] = np.nan
    data = CTADataSet(
        prices=sample.prices.copy(),
        fundamentals=fundamentals,
    )

    cta_main._validate_six_factor_request(
        source="files",
        factor_set="six_factor",
        data=data,
    )

    scores = BasisFactor().compute(data, data.symbols)
    assert np.isfinite(scores.to_numpy(dtype=float)).any()


def test_default_factors_build_sleeves():
    data = _sample_cta_data()
    weights_by_factor, factor_returns, coverage_audit = build_factor_sleeves(
        data,
        factors=default_cta_factors(),
        symbols=data.symbols,
        enforce_coverage=False,
    )

    assert set(weights_by_factor) == {
        "basis",
        "inventory",
        "profit",
        "long_rule_momentum",
        "long_cross_momentum",
        "price_volume_corr",
    }
    assert factor_returns.shape[1] == 6
    assert factor_returns.dropna(how="all").shape[0] > 0
    assert not coverage_audit.empty
    assert coverage_audit["status"].eq("fail").any()


def test_default_factors_enforce_coverage_before_portfolio_conversion():
    data = _sample_cta_data()

    with pytest.raises(FundamentalCoverageError) as error:
        build_factor_sleeves(
            data,
            factors=default_cta_factors(),
            symbols=data.symbols,
            enforce_coverage=True,
        )

    assert "2020-01-01 basis_rate coverage=4 required=6" in str(error.value)


def test_medium_equal_weight_runs_end_to_end():
    data = _sample_cta_data()
    result = run_medium_equal_weight(
        data,
        symbols=data.symbols,
        cost_bps=1.0,
        enforce_coverage=False,
    )

    assert result.metrics["n_periods"] > 100
    assert not result.period_returns.empty
    assert not result.equity.empty
    assert result.weights.abs().sum(axis=1).max() <= 2.5 + 1e-9
    assert result.factor_allocations.shape[1] == 6
    assert not result.fundamental_coverage.empty
    assert result.fundamental_coverage["status"].eq("fail").any()


def test_high_composite_caps_factor_allocations():
    data = _sample_cta_data()
    result = run_high_composite(
        data,
        symbols=data.symbols,
        cost_bps=1.0,
        enforce_coverage=False,
    )

    assert result.metrics["n_periods"] > 100
    assert result.factor_allocations.max().max() <= 0.50 + 1e-12
    assert result.weights.abs().sum(axis=1).max() <= 3.5 + 1e-9
    assert not result.fundamental_coverage.empty
    assert result.fundamental_coverage["status"].eq("fail").any()


def test_long_cross_momentum_is_regression_slope_of_log_price():
    """Deck factor 05 (GTJAQH013): regress log adjusted price on time, take the
    OLS slope.  For a pure log-linear path ``log(P_t) = b * t`` the slope is the
    daily drift ``b`` -- not the point-to-point return ``b * lookback`` that a
    two-endpoint momentum would yield.
    """
    daily_drift = 0.001
    n = 300
    lookback = 252
    closes = np.exp(daily_drift * np.arange(n))
    data = _single_symbol_data(closes)

    factor = LongCrossSectionMomentumFactor(lookback_days=lookback)
    scores = factor.compute(data, ["CU"])

    last = scores["CU"].iloc[-1]
    assert last == pytest.approx(daily_drift, rel=1e-6)


def test_long_cross_momentum_regresses_through_gaps():
    """Real continuous-contract series have gaps (new listings, suspensions).
    The trend slope must still be estimated from the observations present rather
    than collapsing to NaN whenever a window touches a missing day.
    """
    daily_drift = 0.001
    n = 300
    closes = np.exp(daily_drift * np.arange(n))
    closes[::25] = np.nan  # scattered gaps inside every trailing window
    data = _single_symbol_data(closes)

    factor = LongCrossSectionMomentumFactor(lookback_days=252)
    scores = factor.compute(data, ["CU"])

    last = scores["CU"].iloc[-1]
    assert np.isfinite(last)
    assert last == pytest.approx(daily_drift, rel=1e-6)


def test_future_prices_cannot_change_past_high_composite_decisions():
    """No look-ahead: perturbing prices strictly after a cutoff must not change
    any target weight or factor allocation decided on or before that cutoff.

    The rotation sleeve ranks factor sleeves by trailing return, and the vol
    target scales by trailing realized vol; both read a forward-indexed return
    series (``open(t+2)/open(t+1)``), so they must lag it far enough to use only
    information available at the close of the decision day.
    """
    data = _sample_cta_data()
    dates = data.dates
    cutoff = dates[200]

    baseline = run_high_composite(
        data,
        symbols=data.symbols,
        cost_bps=1.0,
        enforce_coverage=False,
    )

    perturbed_prices = data.prices.copy()
    future = perturbed_prices["trade_date"] > cutoff
    # Ratio-breaking, strictly-positive multipliers (a constant factor would
    # leave open(t+2)/open(t+1) unchanged and never exercise the leak).
    multipliers = 1.5 + 0.5 * np.cos(np.arange(int(future.sum())))
    for column in ("open", "close", "volume"):
        perturbed_prices.loc[future, column] = (
            perturbed_prices.loc[future, column].to_numpy() * multipliers
        )
    perturbed = run_high_composite(
        CTADataSet(prices=perturbed_prices, fundamentals=data.fundamentals.copy()),
        symbols=data.symbols,
        cost_bps=1.0,
        enforce_coverage=False,
    )

    for frame_name in ("factor_allocations", "weights"):
        base_frame = getattr(baseline, frame_name)
        pert_frame = getattr(perturbed, frame_name)
        pd.testing.assert_frame_equal(
            base_frame.loc[base_frame.index <= cutoff],
            pert_frame.loc[pert_frame.index <= cutoff],
            obj=frame_name,
        )


def test_data_slice_filters_symbols_and_dates():
    data = _sample_cta_data().slice(
        symbols=["CU", "RB"],
        start=date(2020, 3, 1),
        end=date(2020, 6, 30),
    )

    assert data.symbols == ["CU", "RB"]
    assert min(data.dates) >= date(2020, 3, 1)
    assert max(data.dates) <= date(2020, 6, 30)


def test_data_slice_preserves_data_quality_for_symbols():
    data = _sample_cta_data()
    quality = pd.DataFrame([
        {"base_symbol": "CU", "selected_adj": "fa", "included": True},
        {"base_symbol": "AL", "selected_adj": "ba", "included": True},
        {"base_symbol": "RB", "selected_adj": "fa", "included": True},
        {"base_symbol": "TA", "selected_adj": "fa", "included": True},
    ])
    data = CTADataSet(prices=data.prices, fundamentals=data.fundamentals, data_quality=quality)

    sliced = data.slice(symbols=["CU", "RB"], start=date(2020, 3, 1), end=date(2020, 6, 30))

    assert sorted(sliced.data_quality["base_symbol"].tolist()) == ["CU", "RB"]


def test_price_volume_factor_set_excludes_fundamental_factors():
    factors = cta_factors_for_set("price_volume")

    assert [f.name for f in factors] == [
        "long_rule_momentum",
        "long_cross_momentum",
        "price_volume_corr",
    ]


def test_six_factor_set_remains_default():
    factors = cta_factors_for_set("six_factor")

    assert [f.name for f in factors] == [
        "basis",
        "inventory",
        "profit",
        "long_rule_momentum",
        "long_cross_momentum",
        "price_volume_corr",
    ]


def test_write_cta_outputs_includes_data_quality_sheet(tmp_path):
    data = _sample_cta_data()
    quality = pd.DataFrame([
        {
            "base_symbol": "CU",
            "status": "ok",
            "recommended_adj": "fa",
            "selected_adj": "fa",
            "included": True,
            "raw_fallback": False,
            "exclusion_reason": "",
        }
    ])
    data = CTADataSet(prices=data.prices, fundamentals=data.fundamentals, data_quality=quality)
    result = run_medium_equal_weight(
        data,
        symbols=data.symbols,
        factors=cta_factors_for_set("price_volume"),
        cost_bps=1.0,
    )

    xlsx, _ = write_cta_outputs(result, tmp_path / "cta_guarded")

    sheets = pd.ExcelFile(xlsx).sheet_names
    assert "data_quality" in sheets
    written = pd.read_excel(xlsx, sheet_name="data_quality")
    assert written.loc[0, "base_symbol"] == "CU"
    assert written.loc[0, "selected_adj"] == "fa"


def test_data_quality_summary_counts_retained_excluded_and_raw():
    quality = pd.DataFrame([
        {"base_symbol": "A", "included": True, "raw_fallback": False},
        {"base_symbol": "B", "included": True, "raw_fallback": True},
        {"base_symbol": "C", "included": False, "raw_fallback": False},
    ])

    assert _data_quality_summary(quality) == "symbols retained=2 excluded=1 raw_fallback=1"


def test_file_dataset_marks_fundamentals_unverified(tmp_path):
    data = _sample_cta_data(n=5)
    data.prices.to_csv(tmp_path / "prices.csv", index=False)
    data.fundamentals.to_csv(
        tmp_path / "fundamentals.csv",
        index=False,
    )

    loaded = CTADataSet.from_dir(tmp_path)

    assert loaded.fundamental_quality.empty
    assert loaded.fundamental_metadata == {
        "source": "files-unverified",
        "materialized_daily": False,
    }


@pytest.mark.parametrize(
    ("factor_set", "requested", "expected"),
    [
        ("six_factor", "auto", "standard"),
        ("price_volume", "auto", "none"),
        ("six_factor", "standard", "standard"),
        ("six_factor", "legacy", "legacy"),
    ],
)
def test_fundamentals_source_resolution(factor_set, requested, expected):
    assert (
        cta_main._resolve_fundamentals_source(factor_set, requested)
        == expected
    )


def test_six_factor_rejects_none_fundamentals_source():
    with pytest.raises(SystemExit, match="fundamentals.*required"):
        cta_main._resolve_fundamentals_source("six_factor", "none")


def test_default_symbols_are_pilot_only_for_six_factor():
    assert cta_main._default_symbols_for_factor_set(
        "six_factor", None
    ) == list(PILOT_FUNDAMENTAL_SYMBOLS)
    assert cta_main._default_symbols_for_factor_set(
        "price_volume", None
    ) is None

    explicit_symbols = ["AU", "AG"]
    assert cta_main._default_symbols_for_factor_set(
        "six_factor", explicit_symbols
    ) == explicit_symbols
    assert cta_main._default_symbols_for_factor_set(
        "price_volume", explicit_symbols
    ) == explicit_symbols


def test_fundamental_lineage_summary_renders_standard_metadata():
    assert cta_main._fundamental_lineage_summary(
        {
            "source": "standard",
            "pit_mode": "conservative",
            "build_version": "build-c-1",
            "catalog_version": "v1",
        }
    ) == "fundamentals: source=standard pit_mode=conservative build=build-c-1 catalog=v1"


def test_fundamental_lineage_summary_renders_none_metadata():
    assert cta_main._fundamental_lineage_summary(
        {"source": "none"}
    ) == "fundamentals: source=none"
