"""Causal trailing performance scores for commodity shadow strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import math
from numbers import Real

import numpy as np
import pandas as pd

from common.metrics import cumulative_equity, summarize


_DAILY_COLUMNS = ("product", "trade_date", "net_return")
_TRADE_COLUMNS = ("product", "exit_date")


@dataclass(frozen=True, slots=True)
class ProductScore:
    """One product's fixed-length performance record at a month boundary."""

    product: str
    first_observation: date
    last_observation: date
    observations: int
    trade_count: int
    cumulative_return: float
    annual_return: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float
    calmar: float


def _require_frame(
    value: object,
    *,
    label: str,
    required: tuple[str, ...],
) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise ValueError(f"selection_{label}_frame: expected a DataFrame")
    missing = [column for column in required if column not in value.columns]
    if missing:
        raise ValueError(f"selection_{label}_columns: missing={missing!r}")
    return value


def _date_value(value: object, *, label: str) -> date:
    if pd.isna(value):
        raise ValueError(f"selection_{label}: date value is required")
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError(f"selection_{label}: date must be timezone-naive")
        if value.time() != time.min:
            raise ValueError(f"selection_{label}: date must be at midnight")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise ValueError(f"selection_{label}: date value is required")
        if timestamp.time() != time.min:
            raise ValueError(f"selection_{label}: date must be at midnight")
        return timestamp.date()
    raise ValueError(f"selection_{label}: expected date values")


def _dates(values: pd.Series, *, label: str) -> pd.Series:
    return pd.Series(
        (_date_value(value, label=label) for value in values),
        index=values.index,
        dtype="object",
    )


def _products(values: pd.Series, *, label: str) -> pd.Series:
    invalid = [
        value for value in values if not isinstance(value, str) or not value.strip()
    ]
    if invalid:
        raise ValueError(
            f"selection_{label}: product must be a nonempty string; got {invalid[0]!r}"
        )
    return values.astype("object")


def _returns(values: pd.Series) -> pd.Series:
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("selection_daily: net_return must be finite numeric data")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("selection_daily: net_return must be finite numeric data")
        if numeric <= -1.0:
            raise ValueError("selection_daily: net_return must be greater than -1")
        converted.append(numeric)
    return pd.Series(converted, index=values.index, dtype="float64")


def _trade_ids(values: pd.Series) -> pd.Series:
    invalid = [
        value for value in values if not isinstance(value, str) or not value.strip()
    ]
    if invalid:
        raise ValueError("selection_trades: trade_id must be a nonempty string")
    return values.astype("object")


def trailing_scores(
    *,
    month_start: date,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    observations: int = 252,
) -> dict[str, ProductScore]:
    """Score complete per-product daily windows strictly before a new month.

    A product with fewer than ``observations`` causal daily rows is omitted.
    Rows dated on or after ``month_start`` are outside both validation and score
    calculation, so future shadow output cannot affect an earlier selection.
    """
    if (
        not isinstance(month_start, date)
        or isinstance(month_start, datetime)
        or month_start.day != 1
    ):
        raise ValueError("month_start must be a date on the first day of a month")
    if (
        isinstance(observations, bool)
        or type(observations) is not int
        or observations <= 0
    ):
        raise ValueError("observations must be a positive integer")

    daily_frame = _require_frame(daily, label="daily", required=_DAILY_COLUMNS)
    trades_frame = _require_frame(trades, label="trades", required=_TRADE_COLUMNS)

    daily_dates = _dates(daily_frame["trade_date"], label="daily_trade_date")
    historical = daily_frame.loc[daily_dates < month_start, list(_DAILY_COLUMNS)].copy()
    historical["trade_date"] = daily_dates.loc[historical.index]
    historical["product"] = _products(historical["product"], label="daily")
    historical["net_return"] = _returns(historical["net_return"])
    if historical.duplicated(["product", "trade_date"]).any():
        keys = (
            historical.loc[
                historical.duplicated(["product", "trade_date"], keep=False),
                ["product", "trade_date"],
            ]
            .drop_duplicates()
            .to_dict("records")
        )
        raise ValueError(f"selection_daily_duplicate_key: {keys!r}")

    exit_dates = _dates(trades_frame["exit_date"], label="trades_exit_date")
    historical_trades = trades_frame.loc[exit_dates < month_start].copy()
    historical_trades["exit_date"] = exit_dates.loc[historical_trades.index]
    historical_trades["product"] = _products(
        historical_trades["product"], label="trades"
    )

    scores: dict[str, ProductScore] = {}
    for product in sorted(historical["product"].unique()):
        product_daily = historical.loc[historical["product"] == product].sort_values(
            "trade_date", kind="stable"
        )
        if len(product_daily) < observations:
            continue
        window = product_daily.tail(observations)
        first_observation = window["trade_date"].iloc[0]
        last_observation = window["trade_date"].iloc[-1]

        window_trades = historical_trades.loc[
            (historical_trades["product"] == product)
            & (historical_trades["exit_date"] >= first_observation)
            & (historical_trades["exit_date"] <= last_observation)
        ].copy()
        if "trade_id" in window_trades.columns:
            window_trades["trade_id"] = _trade_ids(window_trades["trade_id"])
            if window_trades.duplicated(["product", "trade_id"]).any():
                raise ValueError(f"selection_trades_duplicate_id: product={product!r}")

        period_returns = pd.Series(
            window["net_return"].to_numpy(dtype="float64"),
            index=pd.Index(window["trade_date"], name="trade_date"),
            dtype="float64",
        )
        metrics = summarize(period_returns, periods_per_year=252)
        equity = cumulative_equity(period_returns)
        annual_return = float(metrics["ann_return"])
        max_drawdown = float(metrics["max_drawdown"])
        calmar = annual_return / max_drawdown if max_drawdown > 0.0 else float("nan")
        scores[product] = ProductScore(
            product=product,
            first_observation=first_observation,
            last_observation=last_observation,
            observations=len(window),
            trade_count=len(window_trades),
            cumulative_return=float(equity.iloc[-1] - 1.0),
            annual_return=annual_return,
            annual_volatility=float(metrics["ann_vol"]),
            sharpe=float(metrics["sharpe"]),
            max_drawdown=max_drawdown,
            calmar=float(calmar),
        )

    return scores


__all__ = ["ProductScore", "trailing_scores"]
