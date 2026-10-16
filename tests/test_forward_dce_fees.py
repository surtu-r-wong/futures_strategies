"""DCE FAQ quantity examples and explicit synthetic price/broker assumptions."""

from copy import deepcopy

import pytest

from forward_account import fees


EXAMPLES = [([10, -3, 20, -30], 99), ([-10, 3, -20, 30], 180), ([-10, 3, 20, -30], 120)]


def rate(value, unit="YUAN_PER_LOT"):
    return {"value": value, "unit": unit}


def model(version="contract-fees-v2", contract="EG2611.DCE"):
    rule = {
        "rule_id": "SYNTHETIC-DCE-FAQ",
        "valid_from": "2026-10-15",
        "valid_to": "2026-10-19",
        "exchange_source": "DCE FAQ 2021-01-21 examples; synthetic date application",
        "broker_source": "SYNTHETIC explicit zero additional broker fee",
        "slippage_source": "SYNTHETIC explicit zero slippage",
        "exchange": dict(zip(fees.ACTIONS, map(rate, (3, 3, 1.5, 1.5)))),
        "broker": {key: rate(0) for key in fees.ACTIONS},
        "slippage": rate(0),
    }
    result = {
        "version": version,
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
        "policy_source": "SYNTHETIC explicit v1 fallback policy",
        "excluded_costs": ["SYNTHETIC zero broker/slippage; no delivery"],
        "rules": {contract: [rule]},
    }
    if version == "contract-fees-v2":
        result["dce_intraday_matching"] = {
            "method": "prior_open_fifo_same_side",
            "components": ["exchange"],
            "source": "User-supplied DCE FAQ; FIFO price attribution is an explicit assumption",
        }
    return result


class Replay:
    def __init__(self, m=None, contract="EG2611.DCE", sign=1):
        self.contract = contract
        self.model = m or model(contract=contract)
        self.specs = {contract: {"multiplier": 10}}
        fees.validate_model(self.model, self.specs)
        self.v2 = self.model["version"] == "contract-fees-v2"
        self.matching = fees.new_matching_state(self.model) if self.v2 else None
        self.positions, self.lots = {}, {}
        self.fills = []
        self.sign = sign
        self.step(sign * 100, "2026-10-15", 0)
        self.fills.clear()  # Overnight opening fees are outside the FAQ day.

    def step(self, delta, day="2026-10-16", minute=0, price=100):
        quantity = self.positions.get(self.contract, 0) + delta
        target = {self.contract: quantity} if quantity else {}
        args = (
            self.model,
            self.positions,
            self.lots,
            target,
            {self.contract: price},
            self.specs,
            day,
            f"{day}T09:{minute:02}:00+08:00",
            "SYNTHETIC ordered fill",
        )
        if self.v2:
            fills, self.lots, self.matching = fees.account_trades_with_matching(
                *args, matching_state=self.matching
            )
        else:
            fills, self.lots = fees.account_trades(*args)
        self.positions = target
        self.fills.extend(fills)
        return fills


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("sequence,expected", EXAMPLES)
def test_official_examples_with_100_overnight_lots(sequence, expected, sign):
    replay = Replay(sign=sign)
    legacy = Replay(model("contract-fees-v1"), sign=sign)
    for index, delta in enumerate(sequence):
        replay.step(sign * delta, minute=index)
        legacy.step(sign * delta, minute=index)
    assert sum(t["fee"] for t in replay.fills) == expected
    assert sum(t["fee"] for t in legacy.fills) == 189
    assert replay.lots == legacy.lots  # Fee matching must not change FIFO inventory.
    assert all(t["action"] != "close_today" for t in replay.fills)


def test_close_before_open_is_not_matched_retroactively():
    replay = Replay()
    assert sum(t["fee"] for t in replay.step(-10)) == 30
    assert sum(t["fee"] for t in replay.step(3, minute=1)) == 9
    fills = replay.step(-5, minute=2)
    assert sum(t["exchange_fee"] for t in fills) == 3 * 1.5 + 2 * 3
    assert sum(t["exchange_open_adjustment"] for t in fills) == -4.5
    assert replay.matching["unmatched"] == {}
    assert sum(t["fee"] for t in replay.step(4, minute=3)) == 12


