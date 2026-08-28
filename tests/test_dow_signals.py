"""Entry latching, turning-point exits, and the literal every-bar variant."""

from __future__ import annotations

import pytest

from cta_dow.indicators import Trend
from cta_dow.signals import (
    REASONS,
    Position,
    SignalDecision,
    SignalMode,
    TradeState,
    decide,
)


def call(position: Position, **overrides) -> SignalDecision:
    gates = {
        "trend": Trend.UP,
        "trend_changed": False,
        "turning_valid": True,
        "dow_resonance": True,
        "close_breakout": True,
        "enough_history": True,
        "mode": SignalMode.LATCHED,
    }
    gates.update(overrides)
    return decide(TradeState(position), **gates)


def test_latched_mode_holds_after_the_entry_breakout() -> None:
    entered = call(Position.FLAT)
    held = decide(
        entered.state,
        trend=Trend.UP,
        trend_changed=False,
        turning_valid=True,
        dow_resonance=True,
        close_breakout=False,
        enough_history=True,
        mode=SignalMode.LATCHED,
    )

    assert entered.target_direction == 1
    assert entered.reason == "dow_entry"
    assert held.target_direction == 1
    assert held.reason == "latched_hold"
    assert held.changed is False


def test_turning_failure_flattens_without_reversing() -> None:
    result = call(Position.LONG, turning_valid=False, close_breakout=False)

    assert result.target_direction == 0
    assert result.reason == "turning_invalid"
    assert result.state.position is Position.FLAT


def test_literal_mode_is_flat_when_the_breakout_gate_is_not_true() -> None:
    result = call(Position.LONG, close_breakout=False, mode=SignalMode.LITERAL)

    assert result.target_direction == 0
    assert result.reason == "literal_gate_closed"


def test_literal_mode_keeps_a_position_that_re_clears_every_gate() -> None:
    result = call(Position.LONG, mode=SignalMode.LITERAL)

    assert result.target_direction == 1
    assert result.reason == "literal_hold"


def test_a_trend_switch_closes_the_position_and_does_not_reverse_into_it() -> None:
    result = call(Position.LONG, trend=Trend.DOWN, trend_changed=True)

    assert result.target_direction == 0
    assert result.reason == "trend_changed"


def test_a_neutral_trend_holds_nothing() -> None:
    result = call(Position.LONG, trend=Trend.NEUTRAL)

    assert result.target_direction == 0
    assert result.reason == "neutral_trend"


@pytest.mark.parametrize(
    ("blocked", "reason"),
    [
        ({"enough_history": False, "dow_resonance": False}, "insufficient_history"),
        ({"dow_resonance": False}, "no_resonance"),
        ({"close_breakout": False}, "no_breakout"),
        ({"turning_valid": False}, "turning_invalid"),
    ],
)
def test_every_entry_gate_has_its_own_rejection_reason(blocked, reason) -> None:
    result = call(Position.FLAT, **blocked)

    assert result.target_direction == 0
    assert result.reason == reason


def test_short_mirrors_long() -> None:
    entered = call(Position.FLAT, trend=Trend.DOWN)
    held = decide(
        entered.state,
        trend=Trend.DOWN,
        trend_changed=False,
        turning_valid=True,
        dow_resonance=True,
        close_breakout=False,
        enough_history=True,
        mode=SignalMode.LATCHED,
    )

    assert entered.target_direction == -1
    assert entered.state.position is Position.SHORT
    assert held.target_direction == -1


def test_a_position_facing_the_wrong_trend_is_closed_not_flipped() -> None:
    # Defensive: the segment machine should never produce this, and if it did
    # the answer is to stand down, not to reverse on a stale gate.
    result = call(Position.SHORT, trend=Trend.UP)

    assert result.target_direction == 0
    assert result.reason == "trend_mismatch"


def test_every_reason_the_decider_can_return_is_registered() -> None:
    seen = set()
    for position in Position:
        for trend in Trend:
            for mode in SignalMode:
                for changed in (False, True):
                    for turning in (False, True):
                        for resonance in (False, True):
                            for breakout in (False, True):
                                for history in (False, True):
                                    seen.add(
                                        call(
                                            position,
                                            trend=trend,
                                            trend_changed=changed,
                                            turning_valid=turning,
                                            dow_resonance=resonance,
                                            close_breakout=breakout,
                                            enough_history=history,
                                            mode=mode,
                                        ).reason
                                    )
    assert seen <= set(REASONS)
    assert seen == set(REASONS)


def test_a_bad_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_signal_mode"):
        call(Position.FLAT, mode="latched")
