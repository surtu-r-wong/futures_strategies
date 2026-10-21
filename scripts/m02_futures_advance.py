"""Advance the M02 futures observation account for the next trading day.

    .venv/bin/python scripts/m02_futures_advance.py                 # normal day
    .venv/bin/python scripts/m02_futures_advance.py --czce-receipt PATH  # D-day

One command per trading day, after the day's data has landed:

- execution quote (build day only): D-day 09:30 first minute bar, opening
  price of the 09:30:00 bar, from public.futures_minute for the 48 non-CZCE
  contracts plus the Wind-gateway capture receipt for the 19 CZCE contracts
  (futures_minute has no CZCE coverage; collector v1 keeps it frozen);
- settlement quote: public.futures_daily settle for every held contract;
- target: the frozen 2026-10-15 batch re-read and hash-checked against the
  pinned declaration (quantity-hold after the D-day build means later days
  carry no execution quote at all).

Fails closed with an explicit missing list when inputs are absent; rerunning
after the data lands is idempotent (packet hash dedupe, atomic feed rewrite).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account import advance, export_feed  # noqa: E402
from forward_account.store import read_status  # noqa: E402
from forward_account.adapters import read_targets  # noqa: E402
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BUILD_DIR = REPO / "docs/requests/2026-10-17-m02-initialization/build"
FROZEN_TARGETS_CSV = (
    REPO
    / "docs/requests/2026-10-14-multi-asset-operational-returns/"
    "m01-offline-diagnosis-evidence/verification-02/targets.csv"
)
FROZEN_METADATA = (
    REPO
    / "docs/requests/2026-10-14-multi-asset-operational-returns/"
    "m01-offline-diagnosis-evidence/verification-02/source-parameters.json"
)
STATE_DEFAULT = REPO / "output/multi_asset/m02_futures/account.sqlite"
FEED_DEFAULT = REPO / "output/multi_asset/returns/latest.json"

# futures_minute stores bare symbols plus an exchange column.
MINUTE_EXCHANGE = {"SHF": "SHFE", "DCE": "DCE", "INE": "INE", "GFE": "GFEX"}


def _czce_contracts(config):
    return sorted(c for c in config["instruments"] if c.endswith(".CZC"))


def _minute_opens(contracts, trade_date):
    """09:30:00 bar opening prices from public.futures_minute (non-CZCE)."""
    wanted = {}
    for contract in contracts:
        if contract.endswith(".CZC"):
            continue
        code, _, suffix = contract.rpartition(".")
        wanted[code] = contract
    query_at = f"{trade_date}T09:30:00+08:00"
    with get_connection(pg_config_from(load_config(resolve_settings_path()))) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, open FROM public.futures_minute "
                "WHERE bar_time = %s AND symbol = ANY(%s)",
                (query_at, sorted(wanted)),
            )
            rows = {r[0]: float(r[1]) for r in cur.fetchall()}
    missing = [wanted[c] for c in wanted if c not in rows]
    if missing:
        raise ValueError(f"futures_minute missing 09:30 bar for: {sorted(missing)}")
    return {wanted[c]: rows[c] for c in rows}


def _czce_opens(receipt_path, config, trade_date):
    """CZCE 09:30 execution prices from the Wind-gateway capture receipt."""
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get("schema") != "m02-czce-wind-minute-receipt.v1":
        raise ValueError("unexpected CZCE receipt schema")
    if receipt.get("trade_date") != trade_date:
        raise ValueError("CZCE receipt trade_date mismatch")
    expected = _czce_contracts(config)
    prices = receipt.get("prices", {})
    missing = [c for c in expected if c not in prices]
    if missing:
        raise ValueError(f"CZCE receipt missing: {missing}")
    values = {}
    for contract in expected:
        price = float(prices[contract])
        if price <= 0:
            raise ValueError(f"{contract}: non-positive CZCE price")
        values[contract] = price
    return values, receipt


def _settlement(config, trade_date, wanted):
    with get_connection(pg_config_from(load_config(resolve_settings_path()))) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, settle FROM public.futures_daily "
                "WHERE trade_date = %s AND symbol = ANY(%s)",
                (trade_date, sorted(wanted)),
            )
            rows = {r[0]: float(r[1]) for r in cur.fetchall()}
    missing = [c for c in wanted if c not in rows]
    if missing:
        raise ValueError(f"futures_daily settle missing for: {sorted(missing)}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(STATE_DEFAULT))
    parser.add_argument("--account", default="m02-futures-observation")
    parser.add_argument("--config", default=str(BUILD_DIR / "account.config.json"))
    parser.add_argument("--czce-receipt", help="D-day Wind capture receipt JSON")
    parser.add_argument("--feed", default=str(FEED_DEFAULT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text())
    status = read_status(args.state, args.account)
    interval = status["next_interval"]
    if interval is None:
        raise ValueError("calendar exhausted; extend references first")
    state = status["state"]
    holding = bool(state["positions"])
    trade_date = interval["trade_date"]

    target = read_targets(
        config,
        FROZEN_TARGETS_CSV,
        FROZEN_METADATA,
        dict(config["target_policy"]["pinned"]),
    )
    packet = {"trade_date": trade_date, "target": target}

    if not holding:
        if not args.czce_receipt:
            # clean error instead of a bare Path(None) traceback; the
            # Wind CZCE receipt is required on every build day
            raise ValueError(
                "build day requires --czce-receipt (Wind CZCE capture receipt "
                "JSON; hold days need no receipt)"
            )
        opens = _minute_opens(config["instruments"], trade_date)
        czce, receipt = _czce_opens(args.czce_receipt, config, trade_date)
        opens.update(czce)
        packet["execution"] = {
            "kind": "execution",
            "at": interval["execution_at"],
            "available_at": receipt["captured_at"],
            "source": (
                "public.futures_minute 09:30:00 bar open (48 non-CZCE); "
                f"Wind gateway CZCE receipt {receipt['raw_sha256'][:16]} (19); "
                "slippage charged separately as 4bp CNY cost"
            ),
            "synthetic": False,
            "prices": {c: opens[c] for c in sorted(opens)},
            "tradable": dict.fromkeys(opens, True),
        }
        wanted = set(config["instruments"])
    else:
        wanted = set(state["positions"])

    import datetime as dt

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
    packet["settlement"] = {
        "kind": "settlement",
        "at": interval["end_at"],
        "available_at": now,
        "source": "public.futures_daily settle (EOD); read-only query",
        "synthetic": False,
        "prices": _settlement(config, trade_date, wanted),
    }

    if args.dry_run:
        print(json.dumps({k: (v if k != "target" else "pinned-ok") for k, v in packet.items()}, ensure_ascii=False, indent=2)[:2000])
        print("DRY RUN: no state change")
        return 0
    result = advance(args.state, args.account, packet)
    export_feed(args.state, args.feed)
    row = result["row"]
    print(
        f"{trade_date}: unit_nav={row['unit_nav']:.6f} "
        f"daily_return={row['daily_return']:+.6%} fees={result['fees']:.2f} "
        f"margin_ratio={row['margin_ratio']:.4f} "
        f"positions={len(result['positions'])} trades={len(result['audit']['trades'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
