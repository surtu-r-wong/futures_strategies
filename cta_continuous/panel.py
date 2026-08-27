"""15 分钟面板与成交价缓存 —— 计划 Task 3。

面板每行 = 一个 `(product, slot_end)`：15 分钟 K 线，外加**下** 5 分钟的 VWAP 成交价。
把成交价一起缓存下来，是为了让后面 12 次网格回测不再过网 —— 本机到库是 DERP 中继，
抖动 0.4–1.7 s 会半开断连，重复拉 7,600 万行分钟数据是这条线最容易失败的地方。

## 成交价为什么必须是「下 5 分钟」

研报 §5.1：成交价 = 信号触发后 5 分钟 VWAP。信号在某根 15 分钟 bar 收盘时产生，
所以成交窗是**那根 bar 之后**的 5 个分钟槽。用当根 bar 自己的价格就是前视。

「之后 5 个分钟槽」按**槽序**取，不按墙钟：上午第一段 10:15 收盘后的 5 个槽是
10:30–10:34，中间那 15 分钟休市本来就不存在成交。只有整个交易时段的最后一根桶
没有后续槽 —— 那一根挂起（`fill_pending`），由下一时段的前 5 分钟补上（计划 D14）。

## 郑商所

`amount` 是按单一整数价合成的，算不出成交价，必须走 `ohlc_typical` 计价基准
（`config/carry_minute_pricing_basis.csv`）。基准会随每一笔成交一起记进面板 ——
按 OHLC 定的价**不是** VWAP，不能被下游当成 VWAP 读。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta

import pandas as pd

from common.minute.bars import (
    MinuteDataError,
    aggregate_fifteen_minute_bar,
    five_minute_vwap,
)

__all__ = [
    "FILL_MINUTES",
    "PANEL_COLUMNS",
    "build_session_bars",
    "context_choices_for_month",
    "resolve_pending_fill",
    "slot_frame",
]

#: 研报 §5.1：信号触发后 5 分钟 VWAP。
FILL_MINUTES = 5

PANEL_COLUMNS = (
    "product",
    "contract",
    "trade_date",
    "slot_end",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "no_trade",
    "adj_factor",
    "continuity_segment",
    "fill_price",
    "fill_pending",
    "fill_unpriceable",
    "pricing_basis",
    "multiplier",
)


def slot_frame(frame: pd.DataFrame, slots: Sequence[datetime]) -> pd.DataFrame:
    """只留下落在 `slots` 里的分钟行。

    共享分钟层要求传进去的 frame **只**含所给槽位的行 —— 多一行就报
    `rows_outside_slots`。这条约束是有意的：它让「这根 bar 是由哪些分钟聚出来的」
    无从含糊。所以每个桶、每个成交窗都得自己切一次。
    """
    return frame.loc[frame["bar_time"].isin(tuple(slots))].copy().reset_index(drop=True)


def build_session_bars(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    buckets: Sequence[Sequence[datetime]],
    contract: str,
    multiplier: int,
    pricing_basis: str = "amount_vwap",
    product: str | None = None,
    trade_date: date | None = None,
    adj_factor: float = 1.0,
    continuity_segment: int = 0,
) -> list[dict[str, object]]:
    """把一个品种-日的分钟行折成 15 分钟 bar，并给每根配好它的成交价。

    返回的是 `dict` 列表而不是 DataFrame：调用方要把很多天拼在一起，逐日建 frame
    再 concat 比一次性建一张贵得多。
    """
    if len(buckets) * 15 != len(slots):
        raise ValueError(
            f"panel_bucket_cover: {len(buckets)} 个桶盖不住 {len(slots)} 个分钟槽"
        )

    rows: list[dict[str, object]] = []
    for index, bucket in enumerate(buckets):
        bar = aggregate_fifteen_minute_bar(
            slot_frame(frame, bucket), slots=bucket, contract=contract
        )
        window = slots[(index + 1) * 15 : (index + 1) * 15 + FILL_MINUTES]
        pending = len(window) < FILL_MINUTES
        price: float | None = None
        unpriceable = False
        if not pending:
            try:
                price = five_minute_vwap(
                    slot_frame(frame, window),
                    slots=window,
                    contract=contract,
                    multiplier=multiplier,
                    pricing_basis=pricing_basis,
                ).price
            except MinuteDataError:
                # 成交窗零成交（或价格与区间对不上）。研报没写这种情形；这里记下
                # 来交给回测层裁定，而不是就地换一个价 —— 换价等于凭空造成交。
                unpriceable = True
        rows.append(
            {
                "product": product,
                "contract": contract,
                "trade_date": trade_date,
                "slot_end": bar.end,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "no_trade": bar.no_trade,
                "adj_factor": adj_factor,
                "continuity_segment": continuity_segment,
                "fill_price": price,
                "fill_pending": pending,
                "fill_unpriceable": unpriceable,
                "pricing_basis": pricing_basis,
                "multiplier": multiplier,
            }
        )
    return rows


def resolve_pending_fill(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    contract: str,
    multiplier: int,
    pricing_basis: str = "amount_vwap",
) -> float | None:
    """下一时段**前** 5 分钟的成交价，用来补上挂起的那一根（计划 D14）。

    下一时段本身零成交时返回 ``None`` —— 那一笔发不出去，由回测层记账。
    """
    window = slots[:FILL_MINUTES]
    if len(window) < FILL_MINUTES:
        return None
    try:
        return five_minute_vwap(
            slot_frame(frame, window),
            slots=window,
            contract=contract,
            multiplier=multiplier,
            pricing_basis=pricing_basis,
        ).price
    except MinuteDataError:
        return None


# ---------------------------------------------------------------------------
# 逐月编排
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402

from common.minute.pg_source import MinuteCandidate, minute_contract_identity  # noqa: E402
from common.minute.sessions import (  # noqa: E402
    SessionRule,
    build_trading_slots,
    fifteen_minute_buckets,
    resolve_session_rule,
)


@dataclass(frozen=True)
class SessionContext:
    """一个品种-日：要查哪张合约的哪段时间，以及它的分钟槽与 15 分钟桶。"""

    candidate: MinuteCandidate
    rule: SessionRule
    slots: tuple[datetime, ...]
    buckets: tuple[tuple[datetime, ...], ...]


def context_choices_for_month(
    choices: Sequence[object], *, month_start: date
) -> tuple[object, ...]:
    """保留当月选择，并为每个品种附上紧邻当月的一个前态。

    主力选择可以用更长历史预热不可逆展期链；时段上下文却只应解析目标月。
    前态只用于给当月首个交易日提供 ``previous_trade_date``，更早选择不得进入
    ``build_contexts``，否则会要求当前策略根本不交易的旧品种日也具备时段规则。
    """
    if type(month_start) is not date or month_start.day != 1:
        raise ValueError(
            "panel_month: month_start 必须是某个自然月的 1 号；"
            f"got {month_start!r}"
        )
    month_end = date(
        month_start.year + month_start.month // 12,
        month_start.month % 12 + 1,
        1,
    )
    predecessor_by_product: dict[str, object] = {}
    current: list[object] = []
    for choice in sorted(choices, key=lambda item: (item.trade_date, item.product)):
        if choice.trade_date < month_start:
            predecessor_by_product[choice.product] = choice
        elif choice.trade_date < month_end:
            current.append(choice)
    selected = [*predecessor_by_product.values(), *current]
    return tuple(sorted(selected, key=lambda item: (item.trade_date, item.product)))


def build_contexts(
    choices: Sequence[object],
    *,
    rules: Sequence[SessionRule],
) -> dict[tuple[date, str], SessionContext]:
    """把主力选择折成分钟层认的候选 + 该日的槽位与桶。

    夜盘属于**下一个**交易日，所以 `build_trading_slots` 需要前一交易日。第一天没有
    前一日可用，因此从第二天起才产出上下文 —— 少一天而不是猜一个前一日。

    ⚠️ 时段规则资产止于 2026-01-30；越界时 `resolve_session_rule` 硬失败，不静默截断。
    """
    ordered = sorted(choices, key=lambda c: (c.product, c.trade_date))
    contexts: dict[tuple[date, str], SessionContext] = {}
    previous_by_product: dict[str, date] = {}
    for choice in ordered:
        predecessor = previous_by_product.get(choice.product)
        selected_from = getattr(choice, "selected_from", None)
        previous = (
            selected_from
            if type(selected_from) is date and selected_from < choice.trade_date
            else predecessor
        )
        previous_by_product[choice.product] = choice.trade_date
        if previous is None:
            continue
        product, minute_symbol, exchange = minute_contract_identity(
            choice.contract, choice.trade_date
        )
        rule = resolve_session_rule(rules, exchange, product, choice.trade_date)
        slots = build_trading_slots(choice.trade_date, previous, rule)
        contexts[(choice.trade_date, product)] = SessionContext(
            candidate=MinuteCandidate(
                trade_date=choice.trade_date,
                product=product,
                daily_contract=choice.contract,
                minute_symbol=minute_symbol,
                exchange=exchange,
                window_start=slots[0],
                window_end=slots[-1] + timedelta(minutes=1),
                candidate_role="dominant",
                causal_in_pool_date=choice.selected_from,
                selection_source="daily_both_max_irreversible",
            ),
            rule=rule,
            slots=tuple(slots),
            buckets=fifteen_minute_buckets(slots, rule),
        )
    return contexts


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        lower = datetime(year, month, 1, tzinfo=slot_tz())
        year_next, month_next = (year + 1, 1) if month == 12 else (year, month + 1)
        yield lower, datetime(year_next, month_next, 1, tzinfo=slot_tz())
        year, month = year_next, month_next


def slot_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


def build_panel(
    *,
    contexts: Mapping[tuple[date, str], SessionContext],
    source,
    pricing_basis_by_exchange: Mapping[str, str],
    multiplier_resolver,
    adjustment_factor_by_key: Mapping[tuple[date, str], float],
    continuity_segment_by_key: Mapping[tuple[date, str], int],
) -> pd.DataFrame:
    """按月分批把面板建出来。

    `multiplier_resolver(candidate, frame)` 交给调用方 —— 生产上走
    `PublicMinuteSource.resolve_metadata_multiplier`，测试里给一个确定性替身。

    **挂起的成交价跨日、跨月接力**：某个品种-日最后一根桶没有「之后 5 分钟」，它由
    该品种**下一次出现**的那一天的前 5 分钟补上（计划 D14）。所以 `pending` 按品种
    维护、跨月存活；月末那一根不会因为换了批次就丢掉。
    """
    if not contexts:
        return pd.DataFrame(columns=list(PANEL_COLUMNS))

    missing_factors = sorted(set(contexts) - set(adjustment_factor_by_key))
    if missing_factors:
        raise ValueError(
            "panel_adjustment_factor_missing: 缺少品种日后复权因子；"
            f"first={missing_factors[0]!r} count={len(missing_factors)}"
        )
    missing_segments = sorted(set(contexts) - set(continuity_segment_by_key))
    if missing_segments:
        raise ValueError(
            "panel_continuity_segment_missing: 缺少品种日连续分段；"
            f"first={missing_segments[0]!r} count={len(missing_segments)}"
        )

    keys = sorted(contexts)
    by_month: dict[tuple[int, int], list[tuple[date, str]]] = {}
    for key in keys:
        by_month.setdefault((key[0].year, key[0].month), []).append(key)

    rows: list[dict[str, object]] = []
    pending: dict[str, int] = {}
    multipliers: dict[str, int] = {}

    for month_lower, _month_upper in _months(keys[0][0], keys[-1][0]):
        month_keys = by_month.get((month_lower.year, month_lower.month))
        if not month_keys:
            continue
        candidates = [contexts[key].candidate for key in month_keys]
        # ⚠️ 批次边界必须由候选窗口自己给出，不能用自然月的两端：夜盘属于**下一个**
        # 交易日，却起在前一个自然日 21:00。2023-02-01 的 CU 候选窗起于 01-31 21:00,
        # 用月首当下界会被分钟层判成"候选窗超出批次边界"而硬失败。
        batch_lower = min(candidate.window_start for candidate in candidates)
        batch_upper = max(candidate.window_end for candidate in candidates)
        frames = list(source.iter_month(candidates, batch_lower, batch_upper))
        if not frames:
            continue
        month = pd.concat(frames, ignore_index=True)

        for key in month_keys:
            context = contexts[key]
            candidate = context.candidate
            symbol = candidate.minute_symbol
            # ⚠️ 只按 symbol 过滤是不够的：同一张合约连着几天当主力时，某一天的夜盘
            # 与前一天的日盘落在同一个自然日上。分钟层随行返回 `trade_date`（由候选
            # 表 join 出来的**归属交易日**），必须拿它来切。
            frame = month.loc[
                (month["trade_date"] == candidate.trade_date)
                & (month["daily_contract"] == candidate.daily_contract)
            ]
            if frame.empty:
                continue
            basis = pricing_basis_by_exchange.get(candidate.exchange, "amount_vwap")
            if symbol not in multipliers:
                multipliers[symbol] = multiplier_resolver(candidate, frame)
            multiplier = multipliers[symbol]

            product = candidate.product
            waiting = pending.pop(product, None)
            if waiting is not None:
                rows[waiting]["fill_price"] = resolve_pending_fill(
                    frame,
                    slots=context.slots,
                    contract=symbol,
                    multiplier=multiplier,
                    pricing_basis=basis,
                )
                rows[waiting]["fill_pending"] = False
                rows[waiting]["fill_unpriceable"] = rows[waiting]["fill_price"] is None

            day_rows = build_session_bars(
                frame,
                slots=context.slots,
                buckets=context.buckets,
                contract=symbol,
                multiplier=multiplier,
                pricing_basis=basis,
                product=product,
                trade_date=candidate.trade_date,
                adj_factor=adjustment_factor_by_key[key],
                continuity_segment=int(continuity_segment_by_key[key]),
            )
            rows.extend(day_rows)
            pending[product] = len(rows) - 1

    return normalise_panel(pd.DataFrame(rows, columns=list(PANEL_COLUMNS)))


#: 无成交 bar 的 O/H/L/C 与发不出的成交价都是 ``None``，落进 DataFrame 会变成
#: object 列。fastparquet 推不出 object 列的类型（本仓没装 pyarrow），而且 object
#: 列在下游做算术时会静默退化成逐元素 Python 运算。所以面板出厂前一律定死列类型。
_FLOAT_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_factor",
    "fill_price",
)
_BOOL_COLUMNS = ("no_trade", "fill_pending", "fill_unpriceable")
_TEXT_COLUMNS = ("product", "contract", "pricing_basis")
_INT_COLUMNS = ("continuity_segment", "multiplier")


def normalise_panel(frame: pd.DataFrame) -> pd.DataFrame:
    """把面板的列类型定死，使它既能写 parquet 也能被下游安全地做算术。

    `trade_date` 存成 `datetime64[ns]`（当日零点）而不是 `datetime.date` 对象：
    date 对象在 pandas 里是 object 列，fastparquet 直接拒绝写。需要 date 的调用方
    用 `.dt.date`。
    """
    out = frame.copy()
    # ⚠️ 单位必须显式钉成 ns。`pd.to_datetime` 喂 `datetime.date` 会给出
    # `datetime64[s]`，fastparquet 把它按 ms 写出去、再读回来就报
    # "Cannot losslessly cast '1709596 ms' to s" —— 写得出、读不回，是最坏的一种。
    out["trade_date"] = pd.to_datetime(out["trade_date"]).astype("datetime64[ns]")
    out["slot_end"] = pd.to_datetime(out["slot_end"]).dt.tz_convert(
        "Asia/Shanghai"
    ).astype("datetime64[ns, Asia/Shanghai]")
    for column in _FLOAT_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    for column in _BOOL_COLUMNS:
        out[column] = out[column].astype("bool")
    for column in _TEXT_COLUMNS:
        out[column] = out[column].astype("string")
    for column in _INT_COLUMNS:
        out[column] = pd.to_numeric(out[column]).astype("int64")
    return out
