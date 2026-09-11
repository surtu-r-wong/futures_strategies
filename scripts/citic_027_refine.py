"""Refine around 3.1's leg pair: R, the smoothing, and the month-gap divide.

    .venv/bin/python scripts/citic_027_refine.py --data-dir data/citic_open

The three-reading grid put 3.1's pair well ahead of 3.5's, and the attribution
ladder found the month-gap division was the one step that cost correlation.
Those two interact: 3.1's pair is two adjacent heavily-held months, so its gaps
are small and vary differently from the pair 3.5 describes, and whether to
divide has to be asked again on the new legs rather than carried over.

The full scan is printed, then the complete comparison at whichever point won.
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.compare import compare, render  # noqa: E402
from citic_index.__main__ import load_prices  # noqa: E402
from citic_index.pipeline import ReplicaConfig, build_from_panel, build_panel  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_027_refine")
    parser.add_argument("--data-dir", default="data/citic_open")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF027.WI")
    parser.add_argument("--t1-leg", default="top_two_by_oi")
    parser.add_argument("--windows", default="60,90,120,150,200")
    parser.add_argument("--smoothings", default="1,5,20")
    parser.add_argument("--out", default="output/citic/refine_027.csv")
    parser.add_argument("--index-out", default="output/citic/cicsf027_best")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s\n", flush=True)

    def config(**kw):
        return ReplicaConfig(
            t1_leg=args.t1_leg, restrict_to_named=False, min_observations=1, **kw
        )

    began = time.time()
    panel = build_panel(prices, config())
    print(f"panel in {time.time() - began:.0f}s\n", flush=True)

    rows = []
    best = None
    for divide in (True, False):
        for window in [int(v) for v in args.windows.split(",")]:
            for smoothing in [int(v) for v in args.smoothings.split(",")]:
                cfg = config(window=window, smoothing=smoothing, normalise_by_gap=divide)
                result = build_from_panel(panel, cfg)
                if result.index.empty:
                    continue
                verdict = compare(result.index, official, code=args.code)
                mine = verdict["replica_performance"]
                rows.append(
                    {
                        "divide_by_gap": divide,
                        "window": window,
                        "smoothing": smoothing,
                        "correlation": verdict["correlation"],
                        "best_lag": verdict["best_lag"],
                        "ann_return": mine["ann_return"],
                        "ann_vol": mine["ann_vol"],
                        "sharpe": mine["sharpe"],
                    }
                )
                print(
                    f"  gap={'yes' if divide else 'no ':>3}  R={window:<4} p={smoothing:<3}"
                    f" corr {verdict['correlation']:+.3f}"
                    f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
                    f"  sharpe {mine['sharpe']:>5.2f}",
                    flush=True,
                )
                if best is None or verdict["correlation"] > best[0]:
                    best = (verdict["correlation"], cfg, result, verdict)

    table = pd.DataFrame(rows)
    print("\nthe whole scan, correlation (rows R, columns p):")
    for divide in (True, False):
        part = table.loc[table["divide_by_gap"] == divide]
        if part.empty:
            continue
        print(f"\n  divide by the month gap: {'yes' if divide else 'no'}")
        print(part.pivot(index="window", columns="smoothing", values="correlation")
              .to_string(float_format=lambda v: f"{v:,.3f}"))

    _, cfg, result, verdict = best
    print(
        f"\n=== best point: {args.t1_leg} R={cfg.window} p={cfg.smoothing}"
        f" gap_divide={cfg.normalise_by_gap} ==="
    )
    print(render(verdict))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    result.index.to_csv(f"{args.index_out}_index.csv", index=False)
    print(f"\nwrote {args.out} and {args.index_out}_index.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
