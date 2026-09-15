"""Scan p, the one parameter CITIC 023 leaves open, against the official series.

    .venv/bin/python scripts/citic_023_scan.py --data-dir data/citic_023

§3.1 names p without giving it a value -- "本策略具有 p 这一个参数" -- so it has
to be chosen here.  The grid is preregistered in the design doc rather than
widened until something wins, and the whole grid is reported, not the point
that won.

Two judges, and they can disagree: correlation with the published series says
whether this replicates, Sharpe says whether the factor is any good.  **When
they disagree, correlation decides** -- the target of this path is the series,
not a return.  A disagreement is itself a finding and belongs in the writeup.

The panel is built once.  p enters only after the pool, the legs and the chain
returns are fixed, so a grid point costs seconds rather than the minutes a full
run takes.
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.compare import compare  # noqa: E402
from citic_index.__main__ import load_prices, load_receipts  # noqa: E402
from citic_index.pipeline import (  # noqa: E402
    ReplicaConfig,
    build_from_panel,
    build_panel,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_023_scan")
    parser.add_argument("--data-dir", default="data/citic_023")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF023.WI")
    parser.add_argument("--lookbacks", default="1,3,5,10,20,40,60")
    parser.add_argument("--baseline-lag", type=int, default=200)
    parser.add_argument("--baseline-window", type=int, default=100)
    parser.add_argument("--open-universe", action="store_true")
    parser.add_argument(
        "--return-basis",
        default="close_to_prev_settle",
        choices=("close_to_prev_settle", "close_to_close"),
    )
    parser.add_argument(
        "--smoothing-target", default="level", choices=("level", "ratio")
    )
    parser.add_argument("--out", default="output/citic/scan_023.csv")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s", flush=True)

    receipts = load_receipts(
        args.data_dir,
        calendar=sorted(prices["trade_date"].unique()),
        through=prices["trade_date"].max(),
    )

    base = ReplicaConfig(
        factor_kind="warehouse_receipt",
        restrict_to_named=not args.open_universe,
        return_basis=args.return_basis,
        smoothing_target=args.smoothing_target,
    )
    started = time.time()
    panel = build_panel(prices, base, receipts=receipts)
    print(f"panel built in {time.time() - started:.0f}s\n", flush=True)

    rows, verdict = [], None
    for lookback in [int(v) for v in args.lookbacks.split(",")]:
        config = ReplicaConfig(
            factor_kind="warehouse_receipt",
            window=lookback,
            baseline_lag=args.baseline_lag,
            baseline_window=args.baseline_window,
            restrict_to_named=not args.open_universe,
            return_basis=args.return_basis,
            smoothing_target=args.smoothing_target,
        )
        began = time.time()
        result = build_from_panel(panel, config)
        if result.index.empty:
            print(f"p={lookback:<3} ranked nobody", flush=True)
            continue
        verdict = compare(result.index, official, code=args.code)
        mine = verdict["replica_performance"]
        rows.append(
            {
                "lookback": lookback,
                "correlation": verdict["correlation"],
                "best_lag": verdict["best_lag"],
                "rank_correlation": verdict["rank_correlation"],
                "ann_return": mine["ann_return"],
                "ann_vol": mine["ann_vol"],
                "sharpe": mine["sharpe"],
                "median_n": float(result.index["n_products"].median()),
            }
        )
        print(
            f"p={lookback:<3} corr {verdict['correlation']:+.3f}"
            f"  rank {verdict['rank_correlation']:+.3f}"
            f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
            f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>3.0f}"
            f"  ({time.time() - began:.0f}s)",
            flush=True,
        )

    if not rows:
        print("no grid point produced an index")
        return 3

    table = pd.DataFrame(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)

    print("\nthe whole grid, not the winner:")
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    by_corr = table.loc[table["correlation"].idxmax()]
    by_sharpe = table.loc[table["sharpe"].idxmax()]
    print(
        f"\nbest correlation: p={int(by_corr['lookback'])} ({by_corr['correlation']:+.3f})"
        f"   best sharpe: p={int(by_sharpe['lookback'])} ({by_sharpe['sharpe']:.2f})"
    )
    if int(by_corr["lookback"]) != int(by_sharpe["lookback"]):
        print(
            "  ** the two judges disagree.  Correlation decides for this path,"
            " and the disagreement goes in the writeup. **"
        )

    stats = verdict["official_performance"]
    print(
        f"\nofficial {args.code}: ann {stats['ann_return']:.2%}"
        f"  vol {stats['ann_vol']:.2%}  sharpe {stats['sharpe']:.2f}"
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
