"""Offline checks of the additive disabled candidate and supplied source bytes."""

from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "2026-10-16-m02-real-inputs"


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    checks = []

    def check(ok, label):
        if not ok:
            raise AssertionError(label)
        checks.append(label)

    for source in load(ROOT / "source-manifest.json"):
        check(
            sha(ROOT / source["path"]) == source["sha256"],
            "source SHA " + source["path"],
        )
    for source in load(ROOT / "parent-manifest.json"):
        check(
            sha(ROOT.parent / source["path"]) == source["sha256"],
            "frozen parent SHA " + source["path"],
        )
    original = load(PRIOR / "account.disabled.json")
    candidate = load(ROOT / "account.disabled.json")
    decisions = load(ROOT / "unit-decisions.json")
    gaps = load(ROOT / "remaining-gaps.json")
    evidence = load(PRIOR / "contract-evidence.json")
    old = original["account_config"]
    new = candidate["account_config"]
    check(
        all(candidate[k] is False for k in ("enabled", "runtime_ready", "executable")),
        "candidate disabled",
    )
    check(candidate["execution_day"] is None, "no future execution day invented")
    check(
        len(decisions) == 11 and len(gaps["remaining_fee_units"]) == 13,
        "11 user units resolved; 13 unresolved",
    )
    check(
        len(new["instruments"]) == 67 and new["instruments"] == old["instruments"],
        "67 multipliers/margins/dimensions/windows unchanged",
    )
    check(
        {k: v for k, v in new.items() if k != "fee_model"}
        == {k: v for k, v in old.items() if k != "fee_model"},
        "no account identity/capital/execution parameters changed",
    )
    check(
        new["fee_model"]["version"] == "contract-fees-v2"
        and new["fee_model"]["dce_intraday_matching"]["components"] == ["exchange"],
        "explicit DCE exchange-only v2 selection",
    )
    check(
        new["fee_model"]["policy_source"] is None,
        "unapproved global/non-DCE policy retained",
    )
    for contract, schedule in new["fee_model"]["rules"].items():
        prior = old["fee_model"]["rules"][contract]
        check(len(schedule) == len(prior) == 1, contract + " unchanged schedule length")
        rule, before = schedule[0], prior[0]
        check(
            rule["valid_from"] == rule["valid_to"] == "2026-10-15",
            contract + " no future validity extension",
        )
        check(
            rule["broker"] is None and rule["slippage"] is None,
            contract + " unknown costs not zeroed",
        )
        if contract in decisions:
            check(
                decisions[contract]["authority"] == "user_decision_2026-10-16"
                and not decisions[contract]["independently_exchange_certified"],
                contract + " user authority distinct from official proof",
            )
            check(
                decisions[contract]["decision_sha256"] == sha(ROOT / "request.md"),
                contract + " user decision bound to request",
            )
            for action, rate in rule["exchange"].items():
                check(
                    rate["unit"] == "PER_TEN_THOUSAND"
                    and rate["value"] == before["exchange"][action]["value"],
                    contract + " unchanged value and explicit unit " + action,
                )
            check(
                decisions[contract]["snapshot_source_versions"]
                == evidence[contract]["source_versions"],
                contract + " source SHA versions retained",
            )
        else:
            check(rule == before, contract + " entire fee rule unchanged")
    check(
        len(gaps["remaining_independent_dimensions"]) == 19
        and all(c.endswith(".DCE") for c in gaps["remaining_independent_dimensions"]),
        "19 independent dimension gaps, not missing numeric multipliers",
    )
    dce = load(ROOT / "data/contract_info.json")
    check(
        len(dce) == 19 and {r["交易日期"] for r in dce} == {"2026-10-15"},
        "19 DCE public rows only October 15",
    )
    mapping = {
        "开仓手续费": "open",
        "平仓手续费": "close",
        "日内开仓手续费": "intraday_open",
        "日内平仓手续费": "close_today",
    }
    for row in dce:
        contract = row["合约代码"].upper() + ".DCE"
        check(
            Decimal(str(row["合约乘数"]))
            == Decimal(str(new["instruments"][contract]["multiplier"])),
            contract + " public numeric multiplier agrees",
        )
        check(
            all(
                Decimal(row[field])
                == Decimal(
                    str(
                        new["fee_model"]["rules"][contract][0]["exchange"][action][
                            "value"
                        ]
                    )
                )
                for field, action in mapping.items()
            ),
            contract + " public four numeric fees agree",
        )
    czce = load(ROOT / "data-czce/contract_info.json")
    check(
        len(czce) == 19 and all(Decimal(r["日内开仓手续费"]) == -1 for r in czce),
        "19 CZCE unexplained -1 placeholders retained only as evidence",
    )
    for contract in evidence:
        if contract.endswith(".CZC"):
            rule = new["fee_model"]["rules"][contract][0]
            check(
                rule["exchange"]["open"]["value"]
                == rule["exchange"]["intraday_open"]["value"],
                contract + " original regular-fee projection retained",
            )
    checks_path = ROOT / "candidate-validation.json"
    checks_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "checks": len(checks),
                "checks_completed": checks,
                "network_calls": 0,
                "database_calls": 0,
                "account_initializations": 0,
                "strategy_runs": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": len(checks),
                "scope": "offline disabled candidate/source invariants",
            }
        )
    )


if __name__ == "__main__":
    main()
