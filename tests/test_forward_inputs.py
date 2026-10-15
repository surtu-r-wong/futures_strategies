"""Input capture rejects hindsight and incomplete or mixed source releases."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from forward_account.contracts import SOURCE_RULES
from forward_account.demo import make_inputs
from forward_account.adapters import read_targets
from forward_account.store import initialize, advance, extend_references
from forward_inputs.adapter import stage


def write(path, value):
    path.write_text(json.dumps(value))


def fixture(root, adapter="citic027_r150_t1"):
    config, calendar, packets = make_inputs(root, adapter)
    for kind in ("execution", "settlement"):
        quote = packets[0][kind]
        rows = []
        for contract, price in quote["prices"].items():
            row = dict(
                contract=contract,
                price=price,
                at=quote["at"],
                available_at=quote["available_at"],
                source=quote["source"],
                price_type="settlement" if kind == "settlement" else "day_open",
            )
            if kind == "execution":
                row.update(tradable="true", tradable_evidence="synthetic fill evidence")
            rows.append(row)
        with (root / f"{kind}.csv").open("w") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    instruments = [{"contract": k, **v} for k, v in config["instruments"].items()]
    with (root / "instruments.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(instruments[0]))
        writer.writeheader()
        writer.writerows(instruments)
    write(
        root / "prior.json",
        {"calendar": calendar, "instruments": config["instruments"]},
    )
    files = {
        "target_csv": "targets-13.csv",
        "source_metadata": "source-parameters.json",
        "calendar": "calendar.json",
        "account_config": "config.json",
        "execution": "execution.csv",
        "settlement": "settlement.csv",
        "instruments": "instruments.csv",
        "previous_references": "prior.json",
    }
    manifest = {
        "complete": True,
        "evidence": "SYNTHETIC completed release",
        "source_version": config["source_version"],
        "synthetic": True,
        "available_at": packets[0]["target"]["available_at"],
        "availability_evidence": "SYNTHETIC contemporaneous receipt",
        "signal_date": "2026-10-12",
        "row_count": 2,
        "production": {
            "project": "futures_strategies",
            "host": "test-host",
            "checkout": "/synthetic",
            "git_head": "a" * 40,
            "source_tree_sha256": "b" * 64,
        },
        "held_contracts": [],
        "holdings_evidence": "SYNTHETIC empty initial account",
        "files": {
            role: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for role, name in files.items()
        },
    }
    write(root / "publication.json", manifest)
    plan = {
        "adapter": adapter,
        "synthetic": True,
        "trade_date": "2026-10-13",
        "files": {**files, "publication": "publication.json"},
        "columns": {},
    }
    write(root / "plan.json", plan)
    return root / "plan.json", config, calendar, manifest


def update_manifest(root, manifest):
    plan = json.loads((root / "plan.json").read_text())
    manifest["files"] = {
        role: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for role, name in plan["files"].items()
        if role != "publication"
    }
    write(root / "publication.json", manifest)


@pytest.mark.parametrize("adapter", SOURCE_RULES)
def test_packet_is_consumable_and_preserves_weights(tmp_path, adapter):
    plan, config, calendar, _ = fixture(tmp_path / "source", adapter)
    out = tmp_path / "staged"
    report = stage(plan, out)
    assert report["status"] == "ready"
    packet = json.loads((out / "interval.json").read_text())
    target = read_targets(
        config,
        out / packet.pop("target_csv"),
        out / packet.pop("source_metadata"),
        json.loads((out / packet.pop("source_receipt")).read_text()),
    )
    assert target["weights"] == {"A.TEST": 1.5, "B.TEST": -0.5}
    db = tmp_path / "synthetic.sqlite"
    initialize(db, config, calendar)
    extend_references(
        db,
        config["account_id"],
        json.loads((out / "calendar.json").read_text()),
        json.loads((out / "instruments.json").read_text()),
    )
    state = advance(db, config["account_id"], {**packet, "target": target})
    assert state["nav"] == pytest.approx(11730)


def test_old_file_without_receipt_is_observation_only(tmp_path):
    plan, _, _, _ = fixture(tmp_path / "source")
    p = json.loads(plan.read_text())
    p["files"]["publication"] = None
    write(plan, p)
    out = tmp_path / "staged"
    report = stage(plan, out)
    assert report["status"] == "blocked"
    assert "missing_publication" in report["blockers"]
    assert report["target"]["available_at"] is None
    assert report["target"]["signal_dates"] == ["2026-10-12"]
    assert report["observations"]["target_csv"]["first_seen"]
    assert not (out / "interval.json").exists()


@pytest.mark.parametrize(
    "fault,code",
    [
        ("hash", "publication_hash_mismatch"),
        ("late", "target_not_available_before_execution"),
        ("complete", "incomplete_publication"),
        ("capital", "invalid_account_config"),
        ("close", "invalid_settlement"),
        ("tradable", "invalid_execution"),
        ("held", "missing_execution_contracts"),
        ("economics", "reference_migration_required"),
        ("version", "source_version_mismatch"),
    ],
)
def test_fail_closed(tmp_path, fault, code):
    plan, _, _, m = fixture(tmp_path / "source")
    root = plan.parent
    if fault == "hash":
        with (root / "targets-13.csv").open("a") as f:
            f.write("2026-10-12,C.TEST,1,0,0,0\n")
    elif fault == "late":
        m["available_at"] = "2026-10-13T10:00:00+08:00"
    elif fault == "complete":
        m["complete"] = False
    elif fault == "capital":
        c = json.loads((root / "config.json").read_text())
        del c["initial_capital"]
        write(root / "config.json", c)
    elif fault == "close":
        path = root / "settlement.csv"
        path.write_text(path.read_text().replace(",settlement", ",close"))
    elif fault == "tradable":
        path = root / "execution.csv"
        path.write_text(path.read_text().replace(",true,", ",false,"))
    elif fault == "held":
        m["held_contracts"] = ["OLD.TEST"]
    elif fault == "economics":
        prior = json.loads((root / "prior.json").read_text())
        prior["instruments"]["A.TEST"]["multiplier"] = 20
        write(root / "prior.json", prior)
    else:
        m["source_version"] = "different"
    if fault == "hash":
        write(root / "publication.json", m)
    else:
        update_manifest(root, m)
    out = tmp_path / "staged"
    report = stage(plan, out)
    assert report["status"] == "blocked"
    assert code in report["blockers"]
    assert not (out / "interval.json").exists()


def test_source_mutation_during_capture_is_rejected(tmp_path, monkeypatch):
    plan, _, _, _ = fixture(tmp_path / "source")
    original = Path.read_bytes
    target = plan.parent / "targets-13.csv"
    reads = 0

    def changing(path):
        nonlocal reads
        data = original(path)
        if path == target:
            reads += 1
            if reads == 1:
                path.write_bytes(data + b"\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", changing)
    out = tmp_path / "staged"
    report = stage(plan, out)
    assert "source_changed_during_capture" in report["blockers"]
    assert not (out / "interval.json").exists()


def test_staging_refuses_existing_destination(tmp_path):
    plan, _, _, _ = fixture(tmp_path / "source")
    with pytest.raises(FileExistsError):
        stage(plan, plan.parent)


@pytest.mark.parametrize(
    "role", ["publication", "calendar", "account_config", "previous_references"]
)
def test_null_sources_never_pass(tmp_path, role):
    plan, _, _, m = fixture(tmp_path / "source")
    p = json.loads(plan.read_text())
    write(plan.parent / p["files"][role], None)
    if role != "publication":
        update_manifest(plan.parent, m)
    report = stage(plan, tmp_path / "staged")
    assert f"invalid_{role}" in report["blockers"]
    assert report["status"] == "blocked"


def test_native_xlsx_and_custom_quote_column_mapping(tmp_path):
    from openpyxl import Workbook

    plan, config, _, m = fixture(tmp_path / "source", "tsindex_bmom020")
    book = Workbook()
    sheet = book.active
    sheet.title = "run_config"
    sheet.append(["key", "value"])
    for key, value in config["source_parameters"].items():
        sheet.append([key, json.dumps(value)])
    book.save(plan.parent / "source.xlsx")
    p = json.loads(plan.read_text())
    p["files"]["source_metadata"] = "source.xlsx"
    p["columns"]["settlement"] = {"price": "settle"}
    path = plan.parent / "settlement.csv"
    path.write_text(path.read_text().replace("contract,price,", "contract,settle,"))
    write(plan, p)
    update_manifest(plan.parent, m)
    assert stage(plan, tmp_path / "staged")["status"] == "ready"


def test_future_sessions_and_new_contracts_reach_existing_extend_interface(tmp_path):
    from forward_account.contracts import digest

    plan, config, calendar, m = fixture(tmp_path / "source")
    db = tmp_path / "synthetic.sqlite"
    initialize(db, config, calendar)
    calendar["intervals"].append(
        {
            "trade_date": "2026-10-15",
            "signal_date": "2026-10-14",
            "start_at": "2026-10-14T15:00:00+08:00",
            "execution_at": "2026-10-15T09:00:00+08:00",
            "end_at": "2026-10-15T15:00:00+08:00",
        }
    )
    calendar["coverage_end"] = "2026-10-15"
    write(plan.parent / "calendar.json", calendar)
    path = plan.parent / "instruments.csv"
    path.write_text(
        path.read_text().replace("2026-10-14", "2026-10-15")
        + "C.TEST,linear_futures,CNY,10,0.1,2026-10-15,2026-10-15,SYNTHETIC new spec\n"
    )
    update_manifest(plan.parent, m)
    out = tmp_path / "staged"
    assert stage(plan, out)["status"] == "ready"
    actual_cal = json.loads((out / "calendar.json").read_text())
    actual_specs = json.loads((out / "instruments.json").read_text())
    assert digest(actual_cal) == digest(calendar)
    extend_references(db, config["account_id"], actual_cal, actual_specs)


def test_no_implicit_calendar_completeness_or_margin(tmp_path):
    plan, _, calendar, m = fixture(tmp_path / "source")
    calendar["complete"] = False
    write(plan.parent / "calendar.json", calendar)
    path = plan.parent / "instruments.csv"
    path.write_text(path.read_text().replace(",0.1,", ",,"))
    update_manifest(plan.parent, m)
    report = stage(plan, tmp_path / "staged")
    assert "invalid_calendar" in report["blockers"]
    assert "invalid_instruments" in report["blockers"]


def test_empty_target_requires_publisher_zero_count(tmp_path):
    plan, _, _, m = fixture(tmp_path / "source")
    path = plan.parent / "targets-13.csv"
    path.write_text("signal_date,contract,target_weight\n")
    m["row_count"] = 0
    update_manifest(plan.parent, m)
    out = tmp_path / "staged"
    assert stage(plan, out)["status"] == "ready"
    assert json.loads((out / "source-receipt.json").read_text())["row_count"] == 0


def test_missing_account_config_still_captures_source(tmp_path):
    plan, _, _, _ = fixture(tmp_path / "source")
    p = json.loads(plan.read_text())
    p["files"]["account_config"] = None
    write(plan, p)
    report = stage(plan, tmp_path / "staged")
    assert "missing_account_config" in report["blockers"]
    assert report["target"]["row_count"] == 2


def test_omitting_previous_references_cannot_bypass_economic_migration(tmp_path):
    plan, _, _, m = fixture(tmp_path / "source")
    p = json.loads(plan.read_text())
    p["files"]["previous_references"] = None
    write(plan, p)
    path = plan.parent / "instruments.csv"
    path.write_text(path.read_text().replace(",10,0.1,", ",20,0.1,"))
    m["files"] = {
        role: hashlib.sha256((plan.parent / name).read_bytes()).hexdigest()
        for role, name in p["files"].items()
        if name and role != "publication"
    }
    write(plan.parent / "publication.json", m)
    report = stage(plan, tmp_path / "staged")
    assert "reference_migration_required" in report["blockers"]
    assert "missing_previous_references" in report["blockers"]


@pytest.mark.parametrize(
    "role,code",
    [
        ("account_config", "invalid_account_config"),
        ("previous_references", "reference_migration_required"),
    ],
)
def test_null_baseline_instruments_are_blocked(tmp_path, role, code):
    plan, _, _, m = fixture(tmp_path / "source")
    p = json.loads(plan.read_text())
    path = plan.parent / p["files"][role]
    obj = json.loads(path.read_text())
    obj["instruments"] = None
    write(path, obj)
    update_manifest(plan.parent, m)
    report = stage(plan, tmp_path / "staged")
    assert code in report["blockers"]
    assert report["status"] == "blocked"
