"""Cumulative MACD distance and the preliminary trend it drives."""

from __future__ import annotations

import numpy as np
import pytest

from common.commodity.indicators import ema
from cta_dow.indicators import (
    MacdPath,
    Trend,
    cumulative_macd,
    macd_path,
    preliminary_trend,
)


def test_cumulative_macd_resets_only_when_diff_changes_sign() -> None:
    got = cumulative_macd([1.0, 2.0, 0.0, -1.0, -2.0, 1.0])

    assert got.tolist() == [1.0, 3.0, 3.0, 2.0, 0.0, 1.0]


def test_a_zero_diff_carries_the_running_total_instead_of_resetting() -> None:
    # 0 * previous is 0, which is not strictly negative, so no sign change
    # happened and the accumulation must survive the flat bar.
    assert cumulative_macd([2.0, 0.0, 1.0]).tolist() == [2.0, 2.0, 3.0]


def test_macd_path_uses_ema_twelve_twenty_six_and_nine() -> None:
    closes = np.arange(40.0)

    path = macd_path(closes)

    expected = ema(closes, span=12) - ema(closes, span=26)
    assert isinstance(path, MacdPath)
    assert np.allclose(path.macd, expected)
    assert np.allclose(path.signal_line, ema(expected, span=9))
    assert np.allclose(path.diff, expected - ema(expected, span=9))
    assert np.allclose(path.cumulative, cumulative_macd(path.diff))


def test_threshold_gap_keeps_the_previous_trend() -> None:
    got = preliminary_trend(
        cumulative=[0.5, 1.1, 0.2, -0.4, -1.2, -0.2],
        atr=[1.0] * 6,
    )

    assert got == (
        Trend.NEUTRAL,
        Trend.UP,
        Trend.UP,
        Trend.UP,
        Trend.DOWN,
        Trend.DOWN,
    )


def test_touching_the_threshold_exactly_switches_the_trend() -> None:
    assert preliminary_trend(cumulative=[1.0], atr=[1.0]) == (Trend.UP,)
    assert preliminary_trend(cumulative=[-1.0], atr=[1.0]) == (Trend.DOWN,)


def test_nothing_before_the_first_trigger_is_a_trend() -> None:
    assert preliminary_trend(cumulative=[0.9, -0.9], atr=[1.0, 1.0]) == (
        Trend.NEUTRAL,
        Trend.NEUTRAL,
    )


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_nonpositive_atr_is_rejected(bad: float) -> None:
    # A zero threshold would make every cumulative distance a trigger, which is
    # not "no trend yet" but "always in trend".
    with pytest.raises(ValueError, match="dow_atr"):
        preliminary_trend(cumulative=[0.5], atr=[bad])


def test_a_nonfinite_bar_is_rejected_rather_than_carried() -> None:
    # No-trade bars are dropped by the caller; they must never arrive as NaN
    # and be silently treated as "keep the previous trend".
    with pytest.raises(ValueError, match="dow_cumulative"):
        preliminary_trend(cumulative=[0.5, np.nan], atr=[1.0, 1.0])
    with pytest.raises(ValueError, match="dow_atr"):
        preliminary_trend(cumulative=[0.5, 0.5], atr=[1.0, np.nan])


def test_mismatched_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="dow_trend_length"):
        preliminary_trend(cumulative=[0.5, 0.5], atr=[1.0])


def test_empty_input_is_empty_output() -> None:
    assert cumulative_macd([]).tolist() == []
    assert preliminary_trend(cumulative=[], atr=[]) == ()


def test_two_dimensional_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_diff"):
        cumulative_macd([[1.0, 2.0], [3.0, 4.0]])
