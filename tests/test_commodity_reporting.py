from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from common.metrics import summarize
from common.commodity.reporting import fidelity_frame, split_metrics


METRIC_COLUMNS = [
    "period",
    "start",
    "end",
    "annual_return",
    "annual_volatility",
    "sharpe",
    "max_drawdown",
    "calmar",
]


def test_metrics_split_without_overlapping_the_cutoff() -> None:
    daily_returns = pd.DataFrame(
        {
            "trade_date": [date(2021, 9, 29), date(2021, 9, 30), date(2021, 10, 1)],
            "net_return": [0.01, -0.005, 0.002],
        }
    )

    rows = split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))

    assert list(rows["period"]) == ["full", "in_sample", "out_of_sample"]
    in_sample = rows.loc[rows["period"] == "in_sample"].iloc[0]
    out_of_sample = rows.loc[rows["period"] == "out_of_sample"].iloc[0]
    assert in_sample["end"] == date(2021, 9, 30)
    assert out_of_sample["start"] == date(2021, 10, 1)
    assert in_sample["end"] < out_of_sample["start"]


@pytest.mark.parametrize(
    ("cutoff", "empty_period"),
    [
        (date(2021, 9, 28), "in_sample"),
        (date(2021, 10, 2), "out_of_sample"),
    ],
)
def test_metrics_split_keeps_an_explicit_empty_row_when_cutoff_is_outside_data(
    cutoff: date,
    empty_period: str,
) -> None:
    daily_returns = pd.DataFrame(
        {
            "trade_date": [date(2021, 9, 29), date(2021, 10, 1)],
            "net_return": [0.01, -0.005],
        }
    )

    rows = split_metrics(daily_returns, in_sample_end=cutoff)

    assert list(rows.columns) == METRIC_COLUMNS
    assert list(rows["period"]) == ["full", "in_sample", "out_of_sample"]
    empty = rows.loc[rows["period"] == empty_period].iloc[0]
    assert empty["start"] is None
    assert empty["end"] is None
    assert empty[METRIC_COLUMNS[3:]].isna().all()


def test_metrics_split_maps_daily_summaries_and_positive_drawdown_calmar() -> None:
    daily_returns = pd.DataFrame(
        {
            "trade_date": [date(2021, 9, 29), date(2021, 9, 30), date(2021, 10, 1)],
            "net_return": [0.01, -0.02, 0.005],
        }
    )
    original = daily_returns.copy(deep=True)

    rows = split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))

    full = rows.loc[rows["period"] == "full"].iloc[0]
    expected = summarize(
        daily_returns.set_index("trade_date")["net_return"], periods_per_year=252
    )
    assert full["annual_return"] == pytest.approx(expected["ann_return"])
    assert full["annual_volatility"] == pytest.approx(expected["ann_vol"])
    assert full["sharpe"] == pytest.approx(expected["sharpe"])
    assert full["max_drawdown"] == pytest.approx(expected["max_drawdown"])
    assert full["calmar"] == pytest.approx(
        expected["ann_return"] / expected["max_drawdown"]
    )
    pd.testing.assert_frame_equal(daily_returns, original)


def test_metrics_split_handles_a_single_row_and_zero_drawdown() -> None:
    daily_returns = pd.DataFrame(
        {"trade_date": [date(2021, 9, 30)], "net_return": [0.01]}
    )

    rows = split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))

    assert rows.loc[0, "start"] == date(2021, 9, 30)
    assert rows.loc[0, "end"] == date(2021, 9, 30)
    assert rows.loc[1, "start"] == date(2021, 9, 30)
    assert rows.loc[1, "end"] == date(2021, 9, 30)
    assert pd.isna(rows.loc[0, "calmar"])
    assert rows.loc[2, "start"] is None
    assert rows.loc[2, "end"] is None


