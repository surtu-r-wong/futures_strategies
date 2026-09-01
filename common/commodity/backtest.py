"""选中组合的事件级回测 —— 两篇研报复刻共用的那一半。

Bollinger 与道氏在这一层只差三件事：资金怎么分（全宇宙等分 vs 当前持仓等分）、
目标波动是 10% 还是 15%、以及有没有持仓量倍率。其余全部相同：月度宇宙 ∩ 影子
筛选、两本平行账、换月两腿、月界对齐、逐事件成交与日切。

**两本账**是这一层的核心。波动乘数要的是"选中组合但**不含**乘数"的收益序列：
从真实账读等于让乘数吃自己的输出，从影子账读等于无视当月到底选了谁。所以真实账
之外并行跑一本 pre-volatility 账，下月乘数只读它。

**resize**：道氏的分母是"当前有持仓的品种数"，所以任一品种进出都会改变**所有**
其他品种的目标。它们不能在别人的成交时点上按陈价成交 —— 那是编造成交。改为标记
待调整，各自在自己下一个可成交窗口调过来。Bollinger 的分母月内恒定，永远不会触发
这条路径。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.panel import SessionCalendar
from common.commodity.selection import ProductScore, trailing_scores
from common.leverage import atr_leverage, final_leverage, monthly_realized_volatility
from common.minute.account import EventAccount

__all__ = [
    "Allocation",
    "BacktestResult",
    "DAILY_COLUMNS",
    "POSITION_COLUMNS",
    "QUALITY_COLUMNS",
    "SELECTION_COLUMNS",
    "StrategySpec",
    "TRADE_COLUMNS",
    "run_portfolio_backtest",
]


COST_BPS = 1.3
SELECTION_OBSERVATIONS = 252
VOL_OBSERVATIONS = 252

_ALIGNMENT_COLUMNS = ("slot_end", "contract", "no_trade", "multiplier")

POSITION_COLUMNS = (
    "trade_date",
    "month_start",
    "product",
    "contract",
    "selected",
    "active_products",
    "universe_weight",
    "base_weight_abs",
    "direction",
    "oi_scale",
    "atr_leverage",
    "realized_vol",
    "target_annual_vol",
    "vol_multiplier",
    "leverage",
    "target_weight",
    "actual_weight",
    "prevol_weight",
)
DAILY_COLUMNS = (
    "trade_date",
    "month_start",
    "gross_return",
    "turnover",
    "cost",
    "direct_cost",
    "net_return",
    "gross_equity",
    "equity",
    "gross_leverage",
    "prevol_net_return",
    "prevol_equity",
    "realized_vol",
    "vol_multiplier",
    "selected_count",
)
TRADE_COLUMNS = (
    "timestamp",
    "trade_date",
    "product",
    "contract",
    "reason",
    "price",
    "old_weight",
    "new_weight",
    "weight_change",
    "turnover",
    "cost",
)
SELECTION_COLUMNS = (
    "month_start",
    "product",
    "in_liquidity_universe",
    "has_score",
    "observations",
    "trade_count",
    "cumulative_return",
    "sharpe",
    "calmar",
    "eligible",
    "selected",
    "reason",
)
QUALITY_COLUMNS = ("metric", "product", "value")

_PRICE, _ROLL, _ALIGN, _TARGET = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Every sheet a commodity replication report is allowed to draw from."""

    daily: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    selection: pd.DataFrame
    data_quality: pd.DataFrame


