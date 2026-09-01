"""附录二的杠杆层：海龟资金管理法 + 已实现波动率反馈。

这套东西出自国信 CTA 系列研报共用的附录二 ——「1 单位 ATR 的波动对应策略整体资金
规模的 0.5%」，再用过去一年的策略已实现波动把整体拉向 15% 的年化目标，两段各自
截到 4 倍。它先在 `index_open_momentum` 里落地（研报《基于开盘动量效应的股指期货
交易策略》Task 6），第二个消费者（《基于连续信号的商品期货交易策略》）出现后搬到
这里。

模块必须保持市场中立：品种集合由调用方传入，不在这里留默认值 —— 商品那条线的
宇宙逐月变化，任何默认值在那边都是错的。
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from collections.abc import Sequence


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
    active_families: Sequence[str], *, universe: Sequence[str]
) -> dict[str, float]:
    """当日有信号的品种之间等分资金。

    只在**当日活跃**的品种之间等分，而不是恒按 |universe| 分母 —— 研报是"满足开仓
    条件品种等权分配资金"，某品种当日无信号时资金给到其余品种，不是空置。

    ``universe`` **没有默认值**，必须由调用方给出。股指那条线的品种集合是研报钉死的
    三个，商品那条线的宇宙逐月变化；在这里留任何默认值都会让其中一方悄悄跑错。
    """
    outsiders = [f for f in active_families if f not in universe]
    if outsiders:
        raise ValueError(
            f"{outsiders} 不在忠实口径的品种集合 {list(universe)} 内；"
            "要跑集合外的品种必须显式换一个 universe 并标注非忠实复刻"
        )
    if not active_families:
        return {}
    weight = 1.0 / len(active_families)
    return {family: weight for family in active_families}


def monthly_realized_volatility(
    *,
    session_date: date,
    returns_by_date: Sequence[tuple[date, float]],
    min_observations: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """研报的**按月重算**节拍：月内保持不变，只用上月末为止的过去一年收益。

    两条性质是分开的，都得有：

    - **月内不变**。研报写的是 "recomputed monthly"，不是逐日重算。逐日重算会让
      杠杆天天微动，换手与成本都不再是研报那条曲线。
    - **不用当月自己的收益**。用了就是前视：当月中旬的杠杆会知道当月前几天发生
      了什么。窗口在**当月第一天之前**截断，这一刀同时兑现了上面那条。

    ⚠️ 窗口取"最近 ``min_observations`` 个**观测**"，不是"最近 365 个自然日"。研报
    只说 "past one year"；按观测数取可以在停市、长假上保持窗口长度稳定，代价是
    真实跨度会略长于一年。这是显式假设，须进保真度报告。
    """
    month_start = session_date.replace(day=1)
    prior = [r for d, r in returns_by_date if d < month_start]
    # 历史不足由 `realized_volatility` 一处判定 —— 在这里再写一遍同样的长度守卫是
    # 死代码（切片后长度不足时它照样返回 None），而"同一条规则有两个出口"迟早会
    # 分叉。
    return realized_volatility(prior[-min_observations:], min_observations=min_observations)
