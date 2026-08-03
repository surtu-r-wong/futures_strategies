import numpy as np
import pandas as pd
import pytest

from cta_gtja.coverage import (
    COVERAGE_COLUMNS,
    FundamentalCoverageError,
    evaluate_daily_fundamental_coverage,
    evaluate_inventory_sides,
)


SYMBOLS = tuple(f"product_{number}" for number in range(1, 10))


def _daily_matrix(dates, finite_counts):
    values = np.full((len(dates), len(SYMBOLS)), np.nan)
    for row_number, finite_count in enumerate(finite_counts):
        values[row_number, :finite_count] = np.arange(
            1, finite_count + 1, dtype=float
        )
    return pd.DataFrame(
        values,
        index=pd.to_datetime(dates),
        columns=SYMBOLS,
    )


def test_daily_coverage_accepts_six_finite_products_per_metric_and_date():
    dates = ["2020-01-01", "2020-01-02"]
    matrices = {
        "basis_rate": _daily_matrix(dates, [6, 6]),
        "carry": _daily_matrix(dates, [6, 6]),
    }

    audit = evaluate_daily_fundamental_coverage(
        matrices,
        symbols=SYMBOLS,
        required_products=6,
        enforce=True,
    )

    assert tuple(audit.columns) == tuple(COVERAGE_COLUMNS)


def test_daily_coverage_raises_for_five_products_with_exact_reason():
    matrices = {"basis_rate": _daily_matrix(["2020-01-02"], [5])}

    with pytest.raises(FundamentalCoverageError) as error:
        evaluate_daily_fundamental_coverage(
            matrices,
            symbols=SYMBOLS,
            required_products=6,
            enforce=True,
        )

    assert str(error.value) == "2020-01-02 basis_rate coverage=5 required=6"


def test_daily_coverage_counts_only_finite_values():
    values = [[1.0, 2.0, 3.0, 4.0, 5.0, np.inf, -np.inf, np.nan, np.nan]]
    matrices = {
        "basis_rate": pd.DataFrame(
            values,
            index=pd.to_datetime(["2020-01-02"]),
            columns=SYMBOLS,
        )
    }

    with pytest.raises(FundamentalCoverageError) as error:
        evaluate_daily_fundamental_coverage(
            matrices,
            symbols=SYMBOLS,
            required_products=6,
            enforce=True,
        )

    assert str(error.value) == "2020-01-02 basis_rate coverage=5 required=6"


def test_daily_coverage_enforce_false_is_deterministic_and_auditable():
    matrices = {"basis_rate": _daily_matrix(["2020-01-02"], [5])}

    first = evaluate_daily_fundamental_coverage(
        matrices,
        symbols=SYMBOLS,
        required_products=6,
        enforce=False,
    )
    second = evaluate_daily_fundamental_coverage(
        matrices,
        symbols=SYMBOLS,
        required_products=6,
        enforce=False,
    )

    pd.testing.assert_frame_equal(first, second)
    assert tuple(first.columns) == tuple(COVERAGE_COLUMNS)
    assert first["status"].tolist() == ["fail"]
    assert first["reason"].tolist() == [
        "2020-01-02 basis_rate coverage=5 required=6"
    ]


def test_daily_coverage_sorts_multiple_failure_reasons_deterministically():
    matrices = {
        "zeta": _daily_matrix(["2020-01-02"], [5]),
        "basis_rate": _daily_matrix(["2020-01-02"], [4]),
    }

    with pytest.raises(FundamentalCoverageError) as error:
        evaluate_daily_fundamental_coverage(
            matrices,
            symbols=SYMBOLS,
            required_products=6,
            enforce=True,
        )

    message = str(error.value)
    reasons = [
        "2020-01-02 basis_rate coverage=4 required=6",
        "2020-01-02 zeta coverage=5 required=6",
    ]
    assert all(reason in message for reason in reasons)
    assert message.index(reasons[0]) < message.index(reasons[1])


def _inventory_scores():
    return pd.DataFrame(
        [
            [1.0, -1.0, np.nan, np.nan],
            [1.0, 2.0, -1.0, -2.0],
            [1.0, 2.0, -1.0, 0.0],
        ],
        index=pd.to_datetime(
            ["2020-01-01", "2020-01-02", "2020-01-03"]
        ),
        columns=["product_a", "product_b", "product_c", "product_d"],
    )


def test_inventory_ignores_warmup_and_requires_both_sides_each_day():
    with pytest.raises(FundamentalCoverageError) as error:
        evaluate_inventory_sides(
            _inventory_scores(),
            required_each=2,
            enforce=True,
        )

    message = str(error.value)
    assert "2020-01-01" not in message
    assert "2020-01-03 inventory long=2 short=1 required_each=2" in message


def test_inventory_enforce_false_is_deterministic_and_auditable():
    scores = _inventory_scores()
    first = evaluate_inventory_sides(scores, required_each=2, enforce=False)
    second = evaluate_inventory_sides(scores, required_each=2, enforce=False)

    pd.testing.assert_frame_equal(first, second)
    assert tuple(first.columns) == tuple(COVERAGE_COLUMNS)
    failures = first.loc[first["status"].eq("fail")]
    assert failures["reason"].tolist() == [
        "2020-01-03 inventory long=2 short=1 required_each=2"
    ]


def test_inventory_audit_columns_match_coverage_columns_in_stable_order():
    audit = evaluate_inventory_sides(
        _inventory_scores(),
        required_each=2,
        enforce=False,
    )

    assert tuple(audit.columns) == tuple(COVERAGE_COLUMNS)
