"""Command-line workflow for daily contract-level Carry research."""

from __future__ import annotations

import argparse
from dataclasses import fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
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
from .minute_backtest import (
    MINUTE_QUERY_RULES_VERSION,
    MULTIPLIER_RESOLUTION_VERSION,
    CarryMinuteBacktester,
)
from common.minute.bars import MinuteDataError
from common.minute.pg_source import MinuteSourceAudit, PublicMinuteSource
from common.minute.sessions import (
    SESSION_RULES_VERSION,
    SessionClockError,
    load_session_rules,
)
from .pg_source import load_public_carry_data
from .session_authority import load_absent_product_days, load_pricing_bases
from .provenance import capture_git_state
from .report import (
    ReportWriteError,
    console_summary,
    write_carry_outputs,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SESSION_RULES_PATH = _REPO_ROOT / "config" / "carry_minute_sessions.csv"
_ABSENT_PRODUCT_DAYS_PATH = (
    _REPO_ROOT / "config" / "carry_minute_absent_product_days.csv"
)
_PRICING_BASIS_PATH = _REPO_ROOT / "config" / "carry_minute_pricing_basis.csv"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cta_carry")
    parser.add_argument(
        "--source",
        choices=["public-pg", "files"],
        default="public-pg",
    )
    parser.add_argument(
        "--execution",
        choices=["daily", "minute"],
        default="daily",
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
    parser.add_argument(
        "--no-trend-opposed",
        dest="allow_trend_opposed",
        action="store_false",
        default=None,
        help="drop the branch that trades against a fading trend",
    )
    parser.add_argument(
        "--no-trend-filter",
        dest="trend_filter_enabled",
        action="store_false",
        default=None,
        help="let the Carry ranking alone decide the position, with no "
        "momentum/volume gate",
    )
    parser.add_argument("--trend-band-atr", type=float)
    parser.add_argument("--trend-confirm-days", type=int)
    parser.add_argument(
        "--equal-weight-capital",
        action="store_true",
        default=None,
    )
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


def _runtime_config(
    *,
    source: str,
    execution_mode: str,
    products: list[str] | None,
    data: CarryDataSet,
) -> pd.DataFrame:
    dates = data.dates
    git_state = capture_git_state(_REPO_ROOT)
    return pd.DataFrame(
        [
            {"key": "source", "value": source},
            {"key": "execution_mode", "value": execution_mode},
            {
                "key": "products",
                "value": ",".join(products) if products else "ALL",
            },
            {"key": "code_version", "value": git_state.version},
            {"key": "code_dirty", "value": git_state.dirty},
            {
                "key": "code_diff_sha256",
                "value": git_state.diff_sha256,
            },
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


def _describe(exc: BaseException) -> str:
    """Render a failure with the notes its raiser went to trouble to attach.

    The minute source records what else went wrong during cleanup as notes on
    the original error rather than letting a secondary failure mask it. Those
    notes are the only thing distinguishing "the query failed" from "the query
    failed and then the rollback failed too", so they belong in the message.
    """
    notes = getattr(exc, "__notes__", ())
    return "\n".join((str(exc), *notes))


def _validate_minute_runtime_provenance(
    run_config: pd.DataFrame,
    audit: object,
) -> None:
    if type(audit) is not MinuteSourceAudit:
        raise MinuteDataError(
            check="minute_source_provenance",
            reason="minute source audit must be an immutable MinuteSourceAudit",
            context={"audit_type": type(audit).__name__},
        )
    if (
        type(audit.minute_table_min) is not datetime
        or type(audit.minute_table_max) is not datetime
        or audit.minute_table_min.tzinfo is None
        or audit.minute_table_min.utcoffset() is None
        or audit.minute_table_max.tzinfo is None
        or audit.minute_table_max.utcoffset() is None
    ):
        raise MinuteDataError(
            check="minute_source_provenance",
            reason="minute source audit table bounds must be aware datetimes",
        )
    counters = {
        "minute_query_months": audit.minute_query_months,
        "minute_rows": audit.minute_rows,
        "minute_candidate_contract_days": audit.minute_candidate_contract_days,
    }
    invalid_counter = next(
        (key for key, value in counters.items() if type(value) is not int or value < 0),
        None,
    )
    if invalid_counter is not None:
        raise MinuteDataError(
            check="minute_source_provenance",
            reason="minute source audit counters must be nonnegative actual integers",
            context={invalid_counter: counters[invalid_counter]},
        )
    if not {"key", "value"}.issubset(run_config.columns):
        raise MinuteDataError(
            check="minute_source_provenance",
            reason="minute engine run_config must contain key and value columns",
        )

    expected = {
        "accounting_clock": "piecewise_close_marked",
        "minute_query_rules_version": MINUTE_QUERY_RULES_VERSION,
        "session_rules_version": SESSION_RULES_VERSION,
        "multiplier_resolution_version": MULTIPLIER_RESOLUTION_VERSION,
        "minute_table_min": audit.minute_table_min.isoformat(),
        "minute_table_max": audit.minute_table_max.isoformat(),
        **counters,
    }
    for key, expected_value in expected.items():
        matches = run_config.loc[run_config["key"].eq(key), "value"]
        if len(matches) != 1:
            raise MinuteDataError(
                check="minute_source_provenance",
                reason="minute engine run_config must contain each provenance key once",
                context={"key": key, "matches": len(matches)},
            )
        actual = matches.iloc[0]
        same_type = type(actual) is type(expected_value)
        if not same_type or actual != expected_value:
            raise MinuteDataError(
                check="minute_source_provenance",
                reason="minute engine run_config provenance disagrees with source audit",
                context={
                    "key": key,
                    "actual": actual,
                    "actual_type": type(actual).__name__,
                    "expected": expected_value,
                    "expected_type": type(expected_value).__name__,
                },
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        if args.start > args.end:
            raise ValueError("start must be on or before end")
        if args.execution == "minute" and args.source != "public-pg":
            raise ValueError("--execution minute requires --source public-pg")
    except ValueError as exc:
        print(_describe(exc), file=sys.stderr)
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
        print(_describe(exc), file=sys.stderr)
        return 2

    minute_source = None
    session_rules = ()
    if args.execution == "minute":
        try:
            minute_source = PublicMinuteSource(
                config_path=args.settings,
                use_test=args.use_test,
            )
            minute_source.load_table_bounds()
            session_rules = load_session_rules(_SESSION_RULES_PATH)
            # Product-days whose minute archive holds no day session. Without
            # them the engine cannot tell an authorised absence from missing
            # data and fails closed on a day it was told to defer.
            absent_product_days = load_absent_product_days(_ABSENT_PRODUCT_DAYS_PATH)
            # Exchanges whose minute turnover cannot price a fill.
            pricing_bases = load_pricing_bases(_PRICING_BASIS_PATH)
        except (OSError, KeyError, ValueError, psycopg2.Error) as exc:
            print(_describe(exc), file=sys.stderr)
            return 2

    try:
        if args.execution == "minute":
            assert minute_source is not None
            result = CarryMinuteBacktester(
                data=data,
                minute_source=minute_source,
                session_rules=session_rules,
                config=config,
                start=args.start,
                end=args.end,
                absent_product_days=absent_product_days,
                pricing_bases=pricing_bases,
            ).run()
            _validate_minute_runtime_provenance(
                result.run_config,
                minute_source.audit,
            )
        else:
            result = CarryBacktester(
                data,
                config=config,
                start=args.start,
                end=args.end,
            ).run()
    except (
        EquityDepletedError,
        ExecutionPriceError,
        MinuteDataError,
        psycopg2.Error,
        SessionClockError,
        SignalInputError,
        WarmupInsufficientError,
    ) as exc:
        print(_describe(exc), file=sys.stderr)
        return 2

    runtime_config = _runtime_config(
        source=args.source,
        execution_mode=args.execution,
        products=products,
        data=data,
    )
    dirty_row = runtime_config["key"].eq("code_dirty")
    runtime_config.loc[dirty_row, "value"] = (
        runtime_config.loc[dirty_row, "value"].astype(str).str.lower()
    )
    result = replace(
        result,
        run_config=pd.concat(
            [
                result.run_config.loc[
                    ~result.run_config["key"].isin(runtime_config["key"])
                ],
                runtime_config,
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
        print(_describe(exc), file=sys.stderr)
        return 3

    print(console_summary(result))
    print(f"xlsx={xlsx.resolve()}")
    print(f"chart={png.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
