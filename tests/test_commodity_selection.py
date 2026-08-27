from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import date

import numpy as np
import pandas as pd
import pytest

from common.metrics import cumulative_equity, summarize
from common.commodity.selection import ProductScore, trailing_scores


@pytest.fixture
def shadow_daily() -> pd.DataFrame:
    dates = pd.bdate_range(end="2024-03-01", periods=254)
    returns = np.resize(np.array([0.01, -0.004, 0.006, 0.002]), len(dates))
    return pd.DataFrame(
        {
            "product": "RB",
            "trade_date": dates,
            "net_return": returns,
        }
    )


@pytest.fixture
def shadow_trades(shadow_daily: pd.DataFrame) -> pd.DataFrame:
    dates = shadow_daily["trade_date"].dt.date.tolist()
    return pd.DataFrame(
        {
            "trade_id": ["old", "first", "last", "boundary"],
            "product": ["RB"] * 4,
            "exit_date": [dates[0], dates[1], dates[-2], dates[-1]],
        }
    )


def test_product_score_is_a_frozen_slotted_complete_metric_record() -> None:
    assert [field.name for field in fields(ProductScore)] == [
        "product",
        "first_observation",
        "last_observation",
        "observations",
        "trade_count",
        "cumulative_return",
        "annual_return",
        "annual_volatility",
        "sharpe",
        "max_drawdown",
        "calmar",
    ]
    score = ProductScore(
        product="RB",
        first_observation=date(2023, 1, 1),
        last_observation=date(2023, 12, 31),
        observations=252,
        trade_count=5,
        cumulative_return=0.1,
        annual_return=0.1,
        annual_volatility=0.2,
        sharpe=0.5,
        max_drawdown=0.05,
        calmar=2.0,
    )

    assert not hasattr(score, "__dict__")
    with pytest.raises(FrozenInstanceError):
        score.trade_count = 6  # type: ignore[misc]


def test_scores_stop_before_the_current_month_and_use_exact_daily_window(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
) -> None:
    scores = trailing_scores(
        month_start=date(2024, 3, 1),
        daily=shadow_daily,
        trades=shadow_trades,
        observations=252,
    )

    score = scores["RB"]
    expected = shadow_daily.iloc[1:-1].set_index("trade_date")["net_return"]
    metrics = summarize(expected, periods_per_year=252)
    assert score.first_observation == expected.index[0].date()
    assert score.last_observation == date(2024, 2, 29)
    assert score.last_observation < date(2024, 3, 1)
    assert score.observations == 252
    assert score.trade_count == 2
    assert score.cumulative_return == pytest.approx(
        cumulative_equity(expected).iloc[-1] - 1.0
    )
    assert score.annual_return == pytest.approx(metrics["ann_return"])
    assert score.annual_volatility == pytest.approx(metrics["ann_vol"])
    assert score.sharpe == pytest.approx(metrics["sharpe"])
    assert score.max_drawdown == pytest.approx(metrics["max_drawdown"])
    assert score.calmar == pytest.approx(
        metrics["ann_return"] / metrics["max_drawdown"]
    )


def test_scores_ignore_current_month_rows_even_when_they_are_nonfinite(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
) -> None:
    baseline = trailing_scores(
        month_start=date(2024, 3, 1),
        daily=shadow_daily,
        trades=shadow_trades,
        observations=252,
    )
    changed = shadow_daily.copy()
    changed.loc[changed["trade_date"] >= "2024-03-01", "net_return"] = np.nan

    assert (
        trailing_scores(
            month_start=date(2024, 3, 1),
            daily=changed,
            trades=shadow_trades,
            observations=252,
        )
        == baseline
    )


def test_scores_omit_products_with_insufficient_history_and_empty_inputs() -> None:
    daily = pd.DataFrame(
        {
            "product": ["RB", "RB"],
            "trade_date": [date(2024, 1, 2), date(2024, 1, 3)],
            "net_return": [0.01, 0.02],
        }
    )
    trades = pd.DataFrame(columns=["product", "exit_date"])

    assert (
        trailing_scores(
            month_start=date(2024, 2, 1), daily=daily, trades=trades, observations=3
        )
        == {}
    )
    assert (
        trailing_scores(
            month_start=date(2024, 2, 1),
            daily=daily.iloc[0:0],
            trades=trades,
            observations=3,
        )
        == {}
    )