@dataclass(frozen=True, slots=True)
class Allocation:
    """One instant's capital split, plus the denominator that produced it."""

    signed: Mapping[str, float]
    sleeve: Mapping[str, float]
    active_products: int


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Everything the shared portfolio runner needs a strategy to decide."""

    name: str
    allocate: Callable[[tuple[str, ...], Mapping[str, float]], Allocation]
    eligible: Callable[[Mapping[str, ProductScore]], tuple[str, ...]]
    rejection: Callable[[ProductScore], str | None]
    target_vol: float
    position_columns: Sequence[str] = POSITION_COLUMNS
    scale_column: str | None = None
    resize_on_allocation_change: bool = False


@dataclass(frozen=True, slots=True)
class _Exposure:
    direction: int
    scale: float
    close: float
    atr: float


@dataclass(frozen=True, slots=True)
class _Event:
    timestamp: pd.Timestamp
    priority: int
    sequence: int
    trade_date: date
    kind: str
    product: str
    payload: dict[str, Any]


def _fail(prefix: str, label: str, detail: str) -> ValueError:
    return ValueError(f"{prefix}_{label}: {detail}")


def _finite(value: object, prefix: str, label: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise _fail(prefix, label, "expected a finite numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise _fail(prefix, label, "expected a finite numeric value")
    if positive and numeric <= 0.0:
        raise _fail(prefix, label, "expected a finite positive value")
    return numeric


def _date_value(value: object, prefix: str, label: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise _fail(prefix, label, "expected a timezone-naive date")
        if value.time() != datetime.min.time():
            raise _fail(prefix, label, "expected a midnight date")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp != stamp.normalize():
            raise _fail(prefix, label, "expected a midnight date")
        return stamp.date()
    raise _fail(prefix, label, "expected date values")


def _text(value: object, prefix: str, label: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise _fail(prefix, label, "expected a nonempty string")
    result = str(value)
    if not result:
        raise _fail(prefix, label, "expected a nonempty string")
    return result


def _require_frame(
    value: object, *, prefix: str, label: str, required: tuple[str, ...]
) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise _fail(prefix, label, "expected a DataFrame")
    missing = [column for column in required if column not in value.columns]
    if missing:
        raise _fail(prefix, f"{label}_columns", f"missing={missing!r}")
    return value


def _normalise_bars(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    bars = frame.copy()
    bars["product"] = [_text(value, prefix, "bars_product") for value in bars["product"]]
    bars["contract"] = [
        _text(value, prefix, "bars_contract") for value in bars["contract"]
    ]
    bars["trade_date"] = [
        _date_value(value, prefix, "bars_trade_date") for value in bars["trade_date"]
    ]
    for column in ("slot_end", "fill_time"):
        converted = pd.to_datetime(bars[column])
        if not isinstance(converted.dtype, pd.DatetimeTZDtype):
            raise _fail(prefix, f"bars_{column}", "expected timezone-aware timestamps")
        bars[column] = converted
    return bars.sort_values(["product", "slot_end"], kind="stable").reset_index(drop=True)


def _normalise_signals(shadow: Any, prefix: str) -> pd.DataFrame:
    signals = _require_frame(
        shadow.signals,
        prefix=prefix,
        label="signals",
        required=(
            "product",
            "trade_date",
            "slot_end",
            "contract",
            "no_trade",
            "raw_close",
            "atr_raw",
            "target_direction",
            "action",
            "action_changed",
            "fill_time",
            "fill_price",
            "multiplier",
        ),
    ).copy()
    signals["product"] = [
        _text(value, prefix, "signals_product") for value in signals["product"]
    ]
    signals["contract"] = [
        _text(value, prefix, "signals_contract") for value in signals["contract"]
    ]
    signals["trade_date"] = [
        _date_value(value, prefix, "signals_trade_date")
        for value in signals["trade_date"]
    ]
    for column in ("slot_end", "fill_time"):
        signals[column] = pd.to_datetime(signals[column])
    return signals.sort_values("slot_end", kind="stable").reset_index(drop=True)


def _check_alignment(
    signals: pd.DataFrame, panel: pd.DataFrame, product: str, prefix: str
) -> None:
    """The shadow must be the one this bundle produced, not a lookalike."""
    if len(signals) != len(panel):
        raise _fail(
            prefix,
            "alignment",
            f"{product}: {len(signals)} signal rows vs {len(panel)} bars",
        )
    for column in _ALIGNMENT_COLUMNS:
        if not np.array_equal(signals[column].to_numpy(), panel[column].to_numpy()):
            raise _fail(
                prefix, "alignment", f"{product}: {column} differs from the bundle"
            )
    for column in ("fill_time", "fill_price"):
        left, right = signals[column], panel[column]
        both = left.notna() & right.notna()
        if not left.loc[both].equals(right.loc[both]):
            raise _fail(
                prefix, "alignment", f"{product}: {column} differs from the bundle"
            )


def _liquidity_universe(frame: pd.DataFrame, prefix: str) -> dict[date, set[str]]:
    universe: dict[date, set[str]] = defaultdict(set)
    for row in frame.itertuples(index=False):
        month = _date_value(row.month_start, prefix, "universes_month_start")
        if month.day != 1:
            raise _fail(prefix, "universes_month_start", f"{month} is not a month start")
        universe[month].add(_text(row.product, prefix, "universes_product"))
    return dict(universe)


def _score_inputs(
    shadows: Mapping[str, Any], prefix: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.concat(
        [
            _require_frame(
                shadow.daily,
                prefix=prefix,
                label="shadow_daily",
                required=("product", "trade_date", "net_return"),
            )[["product", "trade_date", "net_return"]]
            for shadow in shadows.values()
        ],
        ignore_index=True,
    )
    frames = []
    for shadow in shadows.values():
        frame = _require_frame(
            shadow.trades,
            prefix=prefix,
            label="shadow_trades",
            required=("product", "exit_date"),
        )
        frames.append(
            frame[
                [
                    column
                    for column in ("product", "exit_date", "trade_id")
                    if column in frame.columns
                ]
            ]
        )
    return daily, pd.concat(frames, ignore_index=True)


def run_portfolio_backtest(
    *,
    bundle: Any,
    shadows: Mapping[str, Any],
    spec: StrategySpec,
    cost_bps: float = COST_BPS,
    realized_vol_min_observations: int = VOL_OBSERVATIONS,
    selection_observations: int = SELECTION_OBSERVATIONS,
) -> BacktestResult:
    """Run one strategy's selected portfolio over a panel bundle and its shadows."""
    prefix = spec.name
    if not isinstance(shadows, Mapping) or not shadows:
        raise _fail(prefix, "shadows", "expected a nonempty mapping of shadow results")
    for product, shadow in shadows.items():
        if getattr(shadow, "product", None) != product:
            raise _fail(prefix, "shadows", f"{product!r} does not match its result")
    target_vol = _finite(spec.target_vol, prefix, "target_vol", positive=True)
    cost_bps = _finite(cost_bps, prefix, "cost_bps")
    if cost_bps < 0.0:
        raise _fail(prefix, "cost_bps", "expected a nonnegative value")

    bars = _normalise_bars(
        _require_frame(
            getattr(bundle, "bars", None),
            prefix=prefix,
            label="bars",
            required=(
                "product",
                "contract",
                "trade_date",
                "slot_end",
                "close",
                "no_trade",
                "fill_time",
                "fill_price",
                "multiplier",
            ),
        ),
        prefix,
    )
    liquidity = _liquidity_universe(
        _require_frame(
            getattr(bundle, "universes", None),
            prefix=prefix,
            label="universes",
            required=("month_start", "product"),
        ),
        prefix,
    )
    rolls = _require_frame(
        getattr(bundle, "roll_fills", None),
        prefix=prefix,
        label="roll_fills",
        required=(
            "trade_date",
            "product",
            "old_contract",
            "new_contract",
            "fill_time",
            "old_price",
            "new_price",
        ),
    )

    signals_by_product: dict[str, pd.DataFrame] = {}
    calendars: dict[str, SessionCalendar] = {}
    for product in sorted(shadows):
        signals = _normalise_signals(shadows[product], prefix)
        panel = bars.loc[bars["product"] == product].reset_index(drop=True)
        if panel.empty:
            raise _fail(prefix, "bars", f"{product!r} has no rows in the bundle")
        _check_alignment(signals, panel, product, prefix)
        signals_by_product[product] = signals
        calendars[product] = SessionCalendar.from_bars(panel)

    trade_dates = sorted(set(bars["trade_date"]))
    months = sorted({day.replace(day=1) for day in trade_dates})

    score_daily, score_trades = _score_inputs(shadows, prefix)
    selected_by_month: dict[date, tuple[str, ...]] = {}
    selection_rows: list[dict[str, object]] = []
    for month in months:
        scores = trailing_scores(
            month_start=month,
            daily=score_daily,
            trades=score_trades,
            observations=selection_observations,
        )
        eligible = set(spec.eligible(scores))
        pool = liquidity.get(month, set())
        selected = tuple(sorted(pool & eligible))
        selected_by_month[month] = selected
        for product in sorted(set(shadows) | pool):
            score = scores.get(product)
            if score is None:
                reason, is_eligible = "insufficient_history", False
            else:
                rejection = spec.rejection(score)
                is_eligible = product in eligible
                if (rejection is None) is not is_eligible:
                    raise _fail(
                        prefix,
                        "selection_policy",
                        f"{product!r}: eligibility disagrees with its stated reason",
                    )
                reason = rejection or (
                    "selected" if product in pool else "outside_liquidity_universe"
                )
            selection_rows.append(
                {
                    "month_start": month,
                    "product": product,
                    "in_liquidity_universe": product in pool,
                    "has_score": score is not None,
                    "observations": score.observations if score else 0,
                    "trade_count": score.trade_count if score else 0,
                    "cumulative_return": score.cumulative_return if score else np.nan,
                    "sharpe": score.sharpe if score else np.nan,
                    "calmar": score.calmar if score else np.nan,
                    "eligible": is_eligible,
                    "selected": product in selected,
                    "reason": reason,
                }
            )

    events = _build_events(
        signals_by_product=signals_by_product,
        calendars=calendars,
        rolls=rolls,
        shadows=shadows,
        spec=spec,
        prefix=prefix,
    )
    return _run_ledgers(
        bars=bars,
        signals_by_product=signals_by_product,
        events=events,
        trade_dates=trade_dates,
        selected_by_month=selected_by_month,
        selection_rows=selection_rows,
        spec=spec,
        target_vol=target_vol,
        cost_bps=cost_bps,
        realized_vol_min_observations=realized_vol_min_observations,
        prefix=prefix,
    )


