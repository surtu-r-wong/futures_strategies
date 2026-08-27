"""指标层：EMA / 距离扩大 / TNR / ΔTNR / ATR —— 计划 Task 4。

⚠️ **本层只吃有成交的 bar**（计划 D13）。`futures_minute` 里 `volume = 0` 的空 K 线
带的是前收结转价而非成交价：让它进窗口会把 TR 压成 0、把 TNR 的分母压小，两个方向
都会假装市场比实际更平稳 —— 而「平稳」正是本策略的开仓条件。把空 bar 剔掉、在稠密
的已成交序列上算指标，再由回测层铺回全局时间栅格（研报附录一「信号延续」）。

研报没写的两处在这里落地并进裁决表：`alpha = 2/(span+1)`、`adjust=False`（D12）；
路程为 0 时 TNR 取 NaN 而不是 0 或 1（分母为零，研报没写这种情形）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = ["atr_series", "delta_tnr", "ema", "gap_widening", "tnr_series", "true_range"]


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


def gap_widening(short: Sequence[float], long: Sequence[float]) -> list[bool]:
    """两条均线的**绝对**距离是否比上一根更大。

    取绝对值而不是带符号的差：空头一侧短均线在下方越走越远，同样是「距离扩大」。
    首根没有可比对象，取 ``False``。
    """
    fast = _as_float_array(short, "gap_short")
    slow = _as_float_array(long, "gap_long")
    if fast.size != slow.size:
        raise ValueError("gap_length: 两条均线长度必须相同")
    distance = np.abs(fast - slow)
    out = [False]
    out.extend(bool(distance[i] > distance[i - 1]) for i in range(1, distance.size))
    return out


def tnr_series(closes: Sequence[float], *, window: int) -> np.ndarray:
    """趋势噪音比 `|C_t − C_{t−N}| / Σ|C_i − C_{i−1}|`（研报 §3.1）。

    分子是「位移」、分母是「路程」。窗口未满或路程为 0 时取 ``NaN``。
    """
    if type(window) is not int or window < 1:
        raise ValueError(f"tnr_window: window 必须是 >= 1 的整数；got {window!r}")
    array = _as_float_array(closes, "tnr_closes")
    out = np.full(array.size, np.nan)
    if array.size <= window:
        return out
    steps = np.abs(np.diff(array))
    travelled = np.convolve(steps, np.ones(window), mode="valid")
    for index in range(window, array.size):
        path = travelled[index - window]
        if path == 0.0:
            continue
        out[index] = abs(array[index] - array[index - window]) / path
    return out


def delta_tnr(values: Sequence[float], *, k: int = 3) -> np.ndarray:
    """`ΔTNR_t = TNR_t − mean(TNR_t … TNR_{t−k+1})`（计划 D7）。

    ⚠️ 求和**含 TNR_t 自己**，所以 k=3 时展开是 `(2/3)·TNR_t − (1/3)(TNR_{t−1} + TNR_{t−2})`。
    研报正文另说「当日与 3 日前比较」，与公式图不符；这里取公式图。
    """
    if type(k) is not int or k < 1:
        raise ValueError(f"delta_tnr_k: k 必须是 >= 1 的整数；got {k!r}")
    array = _as_float_array(values, "delta_tnr_values")
    out = np.full(array.size, np.nan)
    for index in range(k - 1, array.size):
        window = array[index - k + 1 : index + 1]
        if np.isnan(window).any():
            continue
        out[index] = array[index] - window.mean()
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
