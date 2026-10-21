"""M02 futures scripts: builder economics, frozen pins, fail-closed quote paths.

Regression coverage for the four 2026-10-17 initialization scripts
(prepare_m02_futures_initialization / m02_futures_advance /
m02_czce_wind_capture / m02_czce_wind_capture_host), closed after the
D-day first run per response-erratum-01. Engine-level quantity-hold
semantics (hold day = no execution quote, no trades, no fees; all-zero
build retries) live in test_forward_pinned_batch.py; here they are
exercised once more through the advance CLI end to end.

Two flagship checks:

- builder reproduction: rerunning the builder with a canned business
  calendar must reproduce the canonical build/ artifacts exactly, so any
  drift in the ruled constants (broker 0.1x, +2pp margin, 4bp slippage,
  frozen shas, window dates) turns red;
- advance CLI: build day executes at minute-bar/receipt opens, hold day
  advances settle-to-settle with zero trades and zero fees.

The CLI end-to-end runs on the same synthetic identity chain the D-1
rehearsal used (config + pinned declaration + quotes flipped synthetic,
backdated initial_at): the engine only accepts backdated intervals for
synthetic accounts, while the CLI hardcodes non-synthetic quotes, so the
test wraps adv.advance/export_feed to flip that flag back -- the wrapper
is the only deviation from the production code path.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import m02_czce_wind_capture as cap  # noqa: E402
import m02_czce_wind_capture_host as host  # noqa: E402
import m02_futures_advance as adv  # noqa: E402
import prepare_m02_futures_initialization as prep  # noqa: E402

BUILD = ROOT / "docs/requests/2026-10-17-m02-initialization/build"
CANONICAL_CONFIG = json.loads((BUILD / "account.config.json").read_text())
CANONICAL_CALENDAR = json.loads((BUILD / "calendar.json").read_text())
TARGETS_CSV_SHA = (
    "3ce0759efb9c4a3f2ae105fc864768ab50eeccd0d107ab0b9b9ca260e909f248"
)


# ---------------------------------------------------------------- builder


def test_builder_reproduces_canonical_artifacts(tmp_path, monkeypatch):
    """Canned calendar -> exact rebuild of the frozen build/ files.

    The config carries no timestamps (the calendar query receipt lives
    only in the manifest), so a rerun over unchanged frozen inputs must
    equal the committed artifacts. Any constant drift (multiplier,
    markup, slippage, pinned shas, window dates) breaks the equality.
    """
    trade_days = [row["trade_date"] for row in CANONICAL_CALENDAR["intervals"]]
    monkeypatch.setattr(
        prep,
        "_business_days",
        lambda: (
            trade_days,
            "2026-10-19",
            {
                "table": "data_manager.business_calendar",
                "calendar_id": "CN",
                "queried_at": "2026-10-18T20:00:00+08:00",
                "row_count": len(trade_days) + 1,
                "first": "2026-10-19",
                "last": trade_days[-1],
                "trade_days_from_d": len(trade_days),
                "prior_trading_day": "2026-10-19",
            },
        ),
    )
    monkeypatch.setattr(prep, "REQUEST_DIR", tmp_path)
    assert prep.main(["--skip-preview"]) == 0
    rebuilt_config = json.loads((tmp_path / "build/account.config.json").read_text())

    # The committed artifacts predate the citation correction (commit 9a035b1
    # / response-erratum-01): the erratum fixed the ruling numbering in the
    # builder's source strings and left the sha-frozen artifacts untouched.
    # Strip exactly those three string families, pin the divergence itself,
    # and require everything else -- all economics, pins, dates -- to match.
    def _strip_citation_strings(config):
        config = json.loads(json.dumps(config))
        config["fee_model"].pop("policy_source")
        for rules in config["fee_model"]["rules"].values():
            for rule in rules:
                rule.pop("slippage_source")
        for spec in config["instruments"].values():
            spec.pop("source")
        return config

    assert _strip_citation_strings(rebuilt_config) == _strip_citation_strings(
        CANONICAL_CONFIG
    )
    rebuilt_source = rebuilt_config["fee_model"]["rules"]["TA2701.CZC"][0]
    frozen_source = CANONICAL_CONFIG["fee_model"]["rules"]["TA2701.CZC"][0]
    assert "4-2" in rebuilt_source["slippage_source"]  # corrected numbering
    assert "4-3" in frozen_source["slippage_source"]  # frozen pre-erratum fossil
    rebuilt_calendar = json.loads((tmp_path / "build/calendar.json").read_text())
    assert rebuilt_calendar == CANONICAL_CALENDAR


def test_broker_leg_is_exactly_one_tenth_of_exchange():
    rules = {
        "AG2612.SHF": [
            {
                "exchange": {
                    "open": {"value": 0.00005, "unit": "RATIO"},
                    "close": {"value": 0.00005, "unit": "RATIO"},
                }
            }
        ],
        "TA2701.CZC": [{"exchange": {"open": {"value": 3.0, "unit": "YUAN_PER_LOT"}}}],
    }
    prep._fill_broker_and_slippage(rules)
    ag = rules["AG2612.SHF"][0]
    assert ag["broker"]["open"] == {"value": 5e-06, "unit": "RATIO"}
    assert ag["broker"]["close"] == {"value": 5e-06, "unit": "RATIO"}
    ta = rules["TA2701.CZC"][0]
    assert ta["broker"]["open"] == {"value": 0.3, "unit": "YUAN_PER_LOT"}
    for contract_rules in rules.values():
        for rule in contract_rules:
            assert rule["slippage"] == {"value": 4, "unit": "PER_TEN_THOUSAND"}
            assert rule["valid_to"] == "2026-12-31"
    with pytest.raises(ValueError, match="already priced"):
        prep._fill_broker_and_slippage(
            {
                "AG2612.SHF": [
                    {"exchange": {"open": {"value": 1.0, "unit": "RATIO"}}, "broker": {}}
                ]
            }
        )


def test_margin_markup_adds_two_points_and_freezes_window():
    instruments = {"A2701.DCE": {"margin_rate": "0.07", "source": "exchange 2026-10-15"}}
    prep._mark_up_margin(instruments)
    spec = instruments["A2701.DCE"]
    assert spec["margin_rate"] == 0.09  # exact: 0.07 + 0.02, not 0.0899...
    assert spec["valid_to"] == "2026-12-31"
    assert "0.07" in spec["source"] and "+2pp" in spec["source"]


def test_czce_unit_ruling_fills_exactly_the_thirteen_absolute_contracts():
    rules = {
        contract: [{"exchange": {"open": {"value": 3.0, "unit": None}}}]
        for contract in prep.CZCE_ABSOLUTE_YUAN_PER_LOT
    }
    rules["SA2701.CZC"] = [{"exchange": {"open": {"value": 1e-05, "unit": "RATIO"}}}]
    filled = prep._apply_czce_unit_ruling(rules)
    assert filled == sorted(prep.CZCE_ABSOLUTE_YUAN_PER_LOT)
    assert rules["TA2701.CZC"][0]["exchange"]["open"]["unit"] == "YUAN_PER_LOT"
    assert rules["SA2701.CZC"][0]["exchange"]["open"]["unit"] == "RATIO"  # untouched
    rules["UR2701.CZC"] = [{"exchange": {"open": {"value": 5.0, "unit": None}}}]
    with pytest.raises(ValueError, match="unexpected null fee unit"):
        prep._apply_czce_unit_ruling(rules)


def test_frozen_pins_match_files_and_config():
    pinned = CANONICAL_CONFIG["target_policy"]["pinned"]
    assert pinned["csv_sha256"] == TARGETS_CSV_SHA
    assert pinned["csv_sha256"] == hashlib.sha256(
        prep.FROZEN_TARGETS_CSV.read_bytes()
    ).hexdigest()
    assert pinned["metadata_sha256"] == hashlib.sha256(
        prep.FROZEN_METADATA.read_bytes()
    ).hexdigest()
    assert pinned["row_count"] == 67
    assert pinned["signal_date"] == "2026-10-15"
    assert pinned["available_at"] == "2026-10-15T20:06:21.433269+08:00"
    # the advance script must re-read the exact files the config pins
    assert adv.FROZEN_TARGETS_CSV == prep.FROZEN_TARGETS_CSV
    assert adv.FROZEN_METADATA == prep.FROZEN_METADATA


# ------------------------------------------------------- CZCE capture side


def _czce_payload(trade_date="2026-10-20", *, drop=(), zero_open=(), no_bar=(),
                  bad_code=()):
    codes = {}
    for contract in cap.CZCE_CONTRACTS:
        if contract in drop:
            continue
        entry = {"errorcode": 0, "fields": ["open"], "bars": []}
        if contract not in no_bar:
            price = 0.0 if contract in zero_open else 100.0
            entry["bars"].append({"time": f"{trade_date} 09:30:00", "open": price})
        if contract in bad_code:
            entry = {"errorcode": -40520007, "error": "Server not Find Data"}
        codes[contract] = entry
    return {
        "status": "ok",
        "trade_date": trade_date,
        "captured_at": f"{trade_date}T21:00:00",
        "codes": codes,
    }


def _band_all(payload, factor=0.1):
    """Band at +-10% around each captured open; skips unusable entries."""
    return {
        contract: (entry["bars"][0]["open"] * (1 - factor),
                   entry["bars"][0]["open"] * (1 + factor))
        for contract, entry in payload["codes"].items()
        if entry.get("errorcode") == 0 and entry.get("bars")
    }


def test_capture_receipt_happy_path_is_band_checked(monkeypatch):
    payload = _czce_payload()
    monkeypatch.setattr(cap, "_daily_band", lambda trade_date: _band_all(payload))
    receipt = cap.build_receipt(payload, "2026-10-20", "raw.json")
    assert receipt["schema"] == "m02-czce-wind-minute-receipt.v1"
    assert receipt["trade_date"] == "2026-10-20" and receipt["bar_time"] == "09:30:00"
    assert receipt["captured_at"] == "2026-10-20T21:00:00+08:00"  # tz appended
    assert sorted(receipt["prices"]) == sorted(cap.CZCE_CONTRACTS)
    assert len(receipt["raw_sha256"]) == 64
    assert receipt["plausibility"]["futures_daily_band_checked"] == sorted(
        cap.CZCE_CONTRACTS
    )
    assert receipt["plausibility"]["futures_daily_band_pending"] == []


def test_capture_receipt_pending_band_is_tolerated_but_recorded(monkeypatch):
    payload = _czce_payload()
    monkeypatch.setattr(cap, "_daily_band", lambda trade_date: {})
    receipt = cap.build_receipt(payload, "2026-10-20", "raw.json")
    assert receipt["plausibility"]["futures_daily_band_pending"] == sorted(
        cap.CZCE_CONTRACTS
    )
    assert receipt["plausibility"]["futures_daily_band_checked"] == []


@pytest.mark.parametrize(
    "payload_kw, message",
    [
        ({"drop": ("PR2612.CZC",)}, "missing codes"),
        ({"bad_code": ("TA2701.CZC",)}, "wsi errorcode"),
        ({"no_bar": ("FG2701.CZC",)}, "no 09:30:00 bar"),
        ({"zero_open": ("MA2701.CZC",)}, "non-positive open"),
    ],
)
def test_capture_receipt_fails_closed(monkeypatch, payload_kw, message):
    payload = _czce_payload(**payload_kw)
    monkeypatch.setattr(cap, "_daily_band", lambda trade_date: _band_all(payload))
    with pytest.raises(ValueError, match=message):
        cap.build_receipt(payload, "2026-10-20", "raw.json")


def test_capture_receipt_rejects_price_outside_daily_band(monkeypatch):
    payload = _czce_payload()
    tight = {
        contract: (entry["bars"][0]["open"], entry["bars"][0]["open"])
        for contract, entry in payload["codes"].items()
    }
    tight["SA2701.CZC"] = (50.0, 60.0)  # capture says 100
    monkeypatch.setattr(cap, "_daily_band", lambda trade_date: tight)
    with pytest.raises(ValueError, match="outside daily band"):
        cap.build_receipt(payload, "2026-10-20", "raw.json")


def test_bar_at_matches_t_separator_and_minute_start():
    entry = {"bars": [{"time": "2026-10-20T09:29:00", "open": 1.0},
                      {"time": "2026-10-20T09:30:00", "open": 2.0}]}
    assert cap._bar_at(entry)["open"] == 2.0
    assert cap._bar_at({"bars": [{"time": "2026-10-20 09:31:00"}]}) is None


def test_wind_code_mapping_is_czce_only_one_digit_year():
    f = host._czce_wind_code
    assert f("TA2701.CZC") == "TA701.CZC"
    assert f("PR2612.CZC") == "PR612.CZC"
    assert f("PL2611.CZC") == "PL611.CZC"
    assert f("TA701.CZC") == "TA701.CZC"  # already one-digit: unchanged
    # non-CZCE must pass through: SHF keeps its full year digits
    assert f("AG2612.SHF") == "AG2612.SHF"
    assert f("LC2701.GFE") == "LC2701.GFE"


def test_capture_contract_list_equals_frozen_batch():
    import csv

    frozen = sorted(
        row["contract"]
        for row in csv.DictReader(prep.FROZEN_TARGETS_CSV.open())
        if row["contract"].endswith(".CZC")
    )
    assert sorted(cap.CZCE_CONTRACTS) == frozen == sorted(host.CZCE_CONTRACTS)


# ------------------------------------------------------------ advance side


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._sql, self._params = sql, params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        cursor = _FakeCursor(self._rows)
        self.queries.append(cursor)
        return cursor


def test_minute_opens_maps_bare_codes_and_fails_closed(monkeypatch):
    config = CANONICAL_CONFIG
    non_czce = [c for c in config["instruments"] if not c.endswith(".CZC")]
    rows = [(c.rpartition(".")[0], 100.0 + i) for i, c in enumerate(non_czce)]
    conn = _FakeConn(rows)
    monkeypatch.setattr(adv, "get_connection", lambda _cfg: conn)
    opens = adv._minute_opens(config["instruments"], "2026-10-20")
    assert opens[non_czce[0]] == 100.0
    # queried bare codes only: no suffix can leak into the symbol list
    queried = conn.queries[0]._params[1]
    assert all("." not in code for code in queried)

    conn_missing = _FakeConn(rows[:-1])
    monkeypatch.setattr(adv, "get_connection", lambda _cfg: conn_missing)
    with pytest.raises(ValueError, match="futures_minute missing 09:30 bar"):
        adv._minute_opens(config["instruments"], "2026-10-20")


def test_settlement_fails_closed_on_missing_rows(monkeypatch):
    config = CANONICAL_CONFIG
    wanted = sorted(config["instruments"])[:-1]
    monkeypatch.setattr(
        adv, "get_connection", lambda _cfg: _FakeConn([(c, 50.0) for c in wanted])
    )
    with pytest.raises(ValueError, match="futures_daily settle missing"):
        adv._settlement(config, "2026-10-20", set(config["instruments"]))


def _write_receipt(tmp_path, prices, trade_date="2026-10-20"):
    receipt = {
        "schema": "m02-czce-wind-minute-receipt.v1",
        "trade_date": trade_date,
        "captured_at": f"{trade_date}T21:00:00+08:00",
        "raw_sha256": "0" * 64,
        "prices": prices,
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    return path


def _czce_prices(config, value=100.0):
    return {c: value for c in adv._czce_contracts(config)}


def test_czce_opens_validates_receipt_fail_closed(tmp_path, monkeypatch):
    config = CANONICAL_CONFIG
    monkeypatch.setattr(adv, "get_connection", lambda _cfg: _FakeConn([]))
    path = _write_receipt(tmp_path, _czce_prices(config))
    values, receipt = adv._czce_opens(path, config, "2026-10-20")
    assert values == _czce_prices(config) and receipt["schema"].endswith(".v1")

    prices = _czce_prices(config)
    prices.pop("OI2701.CZC")
    with pytest.raises(ValueError, match="CZCE receipt missing"):
        adv._czce_opens(_write_receipt(tmp_path, prices), config, "2026-10-20")

    with pytest.raises(ValueError, match="trade_date mismatch"):
        adv._czce_opens(path, config, "2026-10-21")

    negative = _czce_prices(config, value=-1.0)
    with pytest.raises(ValueError, match="non-positive CZCE price"):
        adv._czce_opens(_write_receipt(tmp_path, negative), config, "2026-10-20")

    wrong_schema = _write_receipt(tmp_path, _czce_prices(config))
    broken = json.loads(wrong_schema.read_text())
    broken["schema"] = "something.else"
    wrong_schema.write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="unexpected CZCE receipt schema"):
        adv._czce_opens(wrong_schema, config, "2026-10-20")


def test_advance_cli_builds_then_holds_with_zero_trades_and_fees(
    tmp_path, monkeypatch, capsys
):
    """End to end through the real CLI on a synthetic copy of the account.

    Day 1 (build) executes 65 trades -- 48 minute-bar opens + 19 CZCE
    receipt prices; day 2 (hold) advances settle-to-settle with zero
    trades, zero fees and no execution quote or receipt at all. Both
    calendar days are in the past (2026-10-16 / 10-19) so the test never
    races the wall clock on interval completion.
    """
    from forward_account import advance as real_advance
    from forward_account import export_feed as real_export
    from forward_account import initialize
    from forward_account.contracts import digest

    preview = json.loads((BUILD / "integer-preview.json").read_text())
    receipt_prices = json.loads((BUILD / "czce-2026-10-20.json").read_text())["prices"]
    settle = {c: v["settle_2026_10_16"] for c, v in preview["contracts"].items()}
    minute = {
        c.rpartition(".")[0]: v["settle_2026_10_16"]
        for c, v in preview["contracts"].items()
        if not c.endswith(".CZC")
    }
    queried = {"minute": sorted(minute.items()), "daily": sorted(settle.items())}

    class _RoutingCursor(_FakeCursor):
        def execute(self, sql, params):
            self._sql, self._params = sql, params
            self._rows = (
                queried["minute"] if "futures_minute" in sql else queried["daily"]
            )

    class _RoutingConn(_FakeConn):
        def cursor(self):
            cursor = _RoutingCursor([])
            self.queries.append(cursor)
            return cursor

    monkeypatch.setattr(adv, "get_connection", lambda _cfg: _RoutingConn([]))

    config = json.loads(json.dumps(CANONICAL_CONFIG))
    config["synthetic"] = True
    config["initial_at"] = "2026-10-15T15:00:00+08:00"
    config["target_policy"]["pinned"]["synthetic"] = True
    config["label"] = "TEST " + config["label"]
    calendar = {
        "calendar_id": "m02-futures-test-two-days",
        "timezone": "Asia/Shanghai",
        "source": "TEST two backdated intervals (synthetic identity chain)",
        "complete": True,
        "coverage_start": "2026-10-16",
        "coverage_end": "2026-10-19",
        "intervals": [
            {
                "trade_date": trade_date,
                "signal_date": signal_date,
                "start_at": f"{signal_date}T15:00:00+08:00",
                "execution_at": f"{trade_date}T09:30:00+08:00",
                "end_at": f"{trade_date}T15:00:00+08:00",
            }
            for trade_date, signal_date in (
                ("2026-10-16", "2026-10-15"),
                ("2026-10-19", "2026-10-16"),
            )
        ],
    }
    config["calendar_id"] = calendar["calendar_id"]
    config["calendar_sha256"] = digest(calendar)
    config_path = tmp_path / "test.config.json"
    config_path.write_text(json.dumps(config))

    state = tmp_path / "account.sqlite"
    initialize(state, config, calendar)

    feed = tmp_path / "latest.json"

    def _synthetic_bridge(store, account, packet):
        # rehearsal-style identity flip: the CLI hardcodes non-synthetic
        # quotes, but backdated rehearsal stores must carry synthetic ones
        for key in ("execution", "settlement"):
            if key in packet:
                packet[key]["synthetic"] = True
        return real_advance(store, account, packet)

    def _export_synthetic(store, path):
        return real_export(store, path, synthetic=True)

    monkeypatch.setattr(adv, "advance", _synthetic_bridge)
    monkeypatch.setattr(adv, "export_feed", _export_synthetic)

    receipt_path = _write_receipt(tmp_path, receipt_prices, trade_date="2026-10-16")
    common = ["--state", str(state), "--config", str(config_path), "--feed", str(feed)]
    # build day without a receipt fails with a clean message (not Path(None))
    with pytest.raises(ValueError, match="requires --czce-receipt"):
        adv.main(common)
    assert adv.main(common + ["--czce-receipt", str(receipt_path)]) == 0
    day1 = capsys.readouterr().out
    assert day1.startswith("2026-10-16:")
    assert "trades=65" in day1  # 67 - AU2612 - PB2612 zero-lot (frozen preview)

    # hold day: no --czce-receipt, no execution quote, settle-to-settle only
    assert adv.main(common) == 0
    day2 = capsys.readouterr().out
    assert day2.startswith("2026-10-19:")
    assert "trades=0" in day2 and "fees=0.00" in day2
    held = re.search(r"positions=(\d+)", day1).group(1)
    assert f"positions={held}" in day2  # quantity-hold kept every position

    block = json.loads(feed.read_text())["series"][0]
    assert [row["trade_date"] for row in block["rows"]] == [
        "2026-10-16",
        "2026-10-19",
    ]
