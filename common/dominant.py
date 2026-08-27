"""主力合约的选取与对账 —— 与市场无关的那一半。

这套逻辑先在 `index_open_momentum` 落地（研报《基于开盘动量效应的股指期货交易策略》
Task 7），第二个消费者（《基于连续信号的商品期货交易策略》）出现后搬到这里。

⚠️ **复刻假设：选取滞后一个交易日。** 持仓量当日收盘才知道，拿**当日**的持仓量决定
**当日盘中**要交易哪张合约是回看。所以用**前一交易日**的持仓量选今日合约
（`DOMINANT_SELECTION_LAG = 1`），依据日期记在 `DominantChoice.selected_from` 上。
两篇研报都没写主力怎么定，这条必须进各自的保真度报告。

留在消费者那边、**没有**搬过来的：合约代码是否可交易的判据（`is_concrete_index_contract`
钉的是 CFFEX 合成码的形状），以及各自研报对"主力"的额外约束（商品那边还有"成交量与
持仓量均最大"和"主力不可逆"）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

import pandas as pd

__all__ = [
    "DOMINANT_SELECTION_LAG",
    "DominantChoice",
    "choose_dominant",
    "daily_stats_from_minutes",
    "product_of",
    "reconcile_dominant",
]

#: 用前几个交易日的持仓量选今日主力。见模块 docstring 的因果论证。
DOMINANT_SELECTION_LAG = 1


@dataclass(frozen=True)
class DominantChoice:
    """某个交易日、某个品种，今天该交易哪张合约，以及这个判断是怎么来的。"""

    trade_date: date
    product: str
    contract: str
    oi: int
    volume: int
    selected_from: date
    reference_contract: str | None = None
    agrees: bool | None = None



def daily_stats_from_minutes(bars: pd.DataFrame) -> pd.DataFrame:
    """把分钟行折成 `choose_dominant` 认的日频 (trade_date, symbol, oi, volume)。

    ⚠️ **持仓量是时点量，取当日最后一根 bar；成交量是流量，当日求和。** 弄反了会让
    持仓量变成一个没有意义的累加数 —— 而它恰好也是单调的，"看起来对"，所以这条
    在测试里单独钉住。
    """
    missing = {"trade_date", "symbol", "bar_time", "volume", "open_interest"} - set(
        bars.columns
    )
    if missing:
        raise ValueError(f"minute_stats_columns: 缺列 {sorted(missing)}")

    ordered = bars.sort_values(["trade_date", "symbol", "bar_time"], kind="mergesort")
    grouped = ordered.groupby(["trade_date", "symbol"], sort=True, as_index=False)
    stats = grouped.agg(volume=("volume", "sum"), oi=("open_interest", "last"))
    return stats.loc[:, ["trade_date", "symbol", "oi", "volume"]]


def product_of(symbol: str) -> str:
    """把合约代码折成品种代码：`RB2410.SHF` -> `RB`。"""
    head = symbol.split(".", 1)[0]
    return "".join(ch for ch in head if ch.isalpha()).upper()


def choose_dominant(
    daily: pd.DataFrame,
    *,
    products: Sequence[str],
    lag: int = DOMINANT_SELECTION_LAG,
) -> tuple[DominantChoice, ...]:
    """按持仓量（平手看成交量）逐日逐品种选主力，滞后 ``lag`` 个交易日。

    `daily` 需要 `trade_date` / `symbol` / `oi` / `volume` 四列，取自
    `public.futures_daily`。
    """
    if type(lag) is not int or lag < 1:
        raise ValueError("dominant_lag: lag 必须是 >= 1 的整数")
    missing = {"trade_date", "symbol", "oi", "volume"} - set(daily.columns)
    if missing:
        raise ValueError(f"dominant_columns: 缺列 {sorted(missing)}")

    frame = daily.copy()
    frame["product"] = [product_of(symbol) for symbol in frame["symbol"]]

    wanted = tuple(products)
    absent = [p for p in wanted if p not in set(frame["product"])]
    if absent:
        raise ValueError(f"dominant_missing_product: 日线里没有 {absent}")

    sessions = sorted(set(frame["trade_date"]))
    chosen: list[DominantChoice] = []
    for product in wanted:
        rows = frame.loc[frame["product"] == product]
        for index in range(lag, len(sessions)):
            trade_date = sessions[index]
            source_date = sessions[index - lag]
            pool = rows.loc[rows["trade_date"] == source_date]
            if pool.empty:
                continue
            ranked = pool.sort_values(
                ["oi", "volume"], ascending=False, kind="mergesort"
            )
            best = ranked.iloc[0]
            if len(ranked) > 1:
                runner_up = ranked.iloc[1]
                if int(best["oi"]) == int(runner_up["oi"]) and int(
                    best["volume"]
                ) == int(runner_up["volume"]):
                    raise ValueError(
                        "dominant_tie: 持仓量与成交量双双平手，没有主力可言；"
                        f"{source_date} {product} "
                        f"{best['symbol']!r} vs {runner_up['symbol']!r}"
                    )
            chosen.append(
                DominantChoice(
                    trade_date=trade_date,
                    product=product,
                    contract=str(best["symbol"]),
                    oi=int(best["oi"]),
                    volume=int(best["volume"]),
                    selected_from=source_date,
                )
            )
    return tuple(sorted(chosen, key=lambda c: (c.trade_date, c.product)))


def reconcile_dominant(
    choices: Sequence[DominantChoice],
    *,
    reference: Mapping[tuple[date, str], str],
) -> tuple[DominantChoice, ...]:
    """与 `continuous_contract_ohlc.contract_used` 对账。

    参照缺席时 `agrees is None`（**未知**），不是 `False`。连续合约只到 2026-04-29，
    把"没有参照"记成"不一致"会凭空造出一片假分歧。

    分歧**只标注、不改选**：我们自己选的那个才是能跑到分钟表末端的，静默取参照的
    一边等于让一张停更的表决定回测区间。
    """
    return tuple(
        replace(
            choice,
            reference_contract=reference.get((choice.trade_date, choice.product)),
            agrees=(
                None
                if (found := reference.get((choice.trade_date, choice.product))) is None
                else found == choice.contract
            ),
        )
        for choice in choices
    )
