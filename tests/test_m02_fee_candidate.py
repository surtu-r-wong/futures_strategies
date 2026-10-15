"""Local delivered evidence mapping; no database/network/strategy replay."""

import csv
import json
from pathlib import Path

import pytest

from forward_inputs.m02_fees import build, contract_key
from forward_account.fees import validate_model

ROOT = Path("docs/requests/2026-10-15-m02-account-compatibility/candidate")
TARGETS = Path(
    "docs/requests/2026-10-14-multi-asset-operational-returns/m01-offline-diagnosis-evidence/verification-02/targets.csv"
)
ACCOUNTING = ROOT.parent / "accounting-candidate-extract.json"
TEMPLATE = Path("config/forward_inputs/citic027_r150_t1.account.json")


def local_inputs(tmp_path):
    evidence = json.loads((ROOT / "contract-provenance.json").read_text())
    data = {
        "contract_info": [],
        "settlement_rules": [],
        "lifecycles": [],
        "czce_units": [],
    }
    refs = []
    for symbol, row in evidence.items():
        data["contract_info"].append(row["contract_info"])
        data["settlement_rules"].append(row["settlement_rule"])
        if row["independent_lifecycle"]:
            data["lifecycles"].append(row["independent_lifecycle"])
        if row["unit_evidence"]:
            data["czce_units"].append(row["unit_evidence"])
        refs.append(
            {
                "source_project": "futures_strategies",
                "instrument_id": symbol,
                "native_target_weight": row["native_target_weight"],
                "metadata_date": row["metadata_date"],
                "metadata_multiplier": row["contract_info"]["合约乘数"],
                "exchange_margin_long": row["settlement_rule"]["spec_buy_rate"],
                "exchange_margin_short": row["settlement_rule"]["spec_sell_rate"],
                "unit_status": row["currency_quote_unit_status"],
                "internal_expiry_date": row["internal_expiry_reference_only"],
            }
        )
    raw = tmp_path / "local-evidence.json"
    raw.write_text(
        json.dumps(
            {
                "captured_at": "SYNTHETIC-replay-of-delivered-extract",
                "stages": {"data": {"results": data}},
            }
        )
    )
    csv_path = tmp_path / "reference.csv"
    with csv_path.open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(refs[0]))
        writer.writeheader()
        writer.writerows(refs)
    return raw, csv_path


def test_full_candidate_mapping_preserves_weights_and_gaps(tmp_path, monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("offline candidate must not access network")

    monkeypatch.setattr(socket, "socket", no_network)
    raw, refs = local_inputs(tmp_path)
    config, rows, manifest = build(TARGETS, refs, raw, TEMPLATE, ACCOUNTING)
    assert len(config["instruments"]) == len(rows) == 67
    assert manifest["fee_unit_counts"] == {
        "YUAN_PER_LOT": 22,
        "PERMILLE": 16,
        "PER_TEN_THOUSAND": 5,
    }
    assert len(manifest["unknown_fee_units"]) == 24
    assert len(manifest["unapproved_quote_currency_assumptions"]) == 48
    assert manifest["missing_independent_lifecycle"] == ["EC2611.INE"]
    assert config["instruments"]["EC2611.INE"]["multiplier"] == 50
    assert float(rows["PB2612.SHF"]["native_target_weight"]) == 0
    assert config["account_version"] is None and config["initial_capital"] == 100000000
    assert config["lot_rule"] == "nearest_integer_ties_even"
    for spec in config["instruments"].values():
        assert spec["valid_from"] == spec["valid_to"] == "2026-10-15"
    assert contract_key("AP2701.CZC") == ("CZCE", "AP701")
    assert contract_key("EC2611.INE") == ("INE", "EC2611")
    j = config["fee_model"]["rules"]["J2701.DCE"][0]["exchange"]
    assert j["open"]["value"] == 1 and j["intraday_open"]["value"] == 1.4
    with pytest.raises(ValueError):
        validate_model(config["fee_model"], config["instruments"])


def test_candidate_rejects_weight_rescaling_and_directional_margin_conflict(tmp_path):
    raw, refs = local_inputs(tmp_path)
    text = refs.read_text()
    refs.write_text(text.replace("-0.0342696164907662", "-0.01370784659630648"))
    with pytest.raises(ValueError, match="weight"):
        build(TARGETS, refs, raw, TEMPLATE, ACCOUNTING)
    refs.write_text(text)
    evidence = json.loads(raw.read_text())
    evidence["stages"]["data"]["results"]["contract_info"][0]["空头保证金率"] = "0.5"
    raw.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="margin"):
        build(TARGETS, refs, raw, TEMPLATE, ACCOUNTING)
