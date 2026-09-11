"""The daily next-open target sheet for the basis-momentum index strategy.

    .venv/bin/python -m citic_index.daily --capital 100000000 --end 2026-09-10 \
        --output-prefix output/targets/citic027_20260910

Every setting is spelled out in PRODUCTION below rather than left to
ReplicaConfig's defaults.  Those defaults are the 3.5 reading of the leg pair
with the month-gap division, kept so the attribution ladder can still reproduce
it -- and measured against the published series, that reading is the worst of
the three.  A production book must not inherit it by accident, and a reader of
this file should be able to see the whole configuration without opening
another.

Why the run reaches back 900 days: the volatility scale needs 252 index-return
days, each of which needs weights from the day before, which need R=150 traded
days of chain returns behind them.  That is about 402 trading days at minimum;
900 calendar days is roughly 615, leaving real margin.  The carry runner
shipped with 60 days on 2026-09-11, read its volatility at 4.13% against a
converged 4.77% and over-levered the book by 15.6% without erroring -- the
failure mode here is silent, so the margin is deliberate.
"""

import argparse
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import time

import pandas as pd

from citic_index.book import lever, next_targets, rolling_vol_scale
from citic_index.pg_source import load_window
from citic_index.pipeline import ReplicaConfig, build_from_panel, build_panel


# The configuration this book trades.  Derived in
# docs/plans/2026-09-11-cicsf027-pure-index-replica-results.md; R=150 sits at
# the Sharpe peak and within 0.003 correlation of the correlation peak, so the
# tracking and the strategy criteria agree on it.
PRODUCTION = ReplicaConfig(
    factor_kind="basis_momentum",
    # 3.1's leg pair, not 3.5's: the two most held contracts ordered by
    # delivery.  0.537 against 0.402 for the near-dominant reading.
    t1_leg="top_two_by_oi",
    window=150,
    min_observations=1,      # 3.2's listing gate does the history work
    smoothing=1,
    # 3.1's formula has no division; 3.5 step 1's does, and it is worse in
    # every cell of the scan.
    normalise_by_gap=False,
    cadence="daily",
    # Tradeable.  CITIC's own convention divides by the previous settlement,
    # which accrues about 1.8pp a year that nobody can be filled at.
    return_basis="close_to_close",
    roll_blend=True,
    liquidity_window=20,
    liquidity_threshold=2e9,
    min_listing_calendar_days=90,
    restrict_to_named=False,  # the named 37 are examples, not the universe
    exclude_limit_locked=True,
    min_products=5,
    target_vol=0.15,
    vol_window=252,
    max_gross_leverage=4.0,
    cost_bps=4.0,
)

LOOKBACK_DAYS = 900


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="citic_index.daily")
    parser.add_argument("--capital", type=float, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None,
                        help="read a fetched bundle instead of the database")
    return parser.parse_args(argv)


def _load(args) -> pd.DataFrame:
    if args.data_dir:
        from citic_index.__main__ import load_prices
        return load_prices(args.data_dir, start=args.end, end=args.end)
    return load_window(
        start=args.end - timedelta(days=args.lookback_days),
        end=args.end,
        config_path=args.config,
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    config = replace(PRODUCTION, base_date=args.end - timedelta(days=args.lookback_days))

    began = time.time()
    prices = _load(args)
    print(
        f"loaded {len(prices):,} bars"
        f" {prices['trade_date'].min()}..{prices['trade_date'].max()}"
        f" in {time.time() - began:.0f}s",
        flush=True,
    )
    if prices["trade_date"].max() != args.end:
        raise SystemExit(
            f"[abort] the window ends at {prices['trade_date'].max()},"
            f" not the requested {args.end}"
        )

    began = time.time()
    result = build_from_panel(build_panel(prices, config), config)
    print(f"built in {time.time() - began:.0f}s", flush=True)
    if result.index.empty:
        raise SystemExit("[abort] the run ranked nobody")

    index = result.index.set_index("trade_date")["daily_return"]
    scale = rolling_vol_scale(
        index,
        vol_window=config.vol_window,
        target_vol=config.target_vol,
        min_observations=config.vol_window,
    )
    if not pd.notna(scale.get(args.end)):
        covered = int(scale.notna().sum())
        raise SystemExit(
            f"[abort] no volatility scale on {args.end}:"
            f" the {config.vol_window}-day window is not full"
            f" ({covered} days of the run have one)."
            " Widen --lookback-days rather than trading an unsized book."
        )

    levered = lever(result.weights, scale, config=config)
    targets = next_targets(
        levered, result.legs, result.pool, signal_date=args.end, capital=args.capital
    )
    if targets.empty:
        raise SystemExit(f"[abort] no targets on {args.end}")

    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    targets.to_csv(f"{prefix}_next_targets.csv", index=False)

    gross = float(targets["target_weight"].abs().sum())
    net = float(targets["target_weight"].sum())
    vol_scale = float(targets["vol_scale"].iloc[0])
    meta = {k: (str(v) if isinstance(v, date) else v) for k, v in config.as_dict().items()}
    # The diagnostics the acceptance check reads.  A vol scale is only as good
    # as the window behind it: if the cross-section collapsed on some of those
    # 252 days, the estimate mixes a real book with an empty one and the scale
    # comes out wrong while the last day still looks fine.
    window = result.index.tail(config.vol_window)
    meta |= {
        "vol_window_days": int(len(window)),
        "vol_window_min_products": int(window["n_products"].min()),
        "vol_window_median_products": float(window["n_products"].median()),
        "vol_window_realised_vol": float(config.target_vol / vol_scale),
        "signal_date": str(args.end),
        "capital": args.capital,
        "lookback_days": args.lookback_days,
        "bars": int(len(prices)),
        "index_days": int(len(result.index)),
        "vol_scale": vol_scale,
        "gross_exposure": gross,
        "net_exposure": net,
        "products": int(len(targets)),
    }
    Path(f"{prefix}_config.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    print(
        f"{len(targets)} products  vol_scale {vol_scale:.3f}"
        f"  gross {gross:.3f}  net {net:+.1e}"
        f"  long {int(targets.loc[targets['lots'] > 0, 'lots'].sum())}"
        f" / short {int(targets.loc[targets['lots'] < 0, 'lots'].sum())} lots"
    )
    print(f"wrote {prefix}_next_targets.csv / _config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
