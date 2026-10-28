"""daily_policy: per-day targets, mechanical target_missing, untradable holds.

Every expected amount below is hand arithmetic on the synthetic prices in
`world()`, not a re-run of the engine.
"""

from copy import deepcopy

import pytest

from forward_account import advance, initialize, read_state
from forward_account.contracts import DAILY_POLICY, SOURCE_RULES, digest
from forward_account.fees import ACTIONS
from forward_account.store import extend_references, read_status

SHA = "a" * 64
PARAMS = dict(SOURCE_RULES["citic027_r150_t1"][1])


def rate(value, unit="YUAN_PER_LOT"):
    return {"value": value, "unit": unit}


def fee_rule(contract, exchange=2.0, valid_to="2026-10-16"):
    return [
        {
            "rule_id": f"SYNTHETIC-{contract}",
            "valid_from": "2026-10-12",
            "valid_to": valid_to,
            "exchange_source": "SYNTHETIC daily-policy test",
            "broker_source": "SYNTHETIC 0.1 x exchange",
            "slippage_source": "SYNTHETIC zero slippage",
            "exchange": dict(zip(ACTIONS, map(rate, (exchange,) * 4))),
            "broker": dict(zip(ACTIONS, map(rate, (exchange / 10,) * 4))),
            "slippage": rate(0, "PER_TEN_THOUSAND"),
        }
    ]


def spec(multiplier, valid_to="2026-10-16"):
    return {
        "asset_type": "linear_futures",
        "currency": "CNY",
        "multiplier": multiplier,
        "margin_rate": 0.1,
        "valid_from": "2026-10-12",
        "valid_to": valid_to,
        "source": "SYNTHETIC",
    }


def calendar(days=(13, 14, 15)):
    intervals = [
        {
            "trade_date": f"2026-10-{d}",
            "signal_date": f"2026-10-{d - 1}",
            "start_at": f"2026-10-{d - 1}T15:00:00+08:00",
            "execution_at": f"2026-10-{d}T09:30:00+08:00",
            "end_at": f"2026-10-{d}T15:00:00+08:00",
        }
        for d in days
    ]
    return {
        "calendar_id": "synthetic-daily-v1",
        "timezone": "Asia/Shanghai",
        "source": "SYNTHETIC explicit test sessions",
        "complete": True,
        "coverage_start": intervals[0]["trade_date"],
        "coverage_end": intervals[-1]["trade_date"],
        "intervals": intervals,
    }


def world(*, daily=True, contracts=("A.TEST", "B.TEST", "C.DCE")):
    cal = calendar()
    mult = {"A.TEST": 10, "B.TEST": 5, "C.DCE": 10, "D.TEST": 10}
    config = {
        "account_id": "synthetic-daily",
        "account_version": "synthetic-daily-v1",
        "adapter": "citic027_r150_t1",
        "source_version": "SYNTHETIC-daily-source-v1",
        "source_parameters": dict(PARAMS),
        "label": "SYNTHETIC daily policy",
        "mode": "paper",
        "currency": "CNY",
        "synthetic": True,
        "initial_at": "2026-10-12T15:00:00+08:00",
        "initial_capital": 100000.0,
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
            "rules": {c: fee_rule(c) for c in contracts},
        },
        "fee_description": "SYNTHETIC 2.2 CNY per lot",
        "lot_rule": "nearest_integer_ties_even",
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
        "calendar_id": cal["calendar_id"],
        "calendar_sha256": digest(cal),
        "instruments": {c: spec(mult[c]) for c in contracts},
    }
    if daily:
        config["daily_policy"] = dict(DAILY_POLICY)
    return config, cal


def target(day, weights, *, signal=None, available="T08:30:00+08:00"):
    return {
        "source_version": "SYNTHETIC-daily-source-v1",
        "available_at": f"2026-10-{day}{available}",
        "csv_sha256": SHA,
        "metadata_sha256": SHA,
        "row_count": len(weights),
        "signal_date": signal or f"2026-10-{day - 1}",
        "synthetic": True,
        "parameters": dict(PARAMS),
        "weights": weights,
    }


def execution(day, prices, *, blocked=None):
    blocked = blocked or {}
    tradable = {k: k not in blocked for k in set(prices) | set(blocked)}
    return {
        "kind": "execution",
        "at": f"2026-10-{day}T09:30:00+08:00",
        "available_at": f"2026-10-{day}T21:05:00+08:00",
        "source": "SYNTHETIC 09:30 bar open",
        "synthetic": True,
        "prices": prices,
        "tradable": tradable,
        "untradable_reasons": blocked,
    }


def settlement(day, prices):
    return {
        "kind": "settlement",
        "at": f"2026-10-{day}T15:00:00+08:00",
        "available_at": f"2026-10-{day}T21:05:00+08:00",
        "source": "SYNTHETIC settle",
        "synthetic": True,
        "prices": prices,
    }


