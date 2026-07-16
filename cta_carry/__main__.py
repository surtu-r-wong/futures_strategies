"""Command-line workflow for daily contract-level Carry research."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import pandas as pd

from .backtest import CarryBacktester
from .config import CarryConfig
from .data import CarryDataSet
from .pg_source import load_public_carry_data
from .report import console_summary, write_carry_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cta_carry")
    parser.add_argument(
        "--source",
        choices=["public-pg", "files"],
        default="public-pg",
    )
    parser.add_argument("--data-dir")
    parser.add_argument("--settings")
    parser.add_argument("--use-test", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--products", help="comma-separated product codes")
    parser.add_argument(
        "--output-prefix",
        default="output/carry_daily",
    )
    parser.add_argument("--liquidity-window", type=int, default=120)
    parser.add_argument(
        "--liquidity-threshold",
        type=float,
        default=5_000_000_000.0,
    )
    parser.add_argument("--carry-window", type=int, default=10)
    parser.add_argument("--selection-fraction", type=float, default=0.20)
    parser.add_argument("--momentum-window", type=int, default=10)
    parser.add_argument("--atr-window", type=int, default=20)
    parser.add_argument("--atr-risk-budget", type=float, default=0.005)
    parser.add_argument("--vol-window", type=int, default=252)
    parser.add_argument("--min-shadow-active-days", type=int, default=126)
    parser.add_argument("--target-vol", type=float, default=0.15)
    parser.add_argument("--max-gross-leverage", type=float, default=4.0)
    parser.add_argument(
        "--chandelier-atr-multiple",
        type=float,
        default=2.5,
    )
    parser.add_argument("--stop-tranches", type=int, default=3)
    parser.add_argument("--cost-bps", type=float, default=13.0)
    parser.add_argument("--prewarm-calendar-days", type=int, default=730)
    return parser


def _config_from_args(args: argparse.Namespace) -> CarryConfig:
    return CarryConfig(
        liquidity_window=args.liquidity_window,
        liquidity_threshold=args.liquidity_threshold,
        carry_window=args.carry_window,
        selection_fraction=args.selection_fraction,
        momentum_window=args.momentum_window,
        atr_window=args.atr_window,
        atr_risk_budget=args.atr_risk_budget,
        vol_window=args.vol_window,
        min_shadow_active_days=args.min_shadow_active_days,
        target_vol=args.target_vol,
        max_gross_leverage=args.max_gross_leverage,
        chandelier_atr_multiple=args.chandelier_atr_multiple,
        stop_tranches=args.stop_tranches,
        cost_bps=args.cost_bps,
        prewarm_calendar_days=args.prewarm_calendar_days,
    )


def _parse_products(value: str | None) -> list[str] | None:
    if not value:
        return None
    products = {part.strip().upper() for part in value.split(",") if part.strip()}
    return sorted(products) or None


def _git_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _runtime_config(
    *,
    source: str,
    products: list[str] | None,
    data: CarryDataSet,
) -> pd.DataFrame:
    dates = data.dates
    return pd.DataFrame(
        [
            {"key": "source", "value": source},
            {
                "key": "products",
                "value": ",".join(products) if products else "ALL",
            },
            {"key": "code_version", "value": _git_version()},
            {
                "key": "data_start_date",
                "value": dates[0] if dates else None,
            },
            {
                "key": "data_end_date",
                "value": dates[-1] if dates else None,
            },
            {"key": "data_rows", "value": len(data.prices)},
        ],
        columns=["key", "value"],
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        products = _parse_products(args.products)
        if args.source == "files":
            if not args.data_dir:
                raise ValueError("--data-dir is required when --source files")
            query_start = args.start - timedelta(days=config.prewarm_calendar_days)
            data = CarryDataSet.from_dir(args.data_dir).slice(
                products=products,
                start=query_start,
                end=args.end,
            )
        else:
            data = load_public_carry_data(
                start=args.start,
                end=args.end,
                config=config,
                products=products,
                config_path=args.settings,
                use_test=args.use_test,
            )

        result = CarryBacktester(
            data,
            config=config,
            start=args.start,
            end=args.end,
        ).run()
        result = replace(
            result,
            run_config=pd.concat(
                [
                    result.run_config,
                    _runtime_config(
                        source=args.source,
                        products=products,
                        data=data,
                    ),
                ],
                ignore_index=True,
            ),
        )
        xlsx, png = write_carry_outputs(
            result,
            Path(args.output_prefix),
        )
        print(console_summary(result))
        print(f"xlsx={xlsx.resolve()}")
        print(f"chart={png.resolve()}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