def test_metrics_split_returns_three_empty_rows_for_an_empty_input() -> None:
    rows = split_metrics(
        pd.DataFrame(columns=["trade_date", "net_return"]),
        in_sample_end=date(2021, 9, 30),
    )

    assert list(rows.columns) == METRIC_COLUMNS
    assert list(rows["period"]) == ["full", "in_sample", "out_of_sample"]
    assert rows[["start", "end"]].isna().all().all()
    assert rows[METRIC_COLUMNS[3:]].isna().all().all()


@pytest.mark.parametrize(
    ("daily_returns", "message"),
    [
        ([], "reporting_daily_frame"),
        (
            pd.DataFrame(columns=["trade_date"]),
            "reporting_daily_columns",
        ),
        (
            pd.DataFrame(columns=["trade_date", "net_return", "gross_return"]),
            "reporting_daily_columns",
        ),
    ],
)
def test_metrics_split_requires_an_unambiguous_exact_daily_schema(
    daily_returns: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        split_metrics(  # type: ignore[arg-type]
            daily_returns,
            in_sample_end=date(2021, 9, 30),
        )


def test_metrics_split_rejects_duplicate_dates() -> None:
    daily_returns = pd.DataFrame(
        {
            "trade_date": [date(2021, 9, 30), pd.Timestamp("2021-09-30")],
            "net_return": [0.01, 0.02],
        }
    )

    with pytest.raises(ValueError, match="reporting_daily_duplicate_date"):
        split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))


def test_metrics_split_rejects_unsorted_dates_instead_of_silently_sorting() -> None:
    daily_returns = pd.DataFrame(
        {
            "trade_date": [date(2021, 10, 1), date(2021, 9, 30)],
            "net_return": [0.01, 0.02],
        }
    )

    with pytest.raises(ValueError, match="reporting_daily_order"):
        split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))


@pytest.mark.parametrize(
    "trade_date",
    [
        pd.NaT,
        pd.Timestamp("2021-09-30 00:00:00.000000001"),
        pd.Timestamp("2021-09-30", tz="Asia/Shanghai"),
        "2021-09-30",
    ],
)
def test_metrics_split_rejects_invalid_trade_dates(trade_date: object) -> None:
    daily_returns = pd.DataFrame({"trade_date": [trade_date], "net_return": [0.01]})

    with pytest.raises(ValueError, match="reporting_trade_date"):
        split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))


@pytest.mark.parametrize("trade_date", [date(2021, 9, 30), pd.Timestamp("2021-09-30")])
def test_metrics_split_accepts_dates_and_naive_midnight_timestamps(
    trade_date: object,
) -> None:
    rows = split_metrics(
        pd.DataFrame({"trade_date": [trade_date], "net_return": [0.01]}),
        in_sample_end=date(2021, 9, 30),
    )

    assert rows.loc[0, "start"] == date(2021, 9, 30)
    assert type(rows.loc[0, "start"]) is date


@pytest.mark.parametrize("net_return", [np.nan, np.inf, -np.inf, "0.01", True])
def test_metrics_split_rejects_nonfinite_or_nonnumeric_returns(
    net_return: object,
) -> None:
    daily_returns = pd.DataFrame(
        {"trade_date": [date(2021, 9, 30)], "net_return": [net_return]}
    )

    with pytest.raises(ValueError, match="reporting_net_return.*finite numeric"):
        split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))


@pytest.mark.parametrize("net_return", [-1.0, -1.01])
def test_metrics_split_rejects_returns_that_deplete_equity(
    net_return: float,
) -> None:
    daily_returns = pd.DataFrame(
        {"trade_date": [date(2021, 9, 30)], "net_return": [net_return]}
    )

    with pytest.raises(ValueError, match="reporting_net_return.*greater than -1"):
        split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))