def _build_events(
    *,
    signals_by_product: Mapping[str, pd.DataFrame],
    calendars: Mapping[str, SessionCalendar],
    rolls: pd.DataFrame,
    shadows: Mapping[str, Any],
    spec: StrategySpec,
    prefix: str,
) -> list[_Event]:
    events: list[_Event] = []
    sequence = 0

    def add(timestamp, priority, trade_date, kind, product, payload) -> None:
        nonlocal sequence
        sequence += 1
        events.append(
            _Event(
                timestamp=pd.Timestamp(timestamp),
                priority=priority,
                sequence=sequence,
                trade_date=trade_date,
                kind=kind,
                product=product,
                payload=payload,
            )
        )

    for product in sorted(signals_by_product):
        signals = signals_by_product[product]
        calendar = calendars[product]
        seen_months: set[date] = set()
        for row in signals.itertuples(index=False):
            if row.no_trade:
                continue
            close = _finite(row.raw_close, prefix, f"close[{product}]", positive=True)
            add(
                row.slot_end,
                _PRICE,
                row.trade_date,
                "price",
                product,
                {"contract": row.contract, "price": close},
            )

            month = row.trade_date.replace(day=1)
            priced = not pd.isna(row.fill_time) and not pd.isna(row.fill_price)
            if priced:
                execution_date = calendar.execution_trade_date(
                    row.fill_time, row.trade_date
                )
                if month not in seen_months:
                    seen_months.add(month)
                    add(
                        row.fill_time,
                        _ALIGN,
                        execution_date,
                        "align",
                        product,
                        {
                            "contract": row.contract,
                            "price": float(row.fill_price),
                            "month": month,
                        },
                    )
                elif spec.resize_on_allocation_change:
                    add(
                        row.fill_time,
                        _ALIGN,
                        execution_date,
                        "resize",
                        product,
                        {"contract": row.contract, "price": float(row.fill_price)},
                    )

            if not row.action_changed:
                continue
            if pd.isna(row.fill_time):
                raise _fail(
                    prefix,
                    "fill_time",
                    f"{product} {row.slot_end}: a requested trade has none",
                )
            if pd.isna(row.fill_price):
                raise _fail(
                    prefix,
                    "fill_price",
                    f"{product} {row.slot_end}: a requested trade has none",
                )
            price = _finite(row.fill_price, prefix, f"fill_price[{product}]", positive=True)
            _finite(row.multiplier, prefix, f"multiplier[{product}]", positive=True)
            direction = int(row.target_direction)
            exposure = None
            if direction:
                scale = 1.0
                if spec.scale_column is not None:
                    scale = _finite(
                        getattr(row, spec.scale_column), prefix, f"scale[{product}]"
                    )
                atr = _finite(row.atr_raw, prefix, f"atr[{product}]")
                if atr < 0.0:
                    raise _fail(prefix, "atr", f"{product}: expected a nonnegative ATR")
                exposure = _Exposure(
                    direction=direction, scale=scale, close=close, atr=atr
                )
            add(
                row.fill_time,
                _TARGET,
                calendars[product].execution_trade_date(row.fill_time, row.trade_date),
                "target",
                product,
                {
                    "contract": row.contract,
                    "price": price,
                    "exposure": exposure,
                    "reason": str(row.action),
                },
            )

    for row in rolls.itertuples(index=False):
        product = _text(row.product, prefix, "roll_product")
        if product not in shadows:
            continue
        add(
            pd.to_datetime(row.fill_time),
            _ROLL,
            _date_value(row.trade_date, prefix, "roll_trade_date"),
            "roll",
            product,
            {
                "old_contract": _text(row.old_contract, prefix, "roll_old_contract"),
                "new_contract": _text(row.new_contract, prefix, "roll_new_contract"),
                "old_price": _finite(row.old_price, prefix, "roll_old_price", positive=True),
                "new_price": _finite(row.new_price, prefix, "roll_new_price", positive=True),
            },
        )

    events.sort(key=lambda event: (event.timestamp, event.priority, event.sequence))
    return events


