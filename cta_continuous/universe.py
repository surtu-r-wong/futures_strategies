"""成交额宇宙筛选 —— 计划 Task 1。

研报 §5.1「投资标的」：**过去半年中日均成交额超过 50 亿元的商品期货全品种**的主力合约。

研报只说了「过去半年」，没说多久重算一次、日均的分母怎么取。两处裁决（计划 D10 / D15）：

- **每月末重算**，窗口是 `month_start` 之前的 6 个自然月。逐日重算会让品种在 50 亿门槛上
  反复抖动、制造纯噪音换手；研报另一个动件（`Mul_vol`）明写按月。
- **日均的分母是窗口内全市场观测到的交易日数**，品种缺席那天按 0 计。按品种自己的
  观测天数取平均，会让一个刚挂牌、只交易三天但每天 200 亿的品种被算成"日均 200 亿"。

## ⚠️ 去重键不是 symbol

上游 `futures_daily` 有已知缺陷：2015–2017 期间 10 个郑商所品种的**同一张合约**被
3 位与 4 位两种交割码各存了一份、逐字段完全相同（2,565 对）。按 `(symbol, trade_date)`
去重**抓不到**它们 —— 两份的 symbol 本来就不同。必须先把交割码归一到四位。
不去重的话该品种成交额直接翻倍（实测平均高估 18.3%、最高 50%）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from common.minute.bars import MinuteDataError
from common.minute.pg_source import minute_contract_identity

__all__ = [
    "FINANCIAL_FUTURES",
    "LOOKBACK_MONTHS",
    "TURNOVER_THRESHOLD",
    "canonical_contract",
    "product_daily_turnover",
    "universe_for_month",
]

#: 股指与国债期货不是商品。与 `cta_gtja.pg_source.FINANCIAL_FUTURES` 同集合；
#: 两处分叉由 `tests/test_continuous_universe.py` 的漂移闸钉住。
FINANCIAL_FUTURES = frozenset({"IF", "IC", "IH", "IM", "T", "TF", "TL", "TS"})

#: 研报门槛：日均成交额 50 亿元。
TURNOVER_THRESHOLD = 5e9

#: 研报窗口：过去半年。
LOOKBACK_MONTHS = 6


def canonical_contract(symbol: object, trade_date: date) -> str | None:
    """把日线合约代码折成 `交易所:品种:四位交割码`；不是可交易月度合约则 ``None``。

    郑商所的三位交割码要靠 `trade_date` 才能定出年代，所以这个函数**必须**知道日期。
    `futures_daily` 里混着 Wind 的连续/主力合成码（`TA00.CZC` / `RBL0.SHF` 之类），
    它们没有交割月，返回 ``None`` 让调用方跳过。
    """
    if type(symbol) is not str:
        return None
    suffix = symbol.strip().rsplit(".", 1)[-1].upper()
    try:
        product, minute_symbol, _exchange = minute_contract_identity(
            symbol.strip(), trade_date
        )
    except (MinuteDataError, ValueError):
        return None
    return f"{suffix}:{product}:{minute_symbol[len(product):]}"


def product_daily_turnover(daily: pd.DataFrame) -> pd.DataFrame:
    """`(symbol, trade_date, turnover)` → `(product, trade_date, turnover)`。

    先按归一合约去重，再按品种汇总。两份孪生记录的成交额若**不相等**则报错而不是
    静默取一份 —— 那说明上游缺陷换了形态，是新情况，不该被这里吞掉。
    """
    missing = {"symbol", "trade_date", "turnover"} - set(daily.columns)
    if missing:
        raise ValueError(f"universe_columns: 缺列 {sorted(missing)}")

    frame = daily.loc[:, ["symbol", "trade_date", "turnover"]].copy()
    frame["canonical"] = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(frame["symbol"], frame["trade_date"])
    ]
    frame = frame.loc[frame["canonical"].notna()]
    frame = frame.loc[frame["turnover"].notna()]
    frame["turnover"] = frame["turnover"].astype(float)

    spread = frame.groupby(["canonical", "trade_date"], sort=False)["turnover"].nunique()
    disagreeing = spread.loc[spread > 1]
    if len(disagreeing):
        first = disagreeing.index[0]
        raise ValueError(
            "universe_duplicate_disagreement: 同一张合约的孪生记录成交额不相等；"
            f"{first[1]} {first[0]}。上游缺陷换了形态，需要先判定再决定取哪一份"
        )

    deduped = frame.drop_duplicates(subset=["canonical", "trade_date"], keep="first")
    deduped = deduped.assign(
        product=[value.split(":")[1] for value in deduped["canonical"]]
    )
    grouped = deduped.groupby(["product", "trade_date"], as_index=False)["turnover"].sum()
    return grouped.sort_values(["product", "trade_date"], kind="mergesort").reset_index(
        drop=True
    )


def _shift_months(anchor: date, months: int) -> date:
    total = (anchor.year * 12 + anchor.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def universe_for_month(
    turnover: pd.DataFrame,
    *,
    month_start: date,
    threshold: float = TURNOVER_THRESHOLD,
    lookback_months: int = LOOKBACK_MONTHS,
) -> tuple[str, ...]:
    """`month_start` 当月可交易的商品品种，按品种代码排序。

    窗口 = `[month_start - lookback_months 个月, month_start)`，**开区间不含当月**。
    """
    if type(month_start) is not date or month_start.day != 1:
        raise ValueError(
            f"universe_month: month_start 必须是某个自然月的 1 号；got {month_start!r}"
        )
    missing = {"product", "trade_date", "turnover"} - set(turnover.columns)
    if missing:
        raise ValueError(f"universe_columns: 缺列 {sorted(missing)}")

    window_start = _shift_months(month_start, lookback_months)
    inside = turnover.loc[
        (turnover["trade_date"] >= window_start) & (turnover["trade_date"] < month_start)
    ]
    market_days = inside["trade_date"].nunique()
    if market_days == 0:
        return ()

    totals = inside.groupby("product")["turnover"].sum() / market_days
    picked = [
        product
        for product, average in totals.items()
        if product not in FINANCIAL_FUTURES and average >= threshold
    ]
    return tuple(sorted(picked))