@pytest.mark.parametrize(
    "cutoff",
    [
        datetime(2021, 9, 30),
        pd.Timestamp("2021-09-30"),
        "2021-09-30",
    ],
)
def test_metrics_split_requires_a_python_date_cutoff(cutoff: object) -> None:
    daily_returns = pd.DataFrame(
        {"trade_date": [date(2021, 9, 30)], "net_return": [0.01]}
    )

    with pytest.raises(ValueError, match="reporting_in_sample_end"):
        split_metrics(
            daily_returns,
            in_sample_end=cutoff,  # type: ignore[arg-type]
        )


def test_fidelity_rejects_duplicate_normalized_rule_ids() -> None:
    first = {
        "rule_id": "F1",
        "paper_text": "主力合约",
        "implementation": "lag one session",
        "basis": "causality",
        "status": "assumption",
        "variant": "none",
        "impact": "reported",
    }
    second = {**first, "rule_id": " F1 "}

    with pytest.raises(ValueError, match="fidelity_duplicate_rule"):
        fidelity_frame([first, second])


def _fidelity_row(rule_id: str = "F1", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "rule_id": rule_id,
        "paper_text": "主力合约",
        "implementation": "lag one session",
        "basis": "causality",
        "status": "assumption",
        "variant": "none",
        "impact": "reported",
    }
    row.update(overrides)
    return row


def test_fidelity_returns_exact_columns_sorted_rows_and_a_fresh_index() -> None:
    rows = [
        _fidelity_row(" F2 ", status="sensitivity_only"),
        _fidelity_row("F1"),
    ]
    original = [row.copy() for row in rows]

    result = fidelity_frame(rows)

    assert list(result.columns) == [
        "rule_id",
        "paper_text",
        "implementation",
        "basis",
        "status",
        "variant",
        "impact",
    ]
    assert list(result["rule_id"]) == ["F1", "F2"]
    assert list(result.index) == [0, 1]
    assert rows == original


def test_fidelity_empty_input_keeps_the_exact_schema() -> None:
    result = fidelity_frame([])

    assert list(result.columns) == [
        "rule_id",
        "paper_text",
        "implementation",
        "basis",
        "status",
        "variant",
        "impact",
    ]
    assert result.empty


@pytest.mark.parametrize("bad_rows", [None, "F1", pd.DataFrame()])
def test_fidelity_requires_an_iterable_of_mappings(bad_rows: object) -> None:
    with pytest.raises(ValueError, match="fidelity_rows"):
        fidelity_frame(bad_rows)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in _fidelity_row().items() if key != "impact"},
        {**_fidelity_row(), "notes": "hidden"},
    ],
)
def test_fidelity_rejects_missing_and_extra_columns(row: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="fidelity_columns"):
        fidelity_frame([row])


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("rule_id", ""),
        ("rule_id", "   "),
        ("rule_id", None),
        ("paper_text", " "),
        ("implementation", None),
        ("basis", 1),
        ("status", "\t"),
        ("variant", ""),
        ("impact", None),
    ],
)
def test_fidelity_requires_nonblank_string_values(
    column: str,
    value: object,
) -> None:
    row = _fidelity_row()
    row[column] = value

    with pytest.raises(ValueError, match=f"fidelity_{column}"):
        fidelity_frame([row])


@pytest.mark.parametrize("duplicate", ["f1", "Ｆ１"])
def test_fidelity_rejects_casefolded_and_unicode_normalized_duplicate_ids(
    duplicate: str,
) -> None:
    with pytest.raises(ValueError, match="fidelity_duplicate_rule"):
        fidelity_frame([_fidelity_row("F1"), _fidelity_row(duplicate)])


def test_fidelity_accepts_nonblank_statuses_without_inventing_an_enum() -> None:
    result = fidelity_frame(
        [
            _fidelity_row("F1", status="assumption"),
            _fidelity_row("F2", status="sensitivity_only"),
            _fidelity_row("F3", status="known_gap"),
        ]
    )

    assert list(result["status"]) == ["assumption", "sensitivity_only", "known_gap"]
