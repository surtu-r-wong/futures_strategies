"""影子策略的执行与记账层 —— 两篇研报复刻共用的那一半。

Bollinger 与道氏的**信号**不同，**成交**完全相同：同一份 15 分钟面板、同一套
换月两腿、同一个逐事件账户、同一条「这笔成交属于哪个交易日」的判定。把这半边
留在各自策略包里，等于让同一个缺陷需要被修两次 —— 夜盘日切那个 bug 就是活证据。

策略侧只回答"这根 bar 之后目标仓位是多少"；本模块负责它什么时候成交、成交在哪
个合约上、记在哪一天、以及那笔逻辑交易的成本与收益。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
import heapq
import math
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.panel import SessionCalendar
from common.minute.account import EventAccount

__all__ = [
    "unexecutable_transitions",
    "BAR_COLUMNS",
    "DAILY_COLUMNS",
    "ROLL_COLUMNS",
    "ShadowLedger",
    "prepare_bars",
    "prepare_rolls",
    "segment_slices",
]


BAR_COLUMNS = (
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

ROLL_COLUMNS = (
    "trade_date",
    "product",
    "old_contract",
    "new_contract",
    "fill_time",
    "old_price",
    "new_price",
    "old_pricing_basis",
    "new_pricing_basis",
)

DAILY_COLUMNS = (
    "product",
    "trade_date",
    "gross_return",
    "turnover",
    "cost",
    "direct_cost",
    "net_return",
    "gross_equity",
    "equity",
    "gross_leverage",
)


def unexecutable_transitions(
    traded: pd.DataFrame, rolls: pd.DataFrame
) -> tuple[frozenset[int], frozenset[int]]:
    """有成交的 bar 序列里，**换了合约却没有换月成交单**的那些切换。

    返回 ``(break_bars, switch_bars)``：前者是切换**之前**的最后一根有成交 bar
    （在那里强制平仓），后者是切换那一根（状态机清空、不再向换月要成交）。

    面板对成交窗口零成交的换月不发成交单（`build_roll_fills`），所以这种切换是
    「没人能执行的转移」：既不能按建模时点把旧腿卖掉，也不能假装仓位平移过去 ——
    两张合约的原始价不可比，只有复权因子让**价格**连续，仓位不会自己搬家。因此
    与断代同样处理：上一根强制平仓，新合约上重新开始。

    bundle 层保证「没有成交单」只可能是面板按规则跳过的（数量须与 manifest 申报
    一致），所以这里不必再分辨「按规则跳过」和「悄悄丢了一笔」。
    """
    if traded.empty:
        return frozenset(), frozenset()
    keys = set()
    if not rolls.empty:
        keys = {
            (row.trade_date, str(row.old_contract), str(row.new_contract))
            for row in rolls.itertuples(index=False)
        }
    break_bars: set[int] = set()
    switch_bars: set[int] = set()
    previous_index: int | None = None
    previous_contract: str | None = None
    for frame_index, row in traded.iterrows():
        contract = str(row["contract"])
        if previous_contract is not None and contract != previous_contract:
            if (row["trade_date"], previous_contract, contract) not in keys:
                break_bars.add(int(previous_index))
                switch_bars.add(int(frame_index))
        previous_index = frame_index
        previous_contract = contract
    return frozenset(break_bars), frozenset(switch_bars)


def segment_slices(segments: np.ndarray) -> list[slice]:
    """把一列连续分段编号切成若干段连续区间。

    指标、状态机与持仓都必须按段重来 —— 一次市场断代两侧的价格根本不可比，
    跨段算出来的均线只是把两段不相干的价格拼在一起。
    """
    values = np.asarray(segments)
    if values.size == 0:
        return []
    boundaries = np.flatnonzero(values[1:] != values[:-1]) + 1
    edges = [0, *boundaries.tolist(), values.size]
    return [slice(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def required_columns(
    frame: pd.DataFrame, required: tuple[str, ...], label: str
) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"shadow_{label}_columns: missing={missing!r}")


def finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"shadow_{label}: expected finite numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"shadow_{label}: expected finite numeric value")
    if positive and numeric <= 0.0:
        raise ValueError(f"shadow_{label}: expected finite positive value")
    return numeric


def date_value(value: object, label: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError(f"shadow_{label}: expected timezone-naive date")
        if value.time() != time.min:
            raise ValueError(f"shadow_{label}: expected midnight date")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp != timestamp.normalize():
            raise ValueError(f"shadow_{label}: expected midnight date")
        return timestamp.date()
    raise ValueError(f"shadow_{label}: expected date values")


def aware_timestamps(
    series: pd.Series, label: str, *, allow_missing: bool
) -> pd.Series:
    if not allow_missing and series.isna().any():
        raise ValueError(f"shadow_{label}: timestamp is required")
    try:
        converted = pd.to_datetime(series, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"shadow_{label}: invalid timestamp") from exc
    if not isinstance(converted.dtype, pd.DatetimeTZDtype):
        raise ValueError(f"shadow_{label}: expected timezone-aware timestamps")
    return converted


def nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"shadow_{label}: expected nonempty string")
    return value


def prepare_bars(value: object, product: str) -> tuple[pd.DataFrame, object | None]:
    """Validate and order one product's panel rows; return embedded rolls too."""
    embedded_rolls: object | None = None
    if isinstance(value, pd.DataFrame):
        source = value
    else:
        source = getattr(value, "bars", None)
        embedded_rolls = getattr(value, "roll_fills", None)
    if not isinstance(source, pd.DataFrame):
        raise ValueError("shadow_bars: expected DataFrame or bundle")
    required_columns(source, BAR_COLUMNS, "bars")

    frame = source.loc[source["product"] == product, list(BAR_COLUMNS)].copy()
    if frame.empty:
        raise ValueError(f"shadow_product: no rows for {product!r}")
    frame["slot_end"] = aware_timestamps(
        frame["slot_end"], "slot_end", allow_missing=False
    )
    frame["fill_time"] = aware_timestamps(
        frame["fill_time"], "fill_time", allow_missing=True
    )
    if frame.duplicated(["product", "slot_end"]).any():
        raise ValueError("shadow_duplicate: duplicate (product, slot_end)")
    frame = frame.sort_values("slot_end", kind="stable").reset_index(drop=True)
    frame["trade_date"] = [date_value(item, "trade_date") for item in frame["trade_date"]]

    for column in ("no_trade", "fill_pending", "fill_unpriceable"):
        if not frame[column].map(lambda item: isinstance(item, (bool, np.bool_))).all():
            raise ValueError(f"shadow_{column}: expected boolean values")
        frame[column] = frame[column].astype(bool)
    segments = frame["continuity_segment"]
    if not pd.api.types.is_integer_dtype(segments.dtype):
        try:
            segments = segments.astype("int64")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "shadow_continuity_segment: expected integer segment ids"
            ) from exc
        frame["continuity_segment"] = segments
    values = segments.to_numpy()
    if values.size and (values < 0).any():
        raise ValueError("shadow_continuity_segment: expected nonnegative segment ids")
    if values.size > 1 and (values[1:] < values[:-1]).any():
        raise ValueError(
            "shadow_continuity_segment: segments must not decrease over time"
        )

    for row in frame.itertuples(index=False):
        finite(row.multiplier, "multiplier", positive=True)
        finite(row.adj_factor, "adj_factor", positive=True)

    traded = frame.loc[~frame["no_trade"]]
    for row in traded.itertuples(index=False):
        nonempty_string(row.contract, "contract")
        nonempty_string(row.pricing_basis, "pricing_basis")
        for column in ("open", "high", "low", "close", "volume", "open_interest"):
            finite(getattr(row, column), column)
        if row.fill_time is not pd.NaT and not pd.isna(row.fill_time):
            if row.fill_time <= row.slot_end:
                raise ValueError("shadow_fill_time: fill must be after signal")
        if not pd.isna(row.fill_price):
            finite(row.fill_price, "fill_price", positive=True)
        elif not row.fill_pending and not row.fill_unpriceable:
            raise ValueError("shadow_fill_price: expected finite numeric value")
        if pd.isna(row.fill_time) and not row.fill_pending and not row.fill_unpriceable:
            raise ValueError("shadow_fill_time: timestamp is required")
    return frame, embedded_rolls