def _run_ledgers(
    *,
    bars: pd.DataFrame,
    signals_by_product: Mapping[str, pd.DataFrame],
    events: list[_Event],
    trade_dates: list[date],
    selected_by_month: Mapping[date, tuple[str, ...]],
    selection_rows: list[dict[str, object]],
    spec: StrategySpec,
    target_vol: float,
    cost_bps: float,
    realized_vol_min_observations: int,
    prefix: str,
) -> BacktestResult:
    real = EventAccount(cost_bps=cost_bps)
    prevol = EventAccount(cost_bps=cost_bps)
    real.initialize({})
    prevol.initialize({})

    state = _LedgerState(spec=spec, prefix=prefix, target_vol=target_vol)
    events_by_date: dict[date, list[_Event]] = defaultdict(list)
    for event in events:
        events_by_date[event.trade_date].append(event)
    slots_by_date = bars.groupby("trade_date")["slot_end"].max().to_dict()

    trade_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    month_vol: dict[date, float | None] = {}
    flat_volatility_months: set[date] = set()

    for day in sorted(set(trade_dates) | set(events_by_date)):
        month = day.replace(day=1)
        if month not in month_vol:
            month_vol[month] = monthly_realized_volatility(
                session_date=day,
                returns_by_date=state.prevol_returns,
                min_observations=realized_vol_min_observations,
            )
        realized_vol = month_vol[month]
        if realized_vol == 0.0:
            # 窗口内组合的收益一动没动 —— 那是"还没开始交易"，不是"波动率是零"。
            # 与观测数不足同样处理：本月不建仓，并记进 data_quality。硬失败留给
            # 真正病态的值（负数、非有限），那才是数据缺陷。
            flat_volatility_months.add(month)
            realized_vol = None
        selected = selected_by_month.get(month, ())
        previous_selected = set(
            selected_by_month.get(
                max((m for m in selected_by_month if m < month), default=None), ()
            )
        )

        day_events = events_by_date.get(day, [])
        cursor = 0
        while cursor < len(day_events):
            stamp = day_events[cursor].timestamp
            group: list[_Event] = []
            while cursor < len(day_events) and day_events[cursor].timestamp == stamp:
                group.append(day_events[cursor])
                cursor += 1
            state.apply_group(
                group=group,
                stamp=stamp,
                day=day,
                selected=selected,
                previous_selected=previous_selected,
                realized_vol=realized_vol,
                real=real,
                prevol=prevol,
                trade_rows=trade_rows,
            )

        close_stamp = _close_timestamp(
            day=day,
            last_slot=slots_by_date.get(day),
            accounts=(real, prevol),
            events=day_events,
            prefix=prefix,
        )
        real_row = _close_account(real, day, close_stamp, state.latest_price, prefix, "real")
        prevol_row = _close_account(
            prevol, day, close_stamp, state.latest_price, prefix, "prevol"
        )
        state.prevol_returns.append((day, prevol_row.net_return))

        multiplier = (
            target_vol / realized_vol if realized_vol not in (None, 0.0) else np.nan
        )
        daily_rows.append(
            {
                "trade_date": day,
                "month_start": month,
                "gross_return": real_row.gross_return,
                "turnover": real_row.turnover,
                "cost": real_row.cost,
                "direct_cost": real_row.direct_cost,
                "net_return": real_row.net_return,
                "gross_equity": real_row.gross_equity,
                "equity": real_row.equity,
                "gross_leverage": real_row.gross_leverage,
                "prevol_net_return": prevol_row.net_return,
                "prevol_equity": prevol_row.equity,
                "realized_vol": np.nan if realized_vol is None else realized_vol,
                "vol_multiplier": multiplier,
                "selected_count": len(selected),
            }
        )
        allocation = state.allocation_for(selected)
        for product in sorted(signals_by_product):
            exposure = state.exposures.get(product)
            position_rows.append(
                {
                    "trade_date": day,
                    "month_start": month,
                    "product": product,
                    "contract": state.bar_contract.get(product),
                    "selected": product in selected,
                    "active_products": allocation.active_products,
                    "universe_weight": float(allocation.sleeve.get(product, 0.0)),
                    "base_weight_abs": abs(float(allocation.signed.get(product, 0.0))),
                    "direction": exposure.direction if exposure else 0,
                    "oi_scale": exposure.scale if exposure else 0.0,
                    "atr_leverage": state.atr_leverage_of.get(product, np.nan),
                    "realized_vol": np.nan if realized_vol is None else realized_vol,
                    "target_annual_vol": target_vol if realized_vol is not None else np.nan,
                    "vol_multiplier": multiplier,
                    "leverage": state.leverage_of.get(product, np.nan),
                    "target_weight": state.real_weight.get(product, 0.0),
                    "actual_weight": state.real_weight.get(product, 0.0),
                    "prevol_weight": state.prevol_weight.get(product, 0.0),
                }
            )

    signals = pd.concat(
        [frame.assign(product=product) for product, frame in signals_by_product.items()],
        ignore_index=True,
    )
    signals["month_start"] = [day.replace(day=1) for day in signals["trade_date"]]
    signals["selected"] = [
        row.product in selected_by_month.get(row.month_start, ())
        for row in signals.itertuples(index=False)
    ]
    return BacktestResult(
        daily=pd.DataFrame(daily_rows, columns=list(DAILY_COLUMNS)),
        positions=pd.DataFrame(position_rows, columns=list(spec.position_columns)),
        trades=pd.DataFrame(trade_rows, columns=list(TRADE_COLUMNS)),
        signals=signals,
        selection=pd.DataFrame(selection_rows, columns=list(SELECTION_COLUMNS)),
        data_quality=_data_quality(
            signals_by_product, selection_rows, month_vol, flat_volatility_months
        ),
    )


