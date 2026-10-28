"""M02 v2 nightly: gates, target_missing classification, untradable holds,
fail-closed paths, seal, dry run, catch-up.

Sources are an in-memory fake with the PgSources interface; the store is a
real SQLite account with a synthetic (past) calendar.
"""

import csv
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import m02_v2_nightly as nightly  # noqa: E402
import m02_seal  # noqa: E402
from forward_account import initialize, read_state  # noqa: E402
from forward_account.contracts import DAILY_POLICY, SOURCE_RULES, digest  # noqa: E402
from forward_account.store import read_status  # noqa: E402

ACCOUNT = "synthetic-v2"
SOURCE_VERSION = "SYNTHETIC-v2-source"
PARAMS = dict(SOURCE_RULES["citic027_r150_t1"][1])
DAYS = ("2026-10-13", "2026-10-14", "2026-10-15")


def calendar():
    prior = "2026-10-12"
    intervals = []
    for d in DAYS:
        start = f"{prior}T15:00:00+08:00"
        intervals.append(
            {
                "trade_date": d,
                "signal_date": prior,
                "start_at": start,
                "execution_at": f"{d}T09:30:00+08:00",
                "end_at": f"{d}T15:00:00+08:00",
            }
        )
        prior = d
    return {
        "calendar_id": "synthetic-v2-cal",
        "timezone": "Asia/Shanghai",
        "source": "SYNTHETIC",
        "complete": True,
        "coverage_start": DAYS[0],
        "coverage_end": DAYS[-1],
        "intervals": intervals,
    }


def config(cal):
    return {
        "account_id": ACCOUNT,
        "account_version": "synthetic-v2-v1",
        "adapter": "citic027_r150_t1",
        "source_version": SOURCE_VERSION,
        "source_parameters": dict(PARAMS),
        "label": "SYNTHETIC v2 nightly",
        "mode": "paper",
        "currency": "CNY",
        "synthetic": True,
        "initial_at": "2026-10-12T15:00:00+08:00",
        "initial_capital": 1000000.0,
        "initial_unit_nav": 1.0,
        "fee_model": {
            "version": "contract-fees-v2",
            "position_accounting": "dated-net-lots-v1",
            "close_order": "fifo",
            "broker_fee_basis": "additional_to_exchange",
            "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
            "policy_source": "SYNTHETIC",
            "excluded_costs": ["SYNTHETIC none"],
            "dce_intraday_matching": {
                "method": "prior_open_fifo_same_side",
                "components": ["exchange"],
                "source": "SYNTHETIC",
            },
            "rules": {},
        },
        "fee_description": "SYNTHETIC",
        "lot_rule": "nearest_integer_ties_even",
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
        "calendar_id": cal["calendar_id"],
        "calendar_sha256": digest(cal),
        "instruments": {},
        "daily_policy": dict(DAILY_POLICY),
    }


TABLE = {
    "schema": "m02-v2-product-specs.v1",
    "table_sha256": "f" * 64,
    "products": {
        "SHFE:CU": {"exchange": "SHFE", "product": "CU", "multiplier": 5.0, "status": "closed",
                    "pending": [], "fee_unit_ruling": None, "revision": 1},
        "DCE:I": {"exchange": "DCE", "product": "I", "multiplier": 100.0, "status": "closed",
                  "pending": [], "revision": 1,
                  "fee_unit_ruling": {"fee_style": "比例值", "unit": "PER_TEN_THOUSAND",
                                      "authority": "SYNTHETIC ruling"}},
        "DCE:LG": {"exchange": "DCE", "product": "LG", "multiplier": 90.0, "status": "pending",
                   "pending": ["fee unit undeclared"], "fee_unit_ruling": None, "revision": 1},
    },
}

# contract -> (exchange, source id, multiplier, fee_style, fee_scale, fee)
CONTRACTS = {
    "CU2612.SHF": ("SHFE", "CU2612", 5, None, "YUAN_PER_LOT", 3.0),
    "I2701.DCE": ("DCE", "I2701", 100, "比例值", None, 1.0),
    "LG2701.DCE": ("DCE", "LG2701", 90, "比例值", None, 1.0),
}


def spec_rows(day):
    settle, info = [], []
    for contract, (ex, cid, mult, style, scale, fee) in CONTRACTS.items():
        settle.append({
            "exchange": ex, "trade_date": day, "contract_id": cid, "product_id": cid[:-4],
            "product_name": None, "open_fee": fee, "offset_fee": fee, "short_open_fee": fee,
            "short_offset_fee": fee, "fee_style": style, "fee_scale": scale,
            "spec_buy_rate": 0.1, "spec_sell_rate": 0.1, "source_url": "SYNTHETIC",
            "source_sha256": "0" * 64, "fetched_at": f"{day}T17:40:00+08:00",
        })
        info.append({"exchange": ex, "trade_date": day, "contract_id": cid,
                     "multiplier": mult, "last_date": 20261215.0,
                     "limit_up": 1e9, "limit_down": 1.0})
    return settle, info


