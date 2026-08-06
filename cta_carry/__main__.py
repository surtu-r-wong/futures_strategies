"""Command-line workflow for daily contract-level Carry research."""

from __future__ import annotations

import argparse
from dataclasses import fields, replace
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import pandas as pd
import psycopg2

from .backtest import (
    CarryBacktester,
    EquityDepletedError,
    ExecutionPriceError,
    SignalInputError,
    WarmupInsufficientError,
)
from .config import CarryConfig
from .data import CarryDataSet
from .pg_source import load_public_carry_data
from .report import (
    ReportWriteError,
    console_summary,
    write_carry_outputs,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GIT_TIMEOUT_SECONDS = 5.0


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
    # Research parameters intentionally carry no argparse default: CarryConfig is
    # the single source of truth for their defaults (see _config_from_args), so
    # the CLI cannot drift from it.  Only flags the user passes override it.
    parser.add_argument("--liquidity-window", type=int)
    parser.add_argument("--liquidity-threshold", type=float)
    parser.add_argument("--carry-window", type=int)
    parser.add_argument("--selection-fraction", type=float)
    parser.add_argument("--momentum-window", type=int)
    parser.add_argument("--atr-window", type=int)
    parser.add_argument("--atr-risk-budget", type=float)
    parser.add_argument("--vol-window", type=int)
    parser.add_argument("--min-shadow-active-days", type=int)
    parser.add_argument("--target-vol", type=float)
    parser.add_argument("--max-gross-leverage", type=float)
    parser.add_argument("--chandelier-atr-multiple", type=float)
    parser.add_argument("--stop-tranches", type=int)
    parser.add_argument("--trend-band-atr", type=float)
    parser.add_argument("--trend-confirm-days", type=int)
    parser.add_argument(
        "--secondary-selection",
        choices=["strictly_later", "second_by_oi"],
    )
    parser.add_argument("--cost-bps", type=float)
    parser.add_argument("--prewarm-calendar-days", type=int)
    return parser


def _config_from_args(args: argparse.Namespace) -> CarryConfig:
    # CarryConfig owns the defaults; apply only the flags the user set so the CLI
    # never restates (and cannot drift from) those defaults.
    overrides = {
        field.name: getattr(args, field.name)
        for field in fields(CarryConfig)
        if getattr(args, field.name, None) is not None
    }
    return CarryConfig(**overrides)


def _parse_products(value: str | None) -> list[str] | None:
    if not value:
        return None
    products = {part.strip().upper() for part in value.split(",") if part.strip()}
    return sorted(products) or None


def _validate_data_coverage(
    data: CarryDataSet,
    *,
    start: date,
    end: date,
) -> None:
    eligible_dates = data.prices.loc[
        data.prices["trade_date"] <= end,
        "trade_date",
    ].dropna()
    if eligible_dates.empty:
        raise ValueError("no Carry prices on or before end")
    if not (eligible_dates >= start).any():
        raise ValueError("no strategy trading day on or after start")


def _git_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
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
        if args.start > args.end:
            raise ValueError("start must be on or before end")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    products = _parse_products(args.products)
    try:
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
        _validate_data_coverage(data, start=args.start, end=args.end)
    except (OSError, KeyError, ValueError, psycopg2.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        result = CarryBacktester(
            data,
            config=config,
            start=args.start,
            end=args.end,
        ).run()
    except (
        EquityDepletedError,
        ExecutionPriceError,
        SignalInputError,
        WarmupInsufficientError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2

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

    try:
        xlsx, png = write_carry_outputs(
            result,
            Path(args.output_prefix),
        )
    except ReportWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(console_summary(result))
    print(f"xlsx={xlsx.resolve()}")
    print(f"chart={png.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