class _LedgerState:
    """Mutable portfolio state threaded through the event stream."""

    def __init__(self, *, spec: StrategySpec, prefix: str, target_vol: float) -> None:
        self.spec = spec
        self.prefix = prefix
        self.target_vol = target_vol
        self.latest_price: dict[str, float] = {}
        self.bar_contract: dict[str, str] = {}
        self.held_contract: dict[str, str] = {}
        self.exposures: dict[str, _Exposure] = {}
        self.real_weight: dict[str, float] = {}
        self.prevol_weight: dict[str, float] = {}
        self.leverage_of: dict[str, float] = {}
        self.atr_leverage_of: dict[str, float] = {}
        self.prevol_returns: list[tuple[date, float]] = []
        self.pending_resize: set[str] = set()

    def allocation_for(self, selected: tuple[str, ...]) -> Allocation:
        directions = {
            product: float(self.exposures[product].direction)
            if product in self.exposures
            else 0.0
            for product in selected
        }
        return self.spec.allocate(selected, directions)

    def apply_group(
        self,
        *,
        group: list[_Event],
        stamp: pd.Timestamp,
        day: date,
        selected: tuple[str, ...],
        previous_selected: set[str],
        realized_vol: float | None,
        real: EventAccount,
        prevol: EventAccount,
        trade_rows: list[dict[str, object]],
    ) -> None:
        touched: dict[str, str] = {}
        rolled: dict[str, tuple[str, str]] = {}
        for event in group:
            payload = event.payload
            product = event.product
            if event.kind == "price":
                self.latest_price[payload["contract"]] = payload["price"]
                self.bar_contract[product] = payload["contract"]
                continue
            if event.kind == "roll":
                self.latest_price[payload["old_contract"]] = payload["old_price"]
                self.latest_price[payload["new_contract"]] = payload["new_price"]
                self.bar_contract[product] = payload["new_contract"]
                rolled[product] = (payload["old_contract"], payload["new_contract"])
                touched[product] = "roll"
                continue
            # A resize window the product does not need is not an event at
            # all: it must not even refresh the mark price, or two strategies
            # would close their day on different prices for the same bar.
            if event.kind == "resize" and product not in self.pending_resize:
                continue
            self.latest_price[payload["contract"]] = payload["price"]
            self.bar_contract[product] = payload["contract"]
            if event.kind == "target":
                exposure = payload["exposure"]
                if exposure is None:
                    self.exposures.pop(product, None)
                else:
                    self.exposures[product] = exposure
                touched[product] = payload["reason"]
            elif event.kind == "align":
                entering = product in selected and product not in previous_selected
                leaving = product not in selected and product in previous_selected
                touched.setdefault(
                    product,
                    "universe_entry"
                    if entering
                    else "universe_exit"
                    if leaving
                    else "universe_resize",
                )
            else:
                touched.setdefault(product, "allocation_resize")
                self.pending_resize.discard(product)

        if not touched:
            return

        allocation = self.allocation_for(selected)
        for product in touched:
            self._size(product, allocation, realized_vol)

        if self.spec.resize_on_allocation_change:
            # Everyone else's share moved too, but they can only trade at their
            # own next window -- never at this one's price.
            for product in sorted(set(allocation.signed) | set(self.real_weight)):
                if product in touched:
                    continue
                if self._desired(product, allocation, realized_vol) != (
                    self.real_weight.get(product, 0.0),
                    self.prevol_weight.get(product, 0.0),
                ):
                    self.pending_resize.add(product)

        reasons: dict[str, str] = {}
        for product, reason in touched.items():
            if product in rolled:
                old_contract, new_contract = rolled[product]
                reasons[old_contract] = "roll_old"
                reasons[new_contract] = "roll_new"
                continue
            reasons[self.bar_contract[product]] = reason
            previous = self.held_contract.get(product)
            if previous is not None and previous != self.bar_contract[product]:
                reasons[previous] = reason

        for account, weights, is_real in (
            (real, self.real_weight, True),
            (prevol, self.prevol_weight, False),
        ):
            targets = {
                self.bar_contract[product]: weight
                for product, weight in weights.items()
                if weight != 0.0
            }
            if targets == dict(account.weights):
                continue
            prices = self._event_prices(account, targets, stamp)
            event = account.rebalance(stamp, prices, targets, reasons)
            if not is_real:
                continue
            owner = {self.bar_contract[p]: p for p in self.bar_contract}
            for record in event.executions:
                trade_rows.append(
                    {
                        "timestamp": record.timestamp,
                        "trade_date": day,
                        "product": owner.get(
                            record.contract,
                            next(
                                (
                                    product
                                    for product, legs in rolled.items()
                                    if record.contract in legs
                                ),
                                None,
                            ),
                        ),
                        "contract": record.contract,
                        "reason": record.reason,
                        "price": record.price,
                        "old_weight": record.old_weight,
                        "new_weight": record.new_weight,
                        "weight_change": record.weight_change,
                        "turnover": record.turnover,
                        "cost": record.cost,
                    }
                )

        for product in touched:
            self.held_contract[product] = self.bar_contract[product]

    def _desired(
        self, product: str, allocation: Allocation, realized_vol: float | None
    ) -> tuple[float, float]:
        exposure = self.exposures.get(product)
        signed = float(allocation.signed.get(product, 0.0))
        if exposure is None or signed == 0.0:
            return 0.0, 0.0
        raw = atr_leverage(close=exposure.close, atr=exposure.atr)
        scaled = final_leverage(
            close=exposure.close,
            atr=exposure.atr,
            realized_vol=realized_vol,
            target_annual_vol=self.target_vol,
        )
        return signed * exposure.scale * scaled, signed * exposure.scale * raw

    def _size(
        self, product: str, allocation: Allocation, realized_vol: float | None
    ) -> None:
        exposure = self.exposures.get(product)
        signed = float(allocation.signed.get(product, 0.0))
        if exposure is None or signed == 0.0:
            self.leverage_of[product] = 0.0
            self.atr_leverage_of[product] = 0.0
            self.real_weight[product] = 0.0
            self.prevol_weight[product] = 0.0
            return
        self.atr_leverage_of[product] = atr_leverage(
            close=exposure.close, atr=exposure.atr
        )
        self.leverage_of[product] = final_leverage(
            close=exposure.close,
            atr=exposure.atr,
            realized_vol=realized_vol,
            target_annual_vol=self.target_vol,
        )
        real, prevol = self._desired(product, allocation, realized_vol)
        self.real_weight[product] = real
        self.prevol_weight[product] = prevol

    def _event_prices(
        self, account: EventAccount, targets: Mapping[str, float], stamp: pd.Timestamp
    ) -> dict[str, float]:
        needed = set(targets) | {
            contract for contract, weight in account.weights.items() if weight != 0.0
        }
        prices: dict[str, float] = {}
        for contract in sorted(needed):
            if contract not in self.latest_price:
                raise _fail(
                    self.prefix, "price", f"{contract} has no causal price at {stamp}"
                )
            prices[contract] = self.latest_price[contract]
        return prices


