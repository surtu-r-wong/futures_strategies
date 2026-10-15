"""Build a BLOCKED 67-contract candidate from delivered local evidence only.

No source reads beyond the specified files, database clients, account writes,
strategy calculations or HTTP requests. Source URLs are provenance strings.
"""

import argparse
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from forward_account.contracts import SOURCE_RULES
from forward_account.fees import VERSION

SUFFIX = {"DCE": "DCE", "SHF": "SHFE", "INE": "INE", "GFE": "GFEX", "CZC": "CZCE"}
FIELDS = {
    "open": "open_fee",
    "close": "offset_fee",
    "intraday_open": "short_open_fee",
    "close_today": "short_offset_fee",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, obj):
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def contract_key(symbol):
    contract, suffix = symbol.split(".")
    exchange = SUFFIX[suffix]
    if exchange == "CZCE":
        match = re.fullmatch(r"([A-Z]+)(\d{3,4})", contract)
        if not match:
            raise ValueError("expected canonical four-digit CZCE delivery month")
        contract = match[1] + match[2][-3:]
    return exchange, contract


def indexed(rows, key):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValueError("duplicate evidence key")
        result[k] = row
    return result


def build(targets, metadata_csv, evidence_json, template, accounting_candidate):
    targets, metadata_csv, evidence_json, template = map(
        Path, (targets, metadata_csv, evidence_json, template)
    )
    with targets.open() as f:
        native = indexed(list(csv.DictReader(f)), lambda r: r["contract"])
    with metadata_csv.open() as f:
        reference = indexed(
            [
                r
                for r in csv.DictReader(f)
                if r["source_project"] == "futures_strategies"
            ],
            lambda r: r["instrument_id"],
        )
    if len(native) != 67 or set(native) != set(reference):
        raise ValueError(
            "M02 requires the complete unchanged 67-contract native universe"
        )
    evidence = json.loads(evidence_json.read_text())
    data = evidence["stages"]["data"]["results"]

    def key(row):
        return row["requested_exchange"], row["requested_contract"]

    info, fees = (indexed(data[k], key) for k in ("contract_info", "settlement_rules"))
    lifecycle = indexed(data["lifecycles"], lambda r: contract_key(r["symbol"]))
    dimensions = indexed(data["czce_units"], lambda r: r["contract_id"])
    config = json.loads(template.read_text())
    if config["source_parameters"] != SOURCE_RULES["citic027_r150_t1"][1]:
        raise ValueError("candidate must preserve the accepted native parameters")
    accounting_candidate = Path(accounting_candidate)
    pending = json.loads(accounting_candidate.read_text())
    profiles = [
        p for p in pending["profiles"] if p.get("source_variant") == "citic027_r150_t1"
    ]
    if len(profiles) != 1 or profiles[0]["accounting_model_id"] != "linear_futures_v1":
        raise ValueError("expected the consumer's existing 027 accounting candidate")
    if pending["enabled"] is not False or profiles[0]["enabled"] is not False:
        raise ValueError("M02 only consumes disabled preparation candidates")
    candidate_fields = pending["models"]["linear_futures_v1"]["candidate_fields"]
    expected_fields = {
        "mode",
        "currency",
        "synthetic",
        "initial_capital",
        "initial_unit_nav",
        "initial_at",
        "lot_rule",
        "cash_rule",
        "financing_rule",
        "execution_rule",
        "valuation_rule",
    }
    if set(candidate_fields) != expected_fields:
        raise ValueError("unsupported consumer accounting candidate fields")
    config.update(candidate_fields)
    evidence_sha = sha(evidence_json)
    del config["cost_bps"]
    config["instruments"] = {}
    config["fee_model"] = {
        "version": VERSION,
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
        "policy_source": None,
        "excluded_costs": None,
        "rules": {},
    }
    provenance, unknown, assumed, missing_lifecycle = {}, [], [], []
    for symbol, target in native.items():
        ref = reference[symbol]
        if Decimal(target["target_weight"]) != Decimal(ref["native_target_weight"]):
            raise ValueError("reference changed the native weight")
        if target["signal_date"] != "2026-10-15":
            raise ValueError("unexpected M01 signal date")
        k = contract_key(symbol)
        contract, fee = info[k], fees[k]
        if not (
            ref["metadata_date"]
            == contract["交易日期"]
            == fee["trade_date"]
            == "2026-10-15"
        ):
            raise ValueError("metadata must be the delivered same-day snapshot")
        multiplier = Decimal(str(contract["合约乘数"]))
        margins = [
            Decimal(str(v))
            for v in (
                contract["多头保证金率"],
                contract["空头保证金率"],
                fee["spec_buy_rate"],
                fee["spec_sell_rate"],
                ref["exchange_margin_long"],
                ref["exchange_margin_short"],
            )
        ]
        if len(set(margins)) != 1 or multiplier != Decimal(ref["metadata_multiplier"]):
            raise ValueError("inconsistent multiplier/directional margin evidence")
        if multiplier <= 0 or not 0 < margins[0] <= 1:
            raise ValueError("invalid contract economics")
        unit = dimensions.get(k[1]) if k[0] == "CZCE" else None
        if unit and (
            Decimal(unit["contract_multiplier"]) != multiplier
            or unit["currency"] != "CNY"
        ):
            raise ValueError("dimension evidence conflicts with contract info")
        spec = {
            "asset_type": "linear_futures",
            "currency": "CNY",
            "multiplier": float(multiplier),
            "margin_rate": float(margins[0]),
            "valid_from": "2026-10-15",
            "valid_to": "2026-10-15",
            "source": f"local evidence sha256={evidence_sha}; public.futures_contract_info; {k[0]}/{k[1]}; 2026-10-15; currency/quote dimensions "
            + ("CZCE checked" if unit else "UNAPPROVED CNY assumption"),
        }
        config["instruments"][symbol] = spec
        exchange = {}
        for action, field in FIELDS.items():
            value = Decimal(fee[field]) if fee[field] is not None else None
            if value is None or not value.is_finite() or value < 0:
                raise ValueError("missing/negative fee is not a free rate")
            exchange[action] = {"value": float(value), "unit": fee["fee_scale"]}
        config["fee_model"]["rules"][symbol] = [
            {
                "rule_id": f"exchange-20261015-{k[0]}-{k[1]}",
                "valid_from": "2026-10-15",
                "valid_to": "2026-10-15",
                "exchange_source": f"exchange_data.futures_settle_param; {fee['source_url']}; sha256={fee['source_sha256']}; fetched_at={fee['fetched_at']}",
                "broker_source": None,
                "slippage_source": None,
                "exchange": exchange,
                "broker": None,
                "slippage": None,
            }
        ]
        if fee["fee_scale"] is None:
            unknown.append(symbol)
        if not unit:
            assumed.append(symbol)
        if k not in lifecycle:
            missing_lifecycle.append(symbol)
        provenance[symbol] = {
            "native_target_weight": target["target_weight"],
            "signal_date": target["signal_date"],
            "metadata_date": "2026-10-15",
            "currency_quote_unit_status": ref["unit_status"],
            "unit_evidence": unit,
            "independent_lifecycle": lifecycle.get(k),
            "internal_expiry_reference_only": ref["internal_expiry_date"],
            "contract_info": contract,
            "settlement_rule": fee,
            "field_mapping": FIELDS,
            "short_open_semantics": "intraday opening; not opening a short position",
        }
    manifest = {
        "status": "blocked_candidate_only",
        "synthetic": False,
        "account_initialized": False,
        "native_row_count": len(native),
        "signal_date": "2026-10-15",
        "metadata_valid_from": "2026-10-15",
        "metadata_valid_to": "2026-10-15",
        "source_captured_at": evidence["captured_at"],
        "inputs": {
            k: {"path": str(p.resolve()), "sha256": sha(p)}
            for k, p in (
                ("targets", targets),
                ("metadata_csv", metadata_csv),
                ("evidence_json", evidence_json),
                ("config_template", template),
                ("accounting_candidate", accounting_candidate),
            )
        },
        "fee_unit_counts": {
            unit: sum(
                r["settlement_rule"]["fee_scale"] == unit for r in provenance.values()
            )
            for unit in ("YUAN_PER_LOT", "PERMILLE", "PER_TEN_THOUSAND")
        },
        "unknown_fee_units": sorted(unknown),
        "unapproved_quote_currency_assumptions": sorted(assumed),
        "missing_independent_lifecycle": sorted(missing_lifecycle),
        "directional_margin_conflicts": [],
        "blocked_for_all_contracts": [
            "broker additional fee schedule/source and effective dates",
            "slippage policy/source; excluded costs declaration",
            "intraday opening fee policy/source; current implementation is conditional",
            "fee/margin validity and source publication availability for actual future execution",
            "account start/version and calendar approval; existing consumer scale/lot/cash/financing candidates retained, activation pending",
            "actual execution price/tradability and daily settlement price with receipts",
            "signal availability after 2026-10-15 close; no valid execution interval approved",
        ],
        "consumer_accounting_candidate": {
            "profile": profiles[0],
            "fields": candidate_fields,
            "reference_scale": pending["reference_scale"],
        },
        "native_weight_scaling": "unchanged; no 0.4 module allocation or 1000 CNY scenario adopted",
        "new_database_calls": 0,
        "new_network_calls": 0,
        "strategy_runs": 0,
    }
    return config, provenance, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in (
        "targets",
        "metadata-csv",
        "evidence-json",
        "template",
        "accounting-candidate",
        "output-dir",
    ):
        parser.add_argument("--" + arg, required=True)
    args = parser.parse_args(argv)
    root = Path(args.output_dir)
    if root.exists():
        raise ValueError("candidate output must be a new directory")
    config, provenance, manifest = build(
        args.targets,
        args.metadata_csv,
        args.evidence_json,
        args.template,
        args.accounting_candidate,
    )
    root.mkdir(parents=True)
    write(root / "account.candidate.json", config)
    write(root / "contract-provenance.json", provenance)
    write(root / "manifest.json", manifest)
    print(
        f"blocked candidate: {manifest['native_row_count']} contracts; unknown fee units={len(manifest['unknown_fee_units'])}"
    )
    return 0  # Successfully built a blocked candidate, never a readiness signal.


if __name__ == "__main__":
    raise SystemExit(main())
