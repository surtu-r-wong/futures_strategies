from dataclasses import FrozenInstanceError, fields

import numpy as np
import pytest

from cta_bollinger.signals import Action, Position, State, step


def flat() -> State:
    return State(position=Position.FLAT, take_profit=None, oi_scale=0.0)


def bar(**overrides):
    values = {
        "previous_close": 100.0,
        "previous_middle": 100.0,
        "previous_upper": 101.0,
        "previous_lower": 99.0,
        "close": 100.5,
        "middle": 100.5,
        "upper": 101.5,
        "lower": 99.5,
        "std": 1.0,
        "oi_scale": 1.0,
    }
    values.update(overrides)
    return values


def test_long_entry_needs_a_fresh_upper_cross():
    action = step(flat(), **bar(close=102.0))

    assert action.state == State(Position.LONG, take_profit=108.5, oi_scale=1.0)
    assert action.target_direction == 1
    assert action.changed is True
    assert action.reason == "upper_cross"


def test_short_entry_is_the_exact_lower_cross_mirror():
    action = step(
        flat(),
        **bar(
            previous_close=100.0,
            previous_lower=100.0,
            close=98.0,
            middle=99.5,
            lower=98.5,
            oi_scale=0.5,
        ),
    )

    assert action.state == State(Position.SHORT, take_profit=91.5, oi_scale=0.5)
    assert action.target_direction == -1
    assert action.changed is True
    assert action.reason == "lower_cross"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"previous_close": 101.1, "close": 102.0},
        {"previous_close": 101.0, "close": 101.5},
    ],
)
def test_long_entry_requires_a_fresh_strict_current_bar_break(kwargs):
    assert step(flat(), **bar(**kwargs)) == Action(flat(), 0, False, "no_cross")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"previous_close": 98.9, "close": 98.0},
        {"previous_close": 99.0, "close": 99.5, "lower": 99.5},
    ],
)
def test_short_entry_requires_a_fresh_strict_current_bar_break(kwargs):
    assert step(flat(), **bar(**kwargs)) == Action(flat(), 0, False, "no_cross")


def test_entry_uses_each_bars_own_boundary_and_allows_previous_equality():
    long_action = step(
        flat(),
        **bar(previous_close=105.0, previous_upper=105.0, close=102.0),
    )
    short_action = step(
        flat(),
        **bar(
            previous_close=95.0,
            previous_lower=95.0,
            close=98.0,
            lower=98.5,
        ),
    )

    assert long_action.reason == "upper_cross"
    assert short_action.reason == "lower_cross"


@pytest.mark.parametrize(
    ("state", "kwargs", "direction"),
    [
        (
            State(Position.LONG, take_profit=108.5, oi_scale=0.5),
            {"previous_close": 103.0, "close": 104.0, "middle": 102.0},
            1,
        ),
        (
            State(Position.SHORT, take_profit=91.5, oi_scale=1.0),
            {"previous_close": 97.0, "close": 96.0, "middle": 98.0},
            -1,
        ),
    ],
)
def test_take_profit_and_oi_scale_are_frozen_while_positioned(
    state, kwargs, direction
):
    action = step(state, **bar(std=4.0, oi_scale=1.0, **kwargs))

    assert action.state is state
    assert action.target_direction == direction
    assert action.changed is False
    assert action.reason == "hold"


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        (
            State(Position.LONG, take_profit=108.5, oi_scale=0.5),
            dict(
                previous_close=102.0,
                previous_middle=101.0,
                close=100.0,
                middle=100.5,
            ),
        ),
        (
            State(Position.SHORT, take_profit=91.5, oi_scale=0.5),
            dict(
                previous_close=98.0,
                previous_middle=99.0,
                close=100.0,
                middle=99.5,
            ),
        ),
    ],
)
def test_positions_exit_on_a_fresh_middle_cross(state, kwargs):
    assert step(state, **bar(**kwargs)) == Action(flat(), 0, True, "middle_cross")


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        (
            State(Position.LONG, take_profit=108.5, oi_scale=0.5),
            {"previous_close": 102.0, "close": 108.5},
        ),
        (
            State(Position.SHORT, take_profit=91.5, oi_scale=1.0),
            {"previous_close": 98.0, "close": 91.5},
        ),
    ],
)
def test_positions_exit_when_fixed_take_profit_is_reached(state, kwargs):
    assert step(state, **bar(**kwargs)) == Action(flat(), 0, True, "take_profit")


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        (
            State(Position.LONG, take_profit=108.5, oi_scale=0.5),
            dict(
                previous_close=99.0,
                previous_middle=100.0,
                close=98.0,
                middle=99.0,
            ),
        ),
        (
            State(Position.LONG, take_profit=108.5, oi_scale=0.5),
            dict(
                previous_close=101.0,
                previous_middle=100.0,
                close=99.0,
                middle=99.0,
            ),
        ),
        (
            State(Position.SHORT, take_profit=91.5, oi_scale=0.5),
            dict(
                previous_close=101.0,
                previous_middle=100.0,
                close=102.0,
                middle=101.0,
            ),
        ),
        (
            State(Position.SHORT, take_profit=91.5, oi_scale=0.5),
            dict(
                previous_close=99.0,
                previous_middle=100.0,
                close=101.0,
                middle=101.0,
            ),
        ),
    ],
)
def test_middle_exit_requires_a_fresh_strict_current_bar_cross(state, kwargs):
    action = step(state, **bar(**kwargs))

    assert action.state is state
    assert action.target_direction == (1 if state.position is Position.LONG else -1)
    assert action.changed is False
    assert action.reason == "hold"


