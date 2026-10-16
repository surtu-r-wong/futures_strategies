"""Offline additive candidate; never touches the frozen delivery or a database."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "2026-10-16-m02-real-inputs"
RESOLVED = {
    "I2701.DCE",
    "J2701.DCE",
    "JD2612.DCE",
    "JM2701.DCE",
    "LH2701.DCE",
    "MA2701.CZC",
    "PR2612.CZC",
    "PX2612.CZC",
    "SA2701.CZC",
    "SH2701.CZC",
    "UR2701.CZC",
}


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main():
    original = load(PRIOR / "account.disabled.json")
    evidence = load(PRIOR / "contract-evidence.json")
    candidate = deepcopy(original)
    model = candidate["account_config"]["fee_model"]
    pdf_sha = sha(ROOT / "sources/dce-intraday-fee-faq.pdf")
    model["version"] = "contract-fees-v2"
    model["dce_intraday_matching"] = {
        "method": "prior_open_fifo_same_side",
        "components": ["exchange"],
        "source": (
            "user-supplied DCE FAQ published 2021-01-21, printed 2026-10-16; "
            f"sources/dce-intraday-fee-faq.pdf sha256={pdf_sha}; "
            "official prior-open quantity principle; FIFO across entry prices, "
            "same-side net-lot attribution and entry-notional adjustments are explicit model choices; "
            "one account represents one trading code; future applicability not approved"
        ),
    }
    decisions = {}
    missing = []
    for contract, row in evidence.items():
        raw = row["settlement_rule"]
        if raw["fee_scale"] is not None:
            continue
        if contract not in RESOLVED:
            missing.append(contract)
            continue
        assert raw["fee_style"] == "比例值"
        schedule = model["rules"][contract]
        for rule in schedule:
            for rate in rule["exchange"].values():
                assert rate["unit"] is None
                rate["unit"] = "PER_TEN_THOUSAND"
            rule["exchange_source"] += (
                "; unit: user decision 2026-10-16 (request.md); numeric snapshot/source SHA unchanged"
            )
        decisions[contract] = {
            "previous_unit": None,
            "unit": "PER_TEN_THOUSAND",
            "authority": "user_decision_2026-10-16",
            "decision_quote": "比例的单位均为万分，1就代表成交额的万分之一",
            "decision_path": "request.md",
            "decision_sha256": sha(ROOT / "request.md"),
            "independently_exchange_certified": False,
            "raw_fee_style": raw["fee_style"],
            "raw_values": {
                k: raw[k]
                for k in (
                    "open_fee",
                    "offset_fee",
                    "short_open_fee",
                    "short_offset_fee",
                )
            },
            "fee_projection": (
                "DCE exchange_data.futures_settle_param four original fee columns"
                if contract.endswith(".DCE")
                else "CZCE exchange_data original regular fee -> open/close/intraday_open; original intraday fee -> close_today; public -1 sentinel not consumed"
            ),
            "snapshot_source_versions": row["source_versions"],
            "economic_validity_extended": False,
        }
    assert set(decisions) == RESOLVED and len(missing) == 13
    candidate["status"] = "disabled_user_unit_delta_and_DCE_matching_v2"
    candidate["evidence_matrix"] = (
        "../2026-10-16-m02-real-inputs/contract-evidence.json"
    )
    candidate["unit_decisions"] = "unit-decisions.json"
    candidate["remaining_gaps"] = "remaining-gaps.json"
    candidate["frozen_parent_sha256"] = sha(PRIOR / "account.disabled.json")
    write("account.disabled.json", candidate)
    write("unit-decisions.json", decisions)
    write(
        "remaining-gaps.json",
        {
            "previous_missing_fee_units": 24,
            "units_resolved_by_user": sorted(RESOLVED),
            "remaining_fee_units": sorted(missing),
            "remaining_fee_unit_count": len(missing),
            "internal_multiplier_values_present": 67,
            "remaining_independent_dimensions": load(PRIOR / "completion-summary.json")[
                "missing_dimensions"
            ],
            "public_table_review": {
                "actual_table": "public.futures_contract_info",
                "columns": 29,
                "explicit_currency_quote_multiplier_unit_columns": False,
                "DCE_rows": 19,
                "DCE_dates_observed": ["2026-10-15"],
                "DCE_20261016_rows": 0,
                "CZCE_rows": 19,
                "CZCE_intraday_open_placeholder": "-1.0000",
                "CZCE_placeholder_semantics_confirmed": False,
                "CZCE_placeholder_used_as_rate": False,
            },
            "other_blockers_retained": [
                "future D, effective fee/margin windows and actual quotes/settlements unknown",
                "broker/slippage and non-DCE intraday policy unapproved; no zero default",
                "account identity/start/version and complete sessions not accepted",
                "five DCE declared source SHA conflicts retained without reassociation",
                "AU integer target becomes zero under frozen 100m NAV and rounding",
                "DCE matching FIFO/side/notional choices are not literal FAQ proof",
            ],
            "network_calls": 0,
            "database_calls": 0,
            "real_account_initializations": 0,
            "strategy_runs": 0,
        },
    )
    write(
        "parent-manifest.json",
        [
            {
                "path": str((PRIOR / name).relative_to(ROOT.parent)),
                "sha256": sha(PRIOR / name),
            }
            for name in (
                "account.disabled.json",
                "contract-evidence.json",
                "preparation-check.json",
                "completion-summary.json",
            )
        ],
    )


if __name__ == "__main__":
    main()