def missing_packet(day, settle, reason="not_produced"):
    return {
        "trade_date": f"2026-10-{day}",
        "target": None,
        "target_missing": {
            "reason": reason,
            "detail": "SYNTHETIC no target package by 09:30",
            "evidence": {"target_dir": f"targets/2026-10-{day}", "exists": False},
        },
        "settlement": settlement(day, settle),
    }


@pytest.fixture
def store(tmp_path):
    config, cal = world()
    db = tmp_path / "state.sqlite"
    initialize(db, config, cal)
    return db, config, cal


def build_day13(db, config):
    """10% long A at 1000 (x10) and 10% short B at 2000 (x5) on 100,000."""
    return advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-13",
            "target": target(13, {"A.TEST": 0.1, "B.TEST": -0.1}),
            "execution": execution(13, {"A.TEST": 1000.0, "B.TEST": 2000.0}),
            "settlement": settlement(13, {"A.TEST": 1010.0, "B.TEST": 1990.0}),
        },
    )


def test_build_sizes_from_daily_target(store):
    db, config, _ = store
    first = build_day13(db, config)
    # 0.1 * 100000 / (1000 * 10) = 1 lot A; -0.1 * 100000 / (2000 * 5) = -1 lot B
    assert first["positions"] == {"A.TEST": 1, "B.TEST": -1}
    # 2 lots x (2 exchange + 0.2 broker) = 4.4 CNY
    assert first["fees"] == pytest.approx(4.4)
    # A +10 x 10 = 100, B short -10 x 5 -> +50
    assert first["pnl"] == pytest.approx(150)
    assert first["nav"] == pytest.approx(100000 + 150 - 4.4)
    assert first["audit"]["target_status"] == "valid"
    assert first["audit"]["untradable"] == {}


def test_target_missing_carries_positions_and_marks_settle_to_settle(store):
    db, config, _ = store
    first = build_day13(db, config)
    second = advance(
        db,
        config["account_id"],
        missing_packet(14, {"A.TEST": 1030.0, "B.TEST": 1980.0}),
    )
    assert second["positions"] == first["positions"]
    assert second["audit"]["trades"] == []
    assert second["fees"] == 0
    # A +20 x 10 = 200; B short, -10 x 5 -> +50
    assert second["pnl"] == pytest.approx(250)
    assert second["nav"] == pytest.approx(first["nav"] + 250)
    assert second["row"]["daily_return"] == pytest.approx(250 / first["nav"])
    assert second["audit"]["target_status"] == "missing"
    assert second["audit"]["target_missing"]["reason"] == "not_produced"
    assert second["audit"]["signal_date"] is None
    assert second["position_lots"] == first["position_lots"]


def test_target_missing_on_empty_book_publishes_a_flat_day(store):
    db, config, _ = store
    state = advance(db, config["account_id"], missing_packet(13, {}))
    assert state["positions"] == {}
    assert state["nav"] == 100000
    assert state["row"]["daily_return"] == 0
    assert read_status(db, config["account_id"])["next_interval"]["trade_date"] == (
        "2026-10-14"
    )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["target_missing"].update(reason="stale_target_reused"), "reason"),
        (lambda p: p["target_missing"].update(detail=""), "detail"),
        (lambda p: p["target_missing"].pop("evidence"), "target_missing"),
        (lambda p: p.update(execution={}), "unsupported fields"),
    ],
)
def test_target_missing_packet_is_strict(store, mutate, message):
    db, config, _ = store
    build_day13(db, config)
    packet = missing_packet(14, {"A.TEST": 1030.0, "B.TEST": 1980.0})
    mutate(packet)
    with pytest.raises(ValueError, match=message):
        advance(db, config["account_id"], packet)


def test_missing_target_is_not_accepted_without_daily_policy(tmp_path):
    config, cal = world(daily=False)
    db = tmp_path / "strict.sqlite"
    initialize(db, config, cal)
    with pytest.raises(ValueError, match="missing required fields"):
        advance(db, config["account_id"], missing_packet(13, {}))


def test_untradable_held_contract_without_bar_keeps_lots(store):
    db, config, _ = store
    first = build_day13(db, config)
    # A has no 09:30 bar; target wants A flat and B doubled.
    second = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-14",
            "target": target(14, {"A.TEST": 0.0, "B.TEST": -0.2}),
            "execution": execution(
                14, {"B.TEST": 2000.0}, blocked={"A.TEST": "no_0930_bar"}
            ),
            "settlement": settlement(14, {"A.TEST": 1030.0, "B.TEST": 1990.0}),
        },
    )
    assert second["positions"]["A.TEST"] == 1
    # B: gap 1990 -> 2000 on -1 lot = -50; pretrade NAV = first nav - 50 (A gap 0)
    pretrade = first["nav"] - 50
    assert second["audit"]["pretrade_nav"] == pytest.approx(pretrade)
    assert second["positions"]["B.TEST"] == round(-0.2 * pretrade / (2000 * 5))
    assert [t["contract"] for t in second["audit"]["trades"]] == ["B.TEST"]
    assert second["audit"]["untradable"] == {"A.TEST": "no_0930_bar"}
    # A whole-day settle-to-settle: +20 x 10 = 200 lands in the intraday mark.
    # B: -2 lots from 2000 to 1990 = +100; gap -50.
    assert second["pnl"] == pytest.approx(200 - 50 + 100)


