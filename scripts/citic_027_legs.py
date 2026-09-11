"""Do the three readings of the leg pair explain the ceiling?

    .venv/bin/python scripts/citic_027_legs.py --data-dir data/citic_open

The methodology defines T1 twice and incompatibly -- 3.5 step 1 as the
highest-open-interest contract delivering before the dominant, 3.1 as the two
most held contracts sorted by delivery -- and the shipped leg reads it a third
way, as the dominant itself.  The whole R and smoothing grid tops out well
under what the same engine reaches on 025, so a leg pair that is simply the
wrong pair has to be ruled in or out before the gap can be called specification.

One price load, one panel per reading, the grid reused across both.
"""

import argparse
import gc
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.compare import compare  # noqa: E402
from citic_index.__main__ import load_prices  # noqa: E402
from citic_index.pipeline import ReplicaConfig, build_from_panel, build_panel  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_027_legs")
    parser.add_argument("--data-dir", default="data/citic_open")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF027.WI")
    parser.add_argument("--legs", default="near_dominant,main,top_two_by_oi")
    parser.add_argument("--windows", default="60,120,250,500")
    parser.add_argument("--smoothings", default="1,20,60")
    parser.add_argument("--out", default="output/citic/legs_027.csv")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s\n", flush=True)

    windows = [int(v) for v in args.windows.split(",")]
    smoothings = [int(v) for v in args.smoothings.split(",")]
    rows = []
    verdict = None
    for t1_leg in args.legs.split(","):
        base = ReplicaConfig(
            t1_leg=t1_leg,
            # The open universe, which the ladder showed is the largest single
            # step -- the earlier grid ran on the named list by mistake.
            restrict_to_named=False,
            min_observations=1,
        )
        began = time.time()
        panel = build_panel(prices, base)
        print(f"--- {t1_leg}: panel in {time.time() - began:.0f}s", flush=True)

        for window in windows:
            for smoothing in smoothings:
                config = ReplicaConfig(
                    t1_leg=t1_leg,
                    restrict_to_named=False,
                    min_observations=1,
                    window=window,
                    smoothing=smoothing,
                )
                result = build_from_panel(panel, config)
                if result.index.empty:
                    continue
                verdict = compare(result.index, official, code=args.code)
                mine = verdict["replica_performance"]
                rows.append(
                    {
                        "t1_leg": t1_leg,
                        "window": window,
                        "smoothing": smoothing,
                        "correlation": verdict["correlation"],
                        "best_lag": verdict["best_lag"],
                        "ann_return": mine["ann_return"],
                        "ann_vol": mine["ann_vol"],
                        "sharpe": mine["sharpe"],
                        "median_n": float(result.index["n_products"].median()),
                    }
                )
                print(
                    f"  R={window:<4} p={smoothing:<3} corr {verdict['correlation']:+.3f}"
                    f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
                    f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>3.0f}",
                    flush=True,
                )
                del result
        del panel
        gc.collect()

    table = pd.DataFrame(rows)
    print("\ncorrelation, rows (leg, R), columns p -- the whole grid:")
    print(
        table.pivot_table(
            index=["t1_leg", "window"], columns="smoothing", values="correlation"
        ).to_string(float_format=lambda v: f"{v:,.3f}")
    )
    best = table.loc[table["correlation"].idxmax()]
    print(
        f"\nbest: {best['t1_leg']} R={int(best['window'])} p={int(best['smoothing'])}"
        f" corr {best['correlation']:.3f} sharpe {best['sharpe']:.2f}"
    )
    stats = verdict["official_performance"]
    print(
        f"official {args.code}: ann {stats['ann_return']:.2%}"
        f"  vol {stats['ann_vol']:.2%}  sharpe {stats['sharpe']:.2f}"
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
