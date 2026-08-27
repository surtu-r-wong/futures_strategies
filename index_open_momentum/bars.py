"""15 分钟 K 线与 5 分钟 VWAP —— 计划 Task 2。

**薄封装**：聚合、乘数反推、VWAP 与价域校验全部在 `common/minute/bars.py`，本模块
只做四件中金所侧的事：

1. 用 `fifteen_minute_buckets` 发的桶驱动聚合 —— 桶本身不跨休市，所以"绝不跨休市
   拼成一根"是**结构上**保证的，不是这里再判一次；
2. 把成交价越界容差从共享层的 `1e-4` **收紧到 `1e-6`**（见下）；
3. 乘数「先元数据、缺失才反推」的分流；
4. 把 `FifteenMinuteBar` 折成本包的 `Bar`，并把"这根没成交"单独立成一个标志位。

## 为什么容差是 1e-6 而不是共享层的 1e-4

共享层 `_fill_epsilon` 取 `1e-4`，理由写在它自己的 docstring 里：**商品**的涨停锁死
bar（`high == low`）配上取整后的 turnover，能算出略微出界的 VWAP（铁矿石实测高出
606.5 约八千分之一）。那是算术残差，不是成交在市场之外。

中金所不是这个情形。2026-08-27 实测 IF/IC/IH 主力在 2012/2015/2018/2024 四年的
**2,139 个真实执行窗口**，相对越界**最大值 = 0.0** —— 含一个涨停锁死窗口。
所以本层按计划口径收到 `1e-6`：容差是探测 `amount` 是否可信的**唯一探针**
（郑商所的合成 turnover 就是这么抓出来的），能收紧就不该留宽。

⚠️ 但**零宽窗口（`low == high`）豁免这道紧闸**：那时窗口只可能成交在这一个价上，
越界**只能**是取整残差 —— 共享层那段论证在这里同样成立。`relative_excursion()`
仍如实报出残差，好让保真度报告看得见它。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from common.minute.bars import (
    MinuteDataError,
    MultiplierResolution,
    VwapFill,
    aggregate_fifteen_minute_bar,
    five_minute_vwap,
    infer_contract_multiplier,
    validate_metadata_multiplier,
)
from index_open_momentum.types import Bar

__all__ = [
    "MAX_RELATIVE_EXCURSION",
    "IndexBar",
    "build_index_bars",
    "index_execution_fill",
    "relative_excursion",
    "resolve_index_multiplier",
]

#: 中金所侧的成交价越界容差。见模块 docstring：实测 2,139 个执行窗口越界为 0。
MAX_RELATIVE_EXCURSION = 1e-6


@dataclass(frozen=True)
class IndexBar:
    """一根 15 分钟 K 线，外加"这根有没有成交"。

    `bar` 在无成交时是 `None` 而不是一组 0 或 NaN：本包的 `Bar` 语义是"四个价格
    都成立"，拿哨兵值冒充会让下游的比较运算**静默**给出答案。no-trade 必须是
    调用方绕不过去的一个分支。
    """

    start: datetime
    end: datetime
    bar: Bar | None
    volume: float
    traded_rows: int
    missing_slots: int

    @property
    def no_trade(self) -> bool:
        return self.bar is None


def build_index_bars(
    frame: pd.DataFrame,
    *,
    buckets: Sequence[Sequence[datetime]],
    contract: str,
) -> tuple[IndexBar, ...]:
    """按时段桶逐桶聚合。桶来自 `fifteen_minute_buckets`，绝不跨休市。"""
    bars: list[IndexBar] = []
    for bucket in buckets:
        slots = tuple(bucket)
        window = frame.loc[frame["bar_time"].isin(pd.Index(slots))]
        aggregated = aggregate_fifteen_minute_bar(
            window,
            slots=slots,
            contract=contract,
        )
        bars.append(
            IndexBar(
                start=aggregated.start,
                end=aggregated.end,
                bar=(
                    None
                    if aggregated.no_trade
                    else Bar(
                        open=aggregated.open,
                        high=aggregated.high,
                        low=aggregated.low,
                        close=aggregated.close,
                    )
                ),
                volume=aggregated.volume,
                traded_rows=aggregated.traded_rows,
                missing_slots=aggregated.missing_slots,
            )
        )
    return tuple(bars)


def resolve_index_multiplier(
    frame: pd.DataFrame,
    *,
    contract: str,
    metadata_multiplier: int | None = None,
) -> MultiplierResolution:
    """先元数据、缺失才反推。

    ⚠️ `public.futures_contract_info` 实测只覆盖 **2025-12-22 起**（快照表，不是
    历史表），所以 2011–2025 的回测窗口**绝大部分走反推分支**。元数据在场时不是
    直接采信，而是拿它去过价域校验 —— 元数据与 bar 打架必须炸，不许静默选一边。
    """
    if metadata_multiplier is None:
        return infer_contract_multiplier(frame, contract=contract)
    return validate_metadata_multiplier(
        frame,
        contract=contract,
        multiplier=metadata_multiplier,
    )


def _raw_vwap(fill: VwapFill) -> float:
    """成交价被夹回窗口内之前的原始值。

    `five_minute_vwap` 返回的 `price` 已经夹过（`min(max(price, low), high)`），
    所以越界幅度只能从 `amount / volume / multiplier` 重算 —— 这不是重复实现，
    是同一个除法读第二遍。
    """
    return fill.amount / fill.volume / fill.multiplier


def relative_excursion(fill: VwapFill) -> float:
    """原始 VWAP 落在 `[low, high]` 之外多远，按价格量级归一。

    永远如实报，不因零宽窗口豁免而归零 —— 豁免的是**闸门**，不是**度量**。
    """
    raw = _raw_vwap(fill)
    scale = max(1.0, abs(fill.low), abs(fill.high))
    return max(0.0, fill.low - raw, raw - fill.high) / scale


def index_execution_fill(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    contract: str,
    multiplier: int,
    max_relative_excursion: float = MAX_RELATIVE_EXCURSION,
) -> VwapFill:
    """信号后 5 分钟的 VWAP 成交，外加中金所侧收紧的价域闸。

    零成交窗口由共享层硬失败（`execution_vwap`），本层**不接住、不顺延到更晚的
    窗口** —— 顺延等于偷偷换一个研报没写的成交时刻。
    """
    fill = five_minute_vwap(
        frame,
        slots=slots,
        contract=contract,
        multiplier=multiplier,
    )
    if fill.low == fill.high:
        # 零宽窗口：只可能成交在这一个价上，越界只能是取整残差。见模块 docstring。
        return fill

    excursion = relative_excursion(fill)
    if excursion > max_relative_excursion:
        raise MinuteDataError(
            check="execution_vwap",
            reason=(
                "VWAP excursion exceeds the stock-index tolerance; the turnover "
                "column and the bar range disagree"
            ),
            contract=contract,
            trade_date=fill.trade_date,
            timestamp=fill.start,
            context={
                "raw_vwap": _raw_vwap(fill),
                "low": fill.low,
                "high": fill.high,
                "relative_excursion": excursion,
                "max_relative_excursion": max_relative_excursion,
            },
        )
    return fill
