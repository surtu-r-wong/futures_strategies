"""What each rule deviation is worth, one at a time.

    .venv/bin/python scripts/citic_027_attribution.py --data-dir data/citic_open

Starts from the shipped leg's configuration and turns the deviations back to
the methodology one step at a time, so each row differs from the one above it
in exactly one rule.  Changing two at once measures their interaction and
attributes nothing.

Only the steps that move the universe, the T1 rule or the return basis need the
panel rebuilt; the rest reuse it, which is the difference between a ten-minute
run and an hour.
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


# What the production daily runner drops before pooling.
SHIPPED_EXCLUSIONS = "CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD".split(",")

# Changing any of these means the panel has to be rebuilt.
PANEL_KEYS = {
    "t1_leg",
    "return_basis",
    "roll_blend",
    "liquidity_window",
    "liquidity_threshold",
    "min_listing_calendar_days",
    "restrict_to_named",
    "exclude",
}

LADDER = [
    (
        "0  shipped leg",
        dict(
            t1_leg="main",
            normalise_by_gap=False,
            cadence="monthly",
            min_observations=450,
            window=500,
            smoothing=1,
            return_basis="close_to_close",
            restrict_to_named=False,
            exclude_limit_locked=False,
            exclude=SHIPPED_EXCLUSIONS,
        ),
    ),
    ("1  T1 = near dominant", dict(t1_leg="near_dominant")),
    ("2  divide by the month gap", dict(normalise_by_gap=True)),
    ("3  rerank daily", dict(cadence="daily")),
    ("4  listing gate, not 500 days", dict(min_observations=1)),
    ("5  stop excluding the metals", dict(exclude=[])),
    ("6  settlement return basis", dict(return_basis="close_to_prev_settle")),
    ("7  drop the limit-locked", dict(exclude_limit_locked=True)),
    ("8  R = 250 (calibrated)", dict(window=250)),
]

_CONFIG_KEYS = set(ReplicaConfig.__dataclass_fields__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_027_attribution")
    parser.add_argument("--data-dir", default="data/citic_open")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF027.WI")
    parser.add_argument("--out", default="output/citic/attribution_027.csv")
    args = parser.parse_args(argv)

    started = time.time()
    all_prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(all_prices):,} bars in {time.time() - started:.0f}s\n", flush=True)

    state = {"exclude": []}
    panel = None
    panel_state = None
    rows = []
    verdict = None
    for name, overrides in LADDER:
        state = state | overrides
        config = ReplicaConfig(**{k: v for k, v in state.items() if k in _CONFIG_KEYS})
        wanted = {k: state.get(k) for k in PANEL_KEYS}
        wanted["exclude"] = tuple(sorted(wanted["exclude"] or ()))

        began = time.time()
        if wanted != panel_state:
            del panel
            gc.collect()
            excluded = set(wanted["exclude"])
            prices = (
                all_prices.loc[~all_prices["product"].isin(excluded)]
                if excluded
                else all_prices
            )
            panel = build_panel(prices, config)
            panel_state = wanted
            rebuilt = "panel"
        else:
            rebuilt = ""

        result = build_from_panel(panel, config)
        seconds = time.time() - began
        if result.index.empty:
            print(f"{name:<32} ranked nobody", flush=True)
            continue

        verdict = compare(result.index, official, code=args.code)
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
            }
        )
        print(
            f"{name:<32} corr {verdict['correlation']:+.3f}"
            f"  ann {mine['ann_return']:>7.2%}  vol {mine['ann_vol']:>6.2%}"
            f"  sharpe {mine['sharpe']:>5.2f}  N {rows[-1]['median_n']:>3.0f}"
            f"  ({seconds:.0f}s {rebuilt})",
            flush=True,
        )
        del result
        gc.collect()

    if not rows:
        print("every step ranked nobody -- nothing to attribute")
        return 4

    stats = verdict["official_performance"]
    print(
        f"\n{'official ' + args.code:<32} corr  1.000"
        f"  ann {stats['ann_return']:>7.2%}  vol {stats['ann_vol']:>6.2%}"
        f"  sharpe {stats['sharpe']:>5.2f}"
    )

    table = pd.DataFrame(rows)
    table["d_correlation"] = table["correlation"].diff()
    print("\nwhat each step was worth, in correlation:")
    print(
        table.loc[:, ["step", "correlation", "d_correlation"]].to_string(
            index=False, na_rep="-", float_format=lambda v: f"{v:+.3f}"
        )
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
