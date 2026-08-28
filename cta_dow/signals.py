"""Whether a Dow position opens, holds, or stands down on one bar.

The paper draws three same-direction gates and then says the position is held
"until the trend fails". Read literally as a per-bar condition, that would exit
and re-enter on every bar whose close did not make a new extreme -- a breakout
condition is an event, not a state. So the paper default latches: entry needs a
fresh close breakout, and the position then survives until the preliminary
trend switches or the turning-point condition fails (D6). The every-bar reading
is kept as a registered sensitivity mode, never as a candidate default.

Turning-point failure stands the position down; it never reverses it (D4). The
condition that invalidates a long is "this pullback undercut the previous
trough", which says the uptrend is no longer intact -- not that a downtrend has
been established.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cta_dow.indicators import Trend

__all__ = [
    "REASONS",
    "Position",
    "SignalDecision",
    "SignalMode",
    "TradeState",
    "decide",
]


class Position(StrEnum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class SignalMode(StrEnum):
    LATCHED = "latched"
    LITERAL = "literal"


#: 每一条都必须能在 signals 表里被数出来。
REASONS = (
    "neutral_trend",
    "trend_changed",
    "trend_mismatch",
    "turning_invalid",
    "insufficient_history",
    "no_resonance",
    "no_breakout",
    "dow_entry",
    "latched_hold",
    "literal_hold",
    "literal_gate_closed",
)


@dataclass(frozen=True, slots=True)
class TradeState:
    position: Position

    def __post_init__(self) -> None:
        if not isinstance(self.position, Position):
            raise ValueError("dow_signal_position: expected a Position")


@dataclass(frozen=True, slots=True)
class SignalDecision:
    state: TradeState
    target_direction: int
    changed: bool
    reason: str


_FLAT = TradeState(Position.FLAT)
_BY_DIRECTION = {1: TradeState(Position.LONG), -1: TradeState(Position.SHORT)}


def _direction(position: Position) -> int:
    if position is Position.LONG:
        return 1
    if position is Position.SHORT:
        return -1
    return 0


def _decision(previous: TradeState, current: TradeState, reason: str) -> SignalDecision:
    if reason not in REASONS:
        raise ValueError(f"dow_signal_reason: unregistered reason {reason!r}")
    return SignalDecision(
        state=current,
        target_direction=_direction(current.position),
        changed=current != previous,
        reason=reason,
    )


def decide(
    state: TradeState,
    *,
    trend: Trend,
    trend_changed: bool,
    turning_valid: bool,
    dow_resonance: bool,
    close_breakout: bool,
    enough_history: bool,
    mode: SignalMode,
) -> SignalDecision:
    """Advance one traded bar's trade state under the given signal mode."""
    if not isinstance(state, TradeState):
        raise ValueError("dow_signal_state: expected a TradeState")
    if not isinstance(trend, Trend):
        raise ValueError("dow_signal_trend: expected a Trend")
    if not isinstance(mode, SignalMode):
        raise ValueError("dow_signal_mode: expected a SignalMode")
    for name, flag in (
        ("trend_changed", trend_changed),
        ("turning_valid", turning_valid),
        ("dow_resonance", dow_resonance),
        ("close_breakout", close_breakout),
        ("enough_history", enough_history),
    ):
        if not isinstance(flag, bool):
            raise ValueError(f"dow_signal_gate: {name} must be a bool")

    if trend is Trend.NEUTRAL:
        return _decision(state, _FLAT, "neutral_trend")
    if trend_changed:
        return _decision(state, _FLAT, "trend_changed")

    desired = 1 if trend is Trend.UP else -1
    held = _direction(state.position)
    if held and held != desired:
        return _decision(state, _FLAT, "trend_mismatch")

    # The pullback gate governs both standing down and standing up: a trend
    # that is no longer intact cannot be entered either.
    if not turning_valid:
        return _decision(state, _FLAT, "turning_invalid")

    gates_open = enough_history and dow_resonance and close_breakout
    if not held:
        if not enough_history:
            return _decision(state, _FLAT, "insufficient_history")
        if not dow_resonance:
            return _decision(state, _FLAT, "no_resonance")
        if not close_breakout:
            return _decision(state, _FLAT, "no_breakout")
        return _decision(state, _BY_DIRECTION[desired], "dow_entry")

    if mode is SignalMode.LATCHED:
        return _decision(state, state, "latched_hold")
    if gates_open:
        return _decision(state, state, "literal_hold")
    return _decision(state, _FLAT, "literal_gate_closed")
