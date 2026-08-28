"""Deterministic Bollinger trade-state transitions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from numbers import Real
from typing import Any

__all__ = ["Action", "Position", "State", "step"]


class Position(StrEnum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class State:
    position: Position
    take_profit: float | None
    oi_scale: float

    def __post_init__(self) -> None:
        if not isinstance(self.position, Position):
            raise ValueError("bollinger_state_position: expected Position enum")

        scale = _finite_number(self.oi_scale, label="bollinger_state_oi_scale")
        if self.position is Position.FLAT:
            if self.take_profit is not None or scale != 0.0:
                raise ValueError(
                    "bollinger_state_flat: take_profit must be None and oi_scale must be 0"
                )
            object.__setattr__(self, "oi_scale", 0.0)
            return

        if self.take_profit is None:
            raise ValueError(
                "bollinger_state_positioned: take_profit must be finite"
            )
        take_profit = _finite_number(
            self.take_profit, label="bollinger_state_take_profit"
        )
        if scale not in (0.5, 1.0):
            raise ValueError("bollinger_state_positioned: oi_scale must be 0.5 or 1")
        object.__setattr__(self, "take_profit", take_profit)
        object.__setattr__(self, "oi_scale", scale)


@dataclass(frozen=True, slots=True)
class Action:
    state: State
    target_direction: int
    changed: bool
    reason: str


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label}: expected finite numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label}: expected finite numeric value")
    return result


def _supplied_scale(value: Any) -> float:
    scale = _finite_number(value, label="oi_scale")
    if scale not in (0.5, 1.0):
        raise ValueError("oi_scale: expected 0.5 or 1")
    return scale


def _direction(position: Position) -> int:
    if position is Position.LONG:
        return 1
    if position is Position.SHORT:
        return -1
    return 0


def _action(previous: State, current: State, reason: str) -> Action:
    return Action(
        state=current,
        target_direction=_direction(current.position),
        changed=current != previous,
        reason=reason,
    )


def step(
    state: State,
    *,
    previous_close: float,
    previous_middle: float,
    previous_upper: float,
    previous_lower: float,
    close: float,
    middle: float,
    upper: float,
    lower: float,
    std: float,
    oi_scale: float,
) -> Action:
    """Advance one traded bar without incorporating execution prices."""
    if not isinstance(state, State):
        raise ValueError("state: expected State instance")

    previous_close = _finite_number(previous_close, label="previous_close")
    previous_middle = _finite_number(previous_middle, label="previous_middle")
    previous_upper = _finite_number(previous_upper, label="previous_upper")
    previous_lower = _finite_number(previous_lower, label="previous_lower")
    close = _finite_number(close, label="close")
    middle = _finite_number(middle, label="middle")
    upper = _finite_number(upper, label="upper")
    lower = _finite_number(lower, label="lower")
    std = _finite_number(std, label="std")
    if std < 0.0:
        raise ValueError("std: expected finite nonnegative value")
    supplied_scale = _supplied_scale(oi_scale)

    if state.position is Position.LONG:
        if previous_close >= previous_middle and close < middle:
            return _action(
                state,
                State(Position.FLAT, take_profit=None, oi_scale=0.0),
                "middle_cross",
            )
        take_profit = state.take_profit
        assert take_profit is not None
        if close >= take_profit:
            return _action(
                state,
                State(Position.FLAT, take_profit=None, oi_scale=0.0),
                "take_profit",
            )
        return _action(state, state, "hold")

    if state.position is Position.SHORT:
        if previous_close <= previous_middle and close > middle:
            return _action(
                state,
                State(Position.FLAT, take_profit=None, oi_scale=0.0),
                "middle_cross",
            )
        take_profit = state.take_profit
        assert take_profit is not None
        if close <= take_profit:
            return _action(
                state,
                State(Position.FLAT, take_profit=None, oi_scale=0.0),
                "take_profit",
            )
        return _action(state, state, "hold")

    if previous_close <= previous_upper and close > upper:
        next_state = State(Position.LONG, middle + 8.0 * std, supplied_scale)
        return _action(state, next_state, "upper_cross")

    if previous_close >= previous_lower and close < lower:
        next_state = State(Position.SHORT, middle - 8.0 * std, supplied_scale)
        return _action(state, next_state, "lower_cross")

    return _action(state, state, "no_cross")
