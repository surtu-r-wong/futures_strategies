"""开盘趋势信号：研报的"前三根 15 分钟 bar"判据（计划 Task 3）。

本模块只认已经切好的开盘 bar 序列，不碰分钟表、不碰交易时段 —— 那些属于
分钟数据层（计划 Task 0 裁决 A：等 `feature/carry-minute-execution` merge 后
抽到 `common/minute/`）。这样切分使本模块可以在真实分钟数据到位之前就被
确定性合成数据完整验证。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


#: 研报判据只看开盘后的前三根 15 分钟 bar。
OPENING_BAR_COUNT = 3


@dataclass(frozen=True)
class Bar:
    """一根 15 分钟 K 线的价格四元组。"""

    open: float
    high: float
    low: float
    close: float

    def is_valid(self) -> bool:
        """四个价格都是有限数才算有效。

        显式判定，不依赖"NaN 的比较恒为假"这条巧合：多头判据只读
        open/low/close，一根只有 ``high`` 坏掉的 bar 会沿着判据的盲区
        溜过去，靠比较运算是拦不住的。
        """
        return all(
            math.isfinite(v) for v in (self.open, self.high, self.low, self.close)
        )


class OpeningSignal(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


def _strictly_rising(values: Sequence[float]) -> bool:
    return all(a < b for a, b in zip(values, values[1:]))


def _strictly_falling(values: Sequence[float]) -> bool:
    return all(a > b for a, b in zip(values, values[1:]))


def opening_signal(bars: Sequence[Bar]) -> OpeningSignal:
    """按研报口径判定开盘信号。

    多头看 ``low``、空头看 ``high`` —— 这个不对称是研报原文，不是笔误：
    上行趋势要求回撤底部逐根抬高，下行趋势要求反弹顶部逐根降低。
    """
    opening = list(bars[:OPENING_BAR_COUNT])
    if len(opening) < OPENING_BAR_COUNT or not all(b.is_valid() for b in opening):
        # 不足三根、或其中任一根价格不完整，都没有判据。长度这一条必须显式写：
        # ``all(())`` 恒真，缺了它，空序列和两根序列都会被判成多头。
        return OpeningSignal.NEUTRAL

    opens = [b.open for b in opening]
    closes = [b.close for b in opening]

    if (
        _strictly_rising(opens)
        and _strictly_rising([b.low for b in opening])
        and _strictly_rising(closes)
    ):
        return OpeningSignal.LONG
    if (
        _strictly_falling(opens)
        and _strictly_falling([b.high for b in opening])
        and _strictly_falling(closes)
    ):
        return OpeningSignal.SHORT
    return OpeningSignal.NEUTRAL
