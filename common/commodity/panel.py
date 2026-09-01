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

from collections import Counter
from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from pathlib import Path

import pandas as pd

from common.minute.bars import (
    UNRESOLVED_MULTIPLIER_CHECKS,
    MinuteDataError,
    aggregate_fifteen_minute_bar,
    five_minute_vwap,
)

__all__ = [
    "FILL_MINUTES",
    "PANEL_COLUMNS",
    "SessionCalendar",
    "SessionContext",
    "build_contexts",
    "build_panel",
    "build_session_bars",
    "context_choices_for_month",
    "normalise_panel",
    "require_session_coverage",
    "required_session_keys",
    "required_session_keys_by_month",
    "resolve_pending_fill",
    "slot_frame",
    "slot_tz",
]

#: 研报 §5.1：信号触发后 5 分钟 VWAP。
FILL_MINUTES = 5


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """哪一个交易日的时段真正成交了某个时间戳上的委托。

    夜盘挂在**前一个自然日**的晚上（`common.minute.sessions._slot_timestamp`），
    所以"日历日更晚"和"时段更晚"不是同一个问题：交易日 D 的 15:00 那根挂起后，
    由 D+1 夜盘开盘的前 5 分钟补上（`resolve_pending_fill`），而那 5 分钟的墙钟
    仍在 D 这一天。分界线因此是**本交易日最后一根 bar**，不是午夜。

    没有下一个交易日在该日历日开盘时（日盘品种、或面板末尾），只能由日历日裁定。
    """

    last_slot: Mapping[date, pd.Timestamp]
    first_slot: Mapping[date, pd.Timestamp]
    following: Mapping[date, date]

    @classmethod
    def from_bars(cls, frame: pd.DataFrame) -> "SessionCalendar":
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("session_calendar_bars: expected DataFrame")
        for column in ("trade_date", "slot_end"):
            if column not in frame.columns:
                raise ValueError(f"session_calendar_bars: missing {column!r}")
        if frame.empty:
            raise ValueError("session_calendar_bars: expected at least one bar")
        grouped = frame.groupby("trade_date")["slot_end"]
        last = {day: pd.Timestamp(value) for day, value in grouped.max().items()}
        first = {day: pd.Timestamp(value) for day, value in grouped.min().items()}
        ordered = sorted(last)
        following = {
            day: ordered[position + 1] for position, day in enumerate(ordered[:-1])
        }
        return cls(last_slot=last, first_slot=first, following=following)

    def execution_trade_date(self, fill_time: object, trade_date: date) -> date:
        if trade_date not in self.last_slot:
            raise ValueError(
                f"session_calendar_trade_date: {trade_date!r} is not in the panel"
            )
        stamp = pd.Timestamp(fill_time)
        if stamp <= self.last_slot[trade_date]:
            return trade_date
        following = self.following.get(trade_date)
        if (
            following is not None
            and pd.Timestamp(self.first_slot[following]).date() == stamp.date()
        ):
            return following
        return stamp.date()


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
    "open_interest",
    "no_trade",
    "adj_factor",
    "continuity_segment",
    "fill_time",
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
        bucket_frame = slot_frame(frame, bucket)
        bar = aggregate_fifteen_minute_bar(
            bucket_frame, slots=bucket, contract=contract
        )
        traded_bucket = bucket_frame.loc[bucket_frame["volume"] > 0].sort_values(
            "bar_time", kind="mergesort"
        )
        open_interest = None
        if not traded_bucket.empty:
            try:
                candidate_oi = float(traded_bucket["open_interest"].iloc[-1])
            except (TypeError, ValueError):
                pass
            else:
                if math.isfinite(candidate_oi):
                    open_interest = candidate_oi
        window = slots[(index + 1) * 15 : (index + 1) * 15 + FILL_MINUTES]
        pending = len(window) < FILL_MINUTES
        fill_time = None if pending else window[-1]
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
                "open_interest": open_interest,
                "no_trade": bar.no_trade,
                "adj_factor": adj_factor,
                "continuity_segment": continuity_segment,
                "fill_time": fill_time,
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
    SessionClockError,
    SessionRule,
    build_trading_slots,
    fifteen_minute_buckets,
    matching_session_rules,
    resolve_session_rule,
)


