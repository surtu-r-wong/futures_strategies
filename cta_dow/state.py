"""Preliminary trend segments and the extreme history the entry gates read.

Two orderings decide what this module means, and both are silent in the paper.

Segments are cut by the *preliminary* MACD trend alone (D3). Turning-point
invalidation stops trading; it does not end a segment or reclassify it, because
a corrected segment would rewrite the very extremes the correction is measured
against.

Within one bar, the breakout is compared against the extreme as it stood
*before* the bar (D5), and only then does the bar join the extreme. Update the
extreme first and ``close >= segment_high`` degenerates into "the close equals
this bar's own high", which is a description of a bar, not a breakout.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any

from cta_dow.indicators import Trend

__all__ = ["BREAKOUT_REFERENCES", "SegmentDecision", "SegmentState", "inspect_bar"]

#: 研报只用到前两段的极值。
HISTORY_DEPTH = 2

#: 突破参照的两种读法：本段此前的临时极值（登记默认 D5），或上一同向段的整段极值 ——
#: 研报公式块定义了 lastmax_1 / lastmin_1 却没在正文条件里用到，图 13 标注的正是「第一高点」。
BREAKOUT_REFERENCES = ("segment", "prior_extreme")


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label}: expected a finite numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label}: expected a finite numeric value")
    return result


def _history(values: Any, label: str) -> tuple[float, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{label}: expected a tuple")
    if len(values) > HISTORY_DEPTH:
        raise ValueError(f"{label}: expected at most {HISTORY_DEPTH} values")
    return tuple(_finite(value, label) for value in values)


@dataclass(frozen=True, slots=True)
class SegmentState:
    """The running segment plus the two previous segments' extremes."""

    trend: Trend
    segment_high: float | None
    segment_low: float | None
    last_up_highs: tuple[float, ...]
    last_down_lows: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.trend, Trend):
            raise ValueError("dow_segment_trend: expected a Trend")
        object.__setattr__(
            self, "last_up_highs", _history(self.last_up_highs, "dow_segment_up_highs")
        )
        object.__setattr__(
            self,
            "last_down_lows",
            _history(self.last_down_lows, "dow_segment_down_lows"),
        )
        if self.trend is Trend.NEUTRAL:
            if self.segment_high is not None or self.segment_low is not None:
                raise ValueError("dow_segment_neutral: a neutral state has no segment")
            return
        if self.segment_high is None or self.segment_low is None:
            raise ValueError("dow_segment_extent: a trending state needs a segment")
        high = _finite(self.segment_high, "dow_segment_high")
        low = _finite(self.segment_low, "dow_segment_low")
        if high < low:
            raise ValueError("dow_segment_extent: high must not be below low")
        object.__setattr__(self, "segment_high", high)
        object.__setattr__(self, "segment_low", low)

    @classmethod
    def empty(cls) -> "SegmentState":
        return cls(Trend.NEUTRAL, None, None, (), ())


@dataclass(frozen=True, slots=True)
class SegmentDecision:
    """What one bar did to the segment, and which gates it opened."""

    next_state: SegmentState
    turning_valid: bool
    dow_resonance: bool
    close_breakout: bool
    enough_history: bool
    trend_changed: bool


def _prepend(values: tuple[float, ...], value: float) -> tuple[float, ...]:
    return (value, *values)[:HISTORY_DEPTH]


def _switch(
    state: SegmentState, *, trend: Trend, high: float, low: float
) -> SegmentDecision:
    up_highs = state.last_up_highs
    down_lows = state.last_down_lows
    if state.trend is Trend.UP and state.segment_high is not None:
        up_highs = _prepend(up_highs, state.segment_high)
    elif state.trend is Trend.DOWN and state.segment_low is not None:
        down_lows = _prepend(down_lows, state.segment_low)

    trending = trend is not Trend.NEUTRAL
    return SegmentDecision(
        next_state=SegmentState(
            trend=trend,
            segment_high=high if trending else None,
            segment_low=low if trending else None,
            last_up_highs=up_highs,
            last_down_lows=down_lows,
        ),
        turning_valid=False,
        dow_resonance=False,
        close_breakout=False,
        enough_history=False,
        trend_changed=True,
    )


def inspect_bar(
    state: SegmentState,
    *,
    trend: Trend,
    high: float,
    low: float,
    close: float,
    breakout_reference: str = "segment",
) -> SegmentDecision:
    """Advance one traded bar and report the gates it leaves open.

    ``breakout_reference`` picks what the close must clear: ``"segment"`` (the
    registered default) is the running extreme of the current segment before
    this bar joins it; ``"prior_extreme"`` is the whole previous same-side
    segment's extreme -- ``lastmax_1`` in the paper's formula block, the
    "第一高点" of its figure 13 -- so a breakout is a higher high in the Dow
    sense, not merely a new high within the segment.
    """
    if not isinstance(state, SegmentState):
        raise ValueError("dow_state: expected a SegmentState")
    if breakout_reference not in BREAKOUT_REFERENCES:
        raise ValueError(
            "dow_breakout_reference: expected one of "
            f"{BREAKOUT_REFERENCES}; got {breakout_reference!r}"
        )
    if not isinstance(trend, Trend):
        raise ValueError("dow_trend: expected a Trend")
    high = _finite(high, "dow_bar_high")
    low = _finite(low, "dow_bar_low")
    close = _finite(close, "dow_bar_close")
    if not (low <= close <= high):
        raise ValueError("dow_bar: close must lie inside the bar's own range")

    if trend is not state.trend:
        return _switch(state, trend=trend, high=high, low=low)

    if trend is Trend.NEUTRAL:
        return SegmentDecision(
            next_state=state,
            turning_valid=False,
            dow_resonance=False,
            close_breakout=False,
            enough_history=False,
            trend_changed=False,
        )

    assert state.segment_high is not None and state.segment_low is not None
    # Read the breakout reference before the bar joins the extreme (D5).
    prior_high = state.segment_high
    prior_low = state.segment_low
    candidate_high = max(prior_high, high)
    candidate_low = min(prior_low, low)

    if trend is Trend.UP:
        history = state.last_down_lows
        enough_history = len(history) >= HISTORY_DEPTH
        turning_valid = bool(history) and candidate_low > history[0]
        resonance = enough_history and history[0] > history[1]
        if breakout_reference == "prior_extreme":
            breakout = bool(state.last_up_highs) and close >= state.last_up_highs[0]
        else:
            breakout = close >= prior_high
    else:
        history = state.last_up_highs
        enough_history = len(history) >= HISTORY_DEPTH
        turning_valid = bool(history) and candidate_high < history[0]
        resonance = enough_history and history[0] < history[1]
        if breakout_reference == "prior_extreme":
            breakout = bool(state.last_down_lows) and close <= state.last_down_lows[0]
        else:
            breakout = close <= prior_low

    return SegmentDecision(
        next_state=SegmentState(
            trend=trend,
            segment_high=candidate_high,
            segment_low=candidate_low,
            last_up_highs=state.last_up_highs,
            last_down_lows=state.last_down_lows,
        ),
        turning_valid=turning_valid,
        dow_resonance=resonance,
        close_breakout=breakout,
        enough_history=enough_history,
        trend_changed=False,
    )
