"""Build the M02 futures observation account config from frozen evidence.

    .venv/bin/python scripts/prepare_m02_futures_initialization.py

Reads the disabled 2026-10-16 candidate (contract-fees-v2 fee table, 67
instruments, exchange margin/fee values frozen on 2026-10-15), applies the
M02 initialization letter's user-ruled cost model, and writes a store-ready
config + calendar into the 2026-10-17 request directory:

    build/account.config.json   store-ready config (validate_config passes)
    build/calendar.json         CN trading days 2026-10-20..2026-12-31
    build/build-manifest.json   inputs, hashes, transformations, checks
    build/integer-preview.json  illustrative lot sizing on 2026-10-16 settles

The frozen target batch (67 native weights, signal_date 2026-10-15) is pinned
by SHA in target_policy; the advance script re-reads those exact files.

All economics are frozen at 2026-10-15 values through 2026-12-31: exchange fee
changes during observation require a new account generation, by design.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account.adapters import read_targets  # noqa: E402
from forward_account.contracts import validate_config  # noqa: E402
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REQUEST_DIR = REPO / "docs/requests/2026-10-17-m02-initialization"
BASE_CANDIDATE = (
    REPO / "docs/requests/2026-10-16-m02-futures-rule-clarification/account.disabled.json"
)
FROZEN_DIR = (
    REPO
    / "docs/requests/2026-10-14-multi-asset-operational-returns/"
    "m01-offline-diagnosis-evidence/verification-02"
)
FROZEN_TARGETS_CSV = FROZEN_DIR / "targets.csv"
FROZEN_METADATA = FROZEN_DIR / "source-parameters.json"
AVAILABLE_AT = "2026-10-15T20:06:21.433269+08:00"  # native-target-inventory.json
SIGNAL_DATE = "2026-10-15"
D_DATE = "2026-10-20"
COVERAGE_END = "2026-12-31"

ACCOUNT_ID = "m02-futures-observation"
ACCOUNT_VERSION = "m02-futures-observation-v1"
SOURCE_VERSION = "citic027_r150_t1::frozen-2026-10-15::targets-3ce0759e"
LABEL = (
    "M02 futures paper observation: frozen 2026-10-15 027 batch, 67 contracts, "
    "quantity-hold after D=2026-10-20 09:30 build"
)
BROKER_SOURCE = (
    "user ruling 2026-10-16 4-1: exchange fee x1.1, no minimum; broker leg = "
    "10% of the frozen 2026-10-15 exchange rate per action"
)
SLIPPAGE_SOURCE = (
    "value per user ruling 2026-10-16 4-2 (\"4-2、滑点4bp\"); cost-basis "
    "mechanism per user ruling 2026-10-18 21:43+08 (AskUserQuestion: keep 4bp "
    "as a CNY cost, no tick rounding; letter wording deviation, disclosed)"
)
MARGIN_NOTE = (
    "user ruling 2026-10-16 4-3 (\"4-3、先按2%加\"): exchange margin +2pp; "
    "base {} frozen 2026-10-15"
)
FEE_DESCRIPTION = (
    "exchange fee x1.1 (broker +10% of exchange rate, no minimum), single-side "
    "4bp slippage as CNY cost, exchange margin +2pp; frozen 2026-10-15 values"
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


def _business_days():
    """CN trading days around the M02 window, with a read-only query receipt."""
    import datetime as dt

    with get_connection(pg_config_from(load_config(resolve_settings_path()))) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT calendar_date FROM data_manager.business_calendar "
                "WHERE calendar_id='CN' AND is_business_day "
                "AND calendar_date BETWEEN %s AND %s ORDER BY calendar_date",
                ("2026-10-15", COVERAGE_END),
            )
            days = [r[0].isoformat() for r in cur.fetchall()]
    if not days or days[-1] < COVERAGE_END:
        raise ValueError("business_calendar does not cover the full window")
    trade_days = [d for d in days if d >= D_DATE]
    prior = [d for d in days if d < D_DATE]
    if not prior:
        raise ValueError("no CN trading day before D")
    receipt = {
        "table": "data_manager.business_calendar",
        "calendar_id": "CN",
        "queried_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "row_count": len(days),
        "first": days[0],
        "last": days[-1],
        "trade_days_from_d": len(trade_days),
        "prior_trading_day": prior[-1],
    }
    return trade_days, prior[-1], receipt


def _build_calendar():
    trade_days, prior_day, receipt = _business_days()
    intervals = []
    previous = None
    for d in trade_days:
        signal = prior_day if previous is None else previous["trade_date"]
        start = (
            f"{prior_day}T15:00:00+08:00"
            if previous is None
            else previous["end_at"]
        )
        row = {
            "trade_date": d,
            "signal_date": signal,
            "start_at": start,
            "execution_at": f"{d}T09:30:00+08:00",
            "end_at": f"{d}T15:00:00+08:00",
        }
        intervals.append(row)
        previous = row
    calendar = {
        "calendar_id": f"m02-futures-cn-{D_DATE}_{COVERAGE_END}",
        "timezone": "Asia/Shanghai",
        "source": (
            "data_manager.business_calendar CN (read-only query; receipt in "
            "build-manifest.json); day session 09:30 execution, 15:00 settlement"
        ),
        "complete": True,
        "coverage_start": trade_days[0],
        "coverage_end": trade_days[-1],
        "intervals": intervals,
    }
    return calendar, receipt


CZCE_ABSOLUTE_YUAN_PER_LOT = {
    # Ruling 1 (2026-10-16 round 2): the 13 CZCE absolute fee values are CNY
    # yuan/lot, value-matched to the official CZCE fee schedule.
    "AP2701.CZC", "CF2701.CZC", "CJ2701.CZC", "FG2701.CZC", "OI2701.CZC",
    "PF2612.CZC", "PK2612.CZC", "PL2612.CZC", "RM2701.CZC", "SF2701.CZC",
    "SM2701.CZC", "SR2701.CZC", "TA2701.CZC",
}


def _apply_czce_unit_ruling(rules):
    filled = set()
    for contract, contract_rules in rules.items():
        if not contract.endswith(".CZC"):
            continue
        for rule in contract_rules:
            for action, rate in rule["exchange"].items():
                if rate["unit"] is None:
                    if contract not in CZCE_ABSOLUTE_YUAN_PER_LOT:
                        raise ValueError(f"{contract}: unexpected null fee unit")
                    rate["unit"] = "YUAN_PER_LOT"
                    filled.add(contract)
    if filled != CZCE_ABSOLUTE_YUAN_PER_LOT:
        raise ValueError("CZCE unit ruling did not cover the expected 13 contracts")
    return sorted(filled)


def _fill_broker_and_slippage(rules):
    for contract, contract_rules in rules.items():
        for rule in contract_rules:
            if rule.get("broker") is not None or rule.get("slippage") is not None:
                raise ValueError(f"{contract}: base rule already priced")
            broker = {}
            for action, exchange_rate in rule["exchange"].items():
                value = round(float(exchange_rate["value"]) * 0.1, 10)
                if abs(value * 10 - float(exchange_rate["value"])) > 1e-9:
                    raise ValueError(f"{contract}/{action}: broker x1.1 mismatch")
                broker[action] = {"value": value, "unit": exchange_rate["unit"]}
            rule["broker"] = broker
            rule["broker_source"] = BROKER_SOURCE
            rule["slippage"] = {"value": 4, "unit": "PER_TEN_THOUSAND"}
            rule["slippage_source"] = SLIPPAGE_SOURCE
            rule["valid_to"] = COVERAGE_END


def _mark_up_margin(instruments):
    for contract, spec in instruments.items():
        base = float(spec["margin_rate"])
        spec["margin_rate"] = round(base + 0.02, 10)
        spec["valid_to"] = COVERAGE_END
        spec["source"] = (
            f"{spec['source']}; {MARGIN_NOTE.format(base)}"
        )


def _integer_preview(config):
    """Illustrative D-day lot sizing on 2026-10-16 settles; preview only."""
    contracts = sorted(config["instruments"])
    with get_connection(pg_config_from(load_config(resolve_settings_path()))) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, settle FROM public.futures_daily "
                "WHERE trade_date='2026-10-16' AND symbol = ANY(%s)",
                (contracts,),
            )
            rows = {r[0]: float(r[1]) for r in cur.fetchall()}
    missing = [c for c in contracts if c not in rows]
    if missing:
        raise ValueError(f"settle missing for {missing}")
    import csv

    weights = {}
    with FROZEN_TARGETS_CSV.open() as f:
        for row in csv.DictReader(f):
            weights[row["contract"]] = float(row["target_weight"])
    nav = float(config["initial_capital"])
    preview = {}
    for contract in contracts:
        spec = config["instruments"][contract]
        lots = round(
            weights[contract]
            * nav
            / (rows[contract] * float(spec["multiplier"]))
        )
        preview[contract] = {
            "weight": weights[contract],
            "settle_2026_10_16": rows[contract],
            "multiplier": float(spec["multiplier"]),
            "margin_rate": float(spec["margin_rate"]),
            "preview_lots": lots,
            "preview_notional": abs(lots) * rows[contract] * float(spec["multiplier"]),
        }
    gross = sum(v["preview_notional"] for v in preview.values())
    margin = sum(
        v["preview_notional"] * v["margin_rate"] for v in preview.values()
    )
    zero = sorted(k for k, v in preview.items() if v["preview_lots"] == 0)
    return {
        "note": (
            "PREVIEW ONLY: lots as if built at 2026-10-16 settlement prices; the "
            "real build uses D-day 09:30 first-minute-bar open prices"
        ),
        "nav_cny": nav,
        "nonzero_contracts": len(preview) - len(zero),
        "zero_lot_contracts": zero,
        "preview_gross_notional": gross,
        "preview_margin": margin,
        "preview_margin_ratio": margin / nav,
        "contracts": preview,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-preview", action="store_true")
    args = parser.parse_args(argv)

    base = json.loads(BASE_CANDIDATE.read_text())
    config = json.loads(json.dumps(base["account_config"]))
    if config["fee_model"]["version"] != "contract-fees-v2":
        raise ValueError("base candidate must carry contract-fees-v2")
    checks = {
        "base_instruments": len(config["instruments"]),
        "base_rules": sum(len(v) for v in config["fee_model"]["rules"].values()),
    }
    if set(config["instruments"]) != set(config["fee_model"]["rules"]):
        raise ValueError("instrument/rule coverage mismatch")

    config.update(
        {
            "account_id": ACCOUNT_ID,
            "account_version": ACCOUNT_VERSION,
            "source_version": SOURCE_VERSION,
            "label": LABEL,
            "synthetic": False,
            "initial_at": None,
            "lot_rule": "nearest_integer_ties_even",
            "fee_description": FEE_DESCRIPTION,
        }
    )
    config["fee_model"]["policy_source"] = (
        "M02 user rulings 2026-10-16 (commission x1.1 no minimum, 4bp slippage, "
        "exchange margin +2pp; cost-basis slippage mechanism per user ruling "
        "2026-10-18) applied to exchange settlement parameters frozen 2026-10-15"
    )
    config["fee_model"]["excluded_costs"] = [
        "exchange delivery/settlement dues outside trade fees",
        "futures transfer fees and any broker surcharges beyond x1.1",
    ]
    czce_filled = _apply_czce_unit_ruling(config["fee_model"]["rules"])
    _fill_broker_and_slippage(config["fee_model"]["rules"])
    _mark_up_margin(config["instruments"])

    calendar, calendar_receipt = _build_calendar()
    from forward_account.contracts import digest

    config["calendar_id"] = calendar["calendar_id"]
    config["calendar_sha256"] = digest(calendar)

    csv_sha, metadata_sha = _sha(FROZEN_TARGETS_CSV), _sha(FROZEN_METADATA)
    if csv_sha != "3ce0759efb9c4a3f2ae105fc864768ab50eeccd0d107ab0b9b9ca260e909f248":
        raise ValueError("frozen targets.csv hash changed")
    if metadata_sha != "fe2730f60fb5b2db2a6afb3189187009bca3917a05ffd00d9baf5470660af657":
        raise ValueError("frozen source-parameters.json hash changed")
    config["target_policy"] = {
        "mode": "pinned_batch",
        "pinned": {
            "csv_sha256": csv_sha,
            "metadata_sha256": metadata_sha,
            "row_count": 67,
            "signal_date": SIGNAL_DATE,
            "available_at": AVAILABLE_AT,
            "source_version": SOURCE_VERSION,
            "synthetic": False,
        },
    }

    validate_config(config, calendar)
    # The advance-time read must succeed against the exact frozen files.
    receipt = dict(config["target_policy"]["pinned"])
    target = read_targets(config, FROZEN_TARGETS_CSV, FROZEN_METADATA, receipt)
    if len(target["weights"]) != 67 or target["signal_date"] != SIGNAL_DATE:
        raise ValueError("frozen batch round-trip failed")

    out = REQUEST_DIR / "build"
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "account.config.json", config)
    _write(out / "calendar.json", calendar)
    checks.update(
        {
            "validate_config": "passed",
            "frozen_batch_roundtrip": "67 rows",
            "calendar_intervals": len(calendar["intervals"]),
            "broker_total_ratio": "x1.1 per action (0.1 x exchange leg)",
            "margin_markup": "+0.02 on every instrument",
            "slippage": "4 PER_TEN_THOUSAND cost-based per contract",
        }
    )
    manifest = {
        "generated_at": calendar_receipt["queried_at"],
        "inputs": {
            "base_candidate": str(BASE_CANDIDATE.relative_to(REPO)),
            "base_candidate_sha256": _sha(BASE_CANDIDATE),
            "frozen_targets_csv": str(FROZEN_TARGETS_CSV.relative_to(REPO)),
            "frozen_targets_csv_sha256": csv_sha,
            "frozen_metadata": str(FROZEN_METADATA.relative_to(REPO)),
            "frozen_metadata_sha256": metadata_sha,
            "available_at_source": (
                "docs/requests/2026-10-16-m02-real-inputs/native-target-inventory.json"
            ),
        },
        "transformations": [
            "identity: M02 observation account, real (non-synthetic), initial_at=null",
            "instruments: margin_rate += 0.02, valid_to -> 2026-12-31",
            "fee rules: broker = 0.1 x exchange per action (x1.1 total), "
            "slippage = 4bp PER_TEN_THOUSAND cost-based, valid_to -> 2026-12-31",
            "lot_rule = nearest_integer_ties_even (AU2612 -> 0 lots accepted, ruling 8a)",
            "target_policy pins the frozen 2026-10-15 batch (quantity-hold after D)",
            "calendar from data_manager.business_calendar CN, 09:30 execution / 15:00 settle",
        ],
        "calendar_query_receipt": calendar_receipt,
        "checks": checks,
    }
    _write(out / "build-manifest.json", manifest)
    if not args.skip_preview:
        _write(out / "integer-preview.json", _integer_preview(config))
    print(f"M02 build artifacts: {out}")
    print(json.dumps(checks, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
