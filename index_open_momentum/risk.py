"""ATR 与止损事件（计划 Task 4）。

两族止损在研报里是并列的，但**每根 bar 最多减一档**——所以"哪族触发"是
诊断信息，"减不减"才是决策。本模块把两者分开表达：各族的判据是独立的纯
函数，合并成事件的那一步单列。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Sequence

from index_open_momentum.types import Bar, strictly_falling, strictly_rising


def true_range(bar: Bar, previous_close: float | None) -> float:
    """研报口径的真实波幅。

    ``previous_close is None`` 表示这是可用序列的第一根，此时只剩 bar 自身
    的高低差——不是把前收当 0，那会造出一个与价格同量级的假 TR。
    """
    if previous_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


#: 研报在 15 分钟 K 线上取 16 根窗口算 ATR。
ATR_WINDOW = 16


def atr_series(bars: Sequence[Bar], *, window: int = ATR_WINDOW) -> list[float | None]:
    """逐根给出滚动 ATR；窗口未满的位置是 ``None``。

    ⚠️ **复刻假设**：研报只写"16 根窗口"，没写平滑方式。这里取**简单算术
    均值**，不是 Wilder 平滑。两者在同一窗口长度下数值不同，会改变吊灯止损
    的触发时点——所以它是一条必须写进保真度报告的显式假设，不是实现细节。

    ``None`` 而不是 0 或前向填充：窗口未满时 ATR **不存在**，用任何数值占位
    都会让下游的吊灯止损在最不该触发的时候（历史最短处）算出一个阈值。
    """
    ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        ranges.append(true_range(bar, previous_close))
        previous_close = bar.close

    out: list[float | None] = []
    for i in range(len(ranges)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(sum(ranges[i + 1 - window : i + 1]) / window)
    return out


#: 反向信号止损的窗口长度：多头 5 根、空头 3 根（研报原文，两族不等长）。
LONG_REVERSE_STOP_BARS = 5
SHORT_REVERSE_STOP_BARS = 3


def long_reverse_stop(bars: Sequence[Bar]) -> bool:
    """多头反向信号止损：最近 5 根 bar 的 ``high`` 严格递减。"""
    if len(bars) < LONG_REVERSE_STOP_BARS:
        return False
    window = bars[-LONG_REVERSE_STOP_BARS:]
    return strictly_falling([b.high for b in window])


def short_reverse_stop(bars: Sequence[Bar]) -> bool:
    """空头反向信号止损：最近 3 根 bar 的 ``low`` 严格递增。

    与多头不同族也不同长（5 vs 3）—— 研报如此，不要"对称化"。
    """
    if len(bars) < SHORT_REVERSE_STOP_BARS:
        return False
    window = bars[-SHORT_REVERSE_STOP_BARS:]
    return strictly_rising([b.low for b in window])


#: 吊灯止损的 ATR 倍数：多头 2.5、空头 2.0（研报原文，两侧不对称）。
LONG_CHANDELIER_ATR_MULTIPLE = 2.5
SHORT_CHANDELIER_ATR_MULTIPLE = 2.0


def _require_atr(atr: float | None) -> float:
    """ATR 缺失时硬失败，不返回"未触发"。

    窗口未满或数据坏掉时静默判"不触发"，等于在**风控最弱的时刻**把风控关掉，
    而且外部看到的现象与"行情很稳"完全一致 —— 这是最难发现的一类错。调用方
    必须显式处理"这根 bar 还没有 ATR"（研报口径下的处理是当日不建仓：没有
    ATR 就既算不出吊灯阈值，也算不出杠杆）。
    """
    if atr is None or not math.isfinite(atr) or atr < 0:
        raise ValueError(
            f"chandelier stop needs a finite non-negative ATR; got {atr!r}. "
            "ATR 窗口未满就不该建仓，更不该在无 ATR 的情况下判定止损"
        )
    return atr


def long_chandelier_stop(
    *, close: float, best_high_since_entry: float, atr: float | None
) -> bool:
    """多头吊灯止损：收盘跌破"入场后最高价 - 2.5 * ATR"。

    严格小于 —— 研报是"跌破"，阈值相等不触发。
    """
    threshold = best_high_since_entry - LONG_CHANDELIER_ATR_MULTIPLE * _require_atr(atr)
    return close < threshold


def short_chandelier_stop(
    *, close: float, best_low_since_entry: float, atr: float | None
) -> bool:
    """空头吊灯止损：收盘涨破"入场后最低价 + 2.0 * ATR"。"""
    threshold = best_low_since_entry + SHORT_CHANDELIER_ATR_MULTIPLE * _require_atr(atr)
    return close > threshold


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class StopReason(StrEnum):
    CHANDELIER = "chandelier"
    REVERSE = "reverse"


@dataclass(frozen=True)
class StopEvent:
    """一根 bar 上的减仓事件。

    ``triggered`` 记录**所有**响了的族，``scale_down_steps`` 恒为 1 —— 研报
    规定每根 bar 最多减一档。这两件事分开存是有意的：研报没有给两族排优先级，
    所以本实现也不排；把"响了哪些"完整留在事件上交给报告层，胜过在这里编一个
    研报没有的次序、再让下游把它当成事实。
    """

    triggered: tuple[StopReason, ...]
    scale_down_steps: int = 1


def stop_event(
    direction: Direction,
    bars: Sequence[Bar],
    *,
    atr: float | None,
    best_high_since_entry: float | None = None,
    best_low_since_entry: float | None = None,
) -> StopEvent | None:
    """判定这根 bar 是否产生减仓事件；不产生则返回 ``None``。"""
    if direction is Direction.LONG:
        if best_high_since_entry is None:
            raise ValueError("a long position needs best_high_since_entry")
        reverse = long_reverse_stop(bars)
        chandelier = long_chandelier_stop(
            close=bars[-1].close, best_high_since_entry=best_high_since_entry, atr=atr
        )
    else:
        if best_low_since_entry is None:
            raise ValueError("a short position needs best_low_since_entry")
        reverse = short_reverse_stop(bars)
        chandelier = short_chandelier_stop(
            close=bars[-1].close, best_low_since_entry=best_low_since_entry, atr=atr
        )

    triggered = tuple(
        reason
        for reason, fired in (
            (StopReason.CHANDELIER, chandelier),
            (StopReason.REVERSE, reverse),
        )
        if fired
    )
    return StopEvent(triggered=triggered) if triggered else None
