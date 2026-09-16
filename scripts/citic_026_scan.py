"""Scan 026's lookback against the official series.

    .venv/bin/python scripts/citic_026_scan.py --data-dir data/citic_023

026's §3.1 says the strategy has one parameter, the lookback.  It has two: §3.5
step 3 opens "历史波动率定义为" and never finishes, so the volatility window is
undefined too.  Both are scanned, and the whole grid is reported.

This exists to answer one question about 023 rather than to ship 026: if 026
also lands near 0.5, the shortfall against the 025 control arm is a property of
this engine facing any factor but 025's, not something particular to receipts.
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
from citic_index.pipeline import (  # noqa: E402
    ReplicaConfig,
    build_from_panel,
    build_panel,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_026_scan")
    parser.add_argument("--data-dir", default="data/citic_023")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    parser.add_argument("--code", default="CICSF026.WI")
    parser.add_argument("--lookbacks", default="20,60,120,250")
    parser.add_argument("--vol-windows", default="20,60,120")
    parser.add_argument("--execution-lag", type=int, default=1,
                        help="1 = trade at T+1 (default, executable); 0 is a diagnostic")
    parser.add_argument("--open-universe", action="store_true")
    parser.add_argument(
        "--return-basis",
        default="settle_to_settle",
        choices=("close_to_prev_settle", "close_to_close", "settle_to_settle"),
    )
    parser.add_argument("--out", default="output/citic/scan_026.csv")
    args = parser.parse_args(argv)

    started = time.time()
    prices = load_prices(args.data_dir, start=date(2010, 1, 4), end=args.end)
    official = pd.read_csv(Path(args.data_dir) / "official.csv")
    print(f"loaded {len(prices):,} bars in {time.time() - started:.0f}s", flush=True)

    base = ReplicaConfig(
        factor_kind="time_series_momentum",
        restrict_to_named=not args.open_universe,
        return_basis=args.return_basis,
    )
    started = time.time()
    panel = build_panel(prices, base)
    print(f"panel built in {time.time() - started:.0f}s\n", flush=True)

    rows, verdict = [], None
    for lookback in [int(v) for v in args.lookbacks.split(",")]:
        for vol_window in [int(v) for v in args.vol_windows.split(",")]:
            config = ReplicaConfig(
                factor_kind="time_series_momentum",
                window=lookback,
                vol_window=vol_window,
                execution_lag=args.execution_lag,
                restrict_to_named=not args.open_universe,
                return_basis=args.return_basis,
            )
            began = time.time()
            result = build_from_panel(panel, config)
            if result.index.empty:
                print(f"L={lookback:<4} V={vol_window:<4} ranked nobody", flush=True)
                continue
            verdict = compare(result.index, official, code=args.code)
            mine = verdict["replica_performance"]
            rows.append(
                {
                    "lookback": lookback,
                    "vol_window": vol_window,
                    "correlation": verdict["correlation"],
                    "best_lag": verdict["best_lag"],
                    "ann_return": mine["ann_return"],
                    "ann_vol": mine["ann_vol"],
                    "sharpe": mine["sharpe"],
                    "median_n": float(result.index["n_products"].median()),
                }
            )
            print(
                f"L={lookback:<4} V={vol_window:<4} corr {verdict['correlation']:+.3f}"
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
    print(
        table.pivot(
            index="lookback", columns="vol_window", values="correlation"
        ).to_string(float_format=lambda v: f"{v:,.3f}")
    )
    best = table.loc[table["correlation"].idxmax()]
    print(
        f"\nbest: L={int(best['lookback'])} V={int(best['vol_window'])}"
        f" corr {best['correlation']:+.3f} sharpe {best['sharpe']:.2f}"
    )
    stats = verdict["official_performance"]
    print(
        f"official {args.code}: ann {stats['ann_return']:.2%}"
        f"  vol {stats['ann_vol']:.2%}  sharpe {stats['sharpe']:.2f}"
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