class Fake:
    """Per-day market data; tests mutate the dicts to stage each case."""

    def __init__(self):
        self.minutes = {d: {"SHFE": 100, "DCE": 100, "CZCE": 100} for d in DAYS}
        self.opens = {
            "2026-10-13": {"CU2612.SHF": 100000.0, "I2701.DCE": 800.0},
            "2026-10-14": {"CU2612.SHF": 101000.0, "I2701.DCE": 790.0},
            "2026-10-15": {"CU2612.SHF": 102000.0, "I2701.DCE": 795.0},
        }
        self.limit_rows = {d: {c: (1e9, 1.0) for c in CONTRACTS} for d in DAYS}
        self.settle = {
            "2026-10-13": {"CU2612.SHF": 100500.0, "I2701.DCE": 805.0},
            "2026-10-14": {"CU2612.SHF": 101500.0, "I2701.DCE": 792.0},
            "2026-10-15": {"CU2612.SHF": 102500.0, "I2701.DCE": 798.0},
        }
        self.calls = []

    def minute_counts(self, day):
        return self.minutes[day]

    def bars(self, day, contracts):
        return {c: p for c, p in self.opens[day].items() if c in contracts}

    def limits(self, day, contracts):
        return {c: v for c, v in self.limit_rows[day].items() if c in contracts}

    def settles(self, day, contracts):
        return {c: p for c, p in self.settle[day].items() if c in contracts}

    def spec_rows(self, day):
        self.calls.append(("spec_rows", day))
        return spec_rows(day)


def write_package(root, trade_date, signal, weights, *, available=None, status="ready",
                  source_version=SOURCE_VERSION):
    d = Path(root) / trade_date
    d.mkdir(parents=True, exist_ok=True)
    with (d / "targets.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["signal_date", "product", "contract", "target_weight"])
        w.writeheader()
        for c, wt in weights.items():
            w.writerow({"signal_date": signal, "product": c[:2], "contract": c, "target_weight": wt})
    (d / "source-parameters.json").write_text(json.dumps({**PARAMS, "signal_date": signal}))
    sha = lambda p: hashlib.sha256((d / p).read_bytes()).hexdigest()  # noqa: E731
    available = available or f"{trade_date}T08:32:00+08:00"
    manifest = {
        "status": status,
        "reason": None if status == "ready" else "signal_date_mismatch",
        "source_receipt": {
            "source_version": source_version,
            "available_at": available,
            "csv_sha256": sha("targets.csv"),
            "metadata_sha256": sha("source-parameters.json"),
            "row_count": len(weights),
            "signal_date": signal,
            "synthetic": True,
        },
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    cal = calendar()
    cfg = config(cal)
    store = tmp_path / "state" / "account.sqlite"
    initialize(store, cfg, cal)
    table = tmp_path / "specs.json"
    table.write_text(json.dumps(TABLE, ensure_ascii=False))
    fake = Fake()
    targets = tmp_path / "targets"

    @contextmanager
    def factory():
        yield fake

    def run(*extra, today="2026-10-13"):
        argv = [
            "--state", str(store), "--account", ACCOUNT,
            "--targets-root", str(targets), "--spec-table", str(table),
            "--feed", str(tmp_path / "feed" / "latest.json"),
            "--verdict-dir", str(tmp_path / "verdicts"), "--today", today, *extra,
        ]
        rc = nightly.main(argv, sources_factory=factory)
        verdict = json.loads((tmp_path / "verdicts" / f"{today}.json").read_text())
        return rc, verdict

    return {"store": store, "cfg": cfg, "cal": cal, "fake": fake, "targets": targets,
            "run": run, "tmp": tmp_path}


def store_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------- happy path


def test_valid_day_registers_specs_trades_and_publishes(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12",
                  {"CU2612.SHF": 0.5, "I2701.DCE": -0.4, "LG2701.DCE": 0.0})
    rc, verdict = env["run"]()
    assert rc == 0 and verdict["status"] == "PASS", verdict
    state = read_state(env["store"], ACCOUNT)
    # 0.5 * 1e6 / (100000 * 5) = 1 lot; -0.4 * 1e6 / (800 * 100) = -5 lots
    assert state["positions"] == {"CU2612.SHF": 1, "I2701.DCE": -5}
    row = verdict["rows"][0]
    assert row["target_status"] == "valid"
    assert row["new_contracts"] == ["CU2612.SHF", "I2701.DCE"]  # zero-weight LG skipped
    assert verdict["dry_runs"][0]["nav"] == row["nav"]
    cfg, _, _ = nightly.current_references(env["store"], ACCOUNT)
    assert cfg["instruments"]["I2701.DCE"]["margin_rate"] == pytest.approx(0.12)
    assert cfg["fee_model"]["rules"]["I2701.DCE"][0]["exchange"]["open"] == {
        "value": 1.0, "unit": "PER_TEN_THOUSAND"}
    status = json.loads((env["tmp"] / "feed" / "daily-status.json").read_text())
    assert status["days"][0]["target_status"] == "valid"
    assert (env["tmp"] / "feed" / "latest.json").is_file()
    # specs derive from the signal day's rows, never the trade day's
    assert env["fake"].calls == [("spec_rows", "2026-10-12")]


# ------------------------------------------------------------ target_missing


@pytest.mark.parametrize(
    "stage, reason",
    [
        (lambda root: None, "not_produced"),
        (lambda root: write_package(root, "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5},
                                    status="not_produced"), "not_produced"),
        (lambda root: write_package(root, "2026-10-13", "2026-10-09", {"CU2612.SHF": 0.5}),
         "signal_date_mismatch"),
        (lambda root: write_package(root, "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5},
                                    available="2026-10-13T09:31:00+08:00"),
         "not_available_before_execution"),
        (lambda root: write_package(root, "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5},
                                    available="2026-10-12T14:59:00+08:00"),
         "not_available_before_execution"),
        (lambda root: write_package(root, "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5},
                                    source_version="SYNTHETIC-other"), "validation_failed"),
    ],
)
def test_unqualified_package_is_a_mechanical_target_missing(env, stage, reason):
    stage(env["targets"])
    rc, verdict = env["run"]()
    assert rc == 0, verdict
    assert verdict["rows"][0]["target_status"] == "missing"
    assert verdict["rows"][0]["target_missing"]["reason"] == reason
    state = read_state(env["store"], ACCOUNT)
    assert state["positions"] == {} and state["audit"]["trades"] == []


def test_tampered_csv_is_validation_failed(env):
    d = write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5})
    (d / "targets.csv").write_text((d / "targets.csv").read_text().replace("0.5", "0.9"))
    target, missing = nightly.classify_target(env["targets"], env["cal"]["intervals"][0], env["cfg"])
    assert target is None and missing["reason"] == "validation_failed"


