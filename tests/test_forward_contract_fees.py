"""Hand arithmetic for explicitly dated contract fees; all inputs synthetic."""

from copy import deepcopy

import pytest

from forward_account import advance, export_feed, initialize, read_state
from forward_account.demo import make_inputs
from forward_account.contracts import validate_config


def rate(value, unit="YUAN_PER_LOT"):
    return {"value": value, "unit": unit}


def model(contracts=("A.TEST", "B.TEST")):
    rule = {
        "rule_id": "SYNTHETIC-rule-1",
        "valid_from": "2026-10-12",
        "valid_to": "2026-10-14",
        "exchange_source": "SYNTHETIC exchange schedule",
        "broker_source": "SYNTHETIC additional commission agreement",
        "slippage_source": "SYNTHETIC explicit zero assumption",
        "exchange": {
            "open": rate(2),
            "close": rate(3),
            "close_today": rate(7),
            "intraday_open": rate(1),
        },
        "broker": {
            k: rate(0.5) for k in ("open", "close", "close_today", "intraday_open")
        },
        "slippage": rate(0),
    }
    return {
        "version": "contract-fees-v1",
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
        "policy_source": "SYNTHETIC explicit same-day opening adjustment policy",
        "excluded_costs": ["SYNTHETIC: delivery and taxes not modeled"],
        "rules": {k: [deepcopy(rule)] for k in contracts},
    }


def new_config(tmp_path):
    config, calendar, packets = make_inputs(tmp_path / "inputs")
    del config["cost_bps"]
    config["fee_model"] = model()
    config["account_version"] = "SYNTHETIC-contract-fees-v1"
    config["lot_rule"] = "nearest_integer_ties_even"
    return config, calendar, packets


def test_new_model_two_days_reversal_nav_and_no_double_charge(tmp_path):
    config, calendar, packets = new_config(tmp_path)
    db = tmp_path / "new.sqlite"
    initialize(db, config, calendar)
    first = advance(db, config["account_id"], packets[0])
    assert first["fees"] == pytest.approx(20 * 2.5)
    assert first["nav"] == pytest.approx(10000 + 1750 - 50)
    assert first["positions"] == {"A.TEST": 15, "B.TEST": -5}
    assert all(t["action"] == "open" for t in first["audit"]["trades"])
    assert advance(db, config["account_id"], packets[0]) == first
    second = advance(db, config["account_id"], packets[1])
    # Overnight +350; B target round(12050/940)=13. Close A15/B5, open B13.
    assert second["fees"] == pytest.approx(20 * 3.5 + 13 * 2.5)
    assert second["nav"] == pytest.approx(12050 - 102.5 + 13 * 5 * 12)
    assert [
        (t["contract"], t["action"], t["quantity_change"])
        for t in second["audit"]["trades"]
    ] == [("A.TEST", "close", -15), ("B.TEST", "close", 5), ("B.TEST", "open", 13)]
    assert sum(t["fee"] for t in second["audit"]["trades"]) == second["fees"]
    assert advance(db, config["account_id"], packets[1]) == second
    feed = export_feed(db, tmp_path / "synthetic-returns.json", synthetic=True)
    assert "contract-fees-v1" in feed["series"][0]["fee_model"]
    assert len(feed["series"][0]["rows"]) == 2
    with pytest.raises(ValueError, match="immutable"):
        old = deepcopy(config)
        del old["fee_model"]
        old["cost_bps"] = 10
        initialize(db, old, calendar)


def test_units_are_explicit():
    from forward_account.fees import charge

    assert charge(rate(2), 3, 100, 10) == 6
    assert charge(rate(0.6, "PERMILLE"), 3, 100, 10) == pytest.approx(1.8)
    assert charge(rate(6, "PER_TEN_THOUSAND"), 3, 100, 10) == pytest.approx(1.8)
    for bad in (None, "absolute", "1", "bps"):
        with pytest.raises(ValueError, match="unit"):
            charge(rate(2, bad), 3, 100, 10)


def fills(prior, lots, target, date, hour=9):
    from forward_account.fees import account_trades

    return account_trades(
        model(("A.TEST",)),
        prior,
        lots,
        target,
        {"A.TEST": 100},
        {"A.TEST": {"multiplier": 10}},
        date,
        f"{date}T{hour:02}:00:00+08:00",
        "SYNTHETIC execution receipt",
    )


