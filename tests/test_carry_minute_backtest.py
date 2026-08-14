from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cta_carry.minute_backtest import (
    IntradayStopMachine,
    StopDecision,
    merge_close_plan,
)
from cta_carry.minute_bars import FifteenMinuteBar
from cta_carry.risk import PositionState
from tests.carry_fixtures import small_config


TZ = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 1, 8)


def _bar(
    index: int,
    *,
    high: float = 110.0,
    low: float = 100.0,
    close: float = 100.0,
    no_trade: bool = False,
    contract: str = "A2405.SHF",
    day_offset: int = 0,
) -> FifteenMinuteBar:
    start = datetime(2024, 1, 8, 9, 0, tzinfo=TZ) + timedelta(
        days=day_offset, minutes=15 * index
    )
    return FifteenMinuteBar(
        start=start,
        end=start + timedelta(minutes=15),
        contract=contract,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=0.0 if no_trade else 1.0,
        no_trade=no_trade,
    )


def _long_state(
    *,
    contract: str = "A2405.SHF",
    tranches: int = 3,
    highest_high: float | None = 110.0,
) -> PositionState:
    return PositionState(
        direction=1,
        contract=contract,
        tranches_remaining=tranches,
        highest_high=highest_high,
    )


def _signal(
    direction: int = 1,
    contract: str = "A2405.SHF",
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": TRADE_DATE,
                "product": "A",
                "effective_direction": direction,
                "main_contract": contract,
                "strength": 1.0,
                "main_close": 100.0,
                "atr": 2.0,
            }
        ]
    )


def _final_stop(
    state: PositionState,
    *,
    stage: int = 1,
    triggered: bool = True,
) -> StopDecision:
    bar = _bar(4)
    return StopDecision(
        eligible=True,
        triggered=triggered,
        state=state,
        stage=stage if triggered else None,
        threshold=105.0,
        bar=bar,
        not_before=bar.end + timedelta(minutes=5),
    )


def test_three_separate_bars_can_remove_all_tranches_in_one_day() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = _long_state()

    for stage, index in enumerate((0, 2, 4), start=1):
        bar = _bar(index)
        decision = machine.on_bar(
            trade_date=TRADE_DATE,
            product="A",
            state=state,
            bar=bar,
            atr=5.0,
            next_fill_end=bar.end + timedelta(minutes=5),
        )
        assert decision.triggered
        assert decision.stage == stage
        assert decision.threshold == 105.0
        state = decision.state

    assert state == PositionState(locked_direction=1)


def test_bar_overlapping_the_previous_fill_is_not_eligible() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    first_bar = _bar(0)
    first = machine.on_bar(
        TRADE_DATE,
        "A",
        _long_state(),
        first_bar,
        5.0,
        _bar(1).end,
    )

    skipped = machine.on_bar(
        TRADE_DATE,
        "A",
        first.state,
        _bar(1),
        5.0,
        _bar(2).end,
    )

    assert not skipped.eligible
    assert not skipped.triggered
    assert skipped.state is first.state
    assert skipped.not_before == _bar(1).end
    at_gate = machine.on_bar(
        TRADE_DATE,
        "A",
        first.state,
        _bar(2),
        5.0,
        _bar(2).end + timedelta(minutes=5),
    )
    assert at_gate.eligible
    assert at_gate.triggered


def test_no_trade_bar_does_not_update_extreme_or_trigger() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = _long_state(highest_high=105.0)

    decision = machine.on_bar(
        TRADE_DATE,
        "A",
        state,
        _bar(0, high=120.0, low=80.0, close=80.0, no_trade=True),
        5.0,
        _bar(1).end,
    )

    assert isinstance(decision, StopDecision)
    assert not decision.eligible
    assert not decision.triggered
    assert decision.state is state
    assert decision.threshold is None
    with pytest.raises(FrozenInstanceError):
        decision.triggered = True


def test_stop_limit_is_enforced_per_product_and_trade_date() -> None:
    config = small_config(
        chandelier_atr_multiple=1.0,
        stop_tranches=2,
    )
    machine = IntradayStopMachine(config)
    before = _long_state(tranches=2)
    first_bar = _bar(0)
    first = machine.on_bar(
        TRADE_DATE,
        "A",
        before,
        first_bar,
        5.0,
        first_bar.end + timedelta(minutes=5),
    )
    assert first.triggered

    short_state = PositionState(
        direction=-1,
        contract="A2405.SHF",
        tranches_remaining=2,
        lowest_low=90.0,
    )
    second_bar = _bar(2, high=100.0, low=90.0, close=100.0)
    machine.reset_for_transition(
        "A", first.state, short_state, fill_end=second_bar.start
    )
    second = machine.on_bar(
        TRADE_DATE,
        "A",
        short_state,
        second_bar,
        5.0,
        second_bar.end + timedelta(minutes=5),
    )
    assert second.triggered

    state = _long_state(tranches=2)
    third_bar = _bar(4)
    machine.reset_for_transition("A", second.state, state, fill_end=third_bar.start)

    blocked = machine.on_bar(
        TRADE_DATE,
        "A",
        state,
        third_bar,
        5.0,
        third_bar.end + timedelta(minutes=5),
    )
    next_bar = _bar(0, day_offset=1)
    next_day = machine.on_bar(
        date(2024, 1, 9),
        "A",
        state,
        next_bar,
        5.0,
        next_bar.end + timedelta(minutes=5),
    )

    assert not blocked.eligible
    assert not blocked.triggered
    assert next_day.eligible
    assert next_day.triggered


