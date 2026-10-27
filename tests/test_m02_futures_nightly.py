"""M02 futures nightly driver: gates, ordered catch-up, verdict JSON.

Covers the 2026-10-27 Debian unattended-nightly design letter: settle
coverage gate (missing row / zero price = FAIL, zero write), ordered
multi-day catch-up loop, money-chain self-check, PASS/FAIL/SKIP verdict
files and their exit codes. The driver advances through the same
m02_futures_advance CLI the session chain used (dry-run then commit), on
the same synthetic identity-chain harness as test_m02_futures.py -- the
monkeypatched synthetic flip is the only deviation from the production
code path.
"""

import contextlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import m02_futures_advance as adv  # noqa: E402
import m02_futures_nightly as nightly  # noqa: E402
from forward_account import advance as real_advance  # noqa: E402
from forward_account import export_feed as real_export  # noqa: E402
from forward_account import initialize  # noqa: E402
from forward_account.contracts import digest  # noqa: E402
from tests.test_m02_futures import (  # noqa: E402
    BUILD,
    CANONICAL_CONFIG,
    _write_receipt,
)

PRICE = 100.0


class _RoutingCursor:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._sql, self._params = sql, params
        if "futures_minute" in sql:
            table, date, symbols = "minute", params[0][:10], params[1]
        else:
            table, date, symbols = "settle", params[0], params[1]
        prices = self._data.get(table, {}).get(date, {})
        self._rows = [(s, prices[s]) for s in symbols if s in prices]

    def fetchall(self):
        return self._rows


class _RoutingConn:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _RoutingCursor(self._data)


def _synthetic_store(tmp_path, trade_dates, *, first_signal="2026-10-15"):
    """Initialize a synthetic identity-chain store over `trade_dates`.

    Mirrors the D-1 rehearsal pattern from test_m02_futures.py: the
    calendar is backdated and the config flipped synthetic, so backdated
    intervals are accepted without racing the wall clock.
    """
    config = json.loads(json.dumps(CANONICAL_CONFIG))
    config["synthetic"] = True
    config["initial_at"] = f"{first_signal}T15:00:00+08:00"
    config["target_policy"]["pinned"]["synthetic"] = True
    config["label"] = "TEST " + config["label"]
    intervals = []
    signal = first_signal
    for trade_date in trade_dates:
        intervals.append(
            {
                "trade_date": trade_date,
                "signal_date": signal,
                "start_at": f"{signal}T15:00:00+08:00",
                "execution_at": f"{trade_date}T09:30:00+08:00",
                "end_at": f"{trade_date}T15:00:00+08:00",
            }
        )
        signal = trade_date
    calendar = {
        "calendar_id": "m02-futures-nightly-test",
        "timezone": "Asia/Shanghai",
        "source": "TEST synthetic intervals",
        "complete": True,
        "coverage_start": trade_dates[0],
        "coverage_end": trade_dates[-1],
        "intervals": intervals,
    }
    config["calendar_id"] = calendar["calendar_id"]
    config["calendar_sha256"] = digest(calendar)
    config_path = tmp_path / "test.config.json"
    config_path.write_text(json.dumps(config))
    state = tmp_path / "account.sqlite"
    initialize(state, config, calendar)
    return config, config_path, state


def _bridge_synthetic(monkeypatch):
    def _advance(store, account, packet):
        for key in ("execution", "settlement"):
            if key in packet:
                packet[key]["synthetic"] = True
        return real_advance(store, account, packet)

    monkeypatch.setattr(adv, "advance", _advance)
    monkeypatch.setattr(
        adv, "export_feed", lambda s, p: real_export(s, p, synthetic=True)
    )