def test_limit_locked_contract_marks_through_its_bar(store):
    db, config, _ = store
    first = build_day13(db, config)
    second = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-14",
            "target": target(14, {"A.TEST": 0.3, "B.TEST": -0.1}),
            "execution": execution(
                14,
                {"A.TEST": 1050.0, "B.TEST": 1990.0},
                blocked={"A.TEST": "limit_locked_at_open"},
            ),
            "settlement": settlement(14, {"A.TEST": 1040.0, "B.TEST": 1990.0}),
        },
    )
    assert second["positions"] == {"A.TEST": 1, "B.TEST": -1}
    assert second["audit"]["trades"] == []
    # gap A 1010 -> 1050 = +400, intraday 1050 -> 1040 = -100; total = 30 x 10
    assert second["audit"]["gap_pnl"] == pytest.approx(400)
    assert second["pnl"] == pytest.approx(300)
    assert second["nav"] == pytest.approx(first["nav"] + 300)


def test_untradable_unheld_contract_is_not_opened(store):
    db, config, _ = store
    state = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-13",
            "target": target(13, {"A.TEST": 0.1, "B.TEST": -0.1}),
            "execution": execution(
                13, {"A.TEST": 1000.0}, blocked={"B.TEST": "no_0930_bar"}
            ),
            "settlement": settlement(13, {"A.TEST": 1010.0}),
        },
    )
    assert state["positions"] == {"A.TEST": 1}


@pytest.mark.parametrize(
    "prices, blocked, tradable_patch, message",
    [
        # tradable contract without a price
        ({"A.TEST": 1000.0}, {}, {"B.TEST": True}, "missing required contract price"),
        # untradable without a stated reason
        ({"A.TEST": 1000.0}, {}, {"B.TEST": False}, "reasons must match"),
        # reason given for a tradable contract
        (
            {"A.TEST": 1000.0, "B.TEST": 2000.0},
            {"B.TEST": "x"},
            {"B.TEST": True},
            "reasons must match",
        ),
        # truthy but not an explicit boolean
        ({"A.TEST": 1000.0, "B.TEST": 2000.0}, {}, {"B.TEST": 1}, "explicit tradable"),
    ],
)
def test_execution_evidence_is_explicit(store, prices, blocked, tradable_patch, message):
    db, config, _ = store
    quote = execution(13, prices, blocked=blocked)
    quote["tradable"].update(tradable_patch)
    with pytest.raises(ValueError, match=message):
        advance(
            db,
            config["account_id"],
            {
                "trade_date": "2026-10-13",
                "target": target(13, {"A.TEST": 0.1, "B.TEST": -0.1}),
                "execution": quote,
                "settlement": settlement(13, {"A.TEST": 1010.0, "B.TEST": 1990.0}),
            },
        )


def test_zero_weight_unheld_row_needs_no_spec_or_quote(store):
    db, config, _ = store
    state = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-13",
            "target": target(13, {"A.TEST": 0.1, "ZZ.UNKNOWN": 0.0}),
            "execution": execution(13, {"A.TEST": 1000.0}),
            "settlement": settlement(13, {"A.TEST": 1010.0}),
        },
    )
    assert state["positions"] == {"A.TEST": 1}


def test_nonzero_target_without_spec_fails_closed(store):
    db, config, _ = store
    with pytest.raises(ValueError, match="specification missing"):
        advance(
            db,
            config["account_id"],
            {
                "trade_date": "2026-10-13",
                "target": target(13, {"A.TEST": 0.1, "D.TEST": 0.1}),
                "execution": execution(13, {"A.TEST": 1000.0, "D.TEST": 500.0}),
                "settlement": settlement(13, {"A.TEST": 1010.0, "D.TEST": 501.0}),
            },
        )
    assert read_state(db, config["account_id"])["row"] is None


def test_daily_target_signal_date_is_strict(store):
    db, config, _ = store
    with pytest.raises(ValueError, match="prior trading day"):
        advance(
            db,
            config["account_id"],
            {
                "trade_date": "2026-10-13",
                "target": target(13, {"A.TEST": 0.1}, signal="2026-10-11"),
                "execution": execution(13, {"A.TEST": 1000.0}),
                "settlement": settlement(13, {"A.TEST": 1010.0}),
            },
        )