@pytest.mark.parametrize("unit,value", [("PER_TEN_THOUSAND", 2), ("PERMILLE", 0.2)])
def test_partial_fifo_matching_uses_each_opening_price(unit, value):
    m = model()
    r = m["rules"]["EG2611.DCE"][0]
    r["exchange"] = {
        key: rate(value if key in ("open", "close") else value / 2, unit)
        for key in fees.ACTIONS
    }
    replay = Replay(m)
    replay.step(10, price=100)
    replay.step(10, minute=1, price=300)
    fills = replay.step(-12, minute=2, price=200)
    assert sum(t["exchange_fee"] for t in fills) == pytest.approx(2.4)
    assert sum(t["exchange_open_adjustment"] for t in fills) == pytest.approx(-1.6)
    assert sum(t["fee"] for t in replay.fills) == pytest.approx(8.8)
    pending = replay.matching["unmatched"][replay.contract]
    assert [(p["quantity"], p["entry_price"]) for p in pending] == [(8, 300)]
    fills = replay.step(-10, minute=3, price=400)
    assert sum(t["exchange_fee"] for t in fills) == pytest.approx(4.8)
    assert sum(t["exchange_open_adjustment"] for t in fills) == pytest.approx(-2.4)
    assert replay.matching["unmatched"] == {}


@pytest.mark.parametrize(
    "contract", ["EG2611.CZC", "EG2611.SHF", "EG2611.INE", "EG2611.GFE"]
)
def test_non_dce_v2_fills_and_inventory_equal_v1(contract):
    new = Replay(model(contract=contract), contract)
    old = Replay(model("contract-fees-v1", contract), contract)
    for i, delta in enumerate([10, -5, -120, 10, 5]):
        assert new.step(delta, minute=i) == old.step(delta, minute=i)
        assert new.lots == old.lots
    assert new.matching["unmatched"] == {}


def test_day_rollover_drops_unused_openings_without_refund():
    replay = Replay()
    replay.step(10)
    fills = replay.step(-10, day="2026-10-19")
    assert sum(t["fee"] for t in fills) == 30
    assert all(t["exchange_open_adjustment"] == 0 for t in fills)
    assert replay.matching["unmatched"] == {}


def test_reversal_closes_before_opening_other_side_and_no_double_adjustment():
    replay = Replay()
    replay.step(10)
    fills = replay.step(-115, minute=1)
    assert sum(t["exchange_open_adjustment"] for t in fills) == -15
    assert replay.positions == {replay.contract: -5}
    assert replay.matching["unmatched"][replay.contract][0]["position_side"] == "short"
    fills = replay.step(5, minute=2)
    assert sum(t["fee"] for t in fills) == 0  # 7.5 close less 7.5 opening refund.
    assert replay.matching["unmatched"] == {}
    assert replay.lots == {}


def test_dce_broker_keeps_explicit_legacy_schedule_semantics():
    m = model()
    r = m["rules"]["EG2611.DCE"][0]
    r["broker"] = dict(zip(fees.ACTIONS, map(rate, (4, 5, 7, 2))))
    replay = Replay(m)
    replay.step(10)
    fills = replay.step(-5, minute=1)
    assert sum(t["exchange_fee"] for t in fills) == 7.5
    assert sum(t["exchange_open_adjustment"] for t in fills) == -7.5
    assert sum(t["broker_fee"] for t in fills) == 25
    assert sum(t["broker_open_adjustment"] for t in fills) == 0


@pytest.mark.parametrize("intraday", [1.4, 2])
def test_intraday_surcharge_adjusts_opening_upward_at_entry_notional(intraday):
    m = model()
    m["rules"]["EG2611.DCE"][0]["exchange"] = {
        key: rate(1 if key in ("open", "close") else intraday, "PER_TEN_THOUSAND")
        for key in fees.ACTIONS
    }
    replay = Replay(m)
    replay.step(10, price=100)
    fills = replay.step(-5, minute=1, price=200)
    assert sum(t["exchange_fee"] for t in fills) == pytest.approx(intraday)
    assert sum(t["exchange_open_adjustment"] for t in fills) == pytest.approx(
        0.5 * (intraday - 1)
    )


def test_same_timestamp_uses_call_order_for_fee_openings():
    replay = Replay()
    replay.step(3, price=300)
    replay.step(5, price=100)
    fills = replay.step(-4)
    assert [(abs(t["quantity_change"]), t["fee_match_entry_price"]) for t in fills] == [
        (3, 300),
        (1, 100),
    ]
    assert replay.matching["unmatched"][replay.contract][0]["quantity"] == 4


@pytest.mark.parametrize(
    "fault", ["missing", "wrong_model", "future", "rate", "quantity"]
)
def test_matching_state_fails_closed_and_inputs_not_mutated(fault):
    replay = Replay()
    replay.step(10)
    if fault == "missing":
        replay.matching = None
    elif fault == "wrong_model":
        replay.matching["model_sha256"] = "0" * 64
    elif fault == "future":
        replay.matching["last_at"] = "2026-10-16T11:00:00+08:00"
    elif fault == "rate":
        replay.matching["unmatched"][replay.contract][0]["opening_rule_sha256"] = (
            "0" * 64
        )
    else:
        replay.matching["unmatched"][replay.contract][0]["quantity"] = -1
    before = deepcopy((replay.lots, replay.matching, replay.positions))
    with pytest.raises(ValueError):
        replay.step(-1, minute=1)
    assert (replay.lots, replay.matching, replay.positions) == before


