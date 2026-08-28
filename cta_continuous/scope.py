"""面板与采集共用的口径 —— 「哪些品种日属于本策略」只有一个答案。

国信连续信号的宇宙（研报 §5.1：近半年日均成交额 ≥50 亿，按月重算）与 Carry 的
流动性池**不是同一个集合**。时段规则资产原本只覆盖后者，全历史面板因此在
`2011-05-17 SHFE AL` 崩掉（见
`docs/superpowers/specs/2026-08-28-continuous-session-rules-backfill-design.md`）。

补采要覆盖的品种日，必须与面板要索取的品种日**逐点相同**。保证这一点的办法不是
在两处各写一遍等价逻辑，而是两处调同一个函数 —— 就是这里（设计 D4）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from typing import Mapping

import pandas as pd

from common.db import get_connection
from common.dominant import DominantChoice
from cta_continuous.continuous import choose_dominant_commodity
from cta_continuous.universe import product_daily_turnover, universe_for_month

__all__ = ["DAILY_FROM", "PanelScope", "load_scope_daily", "next_month", "panel_scope"]

#: 日线加载起点，**不随请求区间左移**。研报基期即 2010-01-01。
DAILY_FROM = date(2010, 1, 1)


def next_month(anchor: date) -> date:
    return date(anchor.year + anchor.month // 12, anchor.month % 12 + 1, 1)


@dataclass(frozen=True)
class PanelScope:
    """逐月宇宙，以及在这些宇宙下的全历史主力选择。"""

    products_by_month: Mapping[date, tuple[str, ...]]
    choices: tuple[DominantChoice, ...]


def panel_scope(stats: pd.DataFrame, *, start: date, end: date) -> PanelScope:
    """`[start, end]` 逐月宇宙 + 主力选择。

    ⚠️ 主力选择必须喂**全历史**行情：后复权因子沿展期链累乘，从目标月往前截一段会
    让因子在月界重置，连续价就不再连续。所以 `stats` 应当是完整区间的日线，
    由调用方负责，本函数不再截取。
    """
    turnover = product_daily_turnover(
        stats.loc[:, ["symbol", "trade_date", "turnover"]]
    )
    products_by_month: dict[date, tuple[str, ...]] = {}
    month = start
    while month <= end:
        products_by_month[month] = universe_for_month(turnover, month_start=month)
        month = next_month(month)
    history_products = tuple(
        sorted(
            {product for products in products_by_month.values() for product in products}
        )
    )
    return PanelScope(
        products_by_month=products_by_month,
        choices=choose_dominant_commodity(stats, products=history_products),
    )


def load_scope_daily(pg, *, end: date) -> pd.DataFrame:
    """`[DAILY_FROM, end 的下个月)` 的分合约日线。

    ⚠️ 起点固定，不随请求区间左移。两个原因，都会静默出错：

    1. 后复权因子沿展期链累乘，从目标月往前截一段会让每次续跑把因子重置为 1，
       月界就不再是同一条连续价。
    2. 主力选择带「不可逆」规则，是**路径依赖**的。喂不同起点会让主力链分叉，
       于是补采覆盖的品种日与面板索取的品种日对不上 —— 而那正是本次补采要修的病。
    """
    sql = (
        "SELECT symbol, trade_date, oi, volume, turnover, close "
        "FROM public.futures_daily "
        f"WHERE trade_date >= DATE '{DAILY_FROM}' "
        f"AND trade_date < DATE '{next_month(end)}' "
        "AND oi IS NOT NULL AND volume IS NOT NULL"
    )
    buffer = io.StringIO()
    with get_connection(pg) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout='900s'")
        cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buffer)
    frame = pd.read_csv(io.StringIO(buffer.getvalue()))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame.loc[:, ["symbol", "trade_date", "oi", "volume", "turnover", "close"]]