def test_daily_and_pinned_policies_are_exclusive(tmp_path):
    config, cal = world()
    config["target_policy"] = {"mode": "pinned_batch", "pinned": {}}
    with pytest.raises(ValueError, match="daily_policy"):
        initialize(tmp_path / "x.sqlite", config, cal)


def _extend_with_d(db, config, cal):
    instruments = deepcopy(config["instruments"])
    instruments["D.TEST"] = spec(10)
    rules = deepcopy(config["fee_model"]["rules"])
    rules["D.TEST"] = fee_rule("D.TEST", exchange=1.0)
    extend_references(db, config["account_id"], cal, instruments, rules)
    return instruments, rules


def test_fee_rule_extension_admits_a_new_contract(store):
    db, config, cal = store
    build_day13(db, config)
    _extend_with_d(db, config, cal)
    state = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-14",
            "target": target(14, {"A.TEST": 0.1, "B.TEST": -0.1, "D.TEST": 0.05}),
            "execution": execution(
                14, {"A.TEST": 1010.0, "B.TEST": 1990.0, "D.TEST": 500.0}
            ),
            "settlement": settlement(
                14, {"A.TEST": 1010.0, "B.TEST": 1990.0, "D.TEST": 500.0}
            ),
        },
    )
    assert state["positions"]["D.TEST"] == 1
    d_fill = [t for t in state["audit"]["trades"] if t["contract"] == "D.TEST"]
    # D rule: 1.0 exchange + 0.1 broker per lot
    assert d_fill[0]["fee"] == pytest.approx(1.1)


def test_existing_fee_schedules_are_immutable(store):
    db, config, cal = store
    instruments = deepcopy(config["instruments"])
    rules = deepcopy(config["fee_model"]["rules"])
    rules["A.TEST"][0]["exchange"]["open"] = rate(1.0)
    with pytest.raises(ValueError, match="immutable"):
        extend_references(db, config["account_id"], cal, instruments, rules)


def test_calendar_only_extension_keeps_extended_fee_schedule(store, tmp_path):
    db, config, cal = store
    instruments, rules = _extend_with_d(db, config, cal)
    longer = calendar((13, 14, 15, 16))
    extend_references(db, config["account_id"], longer, instruments)
    from forward_account.store import _references, _account, connection

    with connection(db) as con:
        current, _, _ = _references(con, _account(con, config["account_id"]))
    assert "D.TEST" in current["fee_model"]["rules"]


def test_fee_rule_extension_requires_daily_policy(tmp_path):
    config, cal = world(daily=False)
    db = tmp_path / "strict.sqlite"
    initialize(db, config, cal)
    instruments = deepcopy(config["instruments"])
    instruments["D.TEST"] = spec(10)
    rules = deepcopy(config["fee_model"]["rules"])
    rules["D.TEST"] = fee_rule("D.TEST")
    with pytest.raises(ValueError, match="daily_policy"):
        extend_references(db, config["account_id"], cal, instruments, rules)


def test_dce_matching_state_rebinds_after_extension_at_day_boundary(store):
    """Without the rebind, adding D.TEST changes the schedule digest and the
    next DCE fill fails as 'fee matching evidence changed'."""
    db, config, cal = store
    first = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-13",
            "target": target(13, {"C.DCE": 0.1}),
            "execution": execution(13, {"C.DCE": 1000.0}),
            "settlement": settlement(13, {"C.DCE": 1000.0}),
        },
    )
    assert first["fee_matching_state"]["model_sha256"] == digest(config["fee_model"])
    _, rules = _extend_with_d(db, config, cal)
    second = advance(
        db,
        config["account_id"],
        {
            "trade_date": "2026-10-14",
            "target": target(14, {"C.DCE": 0.2}),
            "execution": execution(14, {"C.DCE": 1000.0}),
            "settlement": settlement(14, {"C.DCE": 1000.0}),
        },
    )
    assert second["positions"] == {"C.DCE": 2}
    assert second["audit"]["fee_matching_rebased"] is True
    model = {**config["fee_model"], "rules": rules}
    assert second["fee_matching_state"]["model_sha256"] == digest(model)


def test_matching_rebind_never_happens_within_a_trading_day():
    from forward_account.engine import _rebase_matching

    config, _ = world()
    stale = {"model_sha256": "0" * 64, "trade_date": "2026-10-14", "unmatched": {}}
    assert _rebase_matching(config, stale, "2026-10-14") == (stale, False)
    rebound, flag = _rebase_matching(config, stale, "2026-10-15")
    assert flag and rebound["model_sha256"] == digest(config["fee_model"])
    strict, _ = world(daily=False)
    assert _rebase_matching(strict, stale, "2026-10-15") == (stale, False)
