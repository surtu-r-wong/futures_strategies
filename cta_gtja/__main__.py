"""CLI for replicated CTA factor-combo strategies.

Example:

  .venv/bin/python -m cta_gtja --source public-pg \\
    --strategy high_composite --start 2019-01-01 --end 2025-09-30
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from cta_gtja.backtest import write_cta_outputs
from cta_gtja.data import CTADataSet
from cta_gtja.factors import cta_factors_for_set
from cta_gtja.pg_source import PILOT_FUNDAMENTAL_SYMBOLS, load_public_cta_data
from cta_gtja.strategies import run_high_composite, run_medium_equal_weight


def _resolve_fundamentals_source(factor_set: str, requested: str) -> str:
    if requested == "auto":
        resolved = "standard" if factor_set == "six_factor" else "none"
    else:
        resolved = requested
    if factor_set == "six_factor" and resolved == "none":
        raise SystemExit("fundamentals are required for six_factor")
    return resolved


def _default_symbols_for_factor_set(
    factor_set: str,
    symbols: list[str] | None,
) -> list[str] | None:
    if symbols is not None:
        return symbols
    if factor_set == "six_factor":
        return list(PILOT_FUNDAMENTAL_SYMBOLS)
    return None


def _fundamental_lineage_summary(metadata: dict[str, object]) -> str:
    parts = [f"fundamentals: source={metadata.get('source', 'unknown')}"]
    for key, label in (
        ("pit_mode", "pit_mode"),
        ("build_version", "build"),
        ("catalog_version", "catalog"),
    ):
        value = metadata.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cta_gtja")
    parser.add_argument("--source", choices=["public-pg", "files"], default="public-pg")
    parser.add_argument("--data-dir", default=None, help="Directory containing prices.csv and optional fundamentals.csv")
    parser.add_argument("--strategy", choices=["medium_equal_weight", "high_composite", "both"], default="both")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols; default = nine-product pilot for six_factor, all prices otherwise",
    )
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
        default="price_volume",
        help=(
            "CTA factor set; price_volume is the safe default until a "
            "published conservative fundamentals build is available"
        ),
    )
    parser.add_argument(
        "--fundamentals-source",
        choices=["auto", "standard", "legacy", "none"],
        default="auto",
    )
    parser.add_argument("--output-prefix", default=None, help="Output prefix without suffix; defaults under output/")
    return parser


def _has_finite_fundamental(data: CTADataSet, column: str) -> bool:
    if column not in data.fundamentals.columns:
        return False
    values = pd.to_numeric(data.fundamentals[column], errors="coerce")
    return bool(np.isfinite(values.to_numpy(dtype=float)).any())


def _has_finite_price(data: CTADataSet, column: str) -> bool:
    if column not in data.prices.columns:
        return False
    values = pd.to_numeric(data.prices[column], errors="coerce")
    return bool(np.isfinite(values.to_numpy(dtype=float)).any())


def _validate_six_factor_request(
    *,
    source: str,
    factor_set: str,
    data: CTADataSet | None,
) -> None:
    if factor_set != "six_factor":
        return
    if source != "files":
        return
    if data is None:
        raise SystemExit("six_factor file validation requires loaded data")

    missing: list[str] = []
    has_direct_basis = _has_finite_fundamental(data, "basis_rate")
    has_spot = _has_finite_fundamental(data, "spot")
    if not has_direct_basis and not has_spot:
        missing.append("basis")
    elif (
        not has_direct_basis
        and has_spot
        and not _has_finite_price(data, "close_raw")
    ):
        missing.append("close_raw (required for spot basis fallback)")
    for column in ("inventory", "profit"):
        if not _has_finite_fundamental(data, column):
            missing.append(column)
    if missing:
        raise SystemExit(
            "six_factor files require finite inputs: "
            + ", ".join(missing)
        )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    fundamentals_source = _resolve_fundamentals_source(
        args.factor_set,
        args.fundamentals_source,
    )
    requested_symbols = _default_symbols_for_factor_set(
        args.factor_set,
        _parse_symbols(args.symbols),
    )
    if args.source == "files":
        if not args.data_dir:
            raise SystemExit("--data-dir is required when --source files")
        data = CTADataSet.from_dir(args.data_dir).slice(
            symbols=requested_symbols,
            start=args.start,
            end=args.end,
        )
        _validate_six_factor_request(
            source=args.source,
            factor_set=args.factor_set,
            data=data,
        )
    else:
        data = load_public_cta_data(
            start=args.start,
            end=args.end,
            symbols=requested_symbols,
            rule_type=args.rule_type,
            fundamentals_source=fundamentals_source,
            include_financial=args.include_financial,
            adjustment_policy=args.adjustment_policy,
            allow_raw_fallback=args.allow_raw_fallback,
        )
    symbols = data.symbols
    if not symbols:
        raise SystemExit("no CTA symbols after applying filters")
    print(f"data_quality: {_data_quality_summary(data.data_quality)}")
    print(_fundamental_lineage_summary(data.fundamental_metadata))
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
        print(
            "fundamental_coverage: "
            f"{_fundamental_coverage_summary(result.fundamental_coverage)}"
        )
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


def _fundamental_coverage_summary(coverage) -> str:
    if coverage is None or coverage.empty:
        return "rows=0 failed=0 minimum=unknown"

    rows = len(coverage)
    if "status" in coverage.columns:
        failed_mask = coverage["status"].astype(str).eq("fail")
    else:
        failed_mask = pd.Series(False, index=coverage.index)
    failed = int(failed_mask.sum())

    available = (
        pd.to_numeric(coverage["available_products"], errors="coerce").dropna()
        if "available_products" in coverage.columns
        else pd.Series(dtype=float)
    )
    if available.empty:
        minimum = "unknown"
    else:
        minimum_value = float(available.min())
        minimum = f"{minimum_value:g}"

    summary = f"rows={rows} failed={failed} minimum={minimum}"
    if failed and "reason" in coverage.columns:
        reasons = sorted(
            {
                str(reason).strip()
                for reason in coverage.loc[failed_mask, "reason"].dropna()
                if str(reason).strip()
            }
        )[:3]
        if reasons:
            summary += f" reasons={' | '.join(reasons)}"
    return summary


def _parse_symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


if __name__ == "__main__":
    main()
