"""Offline evidence reconciliation only: no network, database, strategy or ledger."""

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import gzip
import hashlib
import html
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OFFICE = REPO.parent / "data_manager/requests/2026-10-16-multi-asset-m02-real-inputs"
PRIOR = ROOT.parent / "2026-10-15-m02-account-compatibility"
M01 = (
    ROOT.parent
    / "2026-10-14-multi-asset-operational-returns/m01-offline-diagnosis-evidence/verification-02"
)
NOW = datetime.now(timezone.utc).isoformat()


def raw(path):
    p = Path(path)
    if not p.exists() and Path(str(p) + ".gz").exists():
        p = Path(str(p) + ".gz")
    return gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()


def load(path):
    return json.loads(raw(path))


def sha(path):
    return hashlib.sha256(raw(path)).hexdigest()


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def plain(value):
    return html.unescape(re.sub(r"\s+", " ", re.sub("<[^>]*>", " ", value))).strip()


def ec_lifecycle_basis(ec, product_source):
    page = raw(ROOT / product_source["path"]).decode("utf-8")
    # Use the EC-specific table, not the generic physical-delivery table.
    table = page.split("<div v-if=\"type_select == 'ec'\">", 1)[1].split(
        "</el-table>", 1
    )[0]
    labels = dict(re.findall(r'prop="([A-Z]+)"\s+label="([^"]+)"', table))
    assert labels["OPENDATE"] == "上市日"
    assert labels["EXPIREDATE"] == "到期日"
    assert labels["ENDDELIVDATE"] == "交割日"
    first_rule = page.split("let pageList = [", 1)[1].split("},", 1)[0]
    fields = dict(re.findall(r'"([^"\n]+)"\s*:\s*"([^"\n]*)"', first_rule))
    assert fields["ContractSymbol"] == "EC"
    assert fields["DeliveryPeriod"] == "同最后交易日"
    delivery = datetime.strptime(ec["ENDDELIVDATE"], "%Y%m%d").date()
    assert delivery.weekday() == 0
    assert (delivery + timedelta(days=1)).month != delivery.month
    return {
        "symbol": "EC2611.INE",
        "product_source": {
            k: product_source[k] for k in ("url", "path", "sha256", "acquired_at")
        },
        "table_locator": "currinstrumentprop-f-component / desktop / type_select == 'ec'",
        "official_field_labels": {
            key: labels[key] for key in ("OPENDATE", "EXPIREDATE", "ENDDELIVDATE")
        },
        "rule_locator": "let pageList[0] / ContractSymbol=EC / data_standard=2026-05-11",
        "contract_rule": {
            key: fields[key]
            for key in (
                "ContractSymbol",
                "data_standard",
                "LastTradingDay",
                "DeliveryPeriod",
            )
        },
        "date_field_used": "ENDDELIVDATE",
        "last_trade_date": delivery.isoformat(),
        "derivation": "官方EC表ENDDELIVDATE标签为交割日；EC标准合约DeliveryPeriod为同最后交易日，故由交割日映射最后交易日。EXPIREDATE仅标为到期日，不单独用于推定最后交易日。",
        "calendar_cross_check": {
            "weekday": "Monday",
            "last_calendar_day_of_month": True,
            "internal_calendar_path": "calendar.disabled.json",
            "internal_sfe_open": True,
            "independent_ine_session_certification": False,
            "scope": "2026-11-30是11月最后一个自然日且为周一；内部sfe标志仅佐证，未独立认证INE未来会话。",
        },
        "future_adjustments": "官方参数未含节假日调整；合约规则允许能源中心根据法定节假日等调整最后交易日。当前映射保留此限制，未来D须复核公告及日历，不构成未来适用或执行授权。",
    }


