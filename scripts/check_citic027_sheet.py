"""Accept or reject one CICSF027 daily sheet before it is traded.

    .venv/bin/python scripts/check_citic027_sheet.py output/targets/citic027_<END>

The check that matters is not "the last day looks fine".  The volatility scale
is 0.15 over the realised vol of the last 252 index days, so those 252 days
have to be days on which a real cross-section was ranked: a window holding days
where nothing was held reads the vol low and levers against it, and the sheet
still comes out looking complete.  That is exactly how the carry runner
over-levered itself by 15.6% on 2026-09-11, and only a window-wide check caught
it there.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd


def check(prefix: Path) -> list[str]:
    targets = pd.read_csv(f"{prefix}_next_targets.csv")
    meta = json.loads(Path(f"{prefix}_config.json").read_text())
    bad: list[str] = []

    def fail(message):
        bad.append(message)

    # --- the volatility window ------------------------------------------------
    if meta["vol_window_days"] < meta["vol_window"]:
        fail(
            f"the volatility window holds {meta['vol_window_days']} days,"
            f" not {meta['vol_window']} -- the scale is not converged"
        )
    if meta["vol_window_min_products"] < meta["min_products"]:
        fail(
            f"some day in the volatility window ranked only"
            f" {meta['vol_window_min_products']} products (floor"
            f" {meta['min_products']}): the vol estimate mixes a real book with"
            " an empty one and reads low"
        )

    # --- the book ------------------------------------------------------------
    gross = targets["target_weight"].abs().sum()
    net = targets["target_weight"].sum()
    if gross > meta["max_gross_leverage"] + 1e-9:
        fail(f"gross exposure {gross:.3f} is over the {meta['max_gross_leverage']} cap")
    if abs(net) > 1e-6:
        fail(f"net exposure {net:+.2e} is not zero -- the rank weights should cancel")
    if targets["product"].duplicated().any():
        dup = sorted(targets.loc[targets["product"].duplicated(), "product"])
        fail(f"duplicated products: {', '.join(dup)}")
    if targets["lots"].isna().any():
        missing = sorted(targets.loc[targets["lots"].isna(), "product"])
        fail(f"no lots for {', '.join(missing)}")

    # --- the arithmetic, recomputed rather than trusted ------------------------
    implied = targets["target_weight"] * meta["capital"]
    if not (implied - targets["notional"]).abs().lt(1e-6).all():
        fail("notional does not equal target_weight x capital")
    lots = (targets["notional"] / (targets["close"] * targets["multiplier"])).round()
    if not lots.eq(targets["lots"]).all():
        off = int((~lots.eq(targets["lots"])).sum())
        fail(f"{off} rows where lots do not equal notional / (close x multiplier)")

    # --- the codes an order ticket will actually carry -------------------------
    # Zhengzhou quotes a one-digit delivery year, so MA2701 is rejected at entry
    # where MA701 is taken; the other four exchanges take the four-digit code in
    # lower case.  The carry sheet has always got this right, and this is here so
    # that it cannot quietly stop being right.
    exchange = targets["contract"].str.split(".").str[1]
    czce = exchange.eq("CZC")
    wrong_czce = targets.loc[czce & ~targets["order_code"].str.fullmatch(r"[A-Z]+[0-9]{3}")]
    if not wrong_czce.empty:
        codes = ", ".join(wrong_czce["order_code"])
        fail(
            f"Zhengzhou order codes must be upper case with three digits: {codes}"
        )
    wrong_other = targets.loc[~czce & ~targets["order_code"].str.fullmatch(r"[a-z]+[0-9]{4}")]
    if not wrong_other.empty:
        codes = ", ".join(wrong_other["order_code"])
        fail(
            f"non-Zhengzhou order codes must be lower case with four digits: {codes}"
        )

    # --- the sheet is for the day it says --------------------------------------
    if targets["signal_date"].nunique() != 1:
        fail("more than one signal_date in the sheet")
    elif targets["signal_date"].iloc[0] != meta["signal_date"]:
        fail(
            f"sheet is dated {targets['signal_date'].iloc[0]}"
            f" but the run says {meta['signal_date']}"
        )
    return bad


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="check_citic027_sheet")
    parser.add_argument("prefix")
    args = parser.parse_args(argv)
    prefix = Path(args.prefix)

    bad = check(prefix)
    meta = json.loads(Path(f"{prefix}_config.json").read_text())
    print(
        f"{prefix.name}: {meta['products']} products,"
        f" vol_scale {meta['vol_scale']:.3f}"
        f" (realised {meta['vol_window_realised_vol']:.2%} over"
        f" {meta['vol_window_days']} days, min cross-section"
        f" {meta['vol_window_min_products']}),"
        f" gross {meta['gross_exposure']:.3f}"
    )
    if bad:
        print("\nREJECTED:")
        for message in bad:
            print(f"  - {message}")
        return 1
    print("accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
