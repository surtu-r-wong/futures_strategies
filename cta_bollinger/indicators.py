"""Bollinger-band and open-interest indicator paths."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "BandPath",
    "OpenInterestPath",
    "bands",
    "oi_multiplier",
    "rolling_oi",
]


def _readonly_float_array(values: Sequence[float], *, label: str) -> np.ndarray:
    try:
        array = np.array(values, dtype="float64", copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: values must be numeric") from exc
    if array.ndim != 1:
        raise ValueError(f"{label}: expected one dimension")
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class BandPath:
    """Full-length Bollinger path with ``NaN`` values during warmup."""

    middle: np.ndarray
    std: np.ndarray
    upper: np.ndarray
    lower: np.ndarray

    def __post_init__(self) -> None:
        for name in ("middle", "std", "upper", "lower"):
            object.__setattr__(
                self,
                name,
                _readonly_float_array(
                    getattr(self, name), label=f"bollinger_band_{name}"
                ),
            )


@dataclass(frozen=True, slots=True)
class OpenInterestPath:
    """Full-length short and long simple moving-average paths."""

    short: np.ndarray
    long: np.ndarray

    def __post_init__(self) -> None:
        for name in ("short", "long"):
            object.__setattr__(
                self,
                name,
                _readonly_float_array(
                    getattr(self, name), label=f"bollinger_oi_{name}"
                ),
            )


def _finite_float_array(values: Sequence[float], *, label: str) -> np.ndarray:
    try:
        array = np.array(values, dtype="float64", copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: values must be numeric") from exc
    if array.ndim != 1:
        raise ValueError(f"{label}: expected one dimension")
    if not np.isfinite(array).all():
        raise ValueError(f"{label}: values must be finite")
    return array


def _full_window_mean(values: np.ndarray, *, window: int) -> np.ndarray:
    return (
        pd.Series(values, dtype="float64")
        .rolling(window, min_periods=window)
        .mean()
        .to_numpy(dtype="float64", copy=True)
    )


def bands(
    closes: Sequence[float],
    *,
    length: int = 300,
    beta: float = 1.5,
    ddof: int = 0,
) -> BandPath:
    """Return full-window Bollinger paths over a one-dimensional close series."""
    if type(length) is not int or length < 2:
        raise ValueError("bollinger_length: expected integer >= 2")
    if type(ddof) is not int or ddof not in (0, 1):
        raise ValueError("bollinger_ddof: expected 0 or 1")
    try:
        width = float(beta)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("bollinger_beta: expected finite positive value") from exc
    if isinstance(beta, (bool, np.bool_)) or not math.isfinite(width) or width <= 0.0:
        raise ValueError("bollinger_beta: expected finite positive value")

    values = _finite_float_array(closes, label="bollinger_closes")
    frame = pd.Series(values, dtype="float64")
    rolling = frame.rolling(length, min_periods=length)
    middle = rolling.mean().to_numpy(dtype="float64", copy=True)
    std = rolling.std(ddof=ddof).to_numpy(dtype="float64", copy=True)
    return BandPath(
        middle=middle,
        std=std,
        upper=middle + width * std,
        lower=middle - width * std,
    )


def rolling_oi(
    open_interest: Sequence[float],
    *,
    short: int = 150,
    long: int = 300,
) -> OpenInterestPath:
    """Return full-window simple means for short and long open interest."""
    if (
        type(short) is not int
        or type(long) is not int
        or short < 1
        or short >= long
    ):
        raise ValueError(
            "bollinger_oi_windows: expected integers satisfying 1 <= short < long"
        )
    values = _finite_float_array(open_interest, label="bollinger_open_interest")
    return OpenInterestPath(
        short=_full_window_mean(values, window=short),
        long=_full_window_mean(values, window=long),
    )


def oi_multiplier(*, short_oi: float, long_oi: float) -> float:
    """Return full size only when short open interest strictly exceeds long."""
    try:
        short_value = float(short_oi)
        long_value = float(long_oi)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("bollinger_oi: both averages must be finite") from exc
    if not math.isfinite(short_value) or not math.isfinite(long_value):
        raise ValueError("bollinger_oi: both averages must be finite")
    return 1.0 if short_value > long_value else 0.5
