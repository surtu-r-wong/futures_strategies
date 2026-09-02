"""商品期货策略共用的 EMA / TR / ATR 指标。"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = ["atr_series", "daily_atr_by_bar", "ema", "true_range"]


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


def daily_atr_by_bar(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    days: Sequence[object],
    *,
    window: int,
) -> np.ndarray:
    """按「每日」波动幅度算的 ATR，摊到每根 bar 上。

    研报原文说 TR 「用于衡量每日的价格波动幅度」：先把同一交易日的 bar 聚成日
    H/L/C，日 TR 用前一日收盘，ATR 是最近 `window` 个**已完成**交易日的均值。
    t 日的 bar 只能看到 t−1 日为止的值 —— 当天的区间还没走完，不能预知。
    完成日不足 `window` 个的 bar 为 NaN。

    ⚠️ 传进来的序列必须已经剔掉无成交 bar，且按时间排好（`days` 单调不减）。
    """
    if type(window) is not int or window < 1:
        raise ValueError(f"atr_window: window 必须是 >= 1 的整数；got {window!r}")
    highs = _as_float_array(high, "atr_high")
    lows = _as_float_array(low, "atr_low")
    closes = _as_float_array(close, "atr_close")
    labels = list(days)
    if not highs.size == lows.size == closes.size == len(labels):
        raise ValueError("atr_length: 四条序列长度必须相同")
    out = np.full(highs.size, np.nan)
    if not highs.size:
        return out

    # 逐日聚合；日期回退说明序列没排好，直接拒绝而不是悄悄合并。
    starts: list[int] = [0]
    for index in range(1, highs.size):
        if labels[index] != labels[index - 1]:
            if labels[index] in labels[: starts[-1]]:
                raise ValueError("daily_atr_days: 交易日必须按时间排好、不得回退")
            starts.append(index)
    starts.append(highs.size)

    ranges: list[float] = []
    previous_close: float | None = None
    for day, (begin, end) in enumerate(zip(starts, starts[1:])):
        if day >= window:
            out[begin:end] = float(np.mean(ranges[day - window : day]))
        ranges.append(
            true_range(
                float(highs[begin:end].max()),
                float(lows[begin:end].min()),
                previous_close,
            )
        )
        previous_close = float(closes[end - 1])
    return out