@dataclass(frozen=True)
class SessionContext:
    """一个品种-日：要查哪张合约的哪段时间，以及它的分钟槽与 15 分钟桶。"""

    candidate: MinuteCandidate
    rule: SessionRule
    slots: tuple[datetime, ...]
    buckets: tuple[tuple[datetime, ...], ...]


@dataclass(frozen=True)
class UncoveredProductDay:
    """一个形不成 bar 的品种日：乘数定不出来 ⇒ 定不出价 ⇒ 面板不覆盖。"""

    trade_date: date
    product: str
    contract: str
    reason: str


@dataclass(frozen=True)
class PanelMonthChunk:
    """Finalized bars plus the small pending-fill state after one month."""

    month_start: date
    bars: pd.DataFrame
    pending: pd.DataFrame
    uncovered: tuple[UncoveredProductDay, ...] = ()


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
            f"panel_month: month_start 必须是某个自然月的 1 号；got {month_start!r}"
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


def _context_plan(choices: Sequence[object]):
    """Exactly which product-days `build_contexts` will ask a rule for.

    The coverage gate and the context builder share this one derivation, so
    "what the capture covered" and "what the panel demands" cannot drift apart
    by being written correctly in two places.
    """
    ordered = sorted(choices, key=lambda c: (c.product, c.trade_date))
    previous_by_product: dict[str, date] = {}
    plan = []
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
        plan.append((choice, previous, product, minute_symbol, exchange))
    return tuple(plan)


def required_session_keys(
    choices: Sequence[object],
) -> tuple[tuple[str, str, date], ...]:
    """The `(exchange, product, date)` keys `build_contexts` will resolve."""
    return tuple(
        (exchange, product, choice.trade_date)
        for choice, _previous, product, _symbol, exchange in _context_plan(choices)
    )


def required_session_keys_by_month(
    *,
    choices: Sequence[object],
    months: Sequence[date],
) -> dict[date, tuple[tuple[str, str, date], ...]]:
    """Per month, the session-rule keys `build_contexts` will ask for.

    The coverage gate and the capture driver share this one derivation, so
    "what the capture covered" and "what the panel demands" are the same set by
    construction rather than by two people writing it correctly (design D4).
    """
    by_month: dict[date, tuple[tuple[str, str, date], ...]] = {}
    for month in sorted(months):
        keys = tuple(
            key
            for key in required_session_keys(
                context_choices_for_month(choices, month_start=month)
            )
            if _month_of(key[2]) == month
        )
        if keys:
            by_month[month] = keys
    return by_month


def require_session_coverage(
    *,
    choices: Sequence[object],
    months: Sequence[date],
    rules: Sequence[SessionRule],
    manifest_path: Path | None = None,
) -> None:
    """Check session-rule coverage for the whole window before any minute query.

    ⚠️ **Call this before the first minute query.** Expanding month by month and
    reporting the entire shortfall at once is the point: the alternative is
    dying on the first uncovered product-day and learning nothing about scale,
    which is how this problem surfaced twice already.

    Both cardinalities are refused: zero rules is a gap in the asset, two is the
    asset contradicting itself.
    """
    rows: list[tuple[date, str, str, date, int]] = []
    for month, keys in required_session_keys_by_month(
        choices=choices, months=months
    ).items():
        for exchange, product, trade_date in keys:
            found = len(matching_session_rules(rules, exchange, product, trade_date))
            if found != 1:
                rows.append((month, exchange, product, trade_date, found))
    if not rows:
        return
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["month", "exchange", "product", "trade_date", "found"])
            for month, exchange, product, trade_date, found in rows:
                writer.writerow(
                    [f"{month:%Y-%m}", exchange, product, trade_date.isoformat(), found]
                )
    identities = {(row[1], row[2]) for row in rows}
    by_year = Counter(row[3].year for row in rows)
    raise SessionClockError(
        exchange=rows[0][1],
        product=rows[0][2],
        trade_date=rows[0][3],
        check="session_coverage_incomplete",
        reason=(
            f"{len(rows)} product-days lack exactly one session rule "
            f"across {len(identities)} products; by year "
            + " ".join(f"{year}:{count}" for year, count in sorted(by_year.items()))
            + (f"; manifest={manifest_path}" if manifest_path is not None else "")
        ),
    )