def main():
    # The copied office extract remains bound to its own current DB source hashes.
    reused = []
    for name in (
        "evidence-readonly.json",
        "calendar-summary.json",
        "response.md",
        "futures-source-map.json",
        "validation.json",
    ):
        dest = ROOT / "office" / name
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(OFFICE / name, dest)
        reused.append(
            {
                "path": str(OFFICE / name),
                "copied_to": str(dest.relative_to(ROOT)),
                "sha256": sha(dest),
                "copied_at": NOW,
            }
        )
    official = (
        REPO.parent
        / "data_manager/findings/2026-10-14-office-final-continuation/official-sources"
    )
    for row in load(official / "manifest.json"):
        if row["source_id"] not in (
            "shfe_hours",
            "shfe_2026_holiday",
            "ine_2026_holiday",
        ):
            continue
        dest = ROOT / "sources" / ("office_" + row["file"])
        shutil.copyfile(official / row["file"], dest)
        assert sha(dest) == row["sha256"]
        reused.append(
            {
                **row,
                "path": str(dest.relative_to(ROOT)),
                "original_path": str(official / row["file"]),
                "representation": "original_response_bytes",
                "copied_at": NOW,
            }
        )
    old_rules = (
        REPO.parent
        / "data_manager/findings/2026-09-20-stock-exchange-direct-sources/followup-rule-evidence"
    )
    for row in load(old_rules / "report.json"):
        dest = ROOT / "sources" / ("office_" + row["name"] + ".html")
        shutil.copyfile(old_rules / (row["name"] + ".html"), dest)
        assert sha(dest) == row["sha256"]
        reused.append(
            {
                **row,
                "path": str(dest.relative_to(ROOT)),
                "original_path": str(old_rules / (row["name"] + ".html")),
                "representation": "office_browser_rendered_original_page",
                "copied_at": NOW,
            }
        )
    write(ROOT / "reused-sources.json", reused)
    receipts = [
        json.loads(line)
        for line in (ROOT / "acquisitions.jsonl").read_text().splitlines()
    ]
    byid = {r["source_id"]: r for r in receipts}
    old = load(PRIOR / "candidate/contract-provenance.json")
    config = load(PRIOR / "candidate/account.candidate.json")
    office = load(ROOT / "office/evidence-readonly.json")
    office_fees = {
        (r["exchange"], r["contract_id"].upper()): r
        for r in office["queries"]["fee_raw_24"]["rows"]
    }
    specs = {}
    for symbol in old:
        product = re.match("[A-Z]+", symbol)[0].lower()
        ident = (
            ("gfex_pt_http" if product == "pt" else "gfex_" + product + "_product")
            if symbol.endswith(".GFE")
            else "shfe_" + product + "_product"
        )
        if ident not in byid or byid[ident]["status"] != 200:
            continue
        receipt = byid[ident]
        content = raw(ROOT / receipt["path"]).decode("utf-8")
        if symbol.endswith(".GFE"):
            tables = [
                t
                for t in re.findall(r"<table.*?</table>", content, re.S)
                if "报价单位" in t and "合约类型" not in t
            ]
            assert len(tables) == 1
            cells = {}
            for row in re.findall(r"<tr.*?</tr>", tables[0], re.S):
                values = [
                    plain(x)
                    for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
                ]
                if len(values) == 2:
                    cells[values[0]] = values[1]
            values = {
                key: cells.get(label)
                for key, label in {
                    "Product": "合约标的物",
                    "ContractSize": "交易单位",
                    "PriceQuotation": "报价单位",
                    "MinimumPriceFluctuation": "最小变动价位",
                    "TradingHours": "交易时间",
                    "LastTradingDay": "最后交易日",
                }.items()
            }
            values["data_standard"] = None
        else:
            start = content.index("let pageList")
            section = content[start : start + 18000].split("},")[0]
            values = dict(
                re.findall(
                    r'"(Product|ContractSize|PriceQuotation|ContractMultiplier|MinimumPriceFluctuation|data_standard|TradingHours|LastTradingDay)"\s*:\s*"([^"]*)"',
                    section,
                )
            )
        if product == "ec":
            assert (
                values["PriceQuotation"] == "指数点"
                and values["ContractMultiplier"] == "每点50元"
            )
            multiplier = Decimal("50")
            quantity_unit = "CNY/index_point/lot"
        else:
            match = re.fullmatch(r"([0-9.]+)(吨|千克|克|桶)/手", values["ContractSize"])
            assert match, (symbol, values)
            multiplier = Decimal(match[1])
            quantity_unit = match[2] + "/手"
            assert (
                "人民币" in values["PriceQuotation"]
                and "/" + match[2] in values["PriceQuotation"]
            )
        assert multiplier == Decimal(
            str(config["instruments"][symbol]["multiplier"])
        ), symbol
        specs[symbol] = {
            "status": "official_contract_dimensions_match_internal_snapshot",
            "currency": "CNY",
            "multiplier": str(multiplier),
            "multiplier_unit": quantity_unit,
            "price_unit": values["PriceQuotation"],
            "contract_fields": values,
            "source_id": ident,
            "source_url": receipt["url"],
            "source_sha256": receipt["sha256"],
            "acquired_at": receipt["acquired_at"],
            "document_publication_date": values.get("data_standard") or None,
            "economic_valid_from": None,
            "economic_valid_to": None,
            "availability_scope": "observed now; does not prove historical visibility or future D applicability",
            "actual_quote_source_unit_attested": False,
        }
    lifecycle_source = byid["shfe_lifecycle_20261015"]
    lifecycle_raw = load(ROOT / lifecycle_source["path"])
    lifecycle = [
        r for r in lifecycle_raw["ContractBaseInfo"] if r["INSTRUMENTID"] == "ec2611"
    ]
    assert len(lifecycle) == 1
    ec = lifecycle[0]
    assert ec["OPENDATE"] == "20260526" and ec["EXPIREDATE"] == "20261130"
    ec_basis = ec_lifecycle_basis(ec, byid["shfe_ec_product"])
    write(ROOT / "ec-lifecycle-basis.json", ec_basis)
    matrix = {}
    missing_units = []
    missing_dimensions = []
    for symbol, row in old.items():
        item = deepcopy(row)
        old_fee = row["settlement_rule"]
        key = old_fee["requested_exchange"], old_fee["requested_contract"].upper()
        new_fee = office_fees.get(key)
        item["source_versions"] = {
            "m01": {
                "evidence_path": str(PRIOR / "candidate/contract-provenance.json"),
                "evidence_sha256": sha(PRIOR / "candidate/contract-provenance.json"),
                "declared_original_response_sha256": old_fee["source_sha256"],
                "fetched_at": old_fee["fetched_at"],
            },
            "office_20261016": {
                "evidence_path": "office/evidence-readonly.json",
                "evidence_sha256": sha(ROOT / "office/evidence-readonly.json"),
                "database_read_at": office["queries"]["fee_raw_24"]["acquired_at"],
                "row": new_fee,
                "original_response_bytes_rehashed": False,
            }
            if new_fee
            else None,
        }
        if new_fee:
            assert all(
                Decimal(new_fee[k]) == Decimal(old_fee[k])
                for k in (
                    "open_fee",
                    "offset_fee",
                    "short_open_fee",
                    "short_offset_fee",
                )
            )
            item["source_versions"]["declared_source_sha_changed"] = (
                new_fee["source_sha256"] != old_fee["source_sha256"]
            )
        item["dimensions"] = specs.get(symbol) or (
            {
                "status": "retained_M01_CZCE_dimension_evidence",
                "source": row["unit_evidence"],
                "economic_valid_from": None,
                "economic_valid_to": None,
            }
            if row["unit_evidence"]
            else {
                "status": "unresolved_official_dimensions",
                "reason": "DCE public entry and rule fetches unavailable; internal multiplier alone is not dimensions proof",
            }
        )
        if symbol in specs:
            config["instruments"][symbol]["source"] += (
                "; dimension supplement "
                + specs[symbol]["source_url"]
                + " sha256="
                + specs[symbol]["source_sha256"]
                + " acquired_at="
                + specs[symbol]["acquired_at"]
                + "; future applicability NOT approved"
            )
        if item["dimensions"]["status"] == "unresolved_official_dimensions":
            missing_dimensions.append(symbol)
        if symbol == "EC2611.INE":
            item["independent_lifecycle"] = {
                "symbol": symbol,
                "listing_date": "2026-05-26",
                "last_trade_date": ec_basis["last_trade_date"],
                "date_field_used": ec_basis["date_field_used"],
                "derivation_evidence": "ec-lifecycle-basis.json",
                "raw_row": ec,
                "raw_exchange_id_note": "SHFE cross-published source uses EXCHANGEID=SHFE; native EC identity remains INE",
                "source_url": lifecycle_source["url"],
                "source_sha256": lifecycle_source["sha256"],
                "report_date": lifecycle_raw["report_date"],
                "source_update_at": lifecycle_raw["update_date"],
                "source_timezone": "Asia/Shanghai",
                "acquired_at": lifecycle_source["acquired_at"],
                "holiday_adjustments": ec_basis["future_adjustments"],
            }
        if old_fee["fee_scale"] is None:
            missing_units.append(symbol)
        item["fees_status"] = (
            "unit_not_certified_for_snapshot"
            if old_fee["fee_scale"] is None
            else "retained_M01_explicit_unit_not_future_validity"
        )
        item["fee_semantics"] = {
            "open": "ordinary opening both long and short",
            "close": "ordinary offset",
            "intraday_open": "DCE raw shortOpenFee; CZCE same regular fee column projection, not an independently proven adjustment policy",
            "close_today": "DCE raw shortOffsetFee; CZCE raw intraday close column",
            "current_intraday_adjustment_policy_proven": False,
            "ordinary_open_then_same_day_adjustment": "conditional software model; official matching order, retrospective charge and entry-price basis remain unverified",
        }
        item["validity"] = {
            "metadata_day": "2026-10-15",
            "candidate_valid_from": "2026-10-15",
            "candidate_valid_to": "2026-10-15",
            "exchange_rule_effective_from": None,
            "exchange_rule_effective_to": None,
            "original_fee_publication_at": None,
            "original_margin_publication_at": None,
            "collector_fetched_at": old_fee["fetched_at"],
            "next_execution_day": None,
        }
        item["missing_fields"] = [
            "actual_execution_price_and_tradability_receipt",
            "actual_daily_settlement_receipt",
            "fee_and_margin_effective_interval_for_D",
            "broker_fee_and_margin_terms",
            "slippage_and_exclusions",
            "minimum_fee_and_currency_rounding_policy",
            "intraday_open_matching_and_adjustment_policy",
            "order_minimums_limits_and_current_status",
            "account_start_and_version",
            "fresh_native_target_publication_receipt",
        ]
        if symbol in missing_units:
            item["missing_fields"].append("fee_unit_for_four_actions")
        if symbol in missing_dimensions:
            item["missing_fields"].append("official_quote_dimensions")
        matrix[symbol] = item
    write(ROOT / "contract-evidence.json", matrix)
    write(ROOT / "official-dimensions.json", specs)
    write(
        ROOT / "account.disabled.json",
        {
            "enabled": False,
            "runtime_ready": False,
            "executable": False,
            "execution_day": None,
            "status": "evidence_preparation_only",
            "account_config": config,
            "evidence_matrix": "contract-evidence.json",
            "metadata_window_is_not_approved_economic_validity": True,
        },
    )
    # A preparation calculation using frozen CLOSE references, not execution or PnL.
    with (M01 / "targets.csv").open() as f:
        targets = list(csv.DictReader(f))
    assert len(targets) == 67 and set(r["contract"] for r in targets) == set(matrix)
    nav = Decimal(str(config["initial_capital"]))
    diagnostic = []
    for target in targets:
        symbol = target["contract"]
        spec = config["instruments"][symbol]
        w = Decimal(target["target_weight"])
        p = Decimal(target["signal_close_reference"])
        m = Decimal(str(spec["multiplier"]))
        margin = Decimal(str(spec["margin_rate"]))
        q = w * nav / (p * m)
        rounded = q.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        assert int(rounded) == round(float(w) * float(nav) / (float(p) * float(m)))
        notional = rounded * p * m
        actual_w = notional / nav
        diagnostic.append(
            {
                "contract": symbol,
                "native_target_weight": str(w),
                "reference_close": str(p),
                "reference_price_kind": "2026-10-15_eod_close_NOT_execution_or_settlement",
                "multiplier": str(m),
                "dimensions_status": matrix[symbol]["dimensions"]["status"],
                "ideal_reference_lots": str(q),
                "integer_reference_lots": str(rounded),
                "rounded_reference_notional": str(notional),
                "rounded_reference_weight": str(actual_w),
                "weight_error": str(actual_w - w),
                "exchange_margin_reference": str(abs(notional) * margin),
                "fee_cost": None,
                "executable": False,
            }
        )
    with (ROOT / "integer-preparation.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(diagnostic[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(diagnostic)
    gross = sum(abs(Decimal(x["rounded_reference_notional"])) for x in diagnostic)
    total_margin = sum(Decimal(x["exchange_margin_reference"]) for x in diagnostic)
    write(
        ROOT / "preparation-check.json",
        {
            "enabled": False,
            "execution_simulated": False,
            "nav_candidate_cny": str(nav),
            "initial_unit_nav": config["initial_unit_nav"],
            "lot_rule": config["lot_rule"],
            "native_gross_weight": str(
                sum(abs(Decimal(x["native_target_weight"])) for x in diagnostic)
            ),
            "rounded_reference_gross_weight": str(gross / nav),
            "rounded_reference_net_weight": str(
                sum(Decimal(x["rounded_reference_weight"]) for x in diagnostic)
            ),
            "sum_absolute_weight_error": str(
                sum(abs(Decimal(x["weight_error"])) for x in diagnostic)
            ),
            "max_absolute_weight_error": str(
                max(abs(Decimal(x["weight_error"])) for x in diagnostic)
            ),
            "nonzero_targets_rounded_to_zero": [
                r["contract"]
                for r in diagnostic
                if Decimal(r["native_target_weight"])
                and not Decimal(r["integer_reference_lots"])
            ],
            "exchange_margin_reference_cny": str(total_margin),
            "margin_reference_ratio": str(total_margin / nav),
            "headroom_before_unproved_costs_cny": str(nav - total_margin),
            "fee_cost": None,
            "funding_ready": False,
            "limitations": [
                "reference close is not a fill or settlement",
                "19 DCE dimensions still unproved",
                "broker margins/fees missing",
                "exchange dated applicability and order minimums/limits unproved",
                "no unit NAV or return ledger generated",
            ],
        },
    )
    flags = office["queries"]["calendar_flags"]["rows"]
    write(
        ROOT / "calendar.disabled.json",
        {
            "enabled": False,
            "complete": False,
            "range": ["2026-09-01", "2026-11-30"],
            "timezone": "Asia/Shanghai",
            "source": "office/evidence-readonly.json#queries.calendar_flags",
            "acquired_at": office["queries"]["calendar_flags"]["acquired_at"],
            "original_published_at": None,
            "internal_sfe_flags_not_exchange_certification": True,
            "official_holiday_evidence": [
                "sources/office_shfe_2026_holiday.html.gz",
                "sources/office_ine_2026_holiday.html.gz",
            ],
            "official_session_evidence": "sources/office_shfe_hours.html.gz",
            "published_shfe_ine_holidays": [
                {
                    "start": "2026-09-25",
                    "end": "2026-09-27",
                    "prior_evening_no_night": "2026-09-24",
                    "resumes_day": "2026-09-28",
                },
                {
                    "start": "2026-10-01",
                    "end": "2026-10-07",
                    "prior_evening_no_night": "2026-09-30",
                    "resumes_day": "2026-10-08",
                },
            ],
            "night_mapping_status": "no executable interval mapping emitted; preceding evening belongs to following exchange trade date only with verified product/session/holiday mapping",
            "day_execution": "09:00 candidate; distinguish opening auction 08:55-09:00 and breaks 10:15-10:30/11:30-13:30",
            "rows": [
                {
                    "date": r["calendar_date"],
                    "internal_sfe_open": r["sfe"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "actual_execution": None,
                    "actual_settlement": None,
                }
                for r in flags
            ],
        },
    )
    inventory = []
    paths = (
        list((REPO / "output/targets").glob("citic027*_next_targets.csv"))
        + list(
            (REPO / "output/forward_inputs/current-20261015").glob(
                "citic027*_next_targets.csv"
            )
        )
        + [M01 / "targets.csv"]
    )
    for path in sorted(paths):
        with path.open() as f:
            rows = list(csv.DictReader(f))
        inventory.append(
            {
                "path": str(path),
                "sha256": sha(path),
                "row_count": len(rows),
                "signal_dates": sorted({r.get("signal_date", "") for r in rows}),
                "filesystem_mtime_is_not_availability": True,
                "historical_available_at": load(M01 / "manifest.json")["available_at"]
                if path == M01 / "targets.csv"
                else None,
                "kind": "archived_input_offline_reconstruction"
                if path == M01 / "targets.csv"
                else "existing_native_file_without_current_execution_receipt",
                "new_target_computation": False,
            }
        )
    write(
        ROOT / "native-target-inventory.json",
        {
            "observed_at": NOW,
            "files": inventory,
            "future_daily_target_verified": False,
            "strategy_runs": 0,
            "needed_if_fresh_target_authorized": {
                "entry": "python -m forward_inputs.m01 --as-of YYYY-MM-DD --production-observation <actual-observation.json> --output-dir <new-isolated-dir>",
                "inputs": "actual source checkout/parameter attestation, production observation and five-exchange daily coverage; existing adapter requires 900 calendar days for R150 plus252-day volatility warmup",
                "resource_limit": "2GB virtual memory; BLAS/OMP single thread; no parallel historical runs",
                "scope": "not executed; requires a separately accepted current-target computation scope, not reuse of frozen 10-15 target",
            },
        },
    )
    write(
        ROOT / "completion-summary.json",
        {
            "status": "disabled_evidence_delivered_with_unresolved_inputs",
            "generated_at": NOW,
            "contracts": 67,
            "new_official_dimension_matches": len(specs),
            "retained_CZCE_dimensions": 19,
            "missing_dimensions": missing_dimensions,
            "missing_fee_units": missing_units,
            "fee_unit_certifications_new": 0,
            "historical_fee_unit_leads": ["FG2701.CZC", "SA2701.CZC", "JM2701.DCE"],
            "independent_lifecycle_count": 67,
            "missing_independent_lifecycle": [],
            "EC_source_observed_at": lifecycle_source["acquired_at"],
            "DCE_source_version_conflicts": [
                s
                for s, r in matrix.items()
                if r["source_versions"].get("declared_source_sha_changed")
            ],
            "calendar_days": len(flags),
            "calendar_sfe_open_flags": sum(r["sfe"] for r in flags),
            "database_calls_this_project": 0,
            "office_readonly_evidence_reused": True,
            "account_initializations": 0,
            "strategy_runs": 0,
            "shared_database_writes": 0,
            "paid_vendor_calls": 0,
            "production_changes": 0,
            "execution_day": None,
        },
    )
    print(
        json.dumps(
            {
                "dimension_matches": len(specs),
                "missing_dimensions": len(missing_dimensions),
                "missing_fee_units": len(missing_units),
                "EC_lifecycle": ec,
                "margin_reference": str(total_margin),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