def test_scores_reject_duplicate_daily_keys_inside_the_history_window(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
) -> None:
    duplicated = pd.concat([shadow_daily, shadow_daily.iloc[[5]]], ignore_index=True)

    with pytest.raises(ValueError, match="selection_daily_duplicate_key"):
        trailing_scores(
            month_start=date(2024, 3, 1),
            daily=duplicated,
            trades=shadow_trades,
            observations=252,
        )


def test_scores_reject_nonfinite_returns_inside_the_history_window(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
) -> None:
    invalid = shadow_daily.copy()
    invalid.loc[5, "net_return"] = np.inf

    with pytest.raises(ValueError, match="selection_daily.*net_return.*finite"):
        trailing_scores(
            month_start=date(2024, 3, 1),
            daily=invalid,
            trades=shadow_trades,
            observations=252,
        )


@pytest.mark.parametrize("net_return", [-1.0, -1.01])
def test_scores_reject_returns_that_deplete_shadow_equity(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
    net_return: float,
) -> None:
    invalid = shadow_daily.copy()
    invalid.loc[5, "net_return"] = net_return

    with pytest.raises(ValueError, match="selection_daily.*greater than -1"):
        trailing_scores(
            month_start=date(2024, 3, 1),
            daily=invalid,
            trades=shadow_trades,
            observations=252,
        )


def test_scores_reject_duplicate_trade_identifiers_in_the_selected_window(
    shadow_daily: pd.DataFrame,
    shadow_trades: pd.DataFrame,
) -> None:
    duplicated = pd.concat([shadow_trades, shadow_trades.iloc[[1]]], ignore_index=True)

    with pytest.raises(ValueError, match="selection_trades_duplicate_id"):
        trailing_scores(
            month_start=date(2024, 3, 1),
            daily=shadow_daily,
            trades=duplicated,
            observations=252,
        )


@pytest.mark.parametrize(
    ("frame_name", "daily", "trades", "message"),
    [
        (
            "daily",
            pd.DataFrame(columns=["product", "trade_date"]),
            pd.DataFrame(columns=["product", "exit_date"]),
            "selection_daily_columns.*net_return",
        ),
        (
            "trades",
            pd.DataFrame(columns=["product", "trade_date", "net_return"]),
            pd.DataFrame(columns=["product"]),
            "selection_trades_columns.*exit_date",
        ),
    ],
)
def test_scores_require_the_documented_schemas(
    frame_name: str,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    message: str,
) -> None:
    del frame_name
    with pytest.raises(ValueError, match=message):
        trailing_scores(
            month_start=date(2024, 2, 1),
            daily=daily,
            trades=trades,
            observations=2,
        )


def test_scores_require_dataframes() -> None:
    with pytest.raises(ValueError, match="selection_daily.*DataFrame"):
        trailing_scores(
            month_start=date(2024, 2, 1),
            daily=[],  # type: ignore[arg-type]
            trades=pd.DataFrame(columns=["product", "exit_date"]),
            observations=2,
        )


@pytest.mark.parametrize("observations", [0, -1, 1.5, True])
def test_scores_require_a_positive_integer_window(observations: object) -> None:
    with pytest.raises(ValueError, match="observations.*positive integer"):
        trailing_scores(
            month_start=date(2024, 2, 1),
            daily=pd.DataFrame(columns=["product", "trade_date", "net_return"]),
            trades=pd.DataFrame(columns=["product", "exit_date"]),
            observations=observations,  # type: ignore[arg-type]
        )


def test_scores_require_month_start_to_be_the_first_day_of_a_month() -> None:
    with pytest.raises(ValueError, match="month_start.*first day"):
        trailing_scores(
            month_start=date(2024, 2, 2),
            daily=pd.DataFrame(columns=["product", "trade_date", "net_return"]),
            trades=pd.DataFrame(columns=["product", "exit_date"]),
            observations=2,
        )


def test_calmar_is_nan_when_drawdown_is_not_positive() -> None:
    daily = pd.DataFrame(
        {
            "product": "RB",
            "trade_date": pd.bdate_range("2024-01-02", periods=3),
            "net_return": [0.01, 0.01, 0.01],
        }
    )

    score = trailing_scores(
        month_start=date(2024, 2, 1),
        daily=daily,
        trades=pd.DataFrame(columns=["product", "exit_date"]),
        observations=3,
    )["RB"]

    assert score.max_drawdown == 0.0
    assert np.isnan(score.calmar)
