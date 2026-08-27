"""主力合约选取、对账与分钟 candidate 构造 —— 计划 Task 7。

Task 7 分两半，本模块是**能离线验证的那半**：选哪个合约、取哪段时间。真去库里取
的那半复用 `common/minute/pg_source.PublicMinuteSource` —— 它的按月分批、临时表驱动
裸列嵌套循环、事务级 `SET LOCAL`、以及"引用超过 3 个 Timescale chunk 就报错"的
EXPLAIN 闸都已经在那边落地并各有测试家族，这里不重造。

## 主力合约从哪儿推：两条通路，因为日线表有个洞

2026-08-27 **逐月**实测（⚠️ `max(trade_date)` 会骗人：IF 的最大交易日是 2026-08-26，
但那是孤零零一天 4 行，2026-05/06/07 一行都没有）：

| 表 | 可用区间 | 角色 |
|---|---|---|
| `futures_daily` | 2010-04-16 ~ **2026-04-29** 连续 | 主力来源（日频小表，不必扫分钟表） |
| `futures_contract_info` | **2025-12-22** 起连续 | 尾部的**合约名单**（混着 Wind 合成码，要滤） |
| `futures_minute` | 2010-04-16 ~ **2026-08-11** | 尾部的持仓量与成交量 |
| `continuous_contract_ohlc` | ~ 2026-04-29 | **只**当对账参照，不当数据源 |

⇒ 两条通路产出**同一个形状**（`trade_date` / `symbol` / `oi` / `volume`），
所以选主力的逻辑只有一份 `choose_dominant`。2025-12-22 ~ 2026-04-29 是重叠区，
可以拿来互校。

对账那条：`continuous_contract_ohlc.contract_used` 只在 ≤2026-04-29 有话可说；
其后 `agrees is None`（**未知**），不是 `False`（不一致）。

## ⚠️ 复刻假设：选取滞后一个交易日

持仓量当日收盘才知道。拿**当日**的持仓量去决定**当日 10:15** 要交易哪张合约是回看。
所以本实现用**前一交易日**的持仓量选今日合约（`DOMINANT_SELECTION_LAG = 1`），
并把依据日期记在 `causal_in_pool_date` 上。研报没有写它怎么定主力，这条必须进保真度报告。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from common.minute.pg_source import MinuteCandidate, minute_contract_identity
from common.minute.sessions import SessionRule, resolve_session_rule

__all__ = [
    "CFFEX_MULTIPLIERS",
    "DOMINANT_SELECTION_LAG",
    "metadata_multipliers_for",
    "daily_stats_from_minutes",
    "is_concrete_index_contract",
    "INDEX_EXCHANGE",
    "INDEX_PRODUCTS",
    "DominantChoice",
    "build_index_candidates",
    "choose_dominant",
    "reconcile_dominant",
]

SHANGHAI = ZoneInfo("Asia/Shanghai")

INDEX_EXCHANGE = "CFFEX"

#: 研报的品种池是 IF / IC / IH（IM 在研报之后才上市）。这里是**本仓支持**的全集，
#: 具体跑哪几个由调用方给。
INDEX_PRODUCTS = ("IF", "IC", "IH", "IM")

#: 用前几个交易日的持仓量选今日主力。见模块 docstring 的因果论证。
DOMINANT_SELECTION_LAG = 1

_SELECTION_SOURCE = "daily_open_interest"

#: 合约乘数，交易所事实（2026-08-27 与 `public.futures_contract_info` 核对一致）。
#:
#: ⚠️ 用途是**元数据兜底**，不是绕过校验：换月后新主力在我们取的数据里只有一两天，
#: 反推所需的"跨 ≥3 个交易日"够不着（实测 2016-03-18 的 `IF1604` 就是），没有兜底
#: 会硬失败。兜底给出的 `source="metadata_unvalidated"` 会进保真度报告，且样本一够
#: 就自动升级为价域校验过的。
CFFEX_MULTIPLIERS: Mapping[str, int] = {"IF": 300, "IH": 300, "IC": 200, "IM": 200}


def metadata_multipliers_for(
    candidates: Sequence[MinuteCandidate],
    *,
    by_product: Mapping[str, int] = CFFEX_MULTIPLIERS,
) -> dict[str, int]:
    """把品种级乘数摊到候选用的 minute symbol 上。"""
    return {
        candidate.minute_symbol: by_product[candidate.product]
        for candidate in candidates
        if candidate.product in by_product
    }


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


#: 可交易的月度合约码。`futures_contract_info` 里混着 Wind 的连续/主力合成码
#: （`IF00` / `IF001` / `IF01`~`IF12` / `IFL0`~`IFL3` / `IFL00` / `IFmain` / `IFJQ00`），
#: 它们都不是可交易合约。⚠️ 判据钉在**四位**交割码上：`IF01` 那一族最像真合约，
#: 只看"有没有数字"会把它们全放进来。
_CONCRETE_INDEX_CONTRACT = re.compile(r"^(?:IF|IC|IH|IM)\d{4}(?:\.CFE)?$")


def is_concrete_index_contract(symbol: str) -> bool:
    """这个代码是不是一张真能交易的股指月度合约。"""
    return bool(_CONCRETE_INDEX_CONTRACT.fullmatch(str(symbol).strip().upper()))


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


def _product_of(symbol: str) -> str:
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
    frame["product"] = [_product_of(symbol) for symbol in frame["symbol"]]

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


def _window(rule: SessionRule, trade_date: date) -> tuple[datetime, datetime]:
    """当日日盘的物理时间窗，按**官方**时段取。

    不迁就本库 2016 前缺的那 15 分钟：窗口是"该取哪一段"，缺什么由
    `index_open_momentum.sessions.CFFEX_ARCHIVE_GAPS` 单独记账。将来补上数据，
    这里不必改。
    """
    starts = min(segment.start_minute for segment in rule.segments)
    ends = max(segment.end_minute for segment in rule.segments)
    midnight = datetime(
        trade_date.year, trade_date.month, trade_date.day, tzinfo=SHANGHAI
    )
    return midnight + timedelta(minutes=starts), midnight + timedelta(minutes=ends)


def build_index_candidates(
    choices: Sequence[DominantChoice],
    *,
    rules: Sequence[SessionRule],
    exchange: str = INDEX_EXCHANGE,
) -> tuple[MinuteCandidate, ...]:
    """把主力合约选择折成共享分钟层认的 `MinuteCandidate`。

    股指无夜盘，所以一天一个窗口 —— 不像商品要拼夜盘与三段日盘。
    """
    candidates: list[MinuteCandidate] = []
    for choice in choices:
        rule = resolve_session_rule(rules, exchange, choice.product, choice.trade_date)
        window_start, window_end = _window(rule, choice.trade_date)
        product, minute_symbol, minute_exchange = minute_contract_identity(
            choice.contract, choice.trade_date
        )
        candidates.append(
            MinuteCandidate(
                trade_date=choice.trade_date,
                product=product,
                daily_contract=choice.contract,
                minute_symbol=minute_symbol,
                exchange=minute_exchange,
                window_start=window_start,
                window_end=window_end,
                candidate_role="dominant",
                causal_in_pool_date=choice.selected_from,
                selection_source=_SELECTION_SOURCE,
            )
        )
    return tuple(candidates)
