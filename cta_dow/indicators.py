"""Cumulative MACD distance, and the preliminary trend it switches.

The paper prints this recursion only as a picture, so two readings had to be
pinned. The accumulation resets on a *strict* sign change of the MACD
histogram, which means a bar whose histogram is exactly zero carries the
running total rather than restarting it -- resetting on zero would truncate
every accumulation that passes through the axis without crossing it. And the
threshold is compared with ``>=``: a distance that lands exactly on one ATR is
a trigger, not a near miss.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from common.commodity.indicators import ema

__all__ = [
    "MacdPath",
    "Trend",
    "cumulative_macd",
    "macd_path",
    "preliminary_trend",
]

#: 研报未完整披露，预注册裁决（保真度 D1）。
FAST_SPAN = 12
SLOW_SPAN = 26
SIGNAL_SPAN = 9


class Trend(StrEnum):
    NEUTRAL = "neutral"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class MacdPath:
    """One product's MACD, signal line, histogram, and cumulative distance."""

    macd: np.ndarray
    signal_line: np.ndarray
    diff: np.ndarray
    cumulative: np.ndarray


def _one_dimensional(values: Sequence[float], label: str) -> np.ndarray:
    array = np.asarray(values, dtype="float64")
    if array.ndim != 1:
        raise ValueError(f"{label}: expected one dimension")
    return array


def _finite(array: np.ndarray, label: str) -> np.ndarray:
    if array.size and not np.isfinite(array).all():
        raise ValueError(f"{label}: expected finite values on every traded bar")
    return array


def cumulative_macd(diff: Sequence[float]) -> np.ndarray:
    """Accumulate the histogram, restarting only when it crosses the axis."""
    values = _one_dimensional(diff, "dow_diff")
    if not values.size:
        return values.copy()
    out = np.empty_like(values)
    out[0] = values[0]
    for index in range(1, values.size):
        crossed = values[index] * values[index - 1] < 0.0
        out[index] = values[index] if crossed else out[index - 1] + values[index]
    return out


def macd_path(closes: Sequence[float]) -> MacdPath:
    """EMA(12)/EMA(26)/EMA(9) with ``adjust=False``, plus the cumulative path."""
    values = _finite(_one_dimensional(closes, "dow_closes"), "dow_closes")
    macd = ema(values, span=FAST_SPAN) - ema(values, span=SLOW_SPAN)
    signal_line = ema(macd, span=SIGNAL_SPAN)
    diff = macd - signal_line
    return MacdPath(
        macd=macd,
        signal_line=signal_line,
        diff=diff,
        cumulative=cumulative_macd(diff),
    )


def preliminary_trend(
    *,
    cumulative: Sequence[float],
    atr: Sequence[float],
) -> tuple[Trend, ...]:
    """Switch on ``|cumulative| >= atr``; between the thresholds, carry."""
    distances = _finite(
        _one_dimensional(cumulative, "dow_cumulative"), "dow_cumulative"
    )
    thresholds = _finite(_one_dimensional(atr, "dow_atr"), "dow_atr")
    if distances.size != thresholds.size:
        raise ValueError("dow_trend_length: cumulative and atr must be equal length")
    if thresholds.size and not (thresholds > 0.0).all():
        raise ValueError("dow_atr: expected strictly positive thresholds")

    trend = Trend.NEUTRAL
    out: list[Trend] = []
    for distance, threshold in zip(distances, thresholds):
        if distance >= threshold:
            trend = Trend.UP
        elif distance <= -threshold:
            trend = Trend.DOWN
        out.append(trend)
    return tuple(out)
