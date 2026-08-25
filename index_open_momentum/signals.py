"""开盘趋势信号：研报的"前三根 15 分钟 bar"判据（计划 Task 3）。

本模块只认已经切好的开盘 bar 序列，不碰分钟表、不碰交易时段 —— 那些属于
分钟数据层（计划 Task 0 裁决 A：等 `feature/carry-minute-execution` merge 后
抽到 `common/minute/`）。这样切分使本模块可以在真实分钟数据到位之前就被
确定性合成数据完整验证。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from index_open_momentum.types import Bar, strictly_falling, strictly_rising


#: 研报判据只看开盘后的前三根 15 分钟 bar。
OPENING_BAR_COUNT = 3


class OpeningSignal(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


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
        strictly_rising(opens)
        and strictly_rising([b.low for b in opening])
        and strictly_rising(closes)
    ):
        return OpeningSignal.LONG
    if (
        strictly_falling(opens)
        and strictly_falling([b.high for b in opening])
        and strictly_falling(closes)
    ):
        return OpeningSignal.SHORT
    return OpeningSignal.NEUTRAL
