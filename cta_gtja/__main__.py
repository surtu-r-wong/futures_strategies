"""CLI for replicated CTA factor-combo strategies.

Example:

  .venv/bin/python -m cta_gtja --source public-pg \\
    --strategy high_composite --start 2019-01-01 --end 2025-09-30
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from cta_gtja.backtest import write_cta_outputs
from cta_gtja.data import CTADataSet
from cta_gtja.factors import cta_factors_for_set
from cta_gtja.pg_source import load_public_cta_data
from cta_gtja.strategies import run_high_composite, run_medium_equal_weight


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m cta_gtja")
    parser.add_argument("--source", choices=["public-pg", "files"], default="public-pg")
    parser.add_argument("--data-dir", default=None, help="Directory containing prices.csv and optional fundamentals.csv")
    parser.add_argument("--strategy", choices=["medium_equal_weight", "high_composite", "both"], default="both")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated commodity symbols; default = all symbols in prices")
    parser.add_argument("--rule-type", default="standard", help="continuous_contract_ohlc.rule_type when --source public-pg")
    parser.add_argument("--include-financial", action="store_true", help="include stock-index and treasury futures in public-pg mode")
    parser.add_argument("--cost-bps", type=float, default=1.0)
    parser.add_argument(
        "--adjustment-policy",
        choices=["recommended"],
        default="recommended",
        help="price-lineage policy for public-pg source",
    )
    parser.add_argument(
        "--allow-raw-fallback",
        action="store_true",
        help="allow raw prices only for symbols whose adjusted lineages are both corrupt",
    )
    parser.add_argument(
        "--factor-set",
        choices=["six_factor", "price_volume"],
        default="six_factor",
        help="CTA factor set; price_volume avoids sparse fundamental factors",
    )
    parser.add_argument("--output-prefix", default=None, help="Output prefix without suffix; defaults under output/")
    args = parser.parse_args()

    requested_symbols = _parse_symbols(args.symbols)
    if args.source == "files":
        if not args.data_dir:
            raise SystemExit("--data-dir is required when --source files")
        data = CTADataSet.from_dir(args.data_dir).slice(
            symbols=requested_symbols,
            start=args.start,
            end=args.end,
        )
    else:
        data = load_public_cta_data(
            start=args.start,
            end=args.end,
            symbols=requested_symbols,
            rule_type=args.rule_type,
            include_financial=args.include_financial,
            adjustment_policy=args.adjustment_policy,
            allow_raw_fallback=args.allow_raw_fallback,
        )
    symbols = data.symbols
    if not symbols:
        raise SystemExit("no CTA symbols after applying filters")
    print(f"data_quality: {_data_quality_summary(data.data_quality)}")
    print(f"factor_set: {args.factor_set}")
    factors = cta_factors_for_set(args.factor_set)

    jobs = []
    if args.strategy in {"medium_equal_weight", "both"}:
        jobs.append((
            "medium_equal_weight",
            run_medium_equal_weight(
                data, symbols=symbols, factors=factors, cost_bps=args.cost_bps,
            ),
        ))
    if args.strategy in {"high_composite", "both"}:
        jobs.append((
            "high_composite",
            run_high_composite(data, symbols=symbols, factors=factors, cost_bps=args.cost_bps),
        ))

    for name, result in jobs:
        prefix = Path(args.output_prefix) if args.output_prefix else Path("output") / f"cta_{name}"
        if args.strategy == "both":
            prefix = prefix.with_name(f"{prefix.name}_{name}")
        xlsx_path, png_path = write_cta_outputs(result, prefix)
        print(f"strategy: {name}")
        for k, v in result.metrics.items():
            print(f"  {k}: {v}")
        print(f"  xlsx:   {xlsx_path.resolve()}")
        print(f"  equity: {png_path.resolve()}")


def _data_quality_summary(quality) -> str:
    if quality is None or quality.empty or "included" not in quality.columns:
        return "symbols retained=unknown excluded=unknown raw_fallback=0"
    included = quality["included"].fillna(False).astype(bool)
    retained = int(included.sum())
    excluded = int((~included).sum())
    raw = (
        int(quality["raw_fallback"].fillna(False).astype(bool).sum())
        if "raw_fallback" in quality.columns else 0
    )
    return f"symbols retained={retained} excluded={excluded} raw_fallback={raw}"


def _parse_symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


if __name__ == "__main__":
    main()

