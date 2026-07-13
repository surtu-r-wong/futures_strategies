"""Data-quality scan for ``continuous_contract_ohlc`` adjustment lineages.

Surfaces the upstream forward/back-adjustment corruption found 2026-06-16:
non-positive adjusted prices that make continuous-series returns explode to
``-inf``.  This report classifies every symbol so downstream readers know which
adjusted series is currently usable.  The upstream fix now lives on pi at
``/home/pi/market-monitor/backend/``; its generators use multiplicative
adjustment factors and full-rebuild replacement to avoid stale invalid rows.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

_LINEAGES = ("raw", "ba", "fa")
_OHLC_FIELDS = ("open", "high", "low", "close")
_HEALTH_COLUMNS = [
    "base_symbol",
    "raw_ohlc_nonpos",
    "raw_ohlc_incomplete",
    "raw_ohlc_infinite",
    "ba_ohlc_nonpos",
    "ba_ohlc_incomplete",
    "ba_ohlc_infinite",
    "fa_ohlc_nonpos",
    "fa_ohlc_incomplete",
    "fa_ohlc_infinite",
    "suspicious_bar_count",
    "daily_return_invalid",
    "return_index_invalid",
    "first_trade_date",
    "last_trade_date",
    "lag_to_rule_max_days",
    "missing_trade_dates",
]


def summarize_adjustment_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol classification of continuous-contract adjustment quality.

    ``df`` is long-form with a ``base_symbol`` column and ``open_<lineage>`` /
    ``close_<lineage>`` columns for lineage in {raw, ba, fa}.  A lineage is
    "corrupt" for a symbol when any row has a non-positive open or close.
    """
    records = []
    for symbol, g in df.groupby("base_symbol", sort=True):
        counts = {
            f"{lineage}_nonpos": int(
                ((g[f"open_{lineage}"] <= 0) | (g[f"close_{lineage}"] <= 0)).sum()
            )
            for lineage in _LINEAGES
        }
        fa_ok = counts["fa_nonpos"] == 0
        ba_ok = counts["ba_nonpos"] == 0
        recommended = "fa" if fa_ok else "ba" if ba_ok else "raw"
        if fa_ok and ba_ok:
            status = "ok"
        elif ba_ok:
            status = "fa_corrupt"
        elif fa_ok:
            status = "ba_corrupt"
        else:
            status = "both_corrupt"
        records.append(
            {
                "base_symbol": symbol,
                "n_rows": len(g),
                **counts,
                "status": status,
                "recommended_adj": recommended,
            }
        )
    return pd.DataFrame.from_records(
        records,
        columns=[
            "base_symbol", "n_rows", "raw_nonpos", "ba_nonpos", "fa_nonpos",
            "status", "recommended_adj",
        ],
    )