def _month_of(value: date) -> date:
    return value.replace(day=1)


def build_contexts(
    choices: Sequence[object],
    *,
    rules: Sequence[SessionRule],
    month: date | None = None,
) -> dict[tuple[date, str], SessionContext]:
    """把主力选择折成分钟层认的候选 + 该日的槽位与桶。

    夜盘属于**下一个**交易日，所以 `build_trading_slots` 需要前一交易日。第一天没有
    前一日可用，因此从第二天起才产出上下文 —— 少一天而不是猜一个前一日。

    ``month`` 给定时只产出该月的上下文。前态（上个月最后一个选择）仍然参与，
    因为当月首日要靠它拿 ``previous_trade_date``，但**它自己不再索取时段规则** ——
    覆盖闸正是这样按月裁键的（`required_session_keys_by_month`），两边必须同一刀，
    否则资产起点那个月必炸：商品复刻的资产从 2012-01-04 起，而它的前态在 2011-12-30。

    ⚠️ 时段规则资产止于 2026-01-30；越界时 `resolve_session_rule` 硬失败，不静默截断。
    """
    contexts: dict[tuple[date, str], SessionContext] = {}
    for choice, previous, product, minute_symbol, exchange in _context_plan(choices):
        if month is not None and _month_of(choice.trade_date) != month:
            continue
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


