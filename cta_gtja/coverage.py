"""Pre-weight coverage checks for CTA fundamental factor runs."""
from __future__ import annotations

import numpy as np
import pandas as pd


COVERAGE_COLUMNS = [
    "trade_date",
    "check",
    "metric",
    "available_products",
    "required_products",
    "long_candidates",
    "short_candidates",
    "required_each_side",
    "status",
    "reason",
]


class FundamentalCoverageError(ValueError):
    pass


def _numeric_array(values):
    array = np.asarray(values)
    try:
        return array.astype(float, copy=False)
    except (TypeError, ValueError):
        flat = pd.to_numeric(
            pd.Series(array.reshape(-1)),
            errors="coerce",
        )
        return flat.to_numpy(dtype=float).reshape(array.shape)


def _finite_mask(values):
    return np.isfinite(_numeric_array(values))


def _prepare_frame(frame):
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    prepared = frame.copy()
    prepared.index = pd.to_datetime(prepared.index)
    return prepared.sort_index()


def _date_groups(frame):
    for trade_date, day in frame.groupby(level=0, sort=True):
        yield pd.Timestamp(trade_date), day


def _date_label(trade_date):
    return pd.Timestamp(trade_date).strftime("%Y-%m-%d")


def _audit_frame(rows):
    audit = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
    if not audit.empty:
        audit = audit.sort_values(
            ["trade_date", "check", "metric"],
            kind="mergesort",
        ).reset_index(drop=True)
    return audit


def _finish(rows, failures, enforce):
    audit = _audit_frame(rows)
    if enforce and failures:
        raise FundamentalCoverageError("\n".join(sorted(failures)))
    return audit


def _waived_keys(absences):
    """Normalise the build's waived slices to (date, metric) keys.

    The slices come from `fundamental_build.absence_slices`, written by the
    upstream build from a reviewed file. A consumer cannot otherwise tell an
    approved hole from a data outage, and must not guess.
    """
    keys = set()
    for entry in absences or ():
        keys.add(
            (
                pd.Timestamp(entry["trade_date"]),
                str(entry["metric"]),
            )
        )
    return keys


def evaluate_daily_fundamental_coverage(
    matrices,
    *,
    symbols,
    required_products,
    enforce,
    absences=(),
):
    requested_symbols = list(symbols)
    required = int(required_products)
    waived = _waived_keys(absences)
    rows = []
    failures = []

    for metric in sorted(matrices, key=str):
        matrix = _prepare_frame(matrices[metric]).reindex(
            columns=requested_symbols
        )
        for trade_date, day in _date_groups(matrix):
            finite = _finite_mask(day.to_numpy())
            available = int(finite.any(axis=0).sum())
            if (pd.Timestamp(trade_date), str(metric)) in waived:
                status = "waived"
            else:
                status = "pass" if available >= required else "fail"
            reason = (
                ""
                if status != "fail"
                else (
                    f"{_date_label(trade_date)} {metric} "
                    f"coverage={available} required={required}"
                )
            )
            rows.append(
                {
                    "trade_date": trade_date,
                    "check": "daily_fundamental",
                    "metric": str(metric),
                    "available_products": available,
                    "required_products": required,
                    "long_candidates": np.nan,
                    "short_candidates": np.nan,
                    "required_each_side": np.nan,
                    "status": status,
                    "reason": reason,
                }
            )
            if reason:
                failures.append(reason)

    return _finish(rows, failures, enforce)


def evaluate_inventory_sides(scores, *, required_each, enforce):
    required = int(required_each)
    frame = _prepare_frame(scores)
    groups = list(_date_groups(frame))

    first_evaluable = None
    for position, (_, day) in enumerate(groups):
        finite = _finite_mask(day.to_numpy())
        if int(finite.any(axis=0).sum()) >= 4:
            first_evaluable = position
            break

    if first_evaluable is None:
        return _audit_frame([])

    rows = []
    failures = []
    for trade_date, day in groups[first_evaluable:]:
        values = _numeric_array(day.to_numpy())
        finite = np.isfinite(values)
        available = int(finite.any(axis=0).sum())
        long_candidates = int(
            (finite & (values > 0)).any(axis=0).sum()
        )
        short_candidates = int(
            (finite & (values < 0)).any(axis=0).sum()
        )
        status = (
            "pass"
            if long_candidates >= required and short_candidates >= required
            else "fail"
        )
        reason = (
            ""
            if status == "pass"
            else (
                f"{_date_label(trade_date)} inventory "
                f"long={long_candidates} short={short_candidates} "
                f"required_each={required}"
            )
        )
        rows.append(
            {
                "trade_date": trade_date,
                "check": "inventory_sides",
                "metric": "inventory",
                "available_products": available,
                "required_products": 4,
                "long_candidates": long_candidates,
                "short_candidates": short_candidates,
                "required_each_side": required,
                "status": status,
                "reason": reason,
            }
        )
        if reason:
            failures.append(reason)

    return _finish(rows, failures, enforce)
