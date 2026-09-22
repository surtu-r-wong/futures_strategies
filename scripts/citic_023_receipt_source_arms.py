"""The four arms that separate a source swap from the forecast it carries.

    PYTHONPATH=. .venv/bin/python scripts/citic_023_receipt_source_arms.py

023's receipts came from Wind, which publishes registered warrants alone.
The exchange archive also carries 有效预报 -- declared but not yet warranted --
but only from CZCE, and reading it means a new derivation layer.  Two changes
at once, so an improvement would have nowhere to be attributed.  The arms
split them, over one prices dump, one window and one preregistered p grid:

    B0  exchange numbers where the exchange has them, Wind universe  -> source
    C0  B0 plus the forecast, same universe                          -> forecast
    B   B0 plus the twelve products only the exchange covers         -> universe
    C   B plus the forecast                                          -> both

The Wind arm (A) is `citic_023_scan.py` on `receipts.csv` and is the control:
it has to reproduce the number already on record before any of this is worth
reading.  Prices are loaded once -- the dump is 1.4M bars -- and each arm
rebuilds only the panel, which depends on receipts.
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.__main__ import load_prices, load_receipts  # noqa: E402
from citic_index.compare import compare  # noqa: E402
from citic_index.pipeline import ReplicaConfig, build_from_panel, build_panel  # noqa: E402

ARMS = [
    ("B0_source", "arm_b0.csv", False),
    ("C0_forecast", "arm_b0.csv", True),
    ("B_universe", "arm_b.csv", False),
    ("C_both", "arm_b.csv", True),
]

# D is the clean test of the forecast on its own.  The exchange archive starts
# in 2012 for CF/SR/OI and 2015 for TA -- CZCE's pre-2012 report does not say
# which product a row belongs to -- so swapping the source costs two products
# out of the ranked cross-section before the forecast is even considered.  The
# two series are identical to the last digit wherever both exist (ratio 1.0000
# on 42 of 43 products), which is what makes it sound to keep Wind's receipts
# and add only the exchange's forecast to them: same quantity, same unit.
# PK is the exception and is left alone -- its Wind series already equals
# receipts + forecast on all 1,219 days.
DEFAULT_ARM_SPEC = None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_023_receipt_source_arms")
    parser.add_argument("--data-dir", default="data/citic_023")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF023.WI")
    parser.add_argument("--lookbacks", default="1,3,5,10,20,40,60")
    parser.add_argument("--baseline-lag", type=int, default=200)
    parser.add_argument("--baseline-window", type=int, default=100)
    parser.add_argument(
        "--arm",
        action="append",
        default=None,
        help="name:file:forecast, repeatable; default runs B0/C0/B/C",
    )
    parser.add_argument("--out", default="output/citic/arms_023_receipt_source.csv")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    calendar = sorted(prices["trade_date"].unique())
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s", flush=True)

    arms = ARMS
    if args.arm:
        arms = []
        for spec in args.arm:
            name, filename, flag = spec.split(":")
            arms.append((name, filename, flag.lower() in ("1", "true", "yes")))

    rows = []
    for arm, filename, include_forecast in arms:
        receipts = load_receipts(
            args.data_dir,
            calendar=calendar,
            through=prices["trade_date"].max(),
            filename=filename,
            include_forecast=include_forecast,
        )
        began = time.time()
        panel = build_panel(
            prices,
            ReplicaConfig(factor_kind="warehouse_receipt", restrict_to_named=False),
            receipts=receipts,
        )
        print(f"\n[{arm}] panel in {time.time() - began:.0f}s", flush=True)

        for lookback in [int(v) for v in args.lookbacks.split(",")]:
            config = ReplicaConfig(
                factor_kind="warehouse_receipt",
                window=lookback,
                baseline_lag=args.baseline_lag,
                baseline_window=args.baseline_window,
                restrict_to_named=False,
            )
            result = build_from_panel(panel, config)
            if result.index.empty:
                print(f"  p={lookback:<3} ranked nobody", flush=True)
                continue
            verdict = compare(result.index, official, code=args.code)
            mine = verdict["replica_performance"]
            rows.append(
                {
                    "arm": arm,
                    "lookback": lookback,
                    "correlation": verdict["correlation"],
                    "rank_correlation": verdict["rank_correlation"],
                    "ann_return": mine["ann_return"],
                    "ann_vol": mine["ann_vol"],
                    "sharpe": mine["sharpe"],
                    "median_n": float(result.index["n_products"].median()),
                }
            )
            print(
                f"  p={lookback:<3} corr {verdict['correlation']:+.4f}"
                f"  rank {verdict['rank_correlation']:+.4f}"
                f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
                f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>3.0f}",
                flush=True,
            )
        del panel

    table = pd.DataFrame(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    print("\nthe whole grid, not the winner:")
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