def test_version_and_policy_isolation():
    m = model()
    with pytest.raises(ValueError, match="matching"):
        fees.account_trades(
            m,
            {},
            {},
            {},
            {},
            {"EG2611.DCE": {"multiplier": 10}},
            "2026-10-16",
            "2026-10-16T09:00:00+08:00",
            "SYNTHETIC",
        )
    for bad in ("contract-fees-v3", "contract-fees-v1"):
        m["version"] = bad
        with pytest.raises(ValueError):
            fees.validate_model(m, {"EG2611.DCE": {"multiplier": 10}})
    m = model()
    m["dce_intraday_matching"]["components"] = ["exchange", "broker"]
    with pytest.raises(ValueError):
        fees.validate_model(m, {"EG2611.DCE": {"multiplier": 10}})
    assert fees.description({"fee_model": model()}).startswith("contract-fees-v2;")


def test_two_contracts_never_share_openings():
    replay = Replay()
    replay.model["rules"]["EG2612.DCE"] = deepcopy(
        replay.model["rules"][replay.contract]
    )
    replay.specs["EG2612.DCE"] = {"multiplier": 10}
    # Fresh replay state for the explicitly different two-contract model.
    state = fees.new_matching_state(replay.model)
    positions = {"EG2611.DCE": 100}
    _, lots, state = fees.account_trades_with_matching(
        replay.model,
        positions,
        replay.lots,
        {**positions, "EG2612.DCE": 10},
        {"EG2611.DCE": 100, "EG2612.DCE": 100},
        replay.specs,
        "2026-10-16",
        "2026-10-16T09:00:00+08:00",
        "SYNTHETIC",
        matching_state=state,
    )
    fills, _, state = fees.account_trades_with_matching(
        replay.model,
        {**positions, "EG2612.DCE": 10},
        lots,
        {"EG2611.DCE": 90, "EG2612.DCE": 10},
        {"EG2611.DCE": 100, "EG2612.DCE": 100},
        replay.specs,
        "2026-10-16",
        "2026-10-16T09:01:00+08:00",
        "SYNTHETIC",
        matching_state=state,
    )
    assert sum(t["fee"] for t in fills) == 30
    assert state["unmatched"]["EG2612.DCE"][0]["quantity"] == 10


def test_v2_account_state_replay_revision_and_immutable_version(tmp_path):
    from forward_account import advance, initialize, read_state
    from forward_account.demo import make_inputs

    config, calendar, packets = make_inputs(tmp_path / "inputs")
    contract = "EG2611.DCE"
    config["instruments"][contract] = config["instruments"].pop("A.TEST")
    for packet in packets:
        for values in (
            packet["target"]["weights"],
            packet["execution"]["prices"],
            packet["execution"]["tradable"],
            packet["settlement"]["prices"],
        ):
            if "A.TEST" in values:
                values[contract] = values.pop("A.TEST")
    del config["cost_bps"]
    config["fee_model"] = model(contract=contract)
    config["fee_model"]["rules"]["B.TEST"] = deepcopy(
        config["fee_model"]["rules"][contract]
    )
    for schedule in config["fee_model"]["rules"].values():
        schedule[0].update(valid_from="2026-10-12", valid_to="2026-10-14")
    config["account_version"] = "SYNTHETIC-dce-fees-v2"
    path = tmp_path / "synthetic-v2.sqlite"
    initial = initialize(path, config, calendar)
    assert initial["fee_matching_state"]["event_index"] == 0
    first = advance(path, config["account_id"], packets[0])
    assert first["fee_matching_state"]["unmatched"][contract]
    assert advance(path, config["account_id"], packets[0]) == first
    revised = deepcopy(packets[0])
    revised["settlement"]["prices"][contract] += 1
    revision = advance(
        path, config["account_id"], revised, revise_reason="SYNTHETIC correction"
    )
    assert revision["fee_matching_state"] == first["fee_matching_state"]
    assert revision["fees"] == first["fees"]
    second = advance(path, config["account_id"], packets[1])
    assert second == read_state(path, config["account_id"])
    assert second["fee_matching_state"]["unmatched"] == {}
    assert second["audit"]["fee_model_version"] == "contract-fees-v2"
    changed = deepcopy(config)
    changed["fee_model"]["version"] = "contract-fees-v1"
    del changed["fee_model"]["dce_intraday_matching"]
    with pytest.raises(ValueError, match="immutable"):
        initialize(path, changed, calendar)