def _close_timestamp(
    *,
    day: date,
    last_slot: object,
    accounts: tuple[EventAccount, ...],
    events: list[_Event],
    prefix: str,
) -> pd.Timestamp:
    """Close a trade date just after its own last activity, never at midnight."""
    candidates: list[pd.Timestamp] = []
    if last_slot is not None and not pd.isna(last_slot):
        candidates.append(pd.Timestamp(last_slot))
    candidates.extend(event.timestamp for event in events)
    for account in accounts:
        if account.events:
            candidates.append(pd.Timestamp(account.events[-1].timestamp))
    if not candidates:
        raise _fail(prefix, "daily_activity", f"{day} has no activity to close on")
    stamp = max(candidates) + timedelta(microseconds=1)
    if stamp.date() != day:
        raise _fail(prefix, "daily_time", f"{day} activity left its calendar day")
    return stamp


def _close_account(
    account: EventAccount,
    day: date,
    stamp: pd.Timestamp,
    latest_price: Mapping[str, float],
    prefix: str,
    label: str,
) -> Any:
    prices: dict[str, float] = {}
    for contract, weight in account.weights.items():
        if weight == 0.0:
            continue
        if contract not in latest_price:
            raise _fail(
                prefix, "daily_price", f"{label} {contract} has no causal close price"
            )
        prices[contract] = latest_price[contract]
    account.mark_close(day, stamp, prices)
    return account.drain_daily_row(day, "close")