def _build_day(tmp_path, monkeypatch, data, config, config_path, state):
    """Run the build day (first interval) through the real advance CLI."""
    _bridge_synthetic(monkeypatch)
    preview = json.loads((BUILD / "integer-preview.json").read_text())
    receipt_prices = json.loads((BUILD / "czce-2026-10-20.json").read_text())["prices"]
    minute = {
        c.rpartition(".")[0]: v["settle_2026_10_16"]
        for c, v in preview["contracts"].items()
        if not c.endswith(".CZC")
    }
    data["minute"] = {"2026-10-16": minute}
    data.setdefault("settle", {})["2026-10-16"] = {
        c: v["settle_2026_10_16"] for c, v in preview["contracts"].items()
    }
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    receipt = _write_receipt(tmp_path, receipt_prices, trade_date="2026-10-16")
    assert (
        adv.main(
            [
                "--state",
                str(state),
                "--config",
                str(config_path),
                "--feed",
                str(tmp_path / "latest.json"),
                "--czce-receipt",
                str(receipt),
            ]
        )
        == 0
    )


def _hold_settle(data, trade_date, symbols, *, drop=(), zero=()):
    rows = {}
    for s in symbols:
        if s in drop:
            continue
        rows[s] = 0.0 if s in zero else PRICE
    data.setdefault("settle", {})[trade_date] = rows


def _cli(tmp_path, state, config_path, today, *, extra=()):
    return [
        "--state",
        str(state),
        "--config",
        str(config_path),
        "--feed",
        str(tmp_path / "latest.json"),
        "--verdict-dir",
        str(tmp_path / "verdicts"),
        "--today",
        today,
        *extra,
    ]


def _verdict(tmp_path, today):
    return json.loads((tmp_path / "verdicts" / f"{today}.json").read_text())


# ------------------------------------------------------------ pure helpers


def test_settle_gate_flags_missing_rows_and_nonpositive_prices():
    problems = nightly.settle_gate(
        {"A2701.DCE": 3500.0, "TA2701.CZC": 0.0, "AG2612.SHF": None}
    )
    assert problems == ["nonpositive settle: AG2612.SHF, TA2701.CZC"]


def test_settle_gate_passes_when_every_price_is_positive():
    assert nightly.settle_gate({"A2701.DCE": 3500.0}) == []


def test_money_chain_residual_and_flag():
    prev_nav, nav = 100_000_000.0, 101_000_000.0
    row = {"daily_return": nav / prev_nav - 1}
    ok, residual = nightly.money_chain(prev_nav, row, nav)
    assert ok and abs(residual) < 1e-6
    ok, residual = nightly.money_chain(prev_nav, row, nav + 1.0)
    assert not ok and abs(residual) > 0.9


# ------------------------------------------------------------ driver paths