def prepare_rolls(value: object | None, product: str) -> pd.DataFrame:
    """Validate one product's roll fills; both legs must be priced."""
    if value is None:
        return pd.DataFrame(columns=ROLL_COLUMNS)
    if not isinstance(value, pd.DataFrame):
        raise ValueError("shadow_roll_fills: expected DataFrame")
    if value.empty:
        return pd.DataFrame(columns=ROLL_COLUMNS)
    required_columns(value, ROLL_COLUMNS, "roll_fills")
    frame = value.loc[value["product"] == product, list(ROLL_COLUMNS)].copy()
    if frame.empty:
        return frame
    frame["trade_date"] = [
        date_value(item, "roll_trade_date") for item in frame["trade_date"]
    ]
    frame["fill_time"] = aware_timestamps(
        frame["fill_time"], "roll_fill_time", allow_missing=False
    )
    if frame.duplicated(["trade_date", "product"]).any():
        raise ValueError("shadow_roll_duplicate: one roll per product-day")
    for row in frame.itertuples(index=False):
        old_contract = nonempty_string(row.old_contract, "roll_old_contract")
        new_contract = nonempty_string(row.new_contract, "roll_new_contract")
        if old_contract == new_contract:
            raise ValueError("shadow_roll_legs: old and new must differ")
        nonempty_string(row.old_pricing_basis, "roll_old_pricing_basis")
        nonempty_string(row.new_pricing_basis, "roll_new_pricing_basis")
        finite(row.old_price, "roll_old_price", positive=True)
        finite(row.new_price, "roll_new_price", positive=True)
    return frame.sort_values("fill_time", kind="stable").reset_index(drop=True)