def _data_quality(
    signals_by_product: Mapping[str, pd.DataFrame],
    selection_rows: list[dict[str, object]],
    month_vol: Mapping[date, float | None],
    flat_volatility_months: set[date] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for product, frame in sorted(signals_by_product.items()):
        rows.append({"metric": "bars", "product": product, "value": float(len(frame))})
        rows.append(
            {
                "metric": "no_trade_bars",
                "product": product,
                "value": float(frame["no_trade"].sum()),
            }
        )
        for column in ("fill_pending", "fill_unpriceable"):
            if column in frame.columns:
                rows.append(
                    {
                        "metric": column,
                        "product": product,
                        "value": float(frame[column].sum()),
                    }
                )
        if "action" in frame.columns:
            # `fill_unpriceable` 数的是"这根 bar 的成交窗没人成交"；这里数的是
            # **策略真正想调仓却没成交**的次数（保真度 F11），两者差着数量级。
            rows.append(
                {
                    "metric": "signals_cancelled_by_unavailable_fill",
                    "product": product,
                    "value": float((frame["action"] == "fill_unavailable").sum()),
                }
            )
        if "pricing_basis" in frame.columns:
            counts = frame.loc[~frame["no_trade"], "pricing_basis"].value_counts()
            for basis, count in counts.items():
                rows.append(
                    {
                        "metric": f"pricing_basis:{basis}",
                        "product": product,
                        "value": float(count),
                    }
                )
    selected_months: dict[str, int] = defaultdict(int)
    for row in selection_rows:
        if row["selected"]:
            selected_months[str(row["product"])] += 1
    for product in sorted(signals_by_product):
        rows.append(
            {
                "metric": "selected_months",
                "product": product,
                "value": float(selected_months.get(product, 0)),
            }
        )
    rows.append(
        {
            "metric": "months_without_volatility_multiplier",
            "product": None,
            "value": float(sum(1 for value in month_vol.values() if value is None)),
        }
    )
    rows.append(
        {
            "metric": "months_with_flat_volatility_window",
            "product": None,
            "value": float(len(flat_volatility_months or ())),
        }
    )
    return pd.DataFrame(rows, columns=list(QUALITY_COLUMNS))
