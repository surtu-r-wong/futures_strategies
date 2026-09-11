"""What each rule deviation is worth, one at a time.

    .venv/bin/python scripts/citic_027_attribution.py --data-dir data/citic_index

Starts from the shipped leg's configuration and turns the deviations back to
the methodology one at a time, so each row differs from the one above it in
exactly one rule.  Changing two at once measures their interaction and
attributes nothing.

Prices are parsed once and reused: the parse costs three minutes and a run
costs ninety seconds, so loading per row would spend most of the wall clock
re-reading the same file.
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
from citic_index.pipeline import ReplicaConfig, build_replica  # noqa: E402


# What the production daily runner drops before pooling.
SHIPPED_EXCLUSIONS = "CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD".split(",")

# Each step flips exactly one rule relative to the step above it.  The universe
# takes two steps because it changed in two ways: production drops nineteen
# products the methodology keeps, and the methodology names a fixed thirty-seven.
LADDER = [
    (
        "0  shipped leg",
        dict(
            t1_leg="main",
            normalise_by_gap=False,
            cadence="monthly",
            min_observations=450,
            restrict_to_named=False,
            exclude=SHIPPED_EXCLUSIONS,
        ),
    ),
    ("1  T1 = near dominant", dict(t1_leg="near_dominant")),
    ("2  divide by month gap", dict(normalise_by_gap=True)),
    ("3  rerank daily", dict(cadence="daily")),
    ("4  listing gate, not 500 days", dict(min_observations=1)),
    ("5  stop excluding the metals", dict(exclude=[])),
    ("6  restrict to the named 37", dict(restrict_to_named=True)),
]

_CONFIG_KEYS = set(ReplicaConfig.__dataclass_fields__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_027_attribution")
    parser.add_argument("--data-dir", default="data/citic_index")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--window", type=int, default=500)
    parser.add_argument("--out", default="output/citic/attribution_027.csv")
    args = parser.parse_args(argv)

    started = time.time()
    all_prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(all_prices):,} bars in {time.time() - started:.0f}s\n", flush=True)

    state = dict(window=args.window, min_listing_calendar_days=90, exclude=[])
    rows = []
    for name, overrides in LADDER:
        state = state | overrides
        excluded = set(state["exclude"])
        prices = (
            all_prices.loc[~all_prices["product"].isin(excluded)]
            if excluded
            else all_prices
        )
        config = ReplicaConfig(**{k: v for k, v in state.items() if k in _CONFIG_KEYS})

        began = time.time()
        result = build_replica(prices, config)
        seconds = time.time() - began
        if result.index.empty:
            print(f"{name:<30} ranked nobody", flush=True)
            continue

        verdict = compare(result.index, official, code="CICSF027.WI")
        mine = verdict["replica_performance"]
        rows.append(
            {
                "step": name,
                "correlation": verdict["correlation"],
                "best_lag": verdict["best_lag"],
                "rank_correlation": verdict["rank_correlation"],
                "ann_return": mine["ann_return"],
                "ann_vol": mine["ann_vol"],
                "sharpe": mine["sharpe"],
                "max_drawdown": mine["max_drawdown"],
                "median_n": float(result.index["n_products"].median()),
                "seconds": round(seconds),
            }
        )
        print(
            f"{name:<30} corr {verdict['correlation']:+.3f}"
            f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
            f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>4.0f}"
            f"  ({seconds:.0f}s)",
            flush=True,
        )

    if not rows:
        print("every step ranked nobody -- nothing to attribute")
        return 4

    official_stats = verdict["official_performance"]
    print(
        f"\n{'official':<30} corr  1.000"
        f"  ann {official_stats['ann_return']:>7.2%}"
        f"  vol {official_stats['ann_vol']:>6.2%}"
        f"  sharpe {official_stats['sharpe']:>5.2f}"
    )

    table = pd.DataFrame(rows)
    table["d_correlation"] = table["correlation"].diff()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
