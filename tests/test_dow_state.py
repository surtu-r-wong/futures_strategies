"""Preliminary trend segments, their extremes, and the order they update in."""

from __future__ import annotations

import pytest

from cta_dow.indicators import Trend
from cta_dow.state import SegmentDecision, SegmentState, inspect_bar


def prepared_up_state(*, segment_high, segment_low, last_down_lows):
    return SegmentState(
        trend=Trend.UP,
        segment_high=segment_high,
        segment_low=segment_low,
        last_up_highs=(),
        last_down_lows=last_down_lows,
    )


def prepared_down_state(*, segment_high, segment_low, last_up_highs):
    return SegmentState(
        trend=Trend.DOWN,
        segment_high=segment_high,
        segment_low=segment_low,
        last_up_highs=last_up_highs,
        last_down_lows=(),
    )


def test_switch_closes_the_previous_segment_and_starts_with_current_bar() -> None:
    state = SegmentState.empty()
    state = inspect_bar(
        state, trend=Trend.UP, high=11.0, low=9.0, close=10.0
    ).next_state
    state = inspect_bar(
        state, trend=Trend.UP, high=12.0, low=8.0, close=11.0
    ).next_state
    state = inspect_bar(
        state, trend=Trend.DOWN, high=10.0, low=7.0, close=8.0
    ).next_state

    assert state.last_up_highs == (12.0,)
    assert state.segment_high == 10.0
    assert state.segment_low == 7.0


def test_correction_uses_current_running_extreme_but_breakout_uses_prior_extreme() -> (
    None
):
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=13.0, low=9.5, close=12.1)

    assert isinstance(decision, SegmentDecision)
    assert decision.turning_valid is True
    assert decision.dow_resonance is True
    assert decision.close_breakout is True
    assert decision.enough_history is True
    assert decision.trend_changed is False
    assert decision.next_state.segment_high == 13.0


def test_a_close_equal_to_the_prior_peak_is_a_breakout() -> None:
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=12.0, low=11.0, close=12.0)

    assert decision.close_breakout is True


def test_the_breakout_is_measured_before_this_bar_joins_the_extreme() -> None:
    # If the running high were updated first, close >= high would only ever be
    # true when the close is the bar's own high -- that is not a breakout.
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=20.0, low=11.0, close=12.5)

    assert decision.close_breakout is True
    assert decision.next_state.segment_high == 20.0


def test_a_low_equal_to_the_prior_trough_is_not_a_higher_low() -> None:
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=13.0, low=9.0, close=12.5)

    assert decision.turning_valid is False


def test_a_correction_does_not_close_or_reclassify_the_segment() -> None:
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=13.0, low=7.0, close=8.0)

    assert decision.turning_valid is False
    assert decision.trend_changed is False
    assert decision.next_state.trend is Trend.UP
    assert decision.next_state.last_down_lows == (9.0, 8.0)
    assert decision.next_state.segment_low == 7.0


def test_down_mirrors_up() -> None:
    state = prepared_down_state(
        segment_high=10.0, segment_low=8.0, last_up_highs=(11.0, 12.0)
    )

    decision = inspect_bar(state, trend=Trend.DOWN, high=10.5, low=7.0, close=7.9)

    assert decision.turning_valid is True  # candidate high 10.5 < 11.0
    assert decision.dow_resonance is True  # 11.0 < 12.0
    assert decision.close_breakout is True  # 7.9 <= pre-bar low 8.0
    assert decision.next_state.segment_low == 7.0


def test_a_down_high_equal_to_the_prior_peak_is_not_a_lower_high() -> None:
    state = prepared_down_state(
        segment_high=10.0, segment_low=8.0, last_up_highs=(11.0, 12.0)
    )

    decision = inspect_bar(state, trend=Trend.DOWN, high=11.0, low=7.0, close=7.9)

    assert decision.turning_valid is False


def test_history_keeps_only_the_last_two_extremes() -> None:
    state = SegmentState.empty()
    for index, (trend, high, low) in enumerate(
        [
            (Trend.UP, 11.0, 9.0),
            (Trend.DOWN, 10.0, 7.0),
            (Trend.UP, 15.0, 8.0),
            (Trend.DOWN, 14.0, 5.0),
            (Trend.UP, 20.0, 6.0),
            (Trend.DOWN, 19.0, 3.0),
        ]
    ):
        state = inspect_bar(
            state, trend=trend, high=high, low=low, close=(high + low) / 2
        ).next_state

    assert state.last_up_highs == (20.0, 15.0)
    assert state.last_down_lows == (5.0, 7.0)


def test_a_neutral_stretch_creates_no_segment() -> None:
    state = SegmentState.empty()

    decision = inspect_bar(state, trend=Trend.NEUTRAL, high=11.0, low=9.0, close=10.0)

    assert decision.next_state.segment_high is None
    assert decision.next_state.segment_low is None
    assert decision.close_breakout is False
    assert decision.enough_history is False


def test_the_first_bar_of_a_new_segment_is_never_a_breakout() -> None:
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0, 8.0)
    )

    decision = inspect_bar(state, trend=Trend.DOWN, high=13.0, low=7.0, close=7.5)

    assert decision.trend_changed is True
    assert decision.close_breakout is False


def test_enough_history_is_false_until_two_prior_troughs_exist() -> None:
    state = prepared_up_state(
        segment_high=12.0, segment_low=10.0, last_down_lows=(9.0,)
    )

    decision = inspect_bar(state, trend=Trend.UP, high=13.0, low=11.0, close=12.5)

    assert decision.enough_history is False
    assert decision.dow_resonance is False


def test_a_bar_whose_close_sits_outside_its_own_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_bar"):
        inspect_bar(
            SegmentState.empty(), trend=Trend.UP, high=11.0, low=9.0, close=12.0
        )


def test_a_non_neutral_state_without_a_segment_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_segment"):
        SegmentState(
            trend=Trend.UP,
            segment_high=None,
            segment_low=None,
            last_up_highs=(),
            last_down_lows=(),
        )