def iter_panel_months(
    *,
    contexts: Mapping[tuple[date, str], SessionContext],
    source,
    pricing_basis_by_exchange: Mapping[str, str],
    multiplier_resolver,
    adjustment_factor_by_key: Mapping[tuple[date, str], float],
    continuity_segment_by_key: Mapping[tuple[date, str], int],
    resume_after: date | None = None,
    initial_pending: pd.DataFrame | None = None,
    drop_unformable_days: bool = False,
):
    """Yield finalized monthly bars and the small resumable pending state.

    ⚠️ 授权资产登记过的「归档本来就没有这一天」在**上下文构造**时就被剔除了
    （`build_panel._target_contexts`），所以这里见到的空帧一律是"数据丢了"，硬失败。

    ``drop_unformable_days`` 声明面板的覆盖口径：乘数在这一天定不出来时，这个品种日
    **形不成 bar**，于是面板不覆盖它 —— 不产出行、记进 ``chunk.uncovered``、由调用方
    落盘。合成一个乘数就是编价格，中止则让整段历史因为一个死盘品种而做不出来。默认
    仍然硬失败：不覆盖是调用方明确声明的口径，不是随手的容错。
    """
    if not contexts:
        return
    missing_factors = sorted(set(contexts) - set(adjustment_factor_by_key))
    if missing_factors:
        raise ValueError(
            "panel_adjustment_factor_missing: 缺少品种日后复权因子；"
            f"first={missing_factors[0]!r} count={len(missing_factors)}"
        )
    # 分段与因子同等 fail-closed：缺映射时默认成 0，等于把一次市场断代悄悄
    # 抹平成一条连续序列，正是分段机制要防的事。
    missing_segments = sorted(set(contexts) - set(continuity_segment_by_key))
    if missing_segments:
        raise ValueError(
            "panel_continuity_segment_missing: 缺少品种日连续分段；"
            f"first={missing_segments[0]!r} count={len(missing_segments)}"
        )
    if resume_after is not None and (
        type(resume_after) is not date or resume_after.day != 1
    ):
        raise ValueError("panel_resume_month: resume_after must be a month start")

    keys = sorted(contexts)
    by_month: dict[tuple[int, int], list[tuple[date, str]]] = {}
    for key in keys:
        by_month.setdefault((key[0].year, key[0].month), []).append(key)

    pending: dict[str, dict[str, object]] = {}
    if initial_pending is not None and not initial_pending.empty:
        restored = normalise_panel(initial_pending)
        if restored["product"].duplicated().any() or not restored[
            "fill_pending"
        ].all():
            raise ValueError(
                "panel_resume_pending: one pending row per product is required"
            )
        pending = {
            str(row["product"]): row for row in restored.to_dict(orient="records")
        }

    for month_lower, _month_upper in _months(keys[0][0], keys[-1][0]):
        month_start = month_lower.date()
        if resume_after is not None and month_start <= resume_after:
            continue
        month_keys = by_month.get((month_lower.year, month_lower.month))
        if not month_keys:
            continue
        month_rows: list[dict[str, object]] = []
        month_row_ids: set[int] = set()
        candidates = [contexts[key].candidate for key in month_keys]
        batch_lower = min(candidate.window_start for candidate in candidates)
        batch_upper = max(candidate.window_end for candidate in candidates)
        frames = list(source.iter_month(candidates, batch_lower, batch_upper))
        month = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        context_frames: dict[tuple[date, str], pd.DataFrame] = {}
        inference_frames: dict[tuple[date, str], pd.DataFrame] = {}
        required_identity = {"trade_date", "daily_contract"}
        for key in month_keys:
            candidate = contexts[key].candidate
            if not required_identity.issubset(month.columns):
                frame = pd.DataFrame()
            else:
                frame = month.loc[
                    (month["trade_date"] == candidate.trade_date)
                    & (month["daily_contract"] == candidate.daily_contract)
                ]
            if frame.empty:
                raise ValueError(
                    "panel_context_missing: expected product-day minute frame; "
                    f"trade_date={candidate.trade_date.isoformat()} "
                    f"product={candidate.product!r} "
                    f"contract={candidate.daily_contract!r}"
                )
            context_frames[key] = frame
            # 乘数在合约元数据缺档时只能从分钟推断，而推断的取样要**跨多个交易日**
            # （`_select_multiplier_sample` 要 2–3 个）。一个品种日只有一天，所以推断
            # 用的样本必须放宽到这一个月里该**品种**的全部行 —— 白银 AG1209 2012-05
            # 就是这样把整跑打断的。校验仍然只用当天那一帧。
            inference_frames[key] = (
                month.loc[month["product"] == candidate.product]
                if "product" in month.columns
                else month
            )

        uncovered: list[UncoveredProductDay] = []
        for key in month_keys:
            context = contexts[key]
            candidate = context.candidate
            symbol = candidate.minute_symbol
            frame = context_frames[key]
            basis = pricing_basis_by_exchange.get(candidate.exchange, "amount_vwap")
            try:
                multiplier = multiplier_resolver(
                    candidate, frame, inference_frame=inference_frames[key]
                )
            except MinuteDataError as exc:
                if not drop_unformable_days or (
                    exc.check not in UNRESOLVED_MULTIPLIER_CHECKS
                ):
                    raise
                # 证据说不出乘数 ⇒ 这一天的每一根 bar 都定不出价。挂着的成交价不动，
                # 它会在下一个**被覆盖的**时段上兑现。
                uncovered.append(
                    UncoveredProductDay(
                        trade_date=candidate.trade_date,
                        product=candidate.product,
                        contract=candidate.daily_contract,
                        reason=exc.check,
                    )
                )
                continue

            product = candidate.product
            waiting = pending.pop(product, None)
            if waiting is not None:
                opening_window = context.slots[:FILL_MINUTES]
                waiting["fill_price"] = resolve_pending_fill(
                    frame,
                    slots=context.slots,
                    contract=symbol,
                    multiplier=multiplier,
                    pricing_basis=basis,
                )
                waiting["fill_time"] = (
                    opening_window[-1] if len(opening_window) == FILL_MINUTES else None
                )
                waiting["fill_pending"] = False
                waiting["fill_unpriceable"] = waiting["fill_price"] is None
                if id(waiting) not in month_row_ids:
                    month_rows.append(waiting)
                    month_row_ids.add(id(waiting))

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
                continuity_segment=continuity_segment_by_key[key],
            )
            for row in day_rows:
                month_row_ids.add(id(row))
            month_rows.extend(day_rows)
            pending[product] = day_rows[-1]

        pending_ids = {id(row) for row in pending.values()}
        bars = normalise_panel(
            pd.DataFrame(
                [row for row in month_rows if id(row) not in pending_ids],
                columns=list(PANEL_COLUMNS),
            )
        )
        pending_frame = normalise_panel(
            pd.DataFrame(list(pending.values()), columns=list(PANEL_COLUMNS))
        )
        yield PanelMonthChunk(
            month_start=month_start,
            bars=bars,
            pending=pending_frame,
            uncovered=tuple(uncovered),
        )


