"""回测编排 —— 计划 Task 7 第 5 步的前置，也是 Task 8 报告层的输入。

把已有的零件串起来：时段 → 分钟 bar → 开盘信号 → ATR 与止损 → 持仓路径 → 杠杆 →
等资金组合。每一件都已在自己的测试家族里验过，本模块只负责**接缝**，尤其是三处
一接错就静默出错的地方：

## 一、ATR 必须在跨日连续序列上算

研报的 ATR 窗口是 **16 根 15 分钟 bar**，而股指晚年代一天正好 **16 根**
（240 分钟 / 15）。按 session 重置的话，建仓那根（当日第 3 根）**永远**拿不到 ATR
—— 而 `risk._require_atr` 在无 ATR 时硬失败，所以症状会是"整段回测一笔都开不出来"，
不是一个好查的错。因此本模块**按品种累积一条跨日的 bar 序列**，ATR 在整条上算一次，
再按日切片。隔夜跳空因此计入 TR，与复刻假设②一致。

## 二、成交窗是"这根 bar 之后的 5 个交易时钟分钟槽"

不是"之后 5 分钟"。第 8 根 bar 收在 11:30，它的成交窗是 **13:00–13:04** ——
用交易时钟槽自动就对，用挂钟时间会取到午休里根本不存在的分钟。

## 四、波动率反馈必须算在**未缩放**的影子收益上

若算在已加杠杆的实盘收益上，整段回测恒为零：第一年没有波动率历史 ⇒ 杠杆 0 ⇒
收益全 0 ⇒ 波动率 0 ⇒ 杠杆仍是 0，**永远启动不了**。而症状是"回测跑完了，
只是都不赚钱"，不是报错。

所以每天都记一条**影子收益** = 当日策略收益 × ATR 杠杆（不乘波动率倍数），
无论实盘那天有没有真建仓；波动率反馈只读影子序列。这正是
`common/minute/account.py` 那句"正式账户与未缩放影子账户并行"的形状。

## 三、按月分批的意义是让时间边界成为计划期常量

chunk 排除只在边界是常量时发生（实测：从 join 列推导边界 ⇒ 264 个 chunk 全进计划，
planning 209 ms；字面量 ⇒ 1 个 chunk，4.9 ms）。所以窗口按自然月切，不按候选集的
首尾切。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from common.minute.bars import MinuteDataError, MultiplierResolution
from common.minute.pg_source import MinuteCandidate
from common.minute.sessions import (
    SessionRule,
    build_trading_slots,
    fifteen_minute_buckets,
    resolve_session_rule,
)
from index_open_momentum.backtest import FillKind, SessionResult, simulate_session
from index_open_momentum.bars import (
    MAX_RELATIVE_EXCURSION,
    IndexBar,
    build_index_bars,
    index_execution_fill,
    relative_excursion,
    resolve_index_multiplier,
)
from index_open_momentum.leverage import (
    TRADING_DAYS_PER_YEAR,
    atr_leverage,
    equal_capital_weights,
    final_leverage,
    monthly_realized_volatility,
)
from index_open_momentum.risk import ATR_WINDOW, Direction, atr_series
from index_open_momentum.sessions import expected_absent_minutes, known_gaps_on
from index_open_momentum.signals import OPENING_BAR_COUNT

__all__ = [
    "FILL_WINDOW_SLOTS",
    "ProductDay",
    "RunResult",
    "fill_window",
    "known_gap_disclosure",
    "run_backtest",
]

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 成交窗长度：研报的"信号后 5 分钟 VWAP"。
FILL_WINDOW_SLOTS = 5

#: 遇到"必需的成交窗零成交"时的两种处置。
#:
#: 计划写的是**运行硬失败**，所以默认 `abort`。`skip` 只是当日不建仓并如实计数 ——
#: 它**不顺延到更晚的窗口、不替换价格**，因此没有偷换研报的成交时刻；提供它是为了
#: 先把规模量清楚（全历史真跑会在第一个这样的日子上中断，量不出总数）。
_UNPRICEABLE_MODES = frozenset({"abort", "skip"})
_UNPRICEABLE_CHECKS = frozenset({"execution_vwap", "execution_window"})


@dataclass(frozen=True)
class ProductDay:
    """一个品种、一个交易日的结果与出处。"""

    trade_date: date
    product: str
    contract: str
    direction: Direction | None
    gross_return: float
    cost: float
    net_return: float
    leverage: float
    realized_vol: float | None
    atr_at_entry: float | None
    entry_price: float | None
    scale_downs: int
    carried_overnight: float
    bars: int
    no_trade_bars: int
    missing_slots: int
    max_relative_excursion: float
    known_gap_minutes: int
    #: 当日必需的 5 分钟成交窗零成交 ⇒ 研报的成交规则无从适用。
    unpriceable: bool = False
    #: 未按波动率缩放的影子收益（只加 ATR 杠杆）。波动率反馈算在它上面 ——
    #: 算在实盘收益上会自我指涉，见模块 docstring 第四条。
    shadow_return: float = 0.0


@dataclass(frozen=True)
class RunResult:
    product_days: tuple[ProductDay, ...]
    daily: pd.DataFrame
    plan_summaries: tuple
    multipliers: dict[str, MultiplierResolution] = field(default_factory=dict)

    @property
    def unpriceable_sessions(self) -> int:
        return sum(1 for day in self.product_days if day.unpriceable)

    @property
    def portfolio_returns(self) -> pd.Series:
        if self.daily.empty:
            return pd.Series(dtype=float)
        return self.daily["portfolio"]


def fill_window(slots: Sequence[datetime], bar_index: int) -> tuple[datetime, ...]:
    """第 ``bar_index`` 根 bar 之后的 5 个**交易时钟**分钟槽。

    跨午休时自动落到下午段的头 5 分钟 —— 这正是用槽而不是用挂钟时间的理由。
    当日最后一根之后没有窗口，返回空。
    """
    start = (bar_index + 1) * 15
    window = tuple(slots[start : start + FILL_WINDOW_SLOTS])
    return window if len(window) == FILL_WINDOW_SLOTS else ()


def _months(start: date, end: date) -> Iterator[tuple[datetime, datetime]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        lower = datetime(year, month, 1, tzinfo=SHANGHAI)
        year_next, month_next = (year + 1, 1) if month == 12 else (year, month + 1)
        upper = datetime(year_next, month_next, 1, tzinfo=SHANGHAI)
        yield lower, upper
        year, month = year_next, month_next


def run_backtest(
    *,
    candidates: Sequence[MinuteCandidate],
    rules: Sequence[SessionRule],
    source,
    exchange: str = "CFFEX",
    max_relative_excursion: float = MAX_RELATIVE_EXCURSION,
    metadata_multipliers: Mapping[str, int] | None = None,
    realized_vol_min_observations: int = TRADING_DAYS_PER_YEAR,
    on_unpriceable: str = "abort",
) -> RunResult:
    """跑完给定候选覆盖的全部交易日。

    ``source`` 只需提供 ``iter_month(candidates, lower, upper)`` 与 ``plan_audit``；
    生产上是 `common.minute.pg_source.PublicMinuteSource`，测试里是确定性替身。
    """
    if on_unpriceable not in _UNPRICEABLE_MODES:
        raise ValueError(
            f"on_unpriceable: 必须是 {sorted(_UNPRICEABLE_MODES)} 之一；"
            f"得到 {on_unpriceable!r}"
        )
    if not candidates:
        return RunResult((), pd.DataFrame(), ())

    by_month: dict[tuple[int, int], list[MinuteCandidate]] = {}
    for candidate in candidates:
        key = (candidate.trade_date.year, candidate.trade_date.month)
        by_month.setdefault(key, []).append(candidate)

    start = min(c.trade_date for c in candidates)
    end = max(c.trade_date for c in candidates)

    # 按品种累积的跨日 bar 序列与日收益 —— ATR 与已实现波动率都要它。
    history: dict[str, list[IndexBar]] = {}
    returns_by_product: dict[str, list[tuple[date, float]]] = {}
    multipliers: dict[str, MultiplierResolution] = {}
    # 逐合约累积的取样行：`_select_multiplier_sample` 要求跨 ≥3 个交易日，
    # 而换月后的新主力在当月可能只有一两天。
    samples: dict[str, list[pd.DataFrame]] = {}
    outcomes: list[ProductDay] = []

    for lower, upper in _months(start, end):
        month_candidates = by_month.get((lower.year, lower.month))
        if not month_candidates:
            continue
        frames = list(source.iter_month(month_candidates, lower, upper))
        if not frames:
            continue
        month = pd.concat(frames, ignore_index=True)

        for candidate in sorted(
            month_candidates, key=lambda c: (c.trade_date, c.product)
        ):
            outcome = _run_one_day(
                month=month,
                candidate=candidate,
                rules=rules,
                exchange=exchange,
                history=history,
                returns_by_product=returns_by_product,
                multipliers=multipliers,
                samples=samples,
                metadata_multipliers=metadata_multipliers or {},
                max_relative_excursion=max_relative_excursion,
                min_observations=realized_vol_min_observations,
                on_unpriceable=on_unpriceable,
            )
            if outcome is not None:
                outcomes.append(outcome)

    return RunResult(
        product_days=tuple(outcomes),
        daily=_daily_frame(outcomes),
        plan_summaries=tuple(getattr(source, "plan_audit", ())),
        multipliers=multipliers,
    )


def _run_one_day(
    *,
    month: pd.DataFrame,
    candidate: MinuteCandidate,
    rules: Sequence[SessionRule],
    exchange: str,
    history: dict[str, list[IndexBar]],
    returns_by_product: dict[str, list[tuple[date, float]]],
    multipliers: dict[str, MultiplierResolution],
    samples: dict[str, list[pd.DataFrame]],
    metadata_multipliers: Mapping[str, int],
    max_relative_excursion: float,
    min_observations: int,
    on_unpriceable: str = "abort",
) -> ProductDay | None:
    product = candidate.product
    trade_date = candidate.trade_date
    rule = resolve_session_rule(rules, exchange, product, trade_date)
    slots = build_trading_slots(trade_date, trade_date - timedelta(days=1), rule)
    buckets = fifteen_minute_buckets(slots, rule)

    day = month.loc[
        (month["trade_date"] == trade_date)
        & (month["symbol"] == candidate.minute_symbol)
    ]
    if day.empty:
        return None

    samples.setdefault(candidate.minute_symbol, []).append(day)

    day_bars = build_index_bars(day, buckets=buckets, contract=candidate.minute_symbol)

    # ⚠️ ATR 在**跨日连续**序列上算，见模块 docstring 第一条。
    previous = history.setdefault(product, [])
    offset = len(previous)
    previous.extend(day_bars)
    # 只留够 ATR 窗口用的尾巴，否则内存随交易日线性增长。
    keep = ATR_WINDOW + len(day_bars) + 1
    if len(previous) > keep:
        drop = len(previous) - keep
        del previous[:drop]
        offset -= drop
    atr_all = atr_series([b.bar for b in previous], window=ATR_WINDOW)
    atr_at = atr_all[offset:]

    excursions: list[float] = []

    def multiplier_for() -> int:
        """乘数：样本够就校验，不够就用元数据兜底并**在 source 上写明**。

        ⚠️ `_select_multiplier_sample` 要求样本跨 **≥3 个交易日** —— 单日的价域约束
        撑不出唯一解。而**每次换月，新主力合约在我们取的数据里 0 天历史**（只取
        主力窗口），头两天都推不出来。15 年 × 3 品种约 540 个交易日会因此发不出
        成交，所以这条不是边角情形，必须有兜底。

        兜底**不是静默采信**：`source="metadata_unvalidated"` 会一路带进保真度报告，
        而且样本一够就自动改走校验路径；样本够了却与元数据打架，照样硬失败。
        """
        cached = multipliers.get(candidate.minute_symbol)
        if cached is not None and cached.source != "metadata_unvalidated":
            return cached.multiplier
        # 兜底过的要**继续重试**，样本一够就升级成校验过的 —— 否则第一天薄样本
        # 定下的 `metadata_unvalidated` 会跟着整段回测，而它自己的 docstring
        # 明明承诺会升级。

        sample = pd.concat(samples[candidate.minute_symbol], ignore_index=True)
        metadata = metadata_multipliers.get(candidate.minute_symbol)
        try:
            resolution = resolve_index_multiplier(
                sample,
                contract=candidate.minute_symbol,
                metadata_multiplier=metadata,
            )
        except MinuteDataError as exc:
            if exc.check != "contract_multiplier_sample" or metadata is None:
                raise
            resolution = MultiplierResolution(
                multiplier=metadata,
                source="metadata_unvalidated",
                sample_rows=len(sample),
                pass_rate=float("nan"),
                sample_dates=int(sample["trade_date"].nunique()),
            )
        multipliers[candidate.minute_symbol] = resolution
        return resolution.multiplier

    def fill_price(bar_index: int) -> float:
        window = fill_window(slots, bar_index)
        if not window:
            raise MinuteDataError(
                trade_date=trade_date,
                product=product,
                contract=candidate.minute_symbol,
                check="execution_window",
                reason=(
                    "no five-slot execution window follows this bar; the paper "
                    "prices every fill on the five minutes after the signal"
                ),
                context={"bar_index": bar_index},
            )
        fill = index_execution_fill(
            day.loc[day["bar_time"].isin(pd.Index(window))],
            slots=window,
            contract=candidate.minute_symbol,
            multiplier=multiplier_for(),
            max_relative_excursion=max_relative_excursion,
        )
        excursions.append(relative_excursion(fill))
        return fill.price

    entry_index = OPENING_BAR_COUNT - 1
    atr_at_entry = atr_at[entry_index] if entry_index < len(atr_at) else None
    realized_pre = monthly_realized_volatility(
        session_date=trade_date,
        returns_by_date=returns_by_product.setdefault(product, []),
        min_observations=min_observations,
    )
    # 没有 ATR、或已实现波动率为 0 ⇒ 杠杆无定义 ⇒ 当日不建仓。后者不是理论情形：
    # 某品种一整月没有信号时它那段日收益全是 0，标准差就是 0，而 `final_leverage`
    # 对零波动硬失败（正确）—— 本层必须接住，不能让它炸穿整段回测。
    if atr_at_entry is None or (realized_pre is not None and realized_pre <= 0.0):
        # ⚠️ 没有 ATR 就**不建仓**，而且这个判断属于**本层**：`simulate_session`
        # 不管杠杆，而"没 ATR 算不出杠杆"正是 `leverage.atr_leverage` docstring
        # 写明的口径。把闸放进 `simulate_session` 会连带改掉 Task 5 已验证过的
        # 契约（它允许调用方自己保证 ATR 到位）。
        # ATR 窗口 16 根 ≈ 股指一个交易日 ⇒ 每个品种序列的头一天必落此分支；
        # 不拦的话会照常建仓，再在第一次判止损时被 `risk._require_atr` 炸掉。
        # ⚠️ 不建仓的日子影子收益是 0，但**必须记进序列**：少记会让波动率窗口
        # 整体错位，而且错得看不出来 —— 波动率仍会是个合理数字。
        returns_by_product[product].append((trade_date, 0.0))
        return _flat_day(
            candidate=candidate,
            day_bars=day_bars,
            atr_at_entry=atr_at_entry,
            realized=realized_pre,
        )

    last_traded = _last_traded(day_bars)
    try:
        result: SessionResult = simulate_session(
            bars=[b.bar for b in day_bars],
            atr_at=atr_at,
            fill_price=fill_price,
            next_session_open=(
                None if last_traded is None else day_bars[last_traded].bar.close
            ),
        )
    except MinuteDataError as exc:
        # 只接住"这个窗口没法计价"这一类，其余照旧炸出去。
        if on_unpriceable != "skip" or exc.check not in _UNPRICEABLE_CHECKS:
            raise
        returns_by_product[product].append((trade_date, 0.0))
        return _flat_day(
            candidate=candidate,
            day_bars=day_bars,
            atr_at_entry=atr_at_entry,
            realized=realized_pre,
            unpriceable=True,
        )

    entry_bar = day_bars[entry_index].bar if entry_index < len(day_bars) else None
    realized = realized_pre
    # 影子：只加 ATR 杠杆，**不乘**波动率倍数。波动率反馈只读这一条。
    shadow_leverage = (
        0.0
        if result.direction is None or entry_bar is None
        else atr_leverage(close=entry_bar.close, atr=atr_at_entry)
    )
    shadow_net = result.net_return * shadow_leverage
    leverage = (
        0.0
        if result.direction is None or entry_bar is None
        else final_leverage(
            close=entry_bar.close, atr=atr_at_entry, realized_vol=realized
        )
    )
    levered_net = result.net_return * leverage
    returns_by_product[product].append((trade_date, shadow_net))

    entry_fills = [f for f in result.fills if f.kind is FillKind.ENTRY]
    return ProductDay(
        trade_date=trade_date,
        product=product,
        contract=candidate.daily_contract,
        direction=result.direction,
        gross_return=result.gross_return * leverage,
        cost=result.cost * leverage,
        net_return=levered_net,
        leverage=leverage,
        realized_vol=realized,
        atr_at_entry=atr_at_entry,
        entry_price=entry_fills[0].price if entry_fills else None,
        scale_downs=sum(1 for f in result.fills if f.kind is FillKind.SCALE_DOWN),
        carried_overnight=result.carried_overnight,
        bars=len(day_bars),
        no_trade_bars=sum(1 for b in day_bars if b.no_trade),
        missing_slots=sum(b.missing_slots for b in day_bars),
        max_relative_excursion=max(excursions, default=0.0),
        known_gap_minutes=len(expected_absent_minutes(trade_date)),
        shadow_return=shadow_net,
    )


def _flat_day(
    *,
    candidate: MinuteCandidate,
    day_bars: Sequence[IndexBar],
    atr_at_entry: float | None,
    realized: float | None,
    unpriceable: bool = False,
) -> ProductDay:
    """当日不建仓的结果行。仍然如实记 bar 数、缺口与缺槽，报告要看得见。"""
    return ProductDay(
        trade_date=candidate.trade_date,
        product=candidate.product,
        contract=candidate.daily_contract,
        direction=None,
        gross_return=0.0,
        cost=0.0,
        net_return=0.0,
        leverage=0.0,
        realized_vol=realized,
        atr_at_entry=atr_at_entry,
        entry_price=None,
        scale_downs=0,
        carried_overnight=0.0,
        bars=len(day_bars),
        no_trade_bars=sum(1 for b in day_bars if b.no_trade),
        missing_slots=sum(b.missing_slots for b in day_bars),
        max_relative_excursion=0.0,
        known_gap_minutes=len(expected_absent_minutes(candidate.trade_date)),
        unpriceable=unpriceable,
    )


def _last_traded(day_bars: Sequence[IndexBar]) -> int | None:
    for index in range(len(day_bars) - 1, -1, -1):
        if not day_bars[index].no_trade:
            return index
    return None


def _daily_frame(outcomes: Sequence[ProductDay]) -> pd.DataFrame:
    """逐日逐品种收益 + 等资金组合列。

    ⚠️ 组合分母是**当日有信号的品种数**，不是恒定 3 —— 见 `equal_capital_weights`
    的复刻假设⑥。
    """
    if not outcomes:
        return pd.DataFrame()
    rows: dict[date, dict[str, float]] = {}
    active_on: dict[date, list[str]] = {}
    for outcome in outcomes:
        rows.setdefault(outcome.trade_date, {})[outcome.product] = outcome.net_return
        if outcome.direction is not None:
            active_on.setdefault(outcome.trade_date, []).append(outcome.product)

    records = []
    for trade_date in sorted(rows):
        per_product = rows[trade_date]
        active = active_on.get(trade_date, [])
        weights = equal_capital_weights(active) if active else {}
        portfolio = sum(
            per_product.get(product, 0.0) * weight
            for product, weight in weights.items()
        )
        records.append(
            {"trade_date": trade_date, **per_product, "portfolio": portfolio}
        )
    return pd.DataFrame(records).set_index("trade_date").fillna(0.0)


def known_gap_disclosure(start: date, end: date) -> tuple[str, ...]:
    """区间内生效的档案缺口理由，逐条交给保真度报告。"""
    seen: dict[str, str] = {}
    day = start
    while day <= end:
        for gap in known_gaps_on(day):
            seen[gap.key] = gap.reason
        day += timedelta(days=1)
    return tuple(seen.values())