def test_missing_target_carries_positions_and_never_reuses_yesterdays(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5})
    env["run"]()
    rc, verdict = env["run"](today="2026-10-14")  # no 10-14 package
    assert rc == 0
    row = verdict["rows"][0]
    assert row["target_status"] == "missing" and row["trades"] == []
    assert row["positions"] == {"CU2612.SHF": 1}
    # settle-to-settle: (101500 - 100500) * 5
    assert row["pnl"] == pytest.approx(5000)
    # the minute gate is not consulted on a no-trade day
    env["fake"].minutes["2026-10-15"] = {}
    rc, verdict = env["run"](today="2026-10-15")
    assert rc == 0 and verdict["rows"][0]["target_status"] == "missing"


# ---------------------------------------------------------------- untradable


def test_execution_evidence_classifies_bars_and_limits():
    prices, tradable, reasons = nightly.execution_evidence(
        {"A", "B", "C", "D"},
        {"A": 100.0, "B": 110.0, "C": 90.0},
        {"A": (110.0, 90.0), "B": (110.0, 90.0), "C": (110.0, 90.0)},
    )
    assert tradable == {"A": True, "B": False, "C": False, "D": False}
    assert reasons == {"B": "limit_locked_at_open", "C": "limit_locked_at_open",
                       "D": "no_0930_bar"}
    assert prices == {"A": 100.0, "B": 110.0, "C": 90.0}


def test_bar_without_limit_row_fails_closed():
    with pytest.raises(nightly.GateFailure) as exc:
        nightly.execution_evidence({"A"}, {"A": 100.0}, {})
    assert exc.value.stage == "limit_gate"


def test_untradable_contract_keeps_lots_end_to_end(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5, "I2701.DCE": -0.4})
    env["run"]()
    write_package(env["targets"], "2026-10-14", "2026-10-13", {"CU2612.SHF": 0.0, "I2701.DCE": -0.8})
    del env["fake"].opens["2026-10-14"]["CU2612.SHF"]  # no 09:30 bar
    env["fake"].limit_rows["2026-10-14"]["I2701.DCE"] = (790.0, 700.0)  # opens at the up limit
    rc, verdict = env["run"](today="2026-10-14")
    assert rc == 0, verdict
    row = verdict["rows"][0]
    assert row["untradable"] == {"CU2612.SHF": "no_0930_bar", "I2701.DCE": "limit_locked_at_open"}
    assert row["positions"] == {"CU2612.SHF": 1, "I2701.DCE": -5}
    assert row["trades"] == []