@pytest.mark.parametrize("sign", [1, -1])
def test_dated_fifo_partial_close_today_and_reversal(sign):
    trades, lots = fills({}, {}, {"A.TEST": sign * 2}, "2026-10-13")
    assert sum(t["fee"] for t in trades) == 5  # Both long and short use ordinary open.
    trades, lots = fills({"A.TEST": sign * 2}, lots, {"A.TEST": sign * 3}, "2026-10-14")
    assert sum(t["fee"] for t in trades) == 2.5
    trades, lots = fills(
        {"A.TEST": sign * 3}, lots, {"A.TEST": sign * 0.5}, "2026-10-14", 10
    )
    assert [t["action"] for t in trades] == ["close", "close_today"]
    # Yesterday 2*(3+.5), today .5*(7+.5) plus opening adjustment .5*(1-2).
    assert sum(t["fee"] for t in trades) == 10.25
    assert lots["A.TEST"][0]["quantity"] == sign * 0.5
    trades, lots = fills(
        {"A.TEST": sign * 0.5}, lots, {"A.TEST": -sign}, "2026-10-14", 11
    )
    assert [t["action"] for t in trades] == ["close_today", "open"]
    assert sum(t["fee"] for t in trades) == 3.25 + 2.5
    assert lots["A.TEST"][0]["quantity"] == -sign


def test_opening_adjustment_uses_entry_not_exit_notional():
    from forward_account.fees import account_trades

    m = model(("A.TEST",))
    rule = m["rules"]["A.TEST"][0]
    rule["exchange"]["open"] = rate(2, "PERMILLE")
    rule["exchange"]["intraday_open"] = rate(1, "PERMILLE")
    specs = {"A.TEST": {"multiplier": 10}}
    _, lots = account_trades(
        m,
        {},
        {},
        {"A.TEST": 2},
        {"A.TEST": 100},
        specs,
        "2026-10-13",
        "2026-10-13T09:00:00+08:00",
        "SYNTHETIC",
    )
    trades, lots = account_trades(
        m,
        {"A.TEST": 2},
        lots,
        {},
        {"A.TEST": 200},
        specs,
        "2026-10-13",
        "2026-10-13T10:00:00+08:00",
        "SYNTHETIC",
    )
    assert trades[0]["exchange_open_adjustment"] == -2
    assert trades[0]["fee"] == 13
    assert lots == {}


@pytest.mark.parametrize(
    "fault", ["unknown_unit", "broker", "overlap", "policy", "both_models"]
)
def test_config_fails_closed(tmp_path, fault):
    config, cal, _ = new_config(tmp_path)
    m = config["fee_model"]
    r = m["rules"]["A.TEST"][0]
    if fault == "unknown_unit":
        r["exchange"]["open"]["unit"] = None
    elif fault == "broker":
        r["broker"] = None
    elif fault == "overlap":
        m["rules"]["A.TEST"].append(deepcopy(r))
    elif fault == "policy":
        m["policy_source"] = None
    else:
        config["cost_bps"] = 4
    with pytest.raises(ValueError):
        validate_config(config, cal)


def test_expired_fees_and_margin_shortfall_do_not_commit(tmp_path):
    config, cal, packets = new_config(tmp_path)
    for rules in config["fee_model"]["rules"].values():
        rules[0]["valid_to"] = "2026-10-13"
    db = tmp_path / "expiry.sqlite"
    initialize(db, config, cal)
    before = advance(db, config["account_id"], packets[0])
    with pytest.raises(ValueError, match="fee.*validity"):
        advance(db, config["account_id"], packets[1])
    assert read_state(db, config["account_id"]) == before
    config, cal, packets = new_config(tmp_path)
    config["instruments"]["A.TEST"]["margin_rate"] = 0.5
    config["instruments"]["B.TEST"]["margin_rate"] = 0.5
    db = tmp_path / "margin.sqlite"
    before = initialize(db, config, cal)  # 2x gross at 50%: fits before fees only.
    with pytest.raises(ValueError, match="margin"):
        advance(db, config["account_id"], packets[0])
    assert read_state(db, config["account_id"]) == before


