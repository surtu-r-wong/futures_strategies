"""ATR 杠杆、已实现波动率反馈与品种等权（计划 Task 6）。

研报的仓位是两段相乘再截断：先由 ATR 定"一个 ATR 的波动等于总资金的 0.5%"，
再用过去一年的策略已实现波动把整体拉向 15% 的年化目标。两段各自截到 4 倍，
顺序有实质差别（见 `final_leverage`）。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


#: 忠实口径的品种集合。IM 2022-07-22 才挂牌，晚于研报（2021-05-13），不在内。
PAPER_FAMILIES = ("IF", "IC", "IH")

#: 一个 ATR 的波动对应总资金的 0.5%。
ATR_CAPITAL_FRACTION = 0.005

#: 两段截断共用的上限。
MAX_LEVERAGE = 4.0

#: 目标年化波动。
TARGET_ANNUAL_VOL = 0.15

#: 年化用的交易日数，也是"过去一年"所需的最少观测数。
TRADING_DAYS_PER_YEAR = 252


def atr_leverage(*, close: float, atr: float | None) -> float:
    """`clip(0.005 * close / ATR, 0, 4)`。

    ``atr == 0`` 取 0 而不是上限：此时公式是除零、**没有定义**，研报也没写这种
    情形。按字面把 `+inf` 截到 4，等于在跌停封死或数据坏掉的那一天上满杠杆 ——
    所有解读里最差的一种。这是对"研报沉默处"的一个显式裁定，须进保真度报告。
    """
    if atr is None or not math.isfinite(atr) or atr < 0:
        raise ValueError(f"ATR leverage needs a finite non-negative ATR; got {atr!r}")
    if atr == 0:
        return 0.0
    return min(ATR_CAPITAL_FRACTION * close / atr, MAX_LEVERAGE)


def realized_volatility(
    returns: Sequence[float], *, min_observations: int = TRADING_DAYS_PER_YEAR
) -> float | None:
    """过去一年策略日收益的年化波动；历史不足则 ``None``。

    用**样本**标准差（`ddof=1`）。这不是可有可无的细节：同一段收益上 ddof 的
    取舍会直接搬动杠杆，本仓已经为这个量吃过一次亏，所以把它写死并由测试钉住。
    """
    if len(returns) < min_observations:
        return None
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


def final_leverage(
    *, close: float, atr: float | None, realized_vol: float | None
) -> float:
    """`clip(Lev_ATR * 0.15 / realized_vol, 0, 4)`，其中 `Lev_ATR` 自己已截到 4。

    **两道截断的顺序有实质差别**：close=4000、atr=2、已实现波动 60% 时，先截
    得 1.0，不先截得 2.5。

    ``realized_vol is None``（不满一年历史）取 0 —— 不建仓，而不是把乘数默认成 1。
    ⚠️ 这是假设不是研报原文；不过它给研报的样本起点一个可检验的解释：IF 2010-04-16
    挂牌、研报样本自 2011 年 6 月起，相隔约 14 个月，与"先攒够一年策略收益"一致。
    """
    if realized_vol is None:
        return 0.0
    if not math.isfinite(realized_vol) or realized_vol <= 0:
        raise ValueError(
            f"realized volatility must be finite and positive; got {realized_vol!r}"
        )
    scaled = atr_leverage(close=close, atr=atr) * TARGET_ANNUAL_VOL / realized_vol
    return min(scaled, MAX_LEVERAGE)


def equal_capital_weights(
    active_families: Sequence[str], *, universe: Sequence[str] = PAPER_FAMILIES
) -> dict[str, float]:
    """当日有信号的品种之间等分资金。

    只在**当日活跃**的品种之间等分，而不是恒按 |universe| 分母 —— 研报是"等资金
    配置三个品种"，某品种当日无信号时资金给到其余品种，不是空置。
    """
    outsiders = [f for f in active_families if f not in universe]
    if outsiders:
        raise ValueError(
            f"{outsiders} 不在忠实口径的品种集合 {list(universe)} 内；"
            "IM 挂牌晚于研报，要跑它必须显式换一个 universe 并标注非忠实复刻"
        )
    if not active_families:
        return {}
    weight = 1.0 / len(active_families)
    return {family: weight for family in active_families}
