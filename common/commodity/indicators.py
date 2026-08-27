"""商品期货策略共用的 EMA / TR / ATR 指标。"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = ["atr_series", "ema", "true_range"]


def _as_float_array(values: Sequence[float], label: str) -> np.ndarray:
    array = np.asarray(values, dtype="float64")
    if array.ndim != 1:
        raise ValueError(f"{label}: 需要一维序列")
    return array


def ema(values: Sequence[float], *, span: int) -> np.ndarray:
    """指数移动平均，`alpha = 2/(span+1)`，不做 adjust 修正（计划 D12）。

    首项取原值 —— 递推需要一个起点，研报没写别的。
    """
    if type(span) is not int or span < 1:
        raise ValueError(f"ema_span: span 必须是 >= 1 的整数；got {span!r}")
    array = _as_float_array(values, "ema_values")
    if array.size == 0:
        return array
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(array)
    out[0] = array[0]
    for index in range(1, array.size):
        out[index] = alpha * array[index] + (1.0 - alpha) * out[index - 1]
    return out


def true_range(high: float, low: float, previous_close: float | None) -> float:
    """`max(h − l, |h − 前收|, |l − 前收|)`；没有前收时退化为 `h − l`。"""
    span = high - low
    if previous_close is None or (
        isinstance(previous_close, float) and math.isnan(previous_close)
    ):
        return span
    return max(span, abs(high - previous_close), abs(low - previous_close))


def atr_series(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    window: int,
) -> np.ndarray:
    """TR 的移动平均。窗口未满时取前面全部已知的 TR 的均值。

    ⚠️ 传进来的三条序列必须**已经**剔掉无成交 bar（D13）。
    """
    if type(window) is not int or window < 1:
        raise ValueError(f"atr_window: window 必须是 >= 1 的整数；got {window!r}")
    highs = _as_float_array(high, "atr_high")
    lows = _as_float_array(low, "atr_low")
    closes = _as_float_array(close, "atr_close")
    if not highs.size == lows.size == closes.size:
        raise ValueError("atr_length: 三条序列长度必须相同")

    ranges = np.empty(highs.size)
    for index in range(highs.size):
        previous = closes[index - 1] if index else None
        ranges[index] = true_range(highs[index], lows[index], previous)

    out = np.empty(highs.size)
    for index in range(highs.size):
        start = max(0, index - window + 1)
        out[index] = ranges[start : index + 1].mean()
    return out
