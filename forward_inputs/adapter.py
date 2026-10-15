"""Capture existing files; never run producers, open a database or advance accounts."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from forward_account.adapters import read_targets
from forward_account.contracts import (
    SOURCE_RULES,
    day,
    digest,
    now_iso,
    number,
    require_keys,
    text,
    timestamp,
    validate_calendar,
    validate_config,
)

MAX_BYTES = 32 * 1024 * 1024
ROLES = {
    "target_csv",
    "source_metadata",
    "publication",
    "execution",
    "settlement",
    "instruments",
    "calendar",
    "account_config",
    "previous_references",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def read_small(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("input exceeds bounded capture size")
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError("input exceeds bounded capture size")
    return data


def read_parameters(data, suffix):
    if suffix != ".xlsx":
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("parameter object required")
        return value
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        rows = iter(workbook["run_config"].values)
        header = next(rows)
        ki, vi = header.index("key"), header.index("value")
        params = {}
        for i, row in enumerate(rows):
            if i > 10000 or row[ki] in params:
                raise ValueError("invalid run_config")
            value = row[vi]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            params[row[ki]] = value
        return params
    finally:
        workbook.close()


def rows(data, columns, required):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not set(columns).issubset(required):
        raise ValueError("invalid field mapping")
    mapping = {key: columns.get(key, key) for key in required}
    if len(set(mapping.values())) != len(mapping) or not set(mapping.values()).issubset(
        fields
    ):
        raise ValueError("missing source fields")
    result = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("malformed CSV row")
        result.append({key: row[field] for key, field in mapping.items()})
    return result


def quotes(data, columns, kind, synthetic, observed_at):
    fields = ["contract", "price", "price_type", "at", "available_at", "source"]
    if kind == "execution":
        fields += ["tradable", "tradable_evidence"]
    records = rows(data, columns, fields)
    if not records:
        raise ValueError("explicit quote rows required")
    prices, tradable, evidence = {}, {}, {}
    first = records[0]
    for row in records:
        contract = text(row["contract"], "contract")
        if contract in prices or any(
            row[k] != first[k] for k in ("at", "available_at", "source")
        ):
            raise ValueError("duplicate contract or mixed quote events")
        if row["price_type"] != ("day_open" if kind == "execution" else "settlement"):
            raise ValueError("explicit unadjusted day_open/settlement price required")
        if (
            not timestamp(row["at"])
            <= timestamp(row["available_at"])
            <= timestamp(observed_at)
        ):
            raise ValueError("invalid quote availability")
        prices[contract] = number(row["price"], "price", positive=True)
        text(row["source"], "quote source")
        if kind == "execution":
            if row["tradable"] != "true":
                raise ValueError("explicit positive tradability required")
            tradable[contract] = True
            evidence[contract] = text(row["tradable_evidence"], "tradability evidence")
    quote = {
        "kind": kind,
        "at": first["at"],
        "available_at": first["available_at"],
        "source": first["source"],
        "synthetic": synthetic,
        "prices": prices,
    }
    if kind == "execution":
        quote["tradable"] = tradable
    return quote, evidence


def instruments(data, columns):
    fields = [
        "contract",
        "asset_type",
        "currency",
        "multiplier",
        "margin_rate",
        "valid_from",
        "valid_to",
        "source",
    ]
    result = {}
    for row in rows(data, columns, fields):
        contract = text(row.pop("contract"), "contract")
        if contract in result:
            raise ValueError("duplicate instrument")
        for key in ("multiplier", "margin_rate"):
            row[key] = number(row[key], key, positive=True)
        if row["margin_rate"] > 1 or day(row["valid_from"]) > day(row["valid_to"]):
            raise ValueError("invalid instrument economics/validity")
        if row["asset_type"] != "linear_futures" or row["currency"] != "CNY":
            raise ValueError("unsupported instrument")
        text(row["source"], "specification source")
        result[contract] = row
    if not result:
        raise ValueError("empty specifications")
    return result


def check_extension(old, calendar, specs):
    """Mirror extend-references preflight without opening its writable store."""
    require_keys(old, ("calendar", "instruments"), "previous references")
    validate_calendar(old["calendar"])
    if not isinstance(old["instruments"], dict) or not old["instruments"]:
        raise ValueError("previous instruments object required")
    prior = old["calendar"]
    if calendar["intervals"][: len(prior["intervals"])] != prior["intervals"]:
        raise ValueError("existing calendar cannot change")
    if any(
        calendar.get(k) != v
        for k, v in prior.items()
        if k not in {"intervals", "coverage_end"}
    ):
        raise ValueError("calendar identity cannot change")
    for contract, spec in old["instruments"].items():
        if not isinstance(spec, dict):
            raise ValueError("invalid previous instrument")
        new = specs.get(contract)
        if (
            not new
            or any(new.get(k) != v for k, v in spec.items() if k != "valid_to")
            or new["valid_to"] < spec["valid_to"]
        ):
            raise ValueError("existing economics require migration")


def stage(plan_path, output_dir):
    """New directory only; audit always survives blockers, interval only on success.

    Publication is an independently supplied, hash-bound completion declaration.
    Local capture alone cannot establish historical availability or completeness.
    """
    plan_path = Path(plan_path).resolve()
    plan_bytes = read_small(plan_path)
    plan = json.loads(plan_bytes)
    require_keys(
        plan, ("adapter", "synthetic", "trade_date", "files", "columns"), "plan"
    )
    if plan["adapter"] not in SOURCE_RULES or type(plan["synthetic"]) is not bool:
        raise ValueError("unsupported adapter/synthetic flag")
    day(plan["trade_date"])
    if not isinstance(plan["files"], dict) or set(plan["files"]) - ROLES:
        raise ValueError("unsupported file role")
    if not isinstance(plan["columns"], dict) or set(plan["columns"]) - {
        "execution",
        "settlement",
        "instruments",
    }:
        raise ValueError("unsupported column mapping")
    if any(not isinstance(v, dict) for v in plan["columns"].values()):
        raise ValueError("column mappings must be objects")
    root = Path(output_dir).resolve()
    if root.exists():
        raise FileExistsError("new isolated staging directory required")
    paths = {
        role: (plan_path.parent / name).resolve()
        for role, name in plan["files"].items()
        if name is not None
    }
    if any(p == root or root in p.parents for p in [plan_path, *paths.values()]):
        raise ValueError("staging directory overlaps sources")
    root.mkdir(parents=True)
    captured = root / "captured"
    captured.mkdir()
    checkout = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=5,
    )
    report = {
        "status": "blocked",
        "adapter": plan["adapter"],
        "synthetic": plan["synthetic"],
        "observed_at": now_iso(),
        "plan_sha256": sha(plan_bytes),
        "blockers": [],
        "observer": {
            "host": socket.gethostname(),
            "checkout": str(checkout),
            "git_head": head.stdout.strip() if head.returncode == 0 else None,
            "adapter_sha256": sha(Path(__file__).read_bytes()),
        },
        "observations": {},
        "target": {"available_at": None},
    }
    blockers = report["blockers"]
    data, local = {}, {}
    for role in sorted(ROLES):
        if role not in paths:
            if role != "previous_references":
                blockers.append(f"missing_{role}")
            continue
        path = paths[role]
        try:
            raw = read_small(path)
            observed = now_iso()
            suffix = (
                ".xlsx"
                if role == "source_metadata" and path.suffix.lower() == ".xlsx"
                else path.suffix
            )
            destination = captured / (role + suffix)
            destination.write_bytes(raw)
            data[role], local[role] = raw, destination
            report["observations"][role] = {
                "path": str(path),
                "sha256": sha(raw),
                "bytes": len(raw),
                "first_seen_scope": "this_capture_only",
                "first_seen": observed,
                "observed_at": observed,
            }
        except (OSError, ValueError):
            blockers.append(f"unreadable_{role}")

    def parse(role, fn):
        if role not in data:
            return None
        try:
            value = fn(data[role])
            if value is None:
                raise ValueError("null source is not evidence")
            return value
        except (
            ValueError,
            KeyError,
            TypeError,
            StopIteration,
            IndexError,
            BadZipFile,
            ParseError,
        ):
            blockers.append(f"invalid_{role}")
            return None

    target_rows = parse(
        "target_csv",
        lambda raw: rows(raw, {}, ["signal_date", "contract", "target_weight"]),
    )
    weights = {}
    if target_rows is not None:
        try:
            for row in target_rows:
                contract = text(row["contract"], "contract")
                day(row["signal_date"])
                if contract in weights:
                    raise ValueError("duplicate target")
                weights[contract] = number(row["target_weight"], "target_weight")
            report["target"].update(
                row_count=len(target_rows),
                signal_dates=sorted({row["signal_date"] for row in target_rows}),
                weights=weights,
            )
        except ValueError:
            blockers.append("invalid_target_csv")
    params = parse(
        "source_metadata",
        lambda raw: read_parameters(raw, local["source_metadata"].suffix),
    )
    expected = SOURCE_RULES[plan["adapter"]][1]
    if params is not None:
        report["source_parameters"] = {k: params.get(k) for k in expected}
        report["parameter_mismatches"] = [
            k for k, v in expected.items() if params.get(k) != v
        ]
        if (
            "signal_date" in params
            and target_rows
            and any(row["signal_date"] != params["signal_date"] for row in target_rows)
        ):
            blockers.append("source_metadata_date_mismatch")
        if report["parameter_mismatches"]:
            blockers.append("source_parameters_mismatch")
    publication = parse("publication", json.loads)
    receipt = None
    if publication is not None:
        try:
            require_keys(
                publication,
                (
                    "complete",
                    "evidence",
                    "source_version",
                    "synthetic",
                    "available_at",
                    "availability_evidence",
                    "signal_date",
                    "row_count",
                    "production",
                    "held_contracts",
                    "holdings_evidence",
                    "files",
                ),
                "publication",
            )
            if publication["complete"] is not True:
                blockers.append("incomplete_publication")
            for key in (
                "evidence",
                "source_version",
                "availability_evidence",
                "holdings_evidence",
            ):
                text(publication[key], key)
            production = publication["production"]
            require_keys(
                production,
                ("project", "host", "checkout", "git_head", "source_tree_sha256"),
                "production",
            )
            for key, value in production.items():
                text(value, key)
            for key, length in (("git_head", 40), ("source_tree_sha256", 64)):
                if len(production[key]) != length or any(
                    c not in "0123456789abcdef" for c in production[key]
                ):
                    raise ValueError("production fingerprint required")
            if publication["synthetic"] is not plan["synthetic"]:
                raise ValueError("synthetic mismatch")
            if not isinstance(publication["held_contracts"], list) or any(
                not isinstance(k, str) or not k.strip()
                for k in publication["held_contracts"]
            ):
                raise ValueError("explicit held contracts required")
            if not isinstance(publication["files"], dict) or publication["files"] != {
                role: sha(raw) for role, raw in data.items() if role != "publication"
            }:
                blockers.append("publication_hash_mismatch")
            day(publication["signal_date"])
            if type(publication["row_count"]) is not int or publication[
                "row_count"
            ] != len(weights):
                raise ValueError("row count mismatch")
            if any(
                row["signal_date"] != publication["signal_date"]
                for row in target_rows or []
            ):
                raise ValueError("signal dates mismatch")
            if timestamp(publication["available_at"]) > timestamp(now_iso()):
                raise ValueError("availability is in the future")
            report["production"] = production
            report["target"].update(
                declared_available_at=publication["available_at"],
                availability_evidence=publication["availability_evidence"],
            )
            if all(role in data for role in ("target_csv", "source_metadata")):
                receipt = {
                    key: publication[key]
                    for key in (
                        "source_version",
                        "synthetic",
                        "available_at",
                        "signal_date",
                        "row_count",
                    )
                }
                receipt.update(
                    csv_sha256=sha(data["target_csv"]),
                    metadata_sha256=sha(data["source_metadata"]),
                )
        except (ValueError, TypeError, KeyError):
            blockers.append("invalid_publication")
            publication = None
    calendar = parse("calendar", json.loads)
    interval = None
    if calendar is not None:
        try:
            validate_calendar(calendar)
            matches = [
                r
                for r in calendar["intervals"]
                if r["trade_date"] == plan["trade_date"]
            ]
            if len(matches) != 1:
                raise ValueError("trade date absent/ambiguous")
            interval = matches[0]
        except (ValueError, TypeError, KeyError):
            blockers.append("invalid_calendar")
    specs = parse(
        "instruments",
        lambda raw: instruments(raw, plan["columns"].get("instruments", {})),
    )
    config = parse("account_config", json.loads)
    if config is not None and calendar is not None and specs is not None:
        try:
            # The supplied account config may contain the previous references.
            candidate = {
                **config,
                "instruments": specs,
                "calendar_sha256": digest(calendar),
            }
            validate_config(candidate, calendar)
            # Account config is itself a pinned baseline; omitting the optional
            # previous-reference export must not bypass immutable economics.
            if not isinstance(config["instruments"], dict) or not config["instruments"]:
                raise ValueError("explicit baseline instruments required")
            for contract, old_spec in config["instruments"].items():
                if not isinstance(old_spec, dict):
                    raise ValueError("invalid baseline instrument")
                new_spec = specs.get(contract)
                if (
                    not new_spec
                    or any(
                        new_spec.get(k) != v
                        for k, v in old_spec.items()
                        if k != "valid_to"
                    )
                    or new_spec["valid_to"] < old_spec["valid_to"]
                ):
                    blockers.append("reference_migration_required")
            if "previous_references" not in paths and (
                config["calendar_sha256"] != digest(calendar)
                or config["instruments"] != specs
            ):
                blockers.append("missing_previous_references")
            if (
                candidate["adapter"] != plan["adapter"]
                or candidate["synthetic"] is not plan["synthetic"]
            ):
                raise ValueError("account identity mismatch")
            if (
                publication
                and candidate["source_version"] != publication["source_version"]
            ):
                blockers.append("source_version_mismatch")
            if receipt:
                read_targets(
                    candidate, local["target_csv"], local["source_metadata"], receipt
                )
        except (ValueError, TypeError, KeyError):
            blockers.append("invalid_account_config")
    old = parse("previous_references", json.loads)
    if old is not None and calendar is not None and specs is not None:
        try:
            check_extension(old, calendar, specs)
        except (ValueError, TypeError, KeyError):
            blockers.append("reference_migration_required")
    elif "previous_references" not in paths:
        # No previous reference bundle means an initial input proposal, never an
        # assertion that an existing live account is compatible.
        report["reference_scope"] = "initial_proposal_only"
    quote_data = {}
    for kind in ("execution", "settlement"):
        parsed = parse(
            kind,
            lambda raw, kind=kind: quotes(
                raw, plan["columns"].get(kind, {}), kind, plan["synthetic"], now_iso()
            ),
        )
        if parsed:
            quote_data[kind], evidence = parsed
            if evidence:
                report["tradability_evidence"] = evidence
            if interval and timestamp(quote_data[kind]["at"]) != timestamp(
                interval["execution_at" if kind == "execution" else "end_at"]
            ):
                blockers.append(f"invalid_{kind}_event_time")
    if interval:
        if receipt and (
            receipt["signal_date"] != interval["signal_date"]
            or not timestamp(interval["start_at"])
            <= timestamp(receipt["available_at"])
            <= timestamp(interval["execution_at"])
        ):
            blockers.append("target_not_available_before_execution")
        if timestamp(interval["end_at"]) > timestamp(now_iso()):
            blockers.append("interval_not_completed")
        required = set(weights) | set(
            publication["held_contracts"] if publication else []
        )
        for kind, contracts in (
            ("execution", required),
            ("settlement", {k for k, v in weights.items() if v != 0}),
        ):
            if kind in quote_data and not contracts.issubset(
                quote_data[kind]["prices"]
            ):
                blockers.append(f"missing_{kind}_contracts")
        if specs is not None and any(
            k not in specs
            or not specs[k]["valid_from"] <= plan["trade_date"] <= specs[k]["valid_to"]
            for k in required
        ):
            blockers.append("missing_or_expired_instruments")
    # Verify every original again after the entire multi-file capture. Independent
    # publication hashes also detect a stable but half-updated source generation.
    for role, raw in data.items():
        try:
            if read_small(paths[role]) != raw:
                blockers.append("source_changed_during_capture")
        except (OSError, ValueError):
            blockers.append("source_changed_during_capture")
    try:
        if read_small(plan_path) != plan_bytes:
            blockers.append("source_changed_during_capture")
    except (OSError, ValueError):
        blockers.append("source_changed_during_capture")
    report["blockers"] = sorted(set(blockers))
    if not report["blockers"]:
        report["status"] = "ready"
        report["target"]["available_at"] = receipt["available_at"]
        write(root / "source-receipt.json", receipt)
        write(root / "calendar.json", calendar)
        write(root / "instruments.json", specs)
        packet = {
            "trade_date": plan["trade_date"],
            "target_csv": str(local["target_csv"].relative_to(root)),
            "source_metadata": str(local["source_metadata"].relative_to(root)),
            "source_receipt": "source-receipt.json",
            **quote_data,
        }
        write(root / "interval.pending.json", packet)
    report["completed_at"] = now_iso()
    write(root / "audit.json", report)
    if report["status"] == "ready":
        os.replace(root / "interval.pending.json", root / "interval.json")
    return report