class ShadowLedger:
    """One product's event account, pending fills, rolls, and daily boundary.

    交易日的收盘戳锚在**本交易日自己的最后一笔活动**上，绝不用日历日末。夜盘挂在
    前一自然日的晚上，用 `time.max` 会把收盘排到下一交易日的夜盘 bar 之后，账户
    的严格递增时钟直接拒绝。同一个理由，"这笔成交属于哪个交易日"由
    `SessionCalendar` 判定而不是比较日历日。
    """

    def __init__(
        self,
        *,
        product: str,
        calendar: SessionCalendar,
        timezone: Any,
        trade_columns: Sequence[str],
        cost_bps: float = 1.3,
        error_prefix: str = "shadow",
    ) -> None:
        self.product = product
        self.calendar = calendar
        self.timezone = timezone
        self.trade_columns = tuple(trade_columns)
        self.error_prefix = error_prefix
        self.account = EventAccount(cost_bps=cost_bps)
        self.current_contract: str | None = None
        self.current_target = 0.0
        self._latest_prices: dict[str, tuple[pd.Timestamp, float]] = {}
        self._pending: list[tuple[pd.Timestamp, int, dict[str, Any]]] = []
        self._sequence = 0
        self._trades: list[dict[str, Any]] = []
        self._open_trade: dict[str, Any] | None = None
        self._trade_number = 0
        self._daily_rows: list[dict[str, Any]] = []
        self._closed_dates: set[date] = set()

    def _fail(self, detail: str) -> ValueError:
        return ValueError(f"{self.error_prefix}_{detail}")

    def initialize(self, contract: str, price: float) -> None:
        self.current_contract = contract
        self.account.initialize({contract: price})

    def note_price(self, contract: str, timestamp: pd.Timestamp, price: float) -> None:
        self._latest_prices[contract] = (pd.Timestamp(timestamp), float(price))

    @property
    def open_trade(self) -> dict[str, Any] | None:
        return self._open_trade

    def _execute(self, payload: dict[str, Any]) -> None:
        fill_time = pd.Timestamp(payload["fill_time"])
        contract = str(payload["contract"])
        if contract != self.current_contract:
            raise self._fail("fill_contract: pending fill must match active contract")
        fill_price = float(payload["fill_price"])
        next_target = float(payload["next_target"])
        reason = str(payload["reason"])

        equity_before = self.account.equity
        gross_before = self.account.gross_equity
        execution_start = len(self.account.executions)
        self.account.rebalance(
            fill_time,
            {contract: fill_price},
            {contract: next_target} if next_target else {},
            {contract: reason},
        )
        on_execute = payload.get("on_execute")
        if on_execute is not None:
            on_execute()
        self.current_target = next_target
        self.note_price(contract, fill_time, fill_price)

        was_open = self._open_trade is not None
        if was_open:
            self._close_trade(
                fill_time=fill_time,
                contract=contract,
                fill_price=fill_price,
                reason=reason,
                fields=payload.get("exit_fields") or {},
            )
        if next_target:
            self._open_new_trade(
                fill_time=fill_time,
                contract=contract,
                fill_price=fill_price,
                direction=int(payload["direction"]),
                next_target=next_target,
                equity_before=equity_before if not was_open else self.account.equity,
                gross_before=gross_before if not was_open else self.account.gross_equity,
                execution_start=execution_start,
                fields=payload.get("entry_fields") or {},
            )

    def _open_new_trade(
        self,
        *,
        fill_time: pd.Timestamp,
        contract: str,
        fill_price: float,
        direction: int,
        next_target: float,
        equity_before: float,
        gross_before: float,
        execution_start: int,
        fields: Mapping[str, Any],
    ) -> None:
        self._trade_number += 1
        self._open_trade = {
            "product": self.product,
            "trade_id": f"{self.product}-{self._trade_number:06d}",
            "entry_date": fill_time.date(),
            "entry_time": fill_time,
            "entry_contract": contract,
            "entry_price": fill_price,
            "direction": direction,
            "target_magnitude": abs(next_target),
            "roll_count": 0,
            "equity_before": equity_before,
            "gross_before": gross_before,
            "execution_start": execution_start,
            **dict(fields),
        }

    def _close_trade(
        self,
        *,
        fill_time: pd.Timestamp,
        contract: str,
        fill_price: float,
        reason: str,
        fields: Mapping[str, Any],
    ) -> None:
        open_trade = self._open_trade
        assert open_trade is not None
        costs = math.fsum(
            record.cost
            for record in self.account.executions[int(open_trade["execution_start"]) :]
        )
        self._trades.append(
            {
                **{
                    key: open_trade[key]
                    for key in self.trade_columns
                    if key in open_trade
                },
                "exit_date": fill_time.date(),
                "exit_time": fill_time,
                "exit_contract": contract,
                "exit_price": fill_price,
                "exit_reason": reason,
                "gross_return": self.account.gross_equity
                / float(open_trade["gross_before"])
                - 1.0,
                "cost": costs,
                "net_return": self.account.equity / float(open_trade["equity_before"])
                - 1.0,
                **dict(fields),
            }
        )
        self._open_trade = None

    def drain_until(self, until: pd.Timestamp) -> None:
        while self._pending and self._pending[0][0] <= until:
            _, _, payload = heapq.heappop(self._pending)
            self._execute(payload)

    def request_target(
        self,
        *,
        trade_date: date,
        fill_time: object,
        contract: str,
        fill_price: float,
        next_target: float,
        direction: int,
        reason: str,
        entry_fields: Mapping[str, Any] | None = None,
        exit_fields: Mapping[str, Any] | None = None,
        on_execute: Callable[[], None] | None = None,
    ) -> bool:
        """Execute now, or defer to the session that actually trades the fill."""
        payload = {
            "fill_time": fill_time,
            "fill_price": fill_price,
            "contract": contract,
            "next_target": next_target,
            "direction": direction,
            "reason": reason,
            "entry_fields": dict(entry_fields or {}),
            "exit_fields": dict(exit_fields or {}),
            "on_execute": on_execute,
        }
        execution_date = self.calendar.execution_trade_date(fill_time, trade_date)
        if execution_date > trade_date:
            self._sequence += 1
            payload["execution_trade_date"] = execution_date
            heapq.heappush(self._pending, (pd.Timestamp(fill_time), self._sequence, payload))
            return False
        self._execute(payload)
        return True

    def roll(
        self,
        *,
        fill_time: pd.Timestamp,
        old_contract: str,
        new_contract: str,
        old_price: float,
        new_price: float,
    ) -> Any:
        """Move the position to the new contract, pricing both legs at once.

        换月与「前一日收盘信号在今日开盘的成交」由构造决定落在**同一时刻**（当日
        开盘第 5 分钟），而面板给那笔挂单的成交价正是**新合约**那五分钟的 VWAP ——
        实测 RU 三次换月，前一日最后一根的 `fill_price` 与换月 `new_price` 逐位相同。
        所以它们本就是同一笔：合并成一次调仓，旧腿按 `old_price` 平、新腿按
        `new_price` 建目标。分开记两笔会撞上账本「时间戳严格递增」，而且会把新合约
        的价记到旧合约名下。
        """
        stamp = pd.Timestamp(fill_time)
        while self._pending and self._pending[0][0] < stamp:
            _, _, earlier = heapq.heappop(self._pending)
            self._execute(earlier)
        payload = None
        if self._pending and self._pending[0][0] == stamp:
            _, _, payload = heapq.heappop(self._pending)
        if payload is None:
            event = None
            if self.current_target != 0.0:
                event = self.account.rebalance(
                    fill_time,
                    {old_contract: old_price, new_contract: new_price},
                    {new_contract: self.current_target},
                    {old_contract: "roll_old", new_contract: "roll_new"},
                )
                if self._open_trade is not None:
                    self._open_trade["roll_count"] = (
                        int(self._open_trade["roll_count"]) + 1
                    )
            self.current_contract = new_contract
            self.note_price(new_contract, fill_time, new_price)
            return event

        contract = str(payload["contract"])
        if contract != self.current_contract:
            raise self._fail("fill_contract: pending fill must match active contract")
        if not math.isclose(
            float(payload["fill_price"]), float(new_price), rel_tol=1e-9
        ):
            raise self._fail(
                "roll_fill_price: the pending fill and the roll price the same "
                f"window; pending={payload['fill_price']!r} roll={new_price!r}"
            )
        next_target = float(payload["next_target"])
        reason = str(payload["reason"])
        equity_before = self.account.equity
        gross_before = self.account.gross_equity
        execution_start = len(self.account.executions)
        event = self.account.rebalance(
            stamp,
            {old_contract: old_price, new_contract: new_price},
            {new_contract: next_target} if next_target else {},
            {old_contract: "roll_old", new_contract: reason},
        )
        on_execute = payload.get("on_execute")
        if on_execute is not None:
            on_execute()
        self.current_contract = new_contract
        self.current_target = next_target
        self.note_price(new_contract, stamp, new_price)
        was_open = self._open_trade is not None
        if was_open:
            self._close_trade(
                fill_time=stamp,
                contract=old_contract,
                fill_price=float(old_price),
                reason=reason,
                fields=payload.get("exit_fields") or {},
            )
        if next_target:
            self._open_new_trade(
                fill_time=stamp,
                contract=new_contract,
                fill_price=float(new_price),
                direction=int(payload["direction"]),
                next_target=next_target,
                equity_before=equity_before if not was_open else self.account.equity,
                gross_before=(
                    gross_before if not was_open else self.account.gross_equity
                ),
                execution_start=execution_start,
                fields=payload.get("entry_fields") or {},
            )
        return event

    def close_day(self, trade_day: date, last_slot_end: object | None) -> None:
        if trade_day in self._closed_dates:
            raise self._fail(f"daily_duplicate: {trade_day}")
        while self._pending and self._pending[0][2]["execution_trade_date"] <= trade_day:
            _, _, payload = heapq.heappop(self._pending)
            self._execute(payload)

        candidates: list[pd.Timestamp] = []
        if last_slot_end is not None:
            candidates.append(pd.Timestamp(last_slot_end))
        if self.account.events:
            candidates.append(pd.Timestamp(self.account.events[-1].timestamp))
        if not candidates:
            raise self._fail(f"daily_activity: {trade_day} has no activity")
        close_timestamp = max(candidates) + timedelta(microseconds=1)
        if close_timestamp.date() != trade_day:
            raise self._fail("daily_time: trade date activity left its calendar day")

        prices: dict[str, float] = {}
        if self.current_target:
            assert self.current_contract is not None
            latest = self._latest_prices.get(self.current_contract)
            if latest is None or latest[0] > close_timestamp:
                raise self._fail("daily_price: held contract needs a causal price")
            prices[self.current_contract] = latest[1]
        self.account.mark_close(trade_day, close_timestamp, prices)
        daily = self.account.drain_daily_row(trade_day, "close")
        self._daily_rows.append(
            {
                "product": self.product,
                "trade_date": trade_day,
                "gross_return": daily.gross_return,
                "turnover": daily.turnover,
                "cost": daily.cost,
                "direct_cost": daily.direct_cost,
                "net_return": daily.net_return,
                "gross_equity": daily.gross_equity,
                "equity": daily.equity,
                "gross_leverage": daily.gross_leverage,
            }
        )
        self._closed_dates.add(trade_day)

    def close_trailing_days(self, next_day: date | None) -> None:
        while self._pending:
            pending_day = self._pending[0][2]["execution_trade_date"]
            if next_day is not None and pending_day >= next_day:
                return
            self.close_day(pending_day, None)

    def finish(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        trades = pd.DataFrame(self._trades, columns=list(self.trade_columns))
        daily = (
            pd.DataFrame(self._daily_rows, columns=list(DAILY_COLUMNS))
            .sort_values("trade_date", kind="stable")
            .reset_index(drop=True)
        )
        return trades, daily