# ---------------------------------------------------------------- fail closed


def _assert_zero_write(env, before):
    assert store_sha(env["store"]) == before


def test_exchange_with_zero_minute_rows_fails_closed(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5, "I2701.DCE": -0.4})
    env["fake"].minutes["2026-10-13"] = {"SHFE": 100, "CZCE": 50}  # DCE not collected yet
    before = store_sha(env["store"])
    rc, verdict = env["run"]()
    assert rc == 2 and verdict["stage"] == "minute_gate"
    assert "DCE" in verdict["error"]
    _assert_zero_write(env, before)


def test_missing_spec_fails_closed_without_dropping_the_contract(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5, "LG2701.DCE": 0.2})
    before = store_sha(env["store"])
    rc, verdict = env["run"]()
    assert rc == 2 and verdict["stage"] == "spec_gate"
    assert set(verdict["spec_missing"]) == {"LG2701.DCE"}
    _assert_zero_write(env, before)


def test_missing_settle_fails_closed(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5, "I2701.DCE": -0.4})
    del env["fake"].settle["2026-10-13"]["I2701.DCE"]
    before = store_sha(env["store"])
    rc, verdict = env["run"]()
    assert rc == 2 and verdict["stage"] == "settle_gate"
    _assert_zero_write(env, before)


def test_dry_run_only_writes_nothing(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5, "I2701.DCE": -0.4})
    before = store_sha(env["store"])
    rc, verdict = env["run"]("--dry-run-only")
    assert rc == 0 and verdict["rows"] == []
    assert verdict["dry_runs"][0]["positions"] == {"CU2612.SHF": 1, "I2701.DCE": -5}
    _assert_zero_write(env, before)


def test_catch_up_advances_every_due_interval(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5})
    write_package(env["targets"], "2026-10-14", "2026-10-13", {"CU2612.SHF": 0.5, "I2701.DCE": -0.4})
    rc, verdict = env["run"](today="2026-10-14")
    assert rc == 0
    assert [r["trade_date"] for r in verdict["rows"]] == ["2026-10-13", "2026-10-14"]
    assert read_status(env["store"], ACCOUNT)["next_interval"]["trade_date"] == "2026-10-15"


def test_nothing_due_is_a_skip(env):
    rc, verdict = env["run"](today="2026-10-12")
    assert rc == 0 and verdict["status"] == "SKIP"


# ------------------------------------------------------------------- seal


def test_sealed_store_never_advances(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5})
    env["run"]()
    feed = env["tmp"] / "feed" / "latest.json"
    with pytest.raises(ValueError, match="expected 2026-10-14"):
        m02_seal.seal(env["store"], ACCOUNT, feed, "2026-10-14", "test")
    record = m02_seal.seal(env["store"], ACCOUNT, feed, "2026-10-13", "test close")
    assert record["store_sha256"] == store_sha(env["store"])
    assert not os.stat(env["store"]).st_mode & stat.S_IWUSR
    write_package(env["targets"], "2026-10-14", "2026-10-13", {"CU2612.SHF": 0.5})
    rc, verdict = env["run"](today="2026-10-14")
    assert rc == 0 and verdict["status"] == "SEALED"
    assert store_sha(env["store"]) == record["store_sha256"]
    # the sealed series stays readable as history
    assert read_status(env["store"], ACCOUNT)["state"]["row"]["trade_date"] == "2026-10-13"
    # even bypassing the driver, the read-only store refuses the write
    packet = {"trade_date": "2026-10-14"}
    from forward_account import advance

    if os.geteuid() != 0:
        with pytest.raises(Exception):
            advance(env["store"], ACCOUNT, deepcopy(packet))
    with pytest.raises(ValueError, match="already sealed"):
        m02_seal.seal(env["store"], ACCOUNT, feed, "2026-10-13", "again")


def test_position_past_last_trading_day_raises_an_alert(env):
    write_package(env["targets"], "2026-10-13", "2026-10-12", {"CU2612.SHF": 0.5})
    original = CONTRACTS["CU2612.SHF"]
    rows = spec_rows

    def expiring(day):
        settle, info = rows(day)
        for r in info:
            if r["contract_id"] == "CU2612":
                r["last_date"] = 20261013.0
        return settle, info

    env["fake"].spec_rows = expiring
    rc, verdict = env["run"]()
    assert rc == 0
    assert verdict["alerts"][-1] == "calendar has 2 intervals left; extend references"
    assert verdict["alerts"][:-1] == [
        {"contract": "CU2612.SHF", "last_trade_date": "2026-10-13",
         "next_trade_date": "2026-10-14",
         "alert": "held past its last trading day; next interval will fail closed"}
    ]
    assert CONTRACTS["CU2612.SHF"] == original
