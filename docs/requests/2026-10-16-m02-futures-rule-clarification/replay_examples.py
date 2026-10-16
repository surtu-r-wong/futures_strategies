"""Replay supplied FAQ examples in memory, without initializing any ledger."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO))

from forward_account.contracts import digest  # noqa: E402
from forward_account.fee_demo import synthetic_model  # noqa: E402
from forward_account.fees import (  # noqa: E402
    account_trades,
    account_trades_with_matching,
    new_matching_state,
)


def main():
    contract = "EG2611.DCE"
    base = synthetic_model([contract])
    rule = base["rules"][contract][0]
    rule.update(valid_from="2026-10-15", valid_to="2026-10-16")
    rule["exchange_source"] = (
        "User-supplied DCE FAQ example rates, synthetic application date"
    )
    rule["broker_source"] = "SYNTHETIC explicit zero broker fee"
    for action in rule["exchange"]:
        rule["exchange"][action] = {
            "value": 1.5 if action in ("close_today", "intraday_open") else 3,
            "unit": "YUAN_PER_LOT",
        }
        rule["broker"][action] = {"value": 0, "unit": "YUAN_PER_LOT"}
    v2 = deepcopy(base)
    v2["version"] = "contract-fees-v2"
    v2["dce_intraday_matching"] = {
        "method": "prior_open_fifo_same_side",
        "components": ["exchange"],
        "source": "DCE FAQ quantity principle; same-side FIFO and entry-notional are model choices",
    }
    specs = {contract: {"multiplier": 10}}
    cases = []
    for sign in (1, -1):
        for case, (sequence, expected) in enumerate(
            (
                ([10, -3, 20, -30], 99),
                ([-10, 3, -20, 30], 180),
                ([-10, 3, 20, -30], 120),
            ),
            1,
        ):
            totals, end_lots, matched = {}, {}, 0
            for model in (base, v2):
                positions = {contract: sign * 100}
                lots = {
                    contract: [
                        {
                            "lot_id": "SYNTHETIC-overnight",
                            "quantity": sign * 100,
                            "opened_trade_date": "2026-10-15",
                            "opened_at": "2026-10-15T09:00:00+08:00",
                            "entry_price": 100,
                            "multiplier": 10,
                            "opening_rule_sha256": digest(rule),
                            "source": "SYNTHETIC overnight holding; opening fees excluded from FAQ day",
                        }
                    ]
                }
                state = new_matching_state(model) if model is v2 else None
                fills = []
                for i, delta in enumerate(sequence):
                    target = {contract: positions[contract] + sign * delta}
                    args = (
                        model,
                        positions,
                        lots,
                        target,
                        {contract: 100},
                        specs,
                        "2026-10-16",
                        f"2026-10-16T09:{i:02}:00+08:00",
                        "SYNTHETIC ordered fill",
                    )
                    if model is v2:
                        trades, lots, state = account_trades_with_matching(
                            *args, matching_state=state
                        )
                    else:
                        trades, lots = account_trades(*args)
                    fills.extend(trades)
                    positions = target
                totals[model["version"]] = sum(t["fee"] for t in fills)
                end_lots[model["version"]] = lots
                if model is v2:
                    matched = sum(
                        abs(t["quantity_change"])
                        for t in fills
                        if t["exchange_fee_action"] == "close_today"
                    )
            assert totals["contract-fees-v1"] == 189
            assert totals["contract-fees-v2"] == expected
            assert end_lots["contract-fees-v1"] == end_lots["contract-fees-v2"]
            cases.append(
                {
                    "scenario": case,
                    "side": "long" if sign > 0 else "short",
                    "sequence": sequence,
                    "overnight_quantity": sign * 100,
                    "expected_fee": expected,
                    "actual_fee": totals,
                    "intraday_two_leg_quantity": 2 * matched,
                    "FIFO_inventory_unchanged": True,
                }
            )
    report = {
        "status": "passed",
        "synthetic": True,
        "cases": cases,
        "source_pdf_sha256": hashlib.sha256(
            (ROOT / "sources/dce-intraday-fee-faq.pdf").read_bytes()
        ).hexdigest(),
        "code_sha256": {
            name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in (
                "forward_account/fees.py",
                "forward_account/engine.py",
                "forward_account/store.py",
                "forward_account/contracts.py",
            )
        },
        "database_calls": 0,
        "network_calls": 0,
        "account_initializations": 0,
        "strategy_runs": 0,
    }
    (ROOT / "v2-reproduction.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "cases": len(cases),
                "v2": [99, 180, 120],
                "v1": [189, 189, 189],
            }
        )
    )


if __name__ == "__main__":
    main()
