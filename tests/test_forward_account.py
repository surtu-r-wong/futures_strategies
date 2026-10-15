"""Independent cash/quantity checks for the prospective paper account."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import importlib.util

import pytest

from forward_account import advance, export_feed, initialize, read_state
from forward_account.demo import make_inputs


@pytest.fixture
def case(tmp_path):
    config, calendar, packets = make_inputs(tmp_path / "inputs")
    db = tmp_path / "state.sqlite"
    initialize(db, config, calendar)
    return db, config, calendar, packets


def test_two_intervals_cash_fees_fixed_quantity_and_idempotency(case, tmp_path):
    db, config, _, packets = case
    first = advance(db, config["account_id"], packets[0])
    # 15 long A contracts (multiplier 10), 5 short B (multiplier 5).
    # Gross target 2x already includes leverage. Ignore CSV vol_scale/lots.
    assert first["positions"] == pytest.approx({"A.TEST": 15, "B.TEST": -5})
    assert first["fees"] == pytest.approx(20)
    assert first["pnl"] == pytest.approx(15 * 10 * 10 + (-5) * 5 * (-10))
    assert first["cash"] == pytest.approx(11730)
    assert first["nav"] == pytest.approx(11730)
    assert first["row"]["daily_return"] == pytest.approx(0.173)
    assert first["row"]["gross_ratio"] == pytest.approx(21250 / 11730)
    assert first["margin"] == pytest.approx(2125)
    before = read_state(db, config["account_id"])
    assert advance(db, config["account_id"], packets[0]) == first
    assert read_state(db, config["account_id"]) == before

    second = advance(db, config["account_id"], packets[1])
    qty = 12080 / (188 * 5)  # previous NAV plus overnight PnL, before fees
    fees = (15 * 112 * 10 + (qty + 5) * 188 * 5) * 0.001
    nav = 11730 + 350 - fees + qty * 5 * 12
    assert second["positions"] == pytest.approx({"B.TEST": qty})
    assert second["fees"] == pytest.approx(33.58)
    assert second["nav"] == pytest.approx(nav)
    assert second["cash"] == pytest.approx(nav)
    assert second["row"]["daily_return"] == pytest.approx(nav / 11730 - 1)
    assert second["row"]["period_start_at"] == first["row"]["period_end_at"]
    out = tmp_path / "synthetic" / "returns.json"
    export_feed(db, out, synthetic=True)
    raw = out.read_bytes()
    export_feed(db, out, synthetic=True)
    assert out.read_bytes() == raw
    feed = json.loads(raw)
    assert len(feed["series"][0]["rows"]) == 2
    assert feed["series"][0]["inception_date"] == "2026-10-13"
    assert feed["series"][0]["mode"] == "paper"


@pytest.mark.parametrize(
    "fault", ["missing", "future", "stale", "version", "cashflow", "nan"]
)
def test_bad_input_leaves_committed_state_and_feed_unchanged(case, tmp_path, fault):
    db, config, _, packets = case
    advance(db, config["account_id"], packets[0])
    out = tmp_path / "synthetic.json"
    export_feed(db, out, synthetic=True)
    raw = out.read_bytes()
    before = read_state(db, config["account_id"])
    bad = deepcopy(packets[1])
    if fault == "missing":
        del bad["execution"]["prices"]["A.TEST"]
    elif fault == "future":
        bad["execution"]["available_at"] = "2099-01-01T00:00:00+08:00"
    elif fault == "stale":
        bad["execution"]["at"] = "2026-10-13T09:00:00+08:00"
    elif fault == "version":
        bad["target"]["source_version"] = "old-index"
    elif fault == "cashflow":
        bad["cash_flow"] = 100
    else:
        bad["execution"]["prices"]["A.TEST"] = float("nan")
    with pytest.raises(ValueError):
        advance(db, config["account_id"], bad)
    assert read_state(db, config["account_id"]) == before
    assert out.read_bytes() == raw
    assert advance(db, config["account_id"], packets[1])["nav"] > before["nav"]


def test_tail_revision_preserves_old_version_and_requires_reason(case):
    db, config, _, packets = case
    original = advance(db, config["account_id"], packets[0])
    revised = deepcopy(packets[0])
    revised["settlement"]["prices"]["A.TEST"] = 111
    with pytest.raises(ValueError, match="revision"):
        advance(db, config["account_id"], revised)
    new = advance(
        db, config["account_id"], revised, revise_reason="synthetic quote correction"
    )
    assert new["nav"] == pytest.approx(original["nav"] + 150)
    assert new["fees"] == original["fees"]  # one opening charge, not two
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM ledger").fetchone()[0] == 2
    advance(db, config["account_id"], packets[1])
    with pytest.raises(ValueError, match="tail"):
        advance(db, config["account_id"], packets[0], revise_reason="old correction")


def test_no_empty_feed_and_synthetic_never_uses_official_path(case, tmp_path):
    db, config, _, packets = case
    with pytest.raises(ValueError, match="complete"):
        export_feed(db, tmp_path / "empty.json", synthetic=True)
    advance(db, config["account_id"], packets[0])
    with pytest.raises(ValueError, match="synthetic"):
        export_feed(db, tmp_path / "real.json")
    with pytest.raises(ValueError, match="official"):
        export_feed(
            db, tmp_path / "output/multi_asset/returns/latest.json", synthetic=True
        )


def test_missing_calendar_day_is_not_silently_skipped(case):
    db, config, _, packets = case
    before = read_state(db, config["account_id"])
    with pytest.raises(ValueError, match="next"):
        advance(db, config["account_id"], packets[1])
    assert read_state(db, config["account_id"]) == before


def test_configuration_is_mandatory_and_pinned(case):
    db, config, calendar, _ = case
    bad = deepcopy(config)
    del bad["cost_bps"]
    with pytest.raises(ValueError):
        initialize(db, bad, calendar)
    bad = deepcopy(config)
    bad["initial_capital"] *= 2
    with pytest.raises(ValueError, match="immutable"):
        initialize(db, bad, calendar)


def test_feed_matches_supplied_schema(case, tmp_path):
    db, config, _, packets = case
    for packet in packets:
        advance(db, config["account_id"], packet)
    path = tmp_path / "synthetic.json"
    export_feed(db, path, synthetic=True)
    schema = Path(
        "docs/requests/2026-10-14-multi-asset-operational-returns/return-feed.schema.json"
    )
    interpreter = (
        sys.executable if importlib.util.find_spec("jsonschema") else "/usr/bin/python3"
    )
    subprocess.run(
        [
            interpreter,
            "-c",
            (
                "import json, sys, jsonschema; "
                "jsonschema.Draft202012Validator(json.load(open(sys.argv[1])), "
                "format_checker=jsonschema.FormatChecker()).validate(json.load(open(sys.argv[2])))"
            ),
            str(schema),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "adapter", ["tsindex_pure", "tsindex_bmom020", "citic027_r150_t1"]
)
def test_all_target_adapters_preserve_scaled_weights_and_reject_old_metadata(
    tmp_path, adapter
):
    from forward_account.adapters import read_targets

    config, cal, packets = make_inputs(tmp_path / adapter, adapter)
    db = tmp_path / "accounts.sqlite"
    initialize(db, config, cal)
    assert advance(db, config["account_id"], packets[0])["nav"] == pytest.approx(11730)
    root = tmp_path / adapter
    receipt = json.loads((root / "receipt-13.json").read_text())
    (root / "targets-13.csv").write_text("corrupted")
    with pytest.raises(ValueError, match="hash"):
        read_targets(
            config, root / "targets-13.csv", root / "source-parameters.json", receipt
        )
    old = deepcopy(packets[1])
    old["target"]["parameters"] = {**config["source_parameters"], "execution_lag": 0}
    with pytest.raises(ValueError, match="parameters"):
        advance(db, config["account_id"], old)


def test_publish_failure_retries_outbox_without_rebooking(case, tmp_path, monkeypatch):
    import forward_account.store as store

    db, config, _, packets = case
    advance(db, config["account_id"], packets[0])
    out = tmp_path / "synthetic.json"
    export_feed(db, out, synthetic=True)
    original_bytes = out.read_bytes()
    advance(db, config["account_id"], packets[1])
    committed = read_state(db, config["account_id"])
    replace = store.os.replace

    def fail_latest(src, dst):
        if Path(dst) == out:
            raise OSError("synthetic interruption before latest replace")
        return replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_latest)
    with pytest.raises(OSError):
        export_feed(db, out, synthetic=True)
    assert out.read_bytes() == original_bytes
    assert read_state(db, config["account_id"]) == committed
    with sqlite3.connect(db) as con:
        saved = con.execute(
            "SELECT payload FROM publications ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    monkeypatch.setattr(store.os, "replace", replace)
    assert export_feed(db, out, synthetic=True) == json.loads(saved)
    assert advance(db, config["account_id"], packets[1]) == committed


def test_duplicate_concurrent_workers_commit_once(case):
    from concurrent.futures import ThreadPoolExecutor

    db, config, _, packets = case
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: advance(db, config["account_id"], packets[0]), range(2))
        )
    assert results[0] == results[1]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1


def test_real_bootstrap_uses_actual_time_and_skips_partial_interval(
    tmp_path, monkeypatch
):
    import forward_account.store as store

    config, calendar, packets = make_inputs(tmp_path / "inputs")
    config["synthetic"] = False
    config["initial_at"] = None
    monkeypatch.setattr(store, "now_iso", lambda: "2026-10-13T10:00:00+08:00")
    db = tmp_path / "test-only-bootstrap.sqlite"
    state = initialize(db, config, calendar)
    assert state["initialized_at"] == "2026-10-13T10:00:00+08:00"
    assert state["next_index"] == 1
    with pytest.raises(ValueError, match="next"):
        advance(db, config["account_id"], packets[0])
    # No actual production account is initialized: this lives only in pytest's temp dir.


def test_margin_and_untradable_quotes_fail_without_forced_rebalancing(case):
    db, config, _, packets = case
    bad = deepcopy(packets[0])
    bad["target"]["weights"]["A.TEST"] = 20
    with pytest.raises(ValueError, match="margin"):
        advance(db, config["account_id"], bad)
    bad = deepcopy(packets[0])
    bad["execution"]["tradable"]["A.TEST"] = False
    with pytest.raises(ValueError, match="tradable"):
        advance(db, config["account_id"], bad)
    assert read_state(db, config["account_id"])["nav"] == 10000


def test_reference_extension_is_append_only_and_keeps_state(case):
    from forward_account.store import extend_references

    db, config, calendar, packets = case
    advance(db, config["account_id"], packets[0])
    before = read_state(db, config["account_id"])
    cal = deepcopy(calendar)
    cal["coverage_end"] = "2026-10-15"
    cal["intervals"].append(
        {
            "trade_date": "2026-10-15",
            "signal_date": "2026-10-14",
            "start_at": "2026-10-14T15:00:00+08:00",
            "execution_at": "2026-10-15T09:00:00+08:00",
            "end_at": "2026-10-15T15:00:00+08:00",
        }
    )
    instruments = deepcopy(config["instruments"])
    for spec in instruments.values():
        spec["valid_to"] = "2026-10-15"
    extend_references(db, config["account_id"], cal, instruments)
    assert read_state(db, config["account_id"]) == before
    assert (
        advance(db, config["account_id"], packets[1])["audit"]["reference_revision"]
        == 1
    )
    instruments["A.TEST"]["multiplier"] = 100
    with pytest.raises(ValueError, match="economics"):
        extend_references(db, config["account_id"], cal, instruments)


def test_native_carry_workbook_adapter(tmp_path):
    import hashlib
    from openpyxl import Workbook
    from forward_account.adapters import read_targets

    config, _, packets = make_inputs(tmp_path, "tsindex_bmom020")
    wb = Workbook()
    sheet = wb.active
    sheet.title = "run_config"
    sheet.append(["key", "value"])
    for k, v in config["source_parameters"].items():
        sheet.append([k, json.dumps(v) if isinstance(v, bool) else v])
    workbook = tmp_path / "native.xlsx"
    wb.save(workbook)
    wb.close()
    receipt = json.loads((tmp_path / "receipt-13.json").read_text())
    receipt["metadata_sha256"] = hashlib.sha256(workbook.read_bytes()).hexdigest()
    result = read_targets(config, tmp_path / "targets-13.csv", workbook, receipt)
    assert result["weights"] == packets[0]["target"]["weights"]
    assert result["parameters"] == config["source_parameters"]


def test_cli_survives_separate_processes_and_rejects_bad_input(tmp_path):
    config, _, _ = make_inputs(tmp_path / "inputs")
    db = tmp_path / "cli.sqlite"
    prefix = [sys.executable, "-m", "forward_account", "--state", str(db)]

    def run(*args, success=True):
        result = subprocess.run(prefix + list(args), capture_output=True, text=True)
        assert result.returncode == (0 if success else 2), result.stderr
        return result

    run(
        "init",
        "--config",
        str(tmp_path / "inputs/config.json"),
        "--calendar",
        str(tmp_path / "inputs/calendar.json"),
    )
    for day in (13, 14, 14):
        run(
            "advance",
            "--account",
            config["account_id"],
            "--input",
            str(tmp_path / f"inputs/interval-{day}.json"),
        )
    out = tmp_path / "synthetic-cli.json"
    run("export", "--output", str(out), "--synthetic")
    assert len(json.loads(out.read_text())["series"][0]["rows"]) == 2
    run(
        "advance",
        "--account",
        config["account_id"],
        "--input",
        str(tmp_path / "absent.json"),
        success=False,
    )
    status = json.loads(run("status", "--account", config["account_id"]).stdout)
    assert status["last_failure"] is not None
    assert status["state"]["nav"] == pytest.approx(12817.48382978723)


@pytest.mark.parametrize(
    "key,value", [("currency", "USD"), ("asset_type", "option"), ("multiplier", None)]
)
def test_unsupported_contract_economics_are_not_silently_accepted(tmp_path, key, value):
    config, calendar, _ = make_inputs(tmp_path / "inputs")
    config["instruments"]["A.TEST"][key] = value
    with pytest.raises(ValueError):
        initialize(tmp_path / "bad.sqlite", config, calendar)


def test_integer_quantity_rule_is_explicit_and_not_target_csv_lots(tmp_path):
    config, calendar, packets = make_inputs(tmp_path / "inputs")
    config["lot_rule"] = "nearest_integer_ties_even"
    db = tmp_path / "integer.sqlite"
    initialize(db, config, calendar)
    advance(db, config["account_id"], packets[0])
    result = advance(db, config["account_id"], packets[1])
    assert result["positions"] == {"B.TEST": 13}
    assert result["fees"] == pytest.approx(33.72)
    assert result["nav"] == pytest.approx(12080 - 33.72 + 13 * 5 * 12)


def test_initialization_retry_after_calendar_end_keeps_original_time(
    tmp_path, monkeypatch
):
    import forward_account.store as store

    config, calendar, _ = make_inputs(tmp_path / "inputs")
    config["synthetic"] = False
    config["initial_at"] = None
    monkeypatch.setattr(store, "now_iso", lambda: "2026-10-12T14:00:00+08:00")
    db = tmp_path / "test-only-init.sqlite"
    original = initialize(db, config, calendar)
    monkeypatch.setattr(store, "now_iso", lambda: "2026-10-20T10:00:00+08:00")
    assert initialize(db, config, calendar) == original
