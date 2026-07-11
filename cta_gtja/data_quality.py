"""Data-quality scan for ``continuous_contract_ohlc`` adjustment lineages.

Surfaces the upstream forward/back-adjustment corruption found 2026-06-16:
non-positive adjusted prices that make continuous-series returns explode to
``-inf``.  This report classifies every symbol so downstream readers know which
adjusted series is currently usable.  The upstream fix now lives on pi at
``/home/pi/market-monitor/backend/``; its generators use multiplicative
adjustment factors and full-rebuild replacement to avoid stale invalid rows.
"""
from __future__ import annotations

import pandas as pd

_LINEAGES = ("raw", "ba", "fa")


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