def build_panel(
    *,
    contexts: Mapping[tuple[date, str], SessionContext],
    source,
    pricing_basis_by_exchange: Mapping[str, str],
    multiplier_resolver,
    adjustment_factor_by_key: Mapping[tuple[date, str], float],
    continuity_segment_by_key: Mapping[tuple[date, str], int],
) -> pd.DataFrame:
    """Compatibility wrapper around the bounded monthly iterator."""
    chunks = list(
        iter_panel_months(
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange=pricing_basis_by_exchange,
            multiplier_resolver=multiplier_resolver,
            adjustment_factor_by_key=adjustment_factor_by_key,
            continuity_segment_by_key=continuity_segment_by_key,
        )
    )
    if not chunks:
        return normalise_panel(pd.DataFrame(columns=list(PANEL_COLUMNS)))
    frames = [chunk.bars for chunk in chunks]
    frames.append(chunks[-1].pending)
    return normalise_panel(pd.concat(frames, ignore_index=True))


#: 无成交 bar 的 O/H/L/C 与发不出的成交价都是 ``None``，落进 DataFrame 会变成
#: object 列。fastparquet 推不出 object 列的类型（本仓没装 pyarrow），而且 object
#: 列在下游做算术时会静默退化成逐元素 Python 运算。所以面板出厂前一律定死列类型。
_FLOAT_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "adj_factor",
    "fill_price",
)
_BOOL_COLUMNS = ("no_trade", "fill_pending", "fill_unpriceable")
_TEXT_COLUMNS = ("product", "contract", "pricing_basis")


def _normalise_aware_time_column(values: pd.Series, *, column: str) -> pd.Series:
    for value in values:
        if pd.isna(value):
            continue
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(
                f"panel_{column}_timezone: {column} values must be timezone-aware"
            )
    return (
        pd.to_datetime(values, utc=True)
        .dt.tz_convert("Asia/Shanghai")
        .astype("datetime64[ns, Asia/Shanghai]")
    )


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
    for column in ("slot_end", "fill_time"):
        out[column] = _normalise_aware_time_column(out[column], column=column)
    for column in _FLOAT_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    for column in _BOOL_COLUMNS:
        out[column] = out[column].astype("bool")
    for column in _TEXT_COLUMNS:
        out[column] = out[column].astype("string")
    for column in ("multiplier", "continuity_segment"):
        out[column] = pd.to_numeric(out[column]).astype("int64")
    return out