def summarize_continuous_health(df: pd.DataFrame) -> pd.DataFrame:
    """Summarize full-OHLC and date health without changing lineage selection."""
    if df.empty:
        return pd.DataFrame(columns=_HEALTH_COLUMNS)

    work = df.copy()
    work["trade_date"] = pd.to_datetime(
        work["trade_date"], errors="coerce"
    ).dt.normalize()
    rule_calendar = pd.DatetimeIndex(
        work["trade_date"].dropna().unique()
    ).sort_values()
    rule_max = rule_calendar.max() if len(rule_calendar) else pd.NaT

    records = []
    for symbol, group in work.groupby("base_symbol", sort=True):
        group = group.copy()
        suspicious = np.zeros(len(group), dtype=bool)
        record: dict[str, object] = {"base_symbol": symbol}

        for lineage in _LINEAGES:
            columns = [f"{field}_{lineage}" for field in _OHLC_FIELDS]
            numeric = group[columns].apply(
                pd.to_numeric, errors="coerce"
            ).astype(float)
            values = numeric.to_numpy(dtype=float)

            incomplete = np.isnan(values).any(axis=1)
            infinite = np.isinf(values).any(axis=1)
            nonpos = ((values <= 0) & np.isfinite(values)).any(axis=1)

            record[f"{lineage}_ohlc_nonpos"] = int(nonpos.sum())
            record[f"{lineage}_ohlc_incomplete"] = int(incomplete.sum())
            record[f"{lineage}_ohlc_infinite"] = int(infinite.sum())
            suspicious |= nonpos | infinite

        daily_return = pd.to_numeric(
            group["daily_return"], errors="coerce"
        ).astype(float).to_numpy()
        return_index = pd.to_numeric(
            group["return_index"], errors="coerce"
        ).astype(float).to_numpy()

        daily_invalid = np.isinf(daily_return) | (
            np.isfinite(daily_return) & (daily_return <= -1)
        )
        index_invalid = np.isinf(return_index) | (
            np.isfinite(return_index) & (return_index <= 0)
        )

        symbol_dates = pd.DatetimeIndex(
            group["trade_date"].dropna().unique()
        ).sort_values()
        first = symbol_dates.min() if len(symbol_dates) else pd.NaT
        last = symbol_dates.max() if len(symbol_dates) else pd.NaT

        if pd.isna(first) or pd.isna(last) or pd.isna(rule_max):
            lag_days = None
            missing_dates = 0
        else:
            active_calendar = rule_calendar[
                (rule_calendar >= first) & (rule_calendar <= last)
            ]
            missing_dates = len(active_calendar.difference(symbol_dates))
            lag_days = int((rule_max - last).days)

        record.update(
            {
                "suspicious_bar_count": int(
                    group.loc[suspicious, "trade_date"].nunique()
                ),
                "daily_return_invalid": int(daily_invalid.sum()),
                "return_index_invalid": int(index_invalid.sum()),
                "first_trade_date": first.date() if not pd.isna(first) else None,
                "last_trade_date": last.date() if not pd.isna(last) else None,
                "lag_to_rule_max_days": lag_days,
                "missing_trade_dates": int(missing_dates),
            }
        )
        records.append(record)

    return pd.DataFrame.from_records(records, columns=_HEALTH_COLUMNS)


