"""Where 023's gap to the 025 control arm lives.

    .venv/bin/python scripts/citic_023_attribution.py --data-dir data/citic_023

The replica lands at corr 0.494 while the same engine replicates 025 at 0.671.
This asks which of the choices behind that 0.494 is carrying the difference.

Unlike 027's ladder, this one does **not** accumulate.  027 had a path to walk
-- the shipped leg back to the methodology -- so each rung built on the last.
023 has no such path: the question is not "how do we get from A to B" but
"which single choice matters", so every rung is the selected configuration plus
exactly one change, and the deltas are independent of each other.

Rebuilding the panel costs minutes, so the rungs are grouped by whether they
need one: only `return_basis` and `restrict_to_named` do.
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

# The point the preregistered grid selected, both judges agreeing on p=5.
SELECTED = dict(
    factor_kind="warehouse_receipt",
    window=5,
    baseline_lag=200,
    baseline_window=100,
    restrict_to_named=False,
    return_basis="close_to_prev_settle",
    cadence="daily",
    exclude_limit_locked=True,
)

# Rungs that reuse the panel: factor windows, cadence, the limit-lock gate.
CHEAP = [
    ("baseline ends at t-201 (the other reading)", dict(baseline_lag=201)),
    ("baseline spans 101 days (both ends inclusive)", dict(baseline_window=101)),
    ("baseline spans 60 days", dict(baseline_window=60)),
    ("baseline spans 150 days", dict(baseline_window=150)),
    ("baseline ends at t-100", dict(baseline_lag=100)),
    ("baseline ends at t-300", dict(baseline_lag=300)),
    ("monthly rebalance", dict(cadence="monthly")),
    ("keep the limit-locked", dict(exclude_limit_locked=False)),
]

# Rungs that need the panel rebuilt.
EXPENSIVE = [
    ("close-to-close returns", dict(return_basis="close_to_close")),
    ("restrict to the named 37", dict(restrict_to_named=True)),
]

_CONFIG_KEYS = set(ReplicaConfig.__dataclass_fields__)


def _row(name, config, panel, official, code):
    began = time.time()
    result = build_from_panel(panel, config)
    if result.index.empty:
        print(f"{name:46} ranked nobody", flush=True)
        return None, None
    verdict = compare(result.index, official, code=code)
    mine = verdict["replica_performance"]
    row = {
        "rung": name,
        "correlation": verdict["correlation"],
        "rank_correlation": verdict["rank_correlation"],
        "ann_return": mine["ann_return"],
        "ann_vol": mine["ann_vol"],
        "sharpe": mine["sharpe"],
        "median_n": float(result.index["n_products"].median()),
    }
    print(
        f"{name:46} corr {row['correlation']:+.3f}"
        f"  ann {row['ann_return']:>7.2%}  vol {row['ann_vol']:>6.2%}"
        f"  sharpe {row['sharpe']:>5.2f}  N {row['median_n']:>3.0f}"
        f"  ({time.time() - began:.0f}s)",
        flush=True,
    )
    return row, verdict


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_023_attribution")
    parser.add_argument("--data-dir", default="data/citic_023")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF023.WI")
    parser.add_argument("--out", default="output/citic/attribution_023.csv")
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

    def panel_for(**overrides):
        state = SELECTED | overrides
        cfg = ReplicaConfig(**{k: v for k, v in state.items() if k in _CONFIG_KEYS})
        began = time.time()
        built = build_panel(prices, cfg, receipts=receipts)
        print(f"  (panel rebuilt in {time.time() - began:.0f}s)", flush=True)
        return built

    print("\n=== every rung is the selected point plus ONE change ===\n", flush=True)
    base_panel = panel_for()
    rows = []
    base_row, base_verdict = _row(
        "0  selected (p=5, open, settle, daily)",
        ReplicaConfig(**{k: v for k, v in SELECTED.items() if k in _CONFIG_KEYS}),
        base_panel,
        official,
        args.code,
    )
    rows.append(base_row)

    for name, override in CHEAP:
        state = SELECTED | override
        cfg = ReplicaConfig(**{k: v for k, v in state.items() if k in _CONFIG_KEYS})
        row, _ = _row(name, cfg, base_panel, official, args.code)
        if row:
            rows.append(row)

    for name, override in EXPENSIVE:
        panel = panel_for(**override)
        state = SELECTED | override
        cfg = ReplicaConfig(**{k: v for k, v in state.items() if k in _CONFIG_KEYS})
        row, _ = _row(name, cfg, panel, official, args.code)
        if row:
            rows.append(row)
        del panel

    table = pd.DataFrame([r for r in rows if r])
    table["delta_vs_selected"] = table["correlation"] - base_row["correlation"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)

    print("\n=== deltas against the selected point ===")
    print(
        table.loc[:, ["rung", "correlation", "delta_vs_selected", "sharpe"]].to_string(
            index=False, float_format=lambda v: f"{v:,.4f}"
        )
    )

    print("\n=== the selected point, year by year ===")
    yearly = base_verdict["yearly"]
    print(yearly.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
