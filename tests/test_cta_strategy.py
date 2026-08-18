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
    InventoryFactor,
    LongCrossSectionMomentumFactor,
    ProfitFactor,
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


def test_basis_fallback_uses_close_raw_not_adjusted_close():
    trade_date = date(2020, 1, 1)
    data = CTADataSet(
        prices=pd.DataFrame(
            {
                "trade_date": [trade_date],
                "symbol": ["A"],
                "open": [400.0],
                "close": [400.0],
                "close_raw": [100.0],
            }
        ),
        fundamentals=pd.DataFrame(
            {
                "trade_date": [trade_date],
                "symbol": ["A"],
                "spot": [110.0],
            }
        ),
    )

    scores = BasisFactor().compute(data, ["A"])

    assert scores.loc[trade_date, "A"] == pytest.approx(0.10)


def test_basis_factor_falls_back_to_raw_spot_when_basis_rate_nonfinite():
    dates = [date(2020, 1, 1), date(2020, 1, 2)]
    data = CTADataSet(
        prices=pd.DataFrame(
            {
                "trade_date": dates,
                "symbol": ["A", "A"],
                "open": [400.0, 800.0],
                "close": [400.0, 800.0],
                "close_raw": [100.0, 200.0],
            }
        ),
        fundamentals=pd.DataFrame(
            {
                "trade_date": dates,
                "symbol": ["A", "A"],
                "spot": [110.0, 180.0],
                "basis_rate": [np.nan, np.inf],
                "inventory": [10.0, 11.0],
                "profit": [20.0, 21.0],
            }
        ),
    )

    cta_main._validate_six_factor_request(
        source="files",
        factor_set="six_factor",
        data=data,
    )
    scores = BasisFactor().compute(data, ["A"])

    expected = pd.DataFrame(
        {"A": [0.10, -0.10]},
        index=pd.Index(dates, name="trade_date"),
    )
    expected.columns.name = "symbol"
    pd.testing.assert_frame_equal(scores, expected)


def test_basis_stays_missing_without_published_basis_or_close_raw():
    trade_date = date(2020, 1, 1)
    data = CTADataSet(
        prices=pd.DataFrame(
            {
                "trade_date": [trade_date],
                "symbol": ["A"],
                "open": [400.0],
                "close": [400.0],
            }
        ),
        fundamentals=pd.DataFrame(
            {
                "trade_date": [trade_date],
                "symbol": ["A"],
                "spot": [110.0],
            }
        ),
    )

    scores = BasisFactor().compute(data, ["A"])

    assert pd.isna(scores.loc[trade_date, "A"])


def test_materialized_daily_missing_final_fundamental_row_is_not_forward_filled():
    dates = pd.date_range("2020-01-01", periods=6, freq="D").date
    symbols = ["A"]
    data = CTADataSet(
        prices=pd.DataFrame(
            {
                "trade_date": dates,
                "symbol": symbols * len(dates),
                "open": [100.0] * len(dates),
                "close": [100.0] * len(dates),
                "close_raw": [100.0] * len(dates),
            }
        ),
        fundamentals=pd.DataFrame(
            {
                "trade_date": dates[:-1],
                "symbol": ["A"] * (len(dates) - 1),
                "basis_rate": [0.01, 0.02, 0.03, 0.04, 0.05],
                "inventory": [10.0, 11.0, 13.0, 16.0, 20.0],
                "profit": [100.0, 102.0, 105.0, 109.0, 114.0],
            }
        ),
        fundamental_metadata={"materialized_daily": True},
    )

    final_date = dates[-1]
    scores = (
        BasisFactor().compute(data, symbols),
        InventoryFactor(lookback_days=1).compute(data, symbols),
        ProfitFactor(lookback_days=2, min_periods=2).compute(data, symbols),
    )

    assert all(pd.isna(score.loc[final_date, "A"]) for score in scores)


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


