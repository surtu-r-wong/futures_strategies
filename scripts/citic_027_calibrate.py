"""Scan R and the lookback mean against the official series.

    .venv/bin/python scripts/citic_027_calibrate.py --data-dir data/citic_open

The methodology fixes neither: "R 为动量的计算周期" gives no number, and 3.1's
lookback is named without one.  Choosing them by correlation with a published
series is legitimate here -- the target IS that series, not a return -- but the
whole grid gets reported, not the point that won.

The panel is built once.  R and the smoothing only enter after the legs and
their chain returns are fixed, so a grid point costs seconds rather than the
two minutes a full run takes.
"""

import argparse
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
    parser = argparse.ArgumentParser(prog="citic_027_calibrate")
    parser.add_argument("--data-dir", default="data/citic_open")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF027.WI")
    parser.add_argument("--factor", default="basis_momentum")
    parser.add_argument("--windows", default="20,60,120,250,500")
    parser.add_argument("--smoothings", default="1,20,60,90")
    parser.add_argument("--out", default="output/citic/calibration_027.csv")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s", flush=True)

    base = ReplicaConfig(factor_kind=args.factor)
    started = time.time()
    panel = build_panel(prices, base)
    print(f"panel built in {time.time() - started:.0f}s\n", flush=True)

    windows = [int(v) for v in args.windows.split(",")]
    smoothings = [int(v) for v in args.smoothings.split(",")]
    rows = []
    for window in windows:
        for smoothing in smoothings:
            config = ReplicaConfig(
                factor_kind=args.factor,
                window=window,
                # The listing gate does the history work; the factor takes what
                # the product has lived through.
                min_observations=1,
                smoothing=smoothing,
            )
            began = time.time()
            result = build_from_panel(panel, config)
            if result.index.empty:
                print(f"R={window:<4} p={smoothing:<3} ranked nobody", flush=True)
                continue
            verdict = compare(result.index, official, code=args.code)
            mine = verdict["replica_performance"]
            rows.append(
                {
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
                f"R={window:<4} p={smoothing:<3} corr {verdict['correlation']:+.3f}"
                f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
                f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>3.0f}"
                f"  ({time.time() - began:.0f}s)",
                flush=True,
            )

    table = pd.DataFrame(rows)
    print("\ncorrelation grid (rows R, columns p) -- the whole grid, not the winner:")
    print(table.pivot(index="window", columns="smoothing", values="correlation").to_string(
        float_format=lambda v: f"{v:,.3f}"
    ))
    official_stats = verdict["official_performance"]
    print(
        f"\nofficial {args.code}: ann {official_stats['ann_return']:.2%}"
        f"  vol {official_stats['ann_vol']:.2%}  sharpe {official_stats['sharpe']:.2f}"
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