def test_missing_or_inconsistent_age_requires_migration():
    with pytest.raises(ValueError, match="migration"):
        fills({"A.TEST": 1}, None, {}, "2026-10-14")
    with pytest.raises(ValueError, match="lots"):
        fills({"A.TEST": 1}, {}, {}, "2026-10-14")
    _, lots = fills({}, {}, {"A.TEST": 1}, "2026-10-13")
    lots["A.TEST"][0]["opened_trade_date"] = "2026-10-15"
    with pytest.raises(ValueError):
        fills({"A.TEST": 1}, lots, {}, "2026-10-14")


def test_dated_rule_switch_and_opening_evidence_pinned():
    from forward_account.fees import account_trades

    m = model(("A.TEST",))
    old = m["rules"]["A.TEST"][0]
    old["valid_to"] = "2026-10-13"
    new = deepcopy(old)
    new.update(
        rule_id="SYNTHETIC-rule-2", valid_from="2026-10-14", valid_to="2026-10-14"
    )
    new["exchange"]["close"] = rate(9)
    new["slippage"] = rate(0.2, "PERMILLE")
    m["rules"]["A.TEST"].append(new)
    specs = {"A.TEST": {"multiplier": 10}}
    _, lots = account_trades(
        m,
        {},
        {},
        {"A.TEST": 1},
        {"A.TEST": 100},
        specs,
        "2026-10-13",
        "2026-10-13T09:00:00+08:00",
        "SYNTHETIC",
    )
    trades, _ = account_trades(
        m,
        {"A.TEST": 1},
        lots,
        {},
        {"A.TEST": 100},
        specs,
        "2026-10-14",
        "2026-10-14T09:00:00+08:00",
        "SYNTHETIC",
    )
    assert trades[0]["fee"] == pytest.approx(9.7)
    assert trades[0]["rule_id"] == "SYNTHETIC-rule-2"
    old["exchange"]["open"] = rate(8)
    with pytest.raises(ValueError, match="migration"):
        account_trades(
            m,
            {"A.TEST": 1},
            lots,
            {},
            {"A.TEST": 100},
            specs,
            "2026-10-14",
            "2026-10-14T09:00:00+08:00",
            "SYNTHETIC",
        )


def test_tail_revision_rebuilds_lots_and_fees_without_accumulating(tmp_path):
    config, calendar, packets = new_config(tmp_path)
    db = tmp_path / "revision.sqlite"
    initialize(db, config, calendar)
    first = advance(db, config["account_id"], packets[0])
    revised = deepcopy(packets[0])
    revised["settlement"]["prices"]["A.TEST"] = 111
    next_state = advance(
        db, config["account_id"], revised, revise_reason="SYNTHETIC correction"
    )
    assert next_state["fees"] == first["fees"]
    assert next_state["position_lots"] == first["position_lots"]
    assert next_state["nav"] == first["nav"] + 150


def test_demo_replays_two_isolated_feed_series(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    import subprocess
    import sys
    from forward_account.fee_demo import replay

    feed = replay(tmp_path / "evidence")
    assert len(feed["series"]) == 2
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
            "import json,sys,jsonschema; jsonschema.Draft202012Validator(json.load(open(sys.argv[1])),format_checker=jsonschema.FormatChecker()).validate(json.load(open(sys.argv[2])))",
            str(schema),
            str(tmp_path / "evidence/synthetic-returns.json"),
        ],
        check=True,
        capture_output=True,
    )
    ledger = json.loads((tmp_path / "evidence/synthetic-ledger.json").read_text())
    assert "position_lots" not in ledger["uniform-bps-v1"]["states"][0]
    assert "position_lots" in ledger["contract-fees-v1"]["states"][0]


def test_fractional_lot_roundoff_does_not_leave_ghost_positions():
    prior, lots = {}, {}
    for index, qty in enumerate((0.1, 0.7, 0.9, 0.33, 0.51, 0, -0.27, -0.1, 0)):
        from forward_account.fees import account_trades

        target = {"A.TEST": qty} if qty else {}
        _, lots = account_trades(
            model(("A.TEST",)),
            prior,
            lots,
            target,
            {"A.TEST": 100},
            {"A.TEST": {"multiplier": 10}},
            "2026-10-13",
            f"2026-10-13T09:{index:02}:00+08:00",
            "SYNTHETIC",
        )
        assert set(lots) == set(target)
        prior = target
    assert lots == {}
