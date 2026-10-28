"""Build the M02 v2 (daily-follow) account config + calendar.

    .venv/bin/python scripts/prepare_m02_v2.py --first-trade-date 2026-11-16 \\
        --output-dir docs/requests/2026-10-28-m02-futures-daily-follow/build/production
    .venv/bin/python scripts/prepare_m02_v2.py --first-trade-date 2026-11-09 \\
        --variant shadow --output-dir docs/requests/2026-10-28-m02-futures-daily-follow/build/shadow

`shadow` (R8, 11-09..11-13) and `rehearsal` (pre-shadow real-data nights)
are separate accounts in separate stores, never exported to the feed.

Same cost model as v1 (user rulings 2026-10-16: exchange fee x1.1 no minimum,
4bp single-side slippage as CNY cost, exchange margin +2pp, no interest, no
financing, 09:30 execution, daily settlement marks). No target_policy: every
interval needs that day's native target (engine daily path) and the
daily_policy fallbacks apply. Instruments start empty; each contract is
registered from its signal day's exchange parameters the first night it is
needed (forward_inputs.m02_v2_specs).

The calendar is data_manager.business_calendar CN (read-only) from the first
trade date through the last date that source covers. Later sessions are
appended with extend_references once the exchanges publish them.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account.contracts import (  # noqa: E402
    DAILY_POLICY,
    SOURCE_RULES,
    digest,
    now_iso,
    validate_config,
)
from forward_inputs import m02_daily  # noqa: E402
from forward_inputs import m02_v2_specs as specs  # noqa: E402
from forward_inputs.m01 import ReadOnlySource  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
V1_CONFIG = REPO / "docs/requests/2026-10-17-m02-initialization/build/account.config.json"
ADAPTER = "citic027_r150_t1"
FEE_DESCRIPTION = (
    "exchange fee x1.1 (broker +10% of exchange rate, no minimum), single-side 4bp "
    "slippage as CNY cost, exchange margin +2pp; each contract's exchange values "
    "registered from its signal day's settlement parameters and frozen for its life"
)


def calendar_from(days, prior_day, calendar_id, receipt):
    intervals, previous = [], None
    for d in days:
        intervals.append(
            {
                "trade_date": d,
                "signal_date": prior_day if previous is None else previous["trade_date"],
                "start_at": f"{prior_day}T15:00:00+08:00" if previous is None else previous["end_at"],
                "execution_at": f"{d}T09:30:00+08:00",
                "end_at": f"{d}T15:00:00+08:00",
            }
        )
        previous = intervals[-1]
    return {
        "calendar_id": calendar_id,
        "timezone": "Asia/Shanghai",
        "source": (
            "data_manager.business_calendar CN (read-only, queried "
            f"{receipt['queried_at']}, {receipt['row_count']} rows); day session "
            "09:30 execution, 15:00 settlement"
        ),
        "complete": True,
        "coverage_start": days[0],
        "coverage_end": days[-1],
        "intervals": intervals,
    }


def business_days(first):
    with ReadOnlySource() as source:
        frame = source.query(
            "SELECT calendar_date FROM data_manager.business_calendar "
            "WHERE calendar_id = 'CN' AND is_business_day "
            "AND calendar_date >= (%(first)s::date - 20) ORDER BY calendar_date",
            {"first": first},
        )
        queries = source.queries
    days = [d.isoformat() for d in frame["calendar_date"]]
    if first not in days:
        raise ValueError(f"{first} is not a CN business day")
    prior = [d for d in days if d < first][-1]
    trade = [d for d in days if d >= first]
    receipt = {"queried_at": queries[-1]["observed_at"], "row_count": len(days), "query": queries[-1]}
    return trade, prior, receipt


def build(first, *, variant):
    v1 = json.loads(V1_CONFIG.read_text())
    trade, prior, receipt = business_days(first)
    suffix = "" if variant == "production" else f"-{variant}"
    calendar = calendar_from(
        trade, prior, f"m02-futures-v2{suffix}-cn-{trade[0]}_{trade[-1]}", receipt
    )
    fee_model = {k: v for k, v in v1["fee_model"].items() if k != "rules"}
    fee_model["policy_source"] = (
        "M02 user rulings 2026-10-16 (commission x1.1 no minimum, 4bp slippage as CNY "
        "cost, exchange margin +2pp) applied to each contract's signal-day exchange "
        "settlement parameters; v2 daily-follow ruling 2026-10-28"
    )
    fee_model["rules"] = {}
    config = {
        "account_id": f"m02-futures-observation-v2{suffix}",
        "account_version": f"m02-futures-observation-v2{suffix}",
        "adapter": ADAPTER,
        "source_version": m02_daily.source_version(),
        "source_parameters": dict(SOURCE_RULES[ADAPTER][1]),
        "label": (
            ("" if variant == "production" else f"{variant.upper()} (not published) / ")
            + "M02 futures paper observation v2: 027 native daily targets, 09:30 "
            "first-minute execution, daily settlement marks"
        ),
        "mode": "paper",
        "currency": "CNY",
        "synthetic": False,
        "initial_at": None,
        "initial_capital": 100000000,
        "initial_unit_nav": 1,
        "fee_model": fee_model,
        "fee_description": FEE_DESCRIPTION,
        "lot_rule": "nearest_integer_ties_even",
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
        "calendar_id": calendar["calendar_id"],
        "calendar_sha256": digest(calendar),
        "instruments": {},
        "daily_policy": dict(DAILY_POLICY),
    }
    validate_config(config, calendar)
    return config, calendar, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-trade-date", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--variant", choices=("production", "shadow", "rehearsal"), default="production"
    )
    args = parser.parse_args(argv)
    config, calendar, receipt = build(args.first_trade_date, variant=args.variant)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, value in (("account.config.json", config), ("calendar.json", calendar)):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    manifest = {
        "built_at": now_iso(),
        "config_sha256": digest(config),
        "calendar_sha256": digest(calendar),
        "source_version": config["source_version"],
        "weight_files": m02_daily.weight_tree(),
        "spec_table_sha256": specs.load_table()["table_sha256"],
        "first_interval": calendar["intervals"][0],
        "calendar_intervals": len(calendar["intervals"]),
        "calendar_query": receipt,
        "genesis_deadline": calendar["intervals"][0]["start_at"],
    }
    (out / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({k: manifest[k] for k in ("config_sha256", "source_version", "genesis_deadline", "calendar_intervals")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