def test_position_exit_does_not_reenter_on_the_same_bar():
    state = State(Position.LONG, take_profit=108.5, oi_scale=0.5)
    action = step(
        state,
        **bar(
            previous_close=102.0,
            previous_middle=101.0,
            previous_lower=101.0,
            close=98.0,
            middle=100.0,
            lower=99.0,
        ),
    )

    assert action == Action(flat(), 0, True, "middle_cross")


def test_remaining_outside_after_take_profit_does_not_create_a_new_cross():
    exited = step(
        State(Position.LONG, take_profit=108.5, oi_scale=1.0),
        **bar(previous_close=107.0, close=109.0, upper=106.0),
    )
    next_bar = step(
        exited.state,
        **bar(previous_close=109.0, previous_upper=106.0, close=110.0, upper=107.0),
    )

    assert exited.reason == "take_profit"
    assert next_bar == Action(flat(), 0, False, "no_cross")


def test_short_take_profit_is_fixed_from_entry_middle_and_std():
    action = step(
        flat(),
        **bar(
            previous_close=99.0,
            previous_lower=99.0,
            close=97.0,
            middle=100.0,
            lower=98.0,
            std=2.0,
        ),
    )

    assert action.state.take_profit == 84.0


def test_public_types_have_exact_frozen_slotted_fields():
    state = flat()
    action = Action(state, 0, False, "no_cross")

    assert Position.FLAT == "flat"
    assert Position.LONG == "long"
    assert Position.SHORT == "short"
    assert [field.name for field in fields(State)] == [
        "position",
        "take_profit",
        "oi_scale",
    ]
    assert [field.name for field in fields(Action)] == [
        "state",
        "target_direction",
        "changed",
        "reason",
    ]
    assert not hasattr(state, "__dict__")
    assert not hasattr(action, "__dict__")
    with pytest.raises(FrozenInstanceError):
        state.oi_scale = 1.0
    with pytest.raises(FrozenInstanceError):
        action.reason = "hold"


@pytest.mark.parametrize("position", ["flat", 0, 1, -1, True])
def test_state_requires_a_position_enum(position):
    with pytest.raises(ValueError, match="position"):
        State(position, take_profit=None, oi_scale=0.0)


@pytest.mark.parametrize(
    ("take_profit", "oi_scale"),
    [(1.0, 0.0), (None, 0.5), (None, 1.0), (None, False)],
)
def test_flat_state_always_clears_take_profit_and_oi_scale(take_profit, oi_scale):
    with pytest.raises(ValueError):
        State(Position.FLAT, take_profit=take_profit, oi_scale=oi_scale)


@pytest.mark.parametrize(
    ("take_profit", "oi_scale"),
    [
        (None, 0.5),
        (np.nan, 0.5),
        (np.inf, 0.5),
        (True, 0.5),
        (100.0, 0.0),
        (100.0, 0.75),
        (100.0, True),
    ],
)
def test_positioned_state_requires_finite_take_profit_and_exact_scale(
    take_profit, oi_scale
):
    with pytest.raises(ValueError):
        State(Position.LONG, take_profit=take_profit, oi_scale=oi_scale)


@pytest.mark.parametrize(
    "name",
    [
        "previous_close",
        "previous_middle",
        "previous_upper",
        "previous_lower",
        "close",
        "middle",
        "upper",
        "lower",
        "std",
    ],
)
@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf, True])
def test_step_validates_every_supplied_indicator_even_while_positioned(name, bad):
    state = State(Position.LONG, take_profit=108.5, oi_scale=0.5)

    with pytest.raises(ValueError, match=name):
        step(state, **bar(**{name: bad}))


@pytest.mark.parametrize("std", [0.0, -1.0])
def test_step_requires_positive_std(std):
    with pytest.raises(ValueError, match="std"):
        step(flat(), **bar(std=std))


@pytest.mark.parametrize("oi_scale", [0.0, 0.75, np.nan, np.inf, True])
def test_step_validates_supplied_oi_scale_even_when_it_will_be_frozen(oi_scale):
    state = State(Position.LONG, take_profit=108.5, oi_scale=0.5)

    with pytest.raises(ValueError, match="oi_scale"):
        step(state, **bar(oi_scale=oi_scale))


@pytest.mark.parametrize("state", ["flat", Position.FLAT, None])
def test_step_requires_a_state_instance(state):
    with pytest.raises(ValueError, match="state"):
        step(state, **bar())
