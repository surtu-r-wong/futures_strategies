"""Shared reporting tables for commodity-futures strategy replications."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
import json
import math
from numbers import Real
import unicodedata

import numpy as np
import pandas as pd

from common.metrics import summarize


METRIC_COLUMNS = (
    "period",
    "start",
    "end",
    "annual_return",
    "annual_volatility",
    "sharpe",
    "max_drawdown",
    "calmar",
)
FIDELITY_COLUMNS = (
    "rule_id",
    "paper_text",
    "implementation",
    "basis",
    "status",
    "variant",
    "impact",
)


def _daily_frame(value: object) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise ValueError("reporting_daily_frame: expected a DataFrame")
    columns = list(value.columns)
    if len(columns) != 2 or set(columns) != {"trade_date", "net_return"}:
        raise ValueError(
            "reporting_daily_columns: expected exactly ['trade_date', 'net_return']"
        )
    return value.loc[:, ["trade_date", "net_return"]].reset_index(drop=True)


def _date_value(value: object) -> date:
    if pd.isna(value):
        raise ValueError("reporting_trade_date: date value is required")
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("reporting_trade_date: date must be timezone-naive")
        timestamp = pd.Timestamp(value)
        if timestamp != timestamp.normalize():
            raise ValueError("reporting_trade_date: date must be at midnight")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise ValueError("reporting_trade_date: date value is required")
        if timestamp != timestamp.normalize():
            raise ValueError("reporting_trade_date: date must be at midnight")
        return timestamp.date()
    raise ValueError("reporting_trade_date: expected date values")


def _return_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("reporting_net_return: expected finite numeric data")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("reporting_net_return: expected finite numeric data")
    if numeric <= -1.0:
        raise ValueError("reporting_net_return: net_return must be greater than -1")
    return numeric


def _normalized_daily(value: object) -> pd.DataFrame:
    frame = _daily_frame(value)
    normalized = pd.DataFrame(
        {
            "trade_date": [_date_value(value) for value in frame["trade_date"]],
            "net_return": [_return_value(value) for value in frame["net_return"]],
        }
    )
    if normalized["trade_date"].duplicated().any():
        raise ValueError("reporting_daily_duplicate_date")
    if not normalized["trade_date"].is_monotonic_increasing:
        raise ValueError("reporting_daily_order: trade_date must be sorted ascending")
    return normalized


def _metric_row(period: str, frame: pd.DataFrame) -> dict[str, object]:
    if frame.empty:
        return {
            "period": period,
            "start": None,
            "end": None,
            "annual_return": float("nan"),
            "annual_volatility": float("nan"),
            "sharpe": float("nan"),
            "max_drawdown": float("nan"),
            "calmar": float("nan"),
        }

    returns = pd.Series(
        frame["net_return"].to_numpy(),
        index=pd.Index(frame["trade_date"], name="trade_date"),
    )
    metrics = summarize(returns, periods_per_year=252)
    drawdown = float(metrics["max_drawdown"])
    annual_return = float(metrics["ann_return"])
    return {
        "period": period,
        "start": frame["trade_date"].iloc[0],
        "end": frame["trade_date"].iloc[-1],
        "annual_return": annual_return,
        "annual_volatility": float(metrics["ann_vol"]),
        "sharpe": float(metrics["sharpe"]),
        "max_drawdown": drawdown,
        "calmar": annual_return / drawdown if drawdown > 0.0 else float("nan"),
    }


def split_metrics(
    daily_returns: pd.DataFrame,
    *,
    in_sample_end: date,
) -> pd.DataFrame:
    """Return full, inclusive in-sample, and strictly later OOS metrics."""
    if type(in_sample_end) is not date:
        raise ValueError("reporting_in_sample_end: expected a Python date")
    normalized = _normalized_daily(daily_returns)
    in_sample = normalized.loc[normalized["trade_date"] <= in_sample_end]
    out_of_sample = normalized.loc[normalized["trade_date"] > in_sample_end]
    rows = [
        _metric_row("full", normalized),
        _metric_row("in_sample", in_sample),
        _metric_row("out_of_sample", out_of_sample),
    ]
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)


def fidelity_frame(rows: Iterable[Mapping[str, object]]) -> pd.DataFrame:
    """Validate and return a deterministic, normalized fidelity ledger.

    Fidelity text remains data here. Report exporters must prevent spreadsheet
    formula interpretation and XML-invalid controls at the serialization boundary.
    """
    if isinstance(rows, (str, bytes, pd.DataFrame, Mapping)) or not isinstance(
        rows, Iterable
    ):
        raise ValueError("fidelity_rows: expected an iterable of mappings")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("fidelity_rows: expected an iterable of mappings")
        columns = list(row.keys())
        if len(columns) != len(FIDELITY_COLUMNS) or set(columns) != set(
            FIDELITY_COLUMNS
        ):
            raise ValueError(
                f"fidelity_columns: expected exactly {list(FIDELITY_COLUMNS)!r}"
            )

        copied: dict[str, str] = {}
        for column in FIDELITY_COLUMNS:
            value = row[column]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"fidelity_{column}: expected a nonblank string")
            copied[column] = value

        rule_id = unicodedata.normalize("NFKC", copied["rule_id"]).strip()
        duplicate_key = rule_id.casefold()
        if duplicate_key in seen:
            raise ValueError(f"fidelity_duplicate_rule: {rule_id!r}")
        seen.add(duplicate_key)
        copied["rule_id"] = rule_id
        normalized.append(copied)

    ordered = sorted(
        normalized,
        key=lambda row: (row["rule_id"].casefold(), row["rule_id"]),
    )
    return pd.DataFrame(ordered, columns=FIDELITY_COLUMNS).reset_index(drop=True)


#: 工作簿里所有 tz-aware 瞬时都按这个时区落成墙钟。
EXCEL_TIMEZONE = "Asia/Shanghai"

#: Excel 会把这些开头的文本当公式执行。
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _excel_cell(value: object) -> object:
    if isinstance(value, (list, tuple, dict, set)):
        return json.dumps(
            sorted(value) if isinstance(value, set) else value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    if isinstance(value, pd.Timestamp) and value.tzinfo is not None:
        return value.tz_convert(EXCEL_TIMEZONE).tz_localize(None)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return pd.Timestamp(value).tz_convert(EXCEL_TIMEZONE).tz_localize(None)
    if not isinstance(value, str):
        return value
    cleaned = "".join(
        character
        for character in value
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    if cleaned.startswith(_FORMULA_LEAD):
        return f"'{cleaned}"
    return cleaned


def excel_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Make a frame writable by openpyxl without changing what it says.

    Three shapes reach the serialization boundary that a spreadsheet gets
    wrong. Containers become canonical JSON. Timezone-aware instants become
    wall-clock times, because Excel has no notion of an offset. And text that
    opens with ``= + - @`` is quoted, because otherwise the reader executes the
    fidelity ledger instead of displaying it -- the ledger is deliberately
    free-form prose quoted from a paper, so it is exactly the sheet an attacker
    or a careless copy-paste would land in.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("excel_safe_frame: expected a DataFrame")
    out = frame.copy()
    for column in out.columns:
        dtype = out[column].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            out[column] = out[column].dt.tz_convert(EXCEL_TIMEZONE).dt.tz_localize(None)
            continue
        if pd.api.types.is_object_dtype(dtype) or isinstance(dtype, pd.StringDtype):
            out[column] = out[column].map(_excel_cell)
    return out


__all__ = [
    "EXCEL_TIMEZONE",
    "excel_safe_frame",
    "fidelity_frame",
    "split_metrics",
]
