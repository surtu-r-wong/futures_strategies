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

选主力的实现与这条假设的论证已上提到 `common.dominant`（第二个消费者出现后搬的）；
本模块原地再导出，调用方的 import 路径不变。该假设仍须进本策略的保真度报告。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.dominant import (  # noqa: F401  （原地再导出，调用方 import 路径不变）
    DOMINANT_SELECTION_LAG,
    DominantChoice,
    choose_dominant,
    daily_stats_from_minutes,
    reconcile_dominant,
)
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



#: 可交易的月度合约码。`futures_contract_info` 里混着 Wind 的连续/主力合成码
#: （`IF00` / `IF001` / `IF01`~`IF12` / `IFL0`~`IFL3` / `IFL00` / `IFmain` / `IFJQ00`），
#: 它们都不是可交易合约。⚠️ 判据钉在**四位**交割码上：`IF01` 那一族最像真合约，
#: 只看"有没有数字"会把它们全放进来。
_CONCRETE_INDEX_CONTRACT = re.compile(r"^(?:IF|IC|IH|IM)\d{4}(?:\.CFE)?$")


def is_concrete_index_contract(symbol: str) -> bool:
    """这个代码是不是一张真能交易的股指月度合约。"""
    return bool(_CONCRETE_INDEX_CONTRACT.fullmatch(str(symbol).strip().upper()))




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
