"""本包共用的价格值对象。

单独成模块是因为 `signals.py`（Task 3）与 `risk.py`（Task 4）都要用它，而
`bars.py`（Task 2）是分钟层的薄封装、还被 Task 0 裁决 A 阻塞着 —— 值对象不
应该等在那条依赖后面。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


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


def strictly_rising(values: Sequence[float]) -> bool:
    """严格递增。研报的判据一律是"严格"，持平不算。"""
    return all(a < b for a, b in zip(values, values[1:]))


def strictly_falling(values: Sequence[float]) -> bool:
    """严格递减。"""
    return all(a > b for a, b in zip(values, values[1:]))