def _pilot_file_basis_fallback_data(
    *,
    close_raw_mode: str = "finite",
) -> CTADataSet:
    dates = pd.bdate_range("2020-01-01", periods=130).date
    symbols = list(PILOT_FUNDAMENTAL_SYMBOLS)
    price_rows = []
    fundamental_rows = []
    for symbol_index, symbol in enumerate(symbols):
        basis = -0.12 + 0.03 * symbol_index
        inventory_direction = -1.0 if symbol_index < 4 else 1.0
        for date_index, trade_date in enumerate(dates):
            close_raw = 100.0 + 20.0 * symbol_index + 0.1 * date_index
            close = close_raw * 2.0
            price_row = {
                "trade_date": trade_date,
                "symbol": symbol,
                "open": close * 1.001,
                "close": close,
                "volume": 1000.0 + 100.0 * symbol_index + date_index,
            }
            if close_raw_mode == "finite":
                price_row["close_raw"] = close_raw
            elif close_raw_mode == "nonfinite":
                price_row["close_raw"] = (
                    np.nan if date_index % 2 == 0 else np.inf
                )
            elif close_raw_mode != "missing":
                raise ValueError(f"unknown close_raw_mode: {close_raw_mode}")
            price_rows.append(price_row)
            fundamental_rows.append(
                {
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "spot": close_raw * (1.0 + basis),
                    "basis_rate": (
                        np.nan
                        if (date_index + symbol_index) % 2 == 0
                        else np.inf
                    ),
                    "inventory": (
                        300.0
                        + 20.0 * symbol_index
                        + inventory_direction * 0.25 * date_index
                    ),
                    "profit": (
                        50.0
                        + 3.0 * symbol_index
                        + np.sin(date_index / 10.0 + symbol_index)
                    ),
                }
            )
    return CTADataSet(
        prices=pd.DataFrame(price_rows),
        fundamentals=pd.DataFrame(fundamental_rows),
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


def test_file_six_factor_spot_fallback_passes_real_pilot_coverage_and_sleeves():
    data = _pilot_file_basis_fallback_data()
    symbols = list(PILOT_FUNDAMENTAL_SYMBOLS)

    cta_main._validate_six_factor_request(
        source="files",
        factor_set="six_factor",
        data=data,
    )
    expected_basis = (
        data.fundamental_matrix("spot", symbols=symbols)
        / data.price_matrix("close_raw", symbols=symbols)
        - 1.0
    )
    pd.testing.assert_frame_equal(
        BasisFactor().compute(data, symbols),
        expected_basis,
    )

    weights_by_factor, factor_returns, coverage_audit = build_factor_sleeves(
        data,
        factors=default_cta_factors(),
        symbols=symbols,
        enforce_coverage=True,
    )

    basis_coverage = coverage_audit.loc[
        coverage_audit["metric"].eq("basis_rate")
    ]
    assert len(basis_coverage) == len(data.dates)
    assert basis_coverage["status"].eq("pass").all()
    assert basis_coverage["available_products"].eq(len(symbols)).all()
    inventory_sides = coverage_audit.loc[
        coverage_audit["check"].eq("inventory_sides")
    ]
    assert not inventory_sides.empty
    assert inventory_sides["status"].eq("pass").all()
    assert inventory_sides["long_candidates"].ge(2).all()
    assert inventory_sides["short_candidates"].ge(2).all()
    expected_signs = np.sign(expected_basis.iloc[-1])
    actual_signs = np.sign(weights_by_factor["basis"].iloc[-1])
    pd.testing.assert_series_equal(actual_signs, expected_signs)
    assert factor_returns.shape[1] == 6


@pytest.mark.parametrize("close_raw_mode", ["missing", "nonfinite"])
def test_file_six_factor_spot_fallback_requires_finite_close_raw(
    close_raw_mode,
):
    data = _pilot_file_basis_fallback_data(close_raw_mode=close_raw_mode)

    with pytest.raises(SystemExit, match="close_raw"):
        cta_main._validate_six_factor_request(
            source="files",
            factor_set="six_factor",
            data=data,
        )


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


def test_six_factor_set_contains_all_six_factors():
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


def test_write_cta_outputs_includes_fundamental_audit_sheets(tmp_path):
    base = _sample_cta_data()
    quality = pd.DataFrame(
        [
            {
                "product_code": "CU",
                "metric": "basis_rate",
                "status": "ok",
                "lineage": {"series_key": "cu_basis"},
            },
            {
                "product_code": "RB",
                "metric": "inventory",
                "status": "ok",
                "lineage": {"series_key": "rb_inventory"},
            },
        ]
    )
    metadata = {
        "source": "standard",
        "pit_mode": "conservative",
        "build_version": "build-c-1",
        "catalog_version": "v1",
        "source_recorded_cutoff": "2026-07-31T15:00:00+08:00",
        "schema": "commodity_research",
        "materialized_daily": True,
        "lineage": {"basis_rate": ["cu_basis"]},
    }
    data = CTADataSet(
        prices=base.prices,
        fundamentals=base.fundamentals,
        fundamental_quality=quality,
        fundamental_metadata=metadata,
    )

    result = run_medium_equal_weight(
        data,
        symbols=data.symbols,
        enforce_coverage=False,
    )

    pd.testing.assert_frame_equal(result.fundamental_lineage, quality)
    assert result.fundamental_lineage is not quality
    assert result.fundamental_metadata == metadata
    assert result.fundamental_metadata is not metadata

    xlsx, _ = write_cta_outputs(result, tmp_path / "cta_fundamental")

    sheets = pd.ExcelFile(xlsx).sheet_names
    assert "fundamental_coverage" in sheets
    assert "fundamental_lineage" in sheets
    assert "fundamental_build" in sheets
    written_lineage = pd.read_excel(xlsx, sheet_name="fundamental_lineage")
    assert written_lineage["product_code"].tolist() == ["CU", "RB"]
    assert written_lineage.loc[0, "lineage"] == '{"series_key":"cu_basis"}'
    build = pd.read_excel(xlsx, sheet_name="fundamental_build")
    assert list(build.columns) == [
        "source",
        "pit_mode",
        "build_version",
        "catalog_version",
        "source_recorded_cutoff",
        "schema",
        "materialized_daily",
    ]
    assert build.loc[0, "source"] == "standard"
    assert bool(build.loc[0, "materialized_daily"]) is True
    assert "lineage" not in build.columns


def test_price_volume_output_only_writes_fundamental_build_sheet(tmp_path):
    base = _sample_cta_data()
    data = CTADataSet(
        prices=base.prices,
        fundamentals=base.fundamentals,
        fundamental_quality=pd.DataFrame(),
        fundamental_metadata={"source": "none"},
    )
    result = run_medium_equal_weight(
        data,
        symbols=data.symbols,
        factors=cta_factors_for_set("price_volume"),
    )

    xlsx, _ = write_cta_outputs(result, tmp_path / "cta_price_volume")

    sheets = pd.ExcelFile(xlsx).sheet_names
    assert "fundamental_coverage" not in sheets
    assert "fundamental_lineage" not in sheets
    assert "fundamental_build" in sheets
    build = pd.read_excel(xlsx, sheet_name="fundamental_build")
    assert build.loc[0, "source"] == "none"


def test_fundamental_coverage_summary_is_deterministic():
    coverage = pd.DataFrame(
        {
            "available_products": [9, 6, 4, 3, 5, 7, 8],
            "status": ["pass", "fail", "fail", "fail", "fail", "fail", "fail"],
            "reason": [
                "ignored-pass-reason",
                "zeta",
                "beta",
                "",
                "alpha",
                "beta",
                "delta",
            ],
        }
    )

    assert cta_main._fundamental_coverage_summary(coverage) == (
        "rows=7 failed=6 minimum=3 reasons=alpha | beta | delta"
    )
    assert cta_main._fundamental_coverage_summary(pd.DataFrame()) == (
        "rows=0 failed=0 minimum=unknown"
    )
    unavailable = pd.DataFrame(
        {
            "available_products": [None, None],
            "status": ["pass", "pass"],
            "reason": ["", ""],
        }
    )
    assert cta_main._fundamental_coverage_summary(unavailable) == (
        "rows=2 failed=0 minimum=unknown"
    )


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


def test_coverage_gate_honours_the_builds_waived_slices():
    data = _sample_cta_data()
    data.fundamental_metadata["absence_slices"] = [
        {"trade_date": "2020-01-01", "metric": "basis_rate"}
    ]

    with pytest.raises(FundamentalCoverageError) as error:
        build_factor_sleeves(
            data,
            factors=default_cta_factors(),
            symbols=data.symbols,
            enforce_coverage=True,
        )

    # The waived slice is gone; everything else still fails exactly as before.
    assert "2020-01-01 basis_rate" not in str(error.value)


def test_a_waived_slice_is_marked_waived_in_the_coverage_audit():
    data = _sample_cta_data()
    data.fundamental_metadata["absence_slices"] = [
        {"trade_date": "2020-01-01", "metric": "basis_rate"}
    ]

    _, _, coverage_audit = build_factor_sleeves(
        data,
        factors=default_cta_factors(),
        symbols=data.symbols,
        enforce_coverage=False,
    )

    waived = coverage_audit[coverage_audit["status"] == "waived"]
    assert len(waived) == 1
    assert waived.iloc[0]["metric"] == "basis_rate"


def test_factor_signal_is_the_pre_normalisation_position_signal():
    from cta_gtja.factors import CTAFactor
    from cta_gtja.portfolio import factor_signal, factor_weights, normalize_gross

    scores = pd.DataFrame(
        [[3.0, 1.0, 2.0, np.nan]],
        index=pd.to_datetime(["2020-01-01"]),
        columns=list("ABCD"),
    )

    cross = CTAFactor(name="x", construction="cross_section")
    signal = factor_signal(cross, scores)
    # Demeaned over the finite values only, so both sides always exist.
    assert signal.iloc[0].tolist()[:3] == [1.0, -1.0, 0.0]
    assert np.isnan(signal.iloc[0]["D"])

    series = CTAFactor(name="y", construction="time_series")
    assert factor_signal(series, scores).iloc[0].tolist()[:3] == [1.0, 1.0, 1.0]

    for factor in (cross, series):
        pd.testing.assert_frame_equal(
            factor_weights(factor, scores),
            normalize_gross(factor_signal(factor, scores)),
        )


def test_inventory_sides_are_measured_on_the_signal_that_sets_positions():
    """A whole-complex destock is not a one-sided book.

    InventoryFactor is a cross-section factor, so positions come from the
    demeaned score. Testing the raw score's sign fails every day the whole
    complex moves the same way, while the actual basket is two-sided.
    """
    data = _sample_cta_data()
    inventory = InventoryFactor()
    raw = inventory.compute(data, data.symbols).reindex(
        index=data.dates, columns=data.symbols
    )
    shifted = raw + 1000.0  # every product now scores positive; ranks unchanged

    class _ShiftedInventory(InventoryFactor):
        def compute(self, data, symbols):
            return shifted.reindex(columns=symbols)

    _, _, audit = build_factor_sleeves(
        data,
        factors=[_ShiftedInventory()],
        symbols=data.symbols,
        enforce_coverage=False,
    )
    sides = audit[audit["check"] == "inventory_sides"]
    assert not sides.empty
    assert not sides["status"].eq("fail").any()
