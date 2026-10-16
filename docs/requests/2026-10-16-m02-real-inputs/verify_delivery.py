"""Offline source/evidence invariants; never initialize a ledger or run a strategy."""

import csv
from decimal import Decimal, ROUND_HALF_EVEN
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "2026-10-15-m02-account-compatibility"
checks = []


def load(path):
    return json.loads(path.read_text())


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    checks.append(label)


def main():
    rows = load(ROOT / "contract-evidence.json")
    prior = load(PRIOR / "candidate/contract-provenance.json")
    candidate = load(ROOT / "account.disabled.json")
    config = candidate["account_config"]
    summary = load(ROOT / "completion-summary.json")
    check(set(rows) == set(prior) and len(rows) == 67, "full 67 contracts preserved")
    check(
        candidate["enabled"] is False
        and candidate["runtime_ready"] is False
        and candidate["execution_day"] is None,
        "candidate explicitly disabled",
    )
    check(
        config["initial_capital"] == 100000000
        and config["lot_rule"] == "nearest_integer_ties_even",
        "existing capital/rounding candidate retained",
    )
    for contract, row in rows.items():
        check(
            row["native_target_weight"] == prior[contract]["native_target_weight"],
            contract + " unchanged weight",
        )
        check(
            config["instruments"][contract]["valid_from"]
            == config["instruments"][contract]["valid_to"]
            == "2026-10-15",
            contract + " no forward extension",
        )
        rule = config["fee_model"]["rules"][contract][0]
        check(
            rule["broker"] is None and rule["slippage"] is None,
            contract + " unknown costs not zeroed",
        )
    check(
        config["fee_model"]["policy_source"] is None, "intraday policy still unproved"
    )
    check(
        len(summary["missing_dimensions"]) == 19
        and all(x.endswith(".DCE") for x in summary["missing_dimensions"]),
        "19 unresolved DCE dimensions",
    )
    old_missing = [
        s for s, r in prior.items() if r["settlement_rule"]["fee_scale"] is None
    ]
    check(
        set(summary["missing_fee_units"]) == set(old_missing),
        "24 fee units unchanged, no old-notice extrapolation",
    )
    for contract in old_missing:
        check(
            all(
                x["unit"] is None
                for x in config["fee_model"]["rules"][contract][0]["exchange"].values()
            ),
            contract + " four unknown units remain null",
        )
    new = load(ROOT / "official-dimensions.json")
    check(len(new) == 29, "29 new official dimension matches")
    for contract, spec in new.items():
        check(
            Decimal(spec["multiplier"])
            == Decimal(str(config["instruments"][contract]["multiplier"])),
            contract + " multiplier dimension reconciled",
        )
        check(
            spec["economic_valid_to"] is None
            and not spec["actual_quote_source_unit_attested"],
            contract + " no future validity or quote attestation invented",
        )
    ec = rows["EC2611.INE"]["independent_lifecycle"]
    check(
        ec["listing_date"] == "2026-05-26" and ec["last_trade_date"] == "2026-11-30",
        "EC official lifecycle restored",
    )
    check(
        len([r for r in rows.values() if r["independent_lifecycle"]]) == 67,
        "all 67 independent lifecycle references present",
    )
    check(
        len(summary["DCE_source_version_conflicts"]) == 5,
        "five source-version conflicts retained",
    )
    for contract in summary["DCE_source_version_conflicts"]:
        versions = rows[contract]["source_versions"]
        check(
            versions["m01"]["declared_original_response_sha256"]
            != versions["office_20261016"]["row"]["source_sha256"],
            "distinct DCE source hashes " + contract,
        )
        check(
            versions["office_20261016"]["original_response_bytes_rehashed"] is False,
            "DB-declared SHA not raw byte claim " + contract,
        )
    for source in load(ROOT / "source-archives.json"):
        p = ROOT / source["archive_path"]
        raw = gzip.decompress(p.read_bytes())
        check(
            hashlib.sha256(raw).hexdigest() == source["raw_sha256"],
            "raw source SHA " + source["original_path"],
        )
        check(
            hashlib.sha256(p.read_bytes()).hexdigest() == source["archive_sha256"],
            "archive SHA " + source["archive_path"],
        )
    with (ROOT / "integer-preparation.csv").open() as f:
        diagnostic = list(csv.DictReader(f))
    check(len(diagnostic) == 67, "integer preparation universe complete")
    nav = Decimal("100000000")
    for r in diagnostic:
        ideal = (
            Decimal(r["native_target_weight"])
            * nav
            / (Decimal(r["reference_close"]) * Decimal(r["multiplier"]))
        )
        check(
            ideal.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
            == Decimal(r["integer_reference_lots"]),
            "integer arithmetic " + r["contract"],
        )
        check(
            r["executable"] == "False" and r["fee_cost"] == "",
            "not execution or zero fees " + r["contract"],
        )
    total = sum(Decimal(r["exchange_margin_reference"]) for r in diagnostic)
    check(total == Decimal("22048152.60000"), "independent sum of reference margin")
    check(
        next(r for r in diagnostic if r["contract"] == "PB2612.SHF")[
            "integer_reference_lots"
        ]
        == "0",
        "zero PB retained",
    )
    calendar = load(ROOT / "calendar.disabled.json")
    check(
        len(calendar["rows"]) == 91
        and len({r["date"] for r in calendar["rows"]}) == 91,
        "91 unique calendar dates",
    )
    check(
        sum(r["internal_sfe_open"] for r in calendar["rows"]) == 59,
        "59 source sfe flags",
    )
    check(
        calendar["complete"] is False and calendar["enabled"] is False,
        "calendar flags not executable sessions",
    )
    check(
        all(
            r["actual_execution"] is None and r["actual_settlement"] is None
            for r in calendar["rows"]
        ),
        "no invented future observations",
    )
    receipt = load(ROOT / "acquisition-assessment.json")
    check(
        any(
            r["content_assessment"] == "rejected_WAF_not_business_data"
            for r in receipt["attempts"]
        ),
        "WAF HTTP200 explicitly rejected",
    )
    report = {
        "status": "passed",
        "checks": len(checks),
        "scope": "local evidence/bytes/mappings/disabled status/integer reference arithmetic only",
        "network_calls": 0,
        "database_calls": 0,
        "account_initializations": 0,
        "strategy_runs": 0,
        "checks_completed": checks,
    }
    (ROOT / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "checks_completed"}))


if __name__ == "__main__":
    main()