def test_nightly_skips_when_nothing_due(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(tmp_path, ["2026-10-16"])
    data = {}
    _bridge_synthetic(monkeypatch)
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-15"))
    assert rc == 0
    verdict = _verdict(tmp_path, "2026-10-15")
    assert verdict["status"] == "SKIP"
    assert verdict["next_interval"]["trade_date"] == "2026-10-16"


def test_nightly_empty_positions_fail_closed(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(tmp_path, ["2026-10-16"])
    _bridge_synthetic(monkeypatch)
    monkeypatch.setattr(adv, "get_connection", lambda _cfg: _RoutingConn({}))
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-16"))
    assert rc == 2
    verdict = _verdict(tmp_path, "2026-10-16")
    assert verdict["status"] == "FAIL"
    assert "positions" in verdict["error"]


def test_nightly_hold_day_pass_verdict_and_money_chain(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    _hold_settle(data, "2026-10-19", config["instruments"])
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-19"))
    assert rc == 0
    verdict = _verdict(tmp_path, "2026-10-19")
    assert verdict["status"] == "PASS"
    assert [r["trade_date"] for r in verdict["rows"]] == ["2026-10-19"]
    row = verdict["rows"][0]
    assert row["positions"] == 65 and row["fees"] == 0.0
    assert abs(row["pnl_residual"]) < 1e-6  # dr * prev_nav == nav - prev_nav
    assert verdict["engine_sha256"]
    # last calendar interval consumed: the exhaustion warning must surface
    assert verdict["calendar_exhausted_after_run"] is True
    # feed carries the advanced interval
    feed = json.loads((tmp_path / "latest.json").read_text())
    assert feed["series"][0]["rows"][-1]["trade_date"] == "2026-10-19"


def test_nightly_catches_up_multi_day_gap_in_order(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19", "2026-10-20"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    for day in ("2026-10-19", "2026-10-20"):
        _hold_settle(data, day, config["instruments"])
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-20"))
    assert rc == 0
    verdict = _verdict(tmp_path, "2026-10-20")
    assert [r["trade_date"] for r in verdict["rows"]] == [
        "2026-10-19",
        "2026-10-20",
    ]


def test_nightly_missing_settle_fails_with_zero_write(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    _hold_settle(data, "2026-10-19", config["instruments"], drop=("TA2701.CZC",))
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-19"))
    assert rc == 2
    verdict = _verdict(tmp_path, "2026-10-19")
    assert verdict["status"] == "FAIL"
    assert verdict["stage"] == "settle_gate"
    assert "TA2701.CZC" in verdict["error"]
    # zero write: no second ledger row, feed still ends at the build day
    from forward_account.store import read_status

    status = read_status(state, "m02-futures-observation")
    assert status["state"]["row"]["trade_date"] == "2026-10-16"
    feed = json.loads((tmp_path / "latest.json").read_text())
    assert feed["series"][0]["rows"][-1]["trade_date"] == "2026-10-16"


def test_nightly_partial_catchup_fail_keeps_advanced_days(tmp_path, monkeypatch):
    """Day 1 of a two-day catch-up lands, day 2 settle is missing: FAIL
    verdict records both the committed interval and the blocked one."""
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19", "2026-10-20"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    _hold_settle(data, "2026-10-19", config["instruments"])
    _hold_settle(data, "2026-10-20", config["instruments"], zero=("OI2701.CZC",))
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-20"))
    assert rc == 2
    verdict = _verdict(tmp_path, "2026-10-20")
    assert verdict["status"] == "FAIL"
    assert [r["trade_date"] for r in verdict["rows"]] == ["2026-10-19"]
    assert verdict["failed_at"] == "2026-10-20"
    # where the store stood when this attempt began: a retry verdict's
    # rows[] restart empty, so the head makes partial progress traceable
    assert verdict["store_head_at_start"] == "2026-10-16"
    feed = json.loads((tmp_path / "latest.json").read_text())
    assert feed["series"][0]["rows"][-1]["trade_date"] == "2026-10-19"


def test_nightly_interval_cap_is_fail_not_silent_truncation(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19", "2026-10-20"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    for day in ("2026-10-19", "2026-10-20"):
        _hold_settle(data, day, config["instruments"])
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    rc = nightly.main(
        _cli(tmp_path, state, config_path, "2026-10-20", extra=["--max-intervals", "1"])
    )
    assert rc == 2
    verdict = _verdict(tmp_path, "2026-10-20")
    assert verdict["status"] == "FAIL" and "cap" in verdict["error"]


def test_nightly_calendar_exhausted_before_run_is_fail(tmp_path, monkeypatch):
    config, config_path, state = _synthetic_store(
        tmp_path, ["2026-10-16", "2026-10-19"]
    )
    data = {}
    _build_day(tmp_path, monkeypatch, data, config, config_path, state)
    _hold_settle(data, "2026-10-19", config["instruments"])
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: contextlib.nullcontext(_RoutingConn(data))
    )
    assert nightly.main(_cli(tmp_path, state, config_path, "2026-10-19")) == 0
    rc = nightly.main(_cli(tmp_path, state, config_path, "2026-10-20"))
    assert rc == 2
    assert _verdict(tmp_path, "2026-10-20")["status"] == "FAIL"