def test_active_position_rejects_a_bar_for_another_contract() -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0, contract="B2405.SHF")

    with pytest.raises(ValueError, match="bar.contract.*state.contract"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end + timedelta(minutes=5),
        )


@pytest.mark.parametrize("field", ["start", "end"])
def test_stop_bar_requires_timezone_aware_boundaries(field: str) -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0)
    bar = replace(bar, **{field: getattr(bar, field).replace(tzinfo=None)})

    with pytest.raises(ValueError, match="bar.*timezone-aware"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            _bar(0).end + timedelta(minutes=5),
        )


def test_stop_fill_end_requires_timezone_and_must_follow_bar_end() -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0)

    with pytest.raises(ValueError, match="next_fill_end.*timezone-aware"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="next_fill_end.*after bar.end"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end,
        )


def test_signal_exit_reversal_and_roll_reset_the_gate() -> None:
    machine = IntradayStopMachine(small_config())
    before = _long_state()
    fill_end = _bar(2).end

    machine.reset_for_transition("A", before, PositionState(), fill_end=fill_end)
    assert machine.not_before("A") is None

    reversed_state = PositionState(
        direction=-1,
        contract="A2405.SHF",
        tranches_remaining=3,
    )
    machine.reset_for_transition("A", before, reversed_state, fill_end=fill_end)
    assert machine.not_before("A") == fill_end

    rolled_state = _long_state(contract="A2409.SHF", highest_high=None)
    machine.reset_for_transition("A", before, rolled_state, fill_end=fill_end)
    assert machine.not_before("A") == fill_end

    machine.reset_for_transition("A", rolled_state, PositionState(), fill_end=fill_end)
    assert machine.not_before("A") is None


def test_close_merge_keeps_final_stop_reduction_for_same_direction() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "stop_1"}
    assert list(plan.raw_weights) == ["A2405.SHF"]


def test_close_merge_final_stop_and_zero_signal_produces_one_exit() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}
    zero_signal = _signal(direction=0, contract="")

    plan = merge_close_plan(
        post_stop,
        zero_signal,
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states == {"A": PositionState()}
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}


def test_close_merge_full_stop_and_zero_signal_releases_lock_as_exit() -> None:
    config = small_config()
    post_stop = {"A": PositionState(locked_direction=1)}

    plan = merge_close_plan(
        post_stop,
        _signal(direction=0, contract=""),
        config,
        final_stop_decisions={
            "A": _final_stop(post_stop["A"], stage=config.stop_tranches)
        },
    )

    assert plan.states == {"A": PositionState()}
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}


def test_close_merge_final_stop_and_reversal_produces_full_reverse() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(-1),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"].direction == -1
    assert plan.states["A"].tranches_remaining == config.stop_tranches
    assert plan.reasons == {"A": "direction_reversal"}
    assert list(plan.raw_weights) == ["A2405.SHF"]
    assert plan.raw_weights["A2405.SHF"] < 0.0


def test_close_merge_roll_preserves_tranches_and_resets_extremes() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(contract="A2409.SHF"),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"] == PositionState(
        direction=1,
        contract="A2409.SHF",
        tranches_remaining=2,
    )
    assert plan.reasons == {"A": "roll"}
    assert set(plan.raw_weights) == {"A2409.SHF"}


def test_early_stop_without_a_final_bar_trigger_is_not_labeled_as_stop() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"], triggered=False)},
    )

    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "rebalance"}


@pytest.mark.parametrize(
    "decisions",
    [
        {"B": _final_stop(_long_state(tranches=2))},
        {"A": _final_stop(_long_state(tranches=1), stage=2)},
    ],
)
def test_close_merge_rejects_final_stop_product_or_state_mismatch(
    decisions: dict[str, StopDecision],
) -> None:
    with pytest.raises(ValueError, match="final_stop_decisions"):
        merge_close_plan(
            {"A": _long_state(tranches=2)},
            _signal(),
            small_config(),
            final_stop_decisions=decisions,
        )


def test_close_merge_rejects_duplicate_signal_products_before_sorting() -> None:
    duplicate_signals = pd.concat([_signal(1), _signal(-1)], ignore_index=True)

    with pytest.raises(ValueError, match="duplicate.*A"):
        merge_close_plan(
            {"A": _long_state()},
            duplicate_signals,
            small_config(),
        )


def test_close_merge_returns_one_final_net_target_for_a_stop_and_roll() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(contract="A2409.SHF"),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    # Concrete product/window execution-row uniqueness belongs to Task 8,
    # where the actual fill window exists. Task 7 emits only this final target.
    assert plan.states == {
        "A": PositionState(
            direction=1,
            contract="A2409.SHF",
            tranches_remaining=2,
        )
    }
    assert plan.reasons == {"A": "roll"}
    assert set(plan.raw_weights) == {"A2409.SHF"}
