"""pinned_batch target policy: one frozen batch builds once, quantities hold."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from forward_account import advance, initialize, read_state
from forward_account.adapters import read_targets
from forward_account.contracts import SOURCE_RULES, digest
from forward_account.fees import ACTIONS


def rate(value, unit="YUAN_PER_LOT"):
    return {"value": value, "unit": unit}


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def fee_model():
    """contract-fees-v2 with broker = 0.1 x exchange and 4bp slippage."""
    def rule(exchange, broker):
        return {
            "rule_id": f"SYNTHETIC-pinned-{exchange}",
            "valid_from": "2026-10-12",
            "valid_to": "2026-10-14",
            "exchange_source": "SYNTHETIC pinned-batch test",
            "broker_source": "SYNTHETIC 0.1 x exchange (x1.1 total)",
            "slippage_source": "SYNTHETIC 4bp cost-based",
            "exchange": dict(zip(ACTIONS, map(rate, (exchange,) * 4))),
            "broker": dict(zip(ACTIONS, map(rate, (broker,) * 4))),
            "slippage": rate(4, "PER_TEN_THOUSAND"),
        }

    return {
        "version": "contract-fees-v2",
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
        "policy_source": "SYNTHETIC pinned-batch test",
        "excluded_costs": ["SYNTHETIC no delivery/exchange dues"],
        "dce_intraday_matching": {
            "method": "prior_open_fifo_same_side",
            "components": ["exchange"],
            "source": "SYNTHETIC pinned-batch test",
        },
        "rules": {"A.TEST": [rule(3, 0.3)], "B.TEST": [rule(3, 0.3)]},
    }


def pinned_case(tmp_path, *, weights, lot_rule="fractional"):
    """Two-interval world whose target batch predates the calendar itself."""
    root = tmp_path / "inputs"
    root.mkdir(parents=True, exist_ok=True)
    calendar = {
        "calendar_id": "synthetic-pinned-v1",
        "timezone": "Asia/Shanghai",
        "source": "SYNTHETIC explicit test sessions, not an exchange calendar",
        "complete": True,
        "coverage_start": "2026-10-13",
        "coverage_end": "2026-10-14",
        "intervals": [
            {
                "trade_date": f"2026-10-{day}",
                "signal_date": f"2026-10-{day - 1}",
                "start_at": f"2026-10-{day - 1}T15:00:00+08:00",
                "execution_at": f"2026-10-{day}T09:00:00+08:00",
                "end_at": f"2026-10-{day}T15:00:00+08:00",
            }
            for day in (13, 14)
        ],
    }
    target_path = root / "targets-frozen.csv"
    metadata = root / "source-parameters.json"
    _write(metadata, dict(SOURCE_RULES["citic027_r150_t1"][1]))
    with target_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["signal_date", "contract", "target_weight"]
        )
        writer.writeheader()
        for contract, weight in weights.items():
            # Signal predates the calendar's first signal day, as in M02 where
            # the frozen 2026-10-15 batch must drive the 2026-10-20 build.
            writer.writerow(
                {
                    "signal_date": "2026-10-10",
                    "contract": contract,
                    "target_weight": weight,
                }
            )
    config = {
        "account_id": "synthetic-pinned",
        "account_version": "synthetic-pinned-v1",
        "adapter": "citic027_r150_t1",
        "source_version": "SYNTHETIC-pinned-source-v1",
        "source_parameters": dict(SOURCE_RULES["citic027_r150_t1"][1]),
        "label": "SYNTHETIC pinned batch",
        "mode": "paper",
        "currency": "CNY",
        "synthetic": True,
        "initial_at": "2026-10-12T15:00:00+08:00",
        "initial_capital": 10000.0,
        "initial_unit_nav": 1.0,
        "fee_model": fee_model(),
        "fee_description": "SYNTHETIC exchange x1.1 + 4bp slippage cost",
        "lot_rule": lot_rule,
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
        "calendar_id": calendar["calendar_id"],
        "calendar_sha256": digest(calendar),
        "instruments": {
            name: {
                "asset_type": "linear_futures",
                "currency": "CNY",
                "multiplier": mult,
                "margin_rate": 0.1,
                "valid_from": "2026-10-12",
                "valid_to": "2026-10-14",
                "source": "SYNTHETIC test specification",
            }
            for name, mult in (("A.TEST", 10), ("B.TEST", 5))
        },
        "target_policy": {
            "mode": "pinned_batch",
            "pinned": {
                "csv_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
                "row_count": len(weights),
                "signal_date": "2026-10-10",
                "available_at": "2026-10-10T20:00:00+08:00",
                "source_version": "SYNTHETIC-pinned-source-v1",
                "synthetic": True,
            },
        },
    }
    _write(root / "config.json", config)
    _write(root / "calendar.json", calendar)
    db = tmp_path / "state.sqlite"
    initialize(db, config, calendar)
    return db, config, root, target_path, metadata


def _packet(config, target_path, metadata, day, prices, *, execution=None):
    receipt = {
        "source_version": config["source_version"],
        "synthetic": True,
        "available_at": config["target_policy"]["pinned"]["available_at"],
        "row_count": config["target_policy"]["pinned"]["row_count"],
        "signal_date": config["target_policy"]["pinned"]["signal_date"],
        "csv_sha256": hashlib.sha256(Path(target_path).read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(Path(metadata).read_bytes()).hexdigest(),
    }
    packet = {
        "trade_date": f"2026-10-{day}",
        "target": read_targets(config, target_path, metadata, receipt),
    }
    if execution is not None:
        packet["execution"] = {
            "kind": "execution",
            "at": f"2026-10-{day}T09:00:00+08:00",
            "available_at": f"2026-10-{day}T09:10:00+08:00",
            "source": "SYNTHETIC hand-calculated prices",
            "synthetic": True,
            "prices": execution,
            "tradable": dict.fromkeys(execution, True),
        }
    packet["settlement"] = {
        "kind": "settlement",
        "at": f"2026-10-{day}T15:00:00+08:00",
        "available_at": f"2026-10-{day}T15:10:00+08:00",
        "source": "SYNTHETIC hand-calculated prices",
        "synthetic": True,
        "prices": prices,
    }
    return packet


def test_pinned_batch_builds_once_then_holds_without_execution_quote(tmp_path):
    db, config, root, target_path, metadata = pinned_case(
        tmp_path, weights={"A.TEST": 1.5, "B.TEST": -0.5}
    )
    first = advance(
        db,
        config["account_id"],
        _packet(
            config,
            target_path,
            metadata,
            13,
            {"A.TEST": 110, "B.TEST": 190},
            execution={"A.TEST": 100, "B.TEST": 200},
        ),
    )
    assert first["positions"] == pytest.approx({"A.TEST": 15, "B.TEST": -5})
    # A: 15 lots x (3 + 0.3) + 4bp x 15000 = 49.5 + 6.0
    # B: 5 lots x (3 + 0.3) + 4bp x 5000 = 16.5 + 2.0
    assert first["fees"] == pytest.approx(74.0)
    assert first["nav"] == pytest.approx(10000 - 74.0 + 1500 + 250)
    assert first["audit"]["target_policy"] == "pinned_batch"

    # Hold day: no execution quote at all; settle-to-settle mark only.
    second = advance(
        db,
        config["account_id"],
        _packet(
            config, target_path, metadata, 14, {"A.TEST": 112, "B.TEST": 200}
        ),
    )
    assert second["positions"] == first["positions"]
    assert second["fees"] == 0.0
    assert second["pnl"] == pytest.approx(15 * 10 * 2 - 5 * 5 * 10)
    assert second["nav"] == pytest.approx(first["nav"] + 50)
    assert second["row"]["daily_return"] == pytest.approx(
        second["nav"] / first["nav"] - 1
    )
    assert second["audit"]["trades"] == []
    assert second["audit"]["execution_margin"] is None
    assert second["audit"]["gap_pnl"] == 0.0
    assert second["position_lots"] == first["position_lots"]
    assert second["fee_matching_state"] == first["fee_matching_state"]


def test_pinned_batch_rejects_any_other_target(tmp_path):
    db, config, root, target_path, metadata = pinned_case(
        tmp_path, weights={"A.TEST": 1.5, "B.TEST": -0.5}
    )
    advance(
        db,
        config["account_id"],
        _packet(
            config,
            target_path,
            metadata,
            13,
            {"A.TEST": 110, "B.TEST": 190},
            execution={"A.TEST": 100, "B.TEST": 200},
        ),
    )
    before = read_state(db, config["account_id"])
    other = tmp_path / "targets-other.csv"
    other.write_text(
        "signal_date,contract,target_weight\n"
        "2026-10-10,A.TEST,0.6\n"
        "2026-10-10,B.TEST,-0.4\n"
    )
    with pytest.raises(ValueError, match="pinned"):
        advance(
            db,
            config["account_id"],
            _packet(
                config, other, metadata, 14, {"A.TEST": 112, "B.TEST": 200}
            ),
        )
    assert read_state(db, config["account_id"]) == before


def test_pinned_hold_day_rejects_stray_execution_quote(tmp_path):
    db, config, root, target_path, metadata = pinned_case(
        tmp_path, weights={"A.TEST": 1.5, "B.TEST": -0.5}
    )
    advance(
        db,
        config["account_id"],
        _packet(
            config,
            target_path,
            metadata,
            13,
            {"A.TEST": 110, "B.TEST": 190},
            execution={"A.TEST": 100, "B.TEST": 200},
        ),
    )
    with pytest.raises(ValueError, match="unsupported fields"):
        advance(
            db,
            config["account_id"],
            _packet(
                config,
                target_path,
                metadata,
                14,
                {"A.TEST": 112, "B.TEST": 200},
                execution={"A.TEST": 111, "B.TEST": 195},
            ),
        )


def test_pinned_all_zero_build_retries_with_execution_quote(tmp_path):
    # AU2612-style: integer lots round every weight to zero, so no position is
    # ever held and each later interval must re-attempt the build (and therefore
    # still requires an execution quote).
    db, config, root, target_path, metadata = pinned_case(
        tmp_path, weights={"A.TEST": 0.0001, "B.TEST": -0.0001},
        lot_rule="nearest_integer_ties_even",
    )
    first = advance(
        db,
        config["account_id"],
        _packet(
            config,
            target_path,
            metadata,
            13,
            {"A.TEST": 110, "B.TEST": 190},
            execution={"A.TEST": 100, "B.TEST": 200},
        ),
    )
    assert first["positions"] == {}
    with pytest.raises(ValueError, match="missing required fields"):
        advance(
            db,
            config["account_id"],
            _packet(
                config, target_path, metadata, 14, {"A.TEST": 112, "B.TEST": 200}
            ),
        )
    second = advance(
        db,
        config["account_id"],
        _packet(
            config,
            target_path,
            metadata,
            14,
            {"A.TEST": 112, "B.TEST": 200},
            execution={"A.TEST": 111, "B.TEST": 195},
        ),
    )
    assert second["positions"] == {}
    assert second["fees"] == 0.0
    assert second["row"]["daily_return"] == 0.0