def summarize_continuous_contract_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Combine the compatible adjustment report with V2 health diagnostics."""
    adjustment = summarize_adjustment_quality(df)
    health = summarize_continuous_health(df)
    return adjustment.merge(
        health,
        on="base_symbol",
        how="outer",
        validate="one_to_one",
    )


DEFAULT_MAX_LAG_DAYS = 10


def _symbols_matching(report: pd.DataFrame, mask: pd.Series) -> str:
    symbols = report.loc[mask, "base_symbol"].astype(str).unique()
    return ",".join(sorted(symbols))


def _positive_count(report: pd.DataFrame, column: str) -> pd.Series:
    values = report.get(column, pd.Series(0, index=report.index))
    return pd.to_numeric(values, errors="coerce").fillna(0) > 0


def strict_failure_reasons(
    report: pd.DataFrame,
    *,
    as_of: date,
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
) -> list[str]:
    """Return strict failures; diagnostic-only fields never enter this policy."""
    if max_lag_days < 0:
        raise ValueError("max_lag_days must be non-negative")
    if report.empty:
        return ["no symbols returned"]

    reasons: list[str] = []

    corrupt = report["status"].fillna("unknown") != "ok"
    if corrupt.any():
        reasons.append(
            f"adjustment corruption: {_symbols_matching(report, corrupt)}"
        )

    for lineage in _LINEAGES:
        invalid = _positive_count(
            report, f"{lineage}_ohlc_nonpos"
        ) | _positive_count(report, f"{lineage}_ohlc_infinite")
        if invalid.any():
            reasons.append(
                f"{lineage} OHLC invalid: {_symbols_matching(report, invalid)}"
            )

    daily_invalid = _positive_count(report, "daily_return_invalid")
    if daily_invalid.any():
        reasons.append(
            "daily_return invalid (<= -1 or infinite): "
            + _symbols_matching(report, daily_invalid)
        )

    index_invalid = _positive_count(report, "return_index_invalid")
    if index_invalid.any():
        reasons.append(
            "return_index invalid (<= 0 or infinite): "
            + _symbols_matching(report, index_invalid)
        )

    last_trade_date = pd.to_datetime(
        report["last_trade_date"], errors="coerce"
    ).max()
    if pd.isna(last_trade_date):
        reasons.append("table freshness unavailable: no valid last_trade_date")
    else:
        lag_days = int(
            (pd.Timestamp(as_of).normalize() - last_trade_date.normalize()).days
        )
        if lag_days > max_lag_days:
            reasons.append(
                "table stale: "
                f"last_trade_date={last_trade_date.date().isoformat()} "
                f"lag_days={lag_days} max_lag_days={max_lag_days}"
            )

    return reasons


def build_adjustment_audit(
    quality_report: pd.DataFrame,
    *,
    allow_raw_fallback: bool = False,
) -> pd.DataFrame:
    """Convert a quality report into selected-lineage and inclusion decisions.

    Raw fallback is deliberately opt-in because raw continuous prices do not
    solve roll-adjusted return continuity.
    """
    if quality_report.empty:
        return pd.DataFrame(
            columns=[
                "base_symbol",
                "n_rows",
                "raw_nonpos",
                "ba_nonpos",
                "fa_nonpos",
                "status",
                "recommended_adj",
                "selected_adj",
                "included",
                "raw_fallback",
                "exclusion_reason",
            ]
        )

    rows = []
    for rec in quality_report.to_dict("records"):
        recommended = str(rec["recommended_adj"])
        selected = recommended
        included = True
        raw_fallback = recommended == "raw"
        exclusion_reason = ""
        if raw_fallback and not allow_raw_fallback:
            selected = ""
            included = False
            exclusion_reason = "both_adjusted_lineages_corrupt"
        rows.append(
            {
                **rec,
                "selected_adj": selected,
                "included": bool(included),
                "raw_fallback": bool(raw_fallback and included),
                "exclusion_reason": exclusion_reason,
            }
        )
    return pd.DataFrame.from_records(rows)


def scan_continuous_contract_quality(
    *, rule_type: str = "standard", config_path=None, use_test: bool = False
) -> pd.DataFrame:
    """Load ``continuous_contract_ohlc`` from the ``public`` schema and classify
    each symbol's adjustment quality.  Thin DB wrapper around
    :func:`summarize_adjustment_quality`.
    """
    import warnings

    from common.config import load_config, resolve_settings_path
    from common.db import get_connection, pg_config_from

    cfg = load_config(config_path or resolve_settings_path())
    pg = pg_config_from(cfg, use_test=use_test).copy()
    pg["schema"] = "public"
    sql = """
        SELECT base_symbol,
               open_raw, open_ba, open_fa,
               close_raw, close_ba, close_fa
        FROM public.continuous_contract_ohlc
        WHERE rule_type = %(rule_type)s
    """
    with get_connection(pg) as conn:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="pandas only supports SQLAlchemy connectable.*",
                category=UserWarning,
            )
            df = pd.read_sql_query(sql, conn, params={"rule_type": rule_type})
    return summarize_adjustment_quality(df)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m cta_gtja.data_quality")
    parser.add_argument("--rule-type", default="standard")
    parser.add_argument("--use-test", action="store_true")
    parser.add_argument("--csv", default=None, help="optional path to write the full report")
    args = parser.parse_args()

    report = scan_continuous_contract_quality(rule_type=args.rule_type, use_test=args.use_test)
    affected = report[report["status"] != "ok"].sort_values("fa_nonpos", ascending=False)

    print(f"continuous_contract_ohlc adjustment quality  (rule_type={args.rule_type!r})")
    print(f"  symbols scanned: {len(report)}   affected: {len(affected)}")
    print(f"  status breakdown: {report['status'].value_counts().to_dict()}")
    if not affected.empty:
        print("\naffected symbols (use 'recommended_adj' until upstream is fixed):")
        print(affected.to_string(index=False))
    print(
        "\nupstream fix lives on pi at /home/pi/market-monitor/backend/ "
        "(continuous_generator.py = standard, continuous_generator_nh.py = nanhua); "
        "the old additive forward/back-adjust step emitted non-positive prices."
    )
    if args.csv:
        report.to_csv(args.csv, index=False)
        print(f"\nfull report written to {args.csv}")


if __name__ == "__main__":
    main()
