"""Run one pure-index replica.

    .venv/bin/python -m citic_index --data-dir data/citic_index \
        --start 2010-01-04 --end 2026-09-10 --output-prefix output/citic/cicsf027

Every rule the shipped basis-momentum leg departs from is a flag, so the
attribution task can turn them on one at a time.  Defaults are the replica:
T1 is the near dominant, the factor is divided by the month gap, the ranking is
struck daily, the listing gate is three months and the universe is the
thirty-seven products 3.2 names.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from cta_carry.data import normalize_contract_daily

from citic_index.pipeline import ReplicaConfig, build_replica
from citic_index.receipts import fill_absent_zeros


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="citic_index")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2010, 1, 4))
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output-prefix", required=True)

    # 3.1 / 3.5 step 1 -- the factor
    parser.add_argument("--factor", dest="factor_kind",
                        choices=("basis_momentum", "term_structure", "warehouse_receipt"),
                        default="basis_momentum",
                        help="term_structure is the 025 control arm for this engine;"
                        " warehouse_receipt is 023")
    parser.add_argument("--window", type=int, default=500)
    parser.add_argument("--min-observations", type=int, default=None,
                        help="default: 90%% of --window, the strict-history gate")
    parser.add_argument("--smoothing", type=int, default=1,
                        help="arithmetic mean of the factor over this many days "
                             "(3.1's lookback, beside R)")
    parser.add_argument("--t1-leg",
                        choices=("near_dominant", "main", "top_two_by_oi"),
                        default="near_dominant",
                        help="3.5 step 1 reads near_dominant; 3.1 reads "
                             "top_two_by_oi; main is what the shipped leg does")
    gap = parser.add_mutually_exclusive_group()
    gap.add_argument("--normalise-by-gap", dest="normalise_by_gap",
                     action="store_true", default=True)
    gap.add_argument("--no-normalise-by-gap", dest="normalise_by_gap",
                     action="store_false")

    # 3.2 -- the pool
    parser.add_argument("--liquidity-window", type=int, default=20)
    parser.add_argument("--liquidity-threshold", type=float, default=2e9)
    parser.add_argument("--min-listing-calendar-days", type=int, default=90)
    named = parser.add_mutually_exclusive_group()
    named.add_argument("--restrict-to-named", dest="restrict_to_named",
                       action="store_true", default=True)
    named.add_argument("--open-universe", dest="restrict_to_named",
                       action="store_false")
    parser.add_argument("--exclude-products", default=None,
                        help="comma-separated codes dropped before pooling")

    # 3.5 steps 2-5 -- ranking and accumulation
    parser.add_argument("--return-basis",
                        choices=("close_to_prev_settle", "close_to_close"),
                        default="close_to_prev_settle",
                        help="CITIC divides by the previous settlement; "
                             "close_to_close is the tradeable convention")
    parser.add_argument("--cadence", choices=("daily", "monthly"), default="daily")
    parser.add_argument("--min-products", type=int, default=2)
    locked = parser.add_mutually_exclusive_group()
    locked.add_argument("--exclude-limit-locked", dest="exclude_limit_locked",
                        action="store_true", default=True)
    locked.add_argument("--keep-limit-locked", dest="exclude_limit_locked",
                        action="store_false")
    roll = parser.add_mutually_exclusive_group()
    roll.add_argument("--roll-blend", dest="roll_blend",
                      action="store_true", default=True)
    roll.add_argument("--no-roll-blend", dest="roll_blend", action="store_false")
    parser.add_argument("--base-date", type=date.fromisoformat, default=date(2010, 1, 4))
    parser.add_argument("--baseline-lag", type=int, default=200,
                        help="023 only: the baseline window ends this many days back")
    parser.add_argument("--baseline-window", type=int, default=100,
                        help="023 only: how many days that baseline window spans")
    parser.add_argument("--smoothing-target", default="level",
                        choices=("level", "ratio"),
                        help="023 only: average the receipt level (023's wording)"
                        " or the daily factor (025's placement)")
    parser.add_argument("--base-value", type=float, default=1000.0)
    return parser.parse_args(argv)


def load_prices(data_dir: str, *, start: date, end: date) -> pd.DataFrame:
    """Bars from a fetched bundle, through the production normaliser.

    The window needs history before `start` -- five hundred traded days of it at
    the default -- so the bundle is read whole and only the tail is cut.
    """
    path = Path(data_dir) / "prices.csv"
    raw = pd.read_csv(path)
    data = normalize_contract_daily(raw)
    prices = data.prices
    prices = prices.loc[prices["trade_date"] <= end]
    return prices.reset_index(drop=True)


def load_receipts(
    data_dir: str, *, calendar, through
) -> pd.DataFrame:
    """023's factor input, with absences resolved before the factor sees them.

    The calendar is the bundle's own trading days rather than a separate
    source, so a receipt day and a price day cannot disagree about which days
    exist.  `through` is the run's end, which resolves a product whose series
    ends on a zero -- JD does, forty days before the others.
    """
    path = Path(data_dir) / "receipts.csv"
    if not path.exists():
        raise SystemExit(
            f"{path} not found -- re-fetch the bundle (scripts/citic_index_fetch.py)"
        )
    raw = pd.read_csv(path)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date
    frame, report = fill_absent_zeros(
        raw, calendar, through=through, with_report=True
    )
    print(
        f"receipts: {len(frame):,} product-days,"
        f" {frame['product'].nunique()} products;"
        f" filled {report['filled_zero']:,} absent days as zero,"
        f" left {report['left_missing']:,} as genuine gaps",
        flush=True,
    )
    return frame


def main(argv=None) -> int:
    args = _parse_args(argv)
    min_observations = (
        args.min_observations
        if args.min_observations is not None
        else max(1, round(args.window * 0.9))
    )
    config = ReplicaConfig(
        factor_kind=args.factor_kind,
        window=args.window,
        min_observations=min_observations,
        smoothing=args.smoothing,
        normalise_by_gap=args.normalise_by_gap,
        t1_leg=args.t1_leg,
        cadence=args.cadence,
        roll_blend=args.roll_blend,
        return_basis=args.return_basis,
        liquidity_window=args.liquidity_window,
        liquidity_threshold=args.liquidity_threshold,
        min_listing_calendar_days=args.min_listing_calendar_days,
        restrict_to_named=args.restrict_to_named,
        min_products=args.min_products,
        exclude_limit_locked=args.exclude_limit_locked,
        baseline_lag=args.baseline_lag,
        baseline_window=args.baseline_window,
        smoothing_target=args.smoothing_target,
        base_date=args.base_date,
        base_value=args.base_value,
    )

    started = time.time()
    prices = load_prices(args.data_dir, start=args.start, end=args.end)
    if args.exclude_products:
        dropped = {p.strip().upper() for p in args.exclude_products.split(",") if p.strip()}
        prices = prices.loc[~prices["product"].isin(dropped)].reset_index(drop=True)
    print(
        f"loaded {len(prices):,} bars"
        f" {prices['trade_date'].min()}..{prices['trade_date'].max()}"
        f" in {time.time() - started:.0f}s",
        flush=True,
    )

    receipts = None
    if config.factor_kind == "warehouse_receipt":
        receipts = load_receipts(
            args.data_dir,
            calendar=sorted(prices["trade_date"].unique()),
            through=prices["trade_date"].max(),
        )

    started = time.time()
    result = build_replica(prices, config, receipts=receipts)
    print(f"built in {time.time() - started:.0f}s", flush=True)

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    index = result.index.loc[result.index["trade_date"] >= args.base_date]
    index.to_csv(f"{prefix}_index.csv", index=False)
    result.weights.to_csv(f"{prefix}_weights.csv", index=False)

    meta = config.as_dict() | {
        "start": str(args.start),
        "end": str(args.end),
        "exclude_products": args.exclude_products,
        "bars": int(len(prices)),
        "rankable_product_days": int(len(result.weights)),
        "index_days": int(len(index)),
    }
    meta = {key: (str(value) if isinstance(value, date) else value) for key, value in meta.items()}
    Path(f"{prefix}_config.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    if index.empty:
        # Not a crash: a configuration can legitimately rank nobody -- a listing
        # gate longer than the sample, a threshold nothing clears.  Say which,
        # rather than dying on an empty frame three lines later.
        pooled = int(result.pool["in_pool"].sum()) if not result.pool.empty else 0
        ready = int(result.factor["bm_ready"].sum()) if not result.factor.empty else 0
        print(
            f"no index days: {pooled} pooled product-days,"
            f" {ready} passed the history gate,"
            f" {len(result.weights)} rankable -- widen the sample or loosen a gate"
        )
        return 4

    last = index.iloc[-1]
    print(
        f"{len(index):,} index days"
        f" {index['trade_date'].iloc[0]}..{last['trade_date']}"
        f"  final {last['index_value']:.2f}"
        f"  median cross-section {int(index['n_products'].median())}"
    )
    print(f"wrote {prefix}_index.csv / _weights.csv / _config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
