"""The selected Bollinger portfolio: monthly universe, volatility, execution.

The shadow ledger answers "what would this product do on its own". This module
answers the portfolio question the paper actually reports, and the order of the
two is the whole point (design §5.4-§5.5): the dynamic strategy runs first, the
monthly product filter reads only finished shadow history, and the realized
volatility multiplier is applied last, on top of a selection that never saw it.

Capital is split across the *whole* selected universe, so a selected product
that happens to be flat leaves its sleeve in cash rather than handing it to the
products that do have a signal (fidelity rule B3). That is why the multiplier
cannot be derived from the shadow ledger: it needs the return series of the
selected portfolio *before* the multiplier, which is a second ledger run in
parallel here. Deriving it from a ledger that already carried the multiplier
would feed the multiplier its own output.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.panel import SessionCalendar
from common.commodity.portfolio import fixed_universe_weights
from common.commodity.selection import trailing_scores
from common.leverage import atr_leverage, final_leverage, monthly_realized_volatility
from common.minute.account import EventAccount
from cta_bollinger.selection import eligible_products
from cta_bollinger.shadow import ShadowResult

__all__ = ["BacktestResult", "TARGET_ANNUAL_VOL", "run_backtest"]


#: 研报 §5.5：组合年化波动目标 10%（道氏那篇是 15%，所以这里不是共享常量）。
TARGET_ANNUAL_VOL = 0.10
#: 手续费 0.3‱ + 冲击 1‱。
COST_BPS = 1.3
SELECTION_OBSERVATIONS = 252
VOL_OBSERVATIONS = 252

_MIN_TRADES = 5

_ALIGNMENT_COLUMNS = ("slot_end", "contract", "no_trade", "multiplier")

_POSITION_COLUMNS = (
    "trade_date",
    "month_start",
    "product",
    "contract",
    "selected",
    "universe_weight",
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
_DAILY_COLUMNS = (
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
_TRADE_COLUMNS = (
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
_SELECTION_COLUMNS = (
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
_QUALITY_COLUMNS = ("metric", "product", "value")

_PRICE, _ROLL, _ALIGN, _TARGET = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Every sheet the Bollinger report is allowed to draw from."""

    daily: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    selection: pd.DataFrame
    data_quality: pd.DataFrame


@dataclass(frozen=True, slots=True)
class _Exposure:
    """What the shadow froze at an entry, in the units the portfolio rescales."""

    direction: int
    oi_scale: float
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


def _fail(label: str, detail: str) -> ValueError:
    return ValueError(f"bollinger_backtest_{label}: {detail}")


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise _fail(label, "expected a finite numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise _fail(label, "expected a finite numeric value")
    if positive and numeric <= 0.0:
        raise _fail(label, "expected a finite positive value")
    return numeric


def _date_value(value: object, label: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise _fail(label, "expected a timezone-naive date")
        if value.time() != datetime.min.time():
            raise _fail(label, "expected a midnight date")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp != stamp.normalize():
            raise _fail(label, "expected a midnight date")
        return stamp.date()
    raise _fail(label, "expected date values")


def _text(value: object, label: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise _fail(label, "expected a nonempty string")
    result = str(value)
    if not result:
        raise _fail(label, "expected a nonempty string")
    return result


def _require_frame(value: object, *, label: str, required: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise _fail(label, "expected a DataFrame")
    missing = [column for column in required if column not in value.columns]
    if missing:
        raise _fail(f"{label}_columns", f"missing={missing!r}")
    return value


def _normalise_bars(frame: pd.DataFrame) -> pd.DataFrame:
    bars = frame.copy()
    bars["product"] = [_text(value, "bars_product") for value in bars["product"]]
    bars["contract"] = [_text(value, "bars_contract") for value in bars["contract"]]
    bars["trade_date"] = [
        _date_value(value, "bars_trade_date") for value in bars["trade_date"]
    ]
    for column in ("slot_end", "fill_time"):
        converted = pd.to_datetime(bars[column])
        if not isinstance(converted.dtype, pd.DatetimeTZDtype):
            raise _fail(f"bars_{column}", "expected timezone-aware timestamps")
        bars[column] = converted
    return bars.sort_values(["product", "slot_end"], kind="stable").reset_index(drop=True)


def _normalise_signals(shadow: ShadowResult) -> pd.DataFrame:
    signals = _require_frame(
        shadow.signals,
        label="signals",
        required=(
            "product",
            "trade_date",
            "slot_end",
            "contract",
            "no_trade",
            "raw_close",
            "atr_raw",
            "state_oi_scale",
            "target_direction",
            "action",
            "action_changed",
            "fill_time",
            "fill_price",
            "multiplier",
        ),
    ).copy()
    signals["product"] = [_text(value, "signals_product") for value in signals["product"]]
    signals["contract"] = [
        _text(value, "signals_contract") for value in signals["contract"]
    ]
    signals["trade_date"] = [
        _date_value(value, "signals_trade_date") for value in signals["trade_date"]
    ]
    for column in ("slot_end", "fill_time"):
        signals[column] = pd.to_datetime(signals[column])
    return signals.sort_values("slot_end", kind="stable").reset_index(drop=True)


def _check_alignment(signals: pd.DataFrame, panel: pd.DataFrame, product: str) -> None:
    """The shadow must be the one this bundle produced, not a lookalike.

    Only structural facts are compared here. A null fill price is left to the
    target gate below, which can say *which* requested trade has no price.
    """
    if len(signals) != len(panel):
        raise _fail(
            "alignment", f"{product}: {len(signals)} signal rows vs {len(panel)} bars"
        )
    for column in _ALIGNMENT_COLUMNS:
        left = signals[column].to_numpy()
        right = panel[column].to_numpy()
        if not np.array_equal(left, right):
            raise _fail("alignment", f"{product}: {column} differs from the bundle")
    for column in ("fill_time", "fill_price"):
        left = signals[column]
        right = panel[column]
        both = left.notna() & right.notna()
        if not left.loc[both].equals(right.loc[both]):
            raise _fail("alignment", f"{product}: {column} differs from the bundle")


def _liquidity_universe(frame: pd.DataFrame) -> dict[date, set[str]]:
    universe: dict[date, set[str]] = defaultdict(set)
    for row in frame.itertuples(index=False):
        month = _date_value(row.month_start, "universes_month_start")
        if month.day != 1:
            raise _fail("universes_month_start", f"{month} is not a month start")
        universe[month].add(_text(row.product, "universes_product"))
    return dict(universe)


def _score_inputs(
    shadows: Mapping[str, ShadowResult],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.concat(
        [
            _require_frame(
                shadow.daily,
                label="shadow_daily",
                required=("product", "trade_date", "net_return"),
            )[["product", "trade_date", "net_return"]]
            for shadow in shadows.values()
        ],
        ignore_index=True,
    )
    trade_frames = []
    for shadow in shadows.values():
        frame = _require_frame(
            shadow.trades, label="shadow_trades", required=("product", "exit_date")
        )
        columns = [
            column
            for column in ("product", "exit_date", "trade_id")
            if column in frame.columns
        ]
        trade_frames.append(frame[columns])
    return daily, pd.concat(trade_frames, ignore_index=True)


def _selection_reason(
    *, in_pool: bool, score: object, eligible: bool
) -> tuple[str, bool]:
    if score is None:
        return "insufficient_history", False
    if getattr(score, "trade_count") < _MIN_TRADES:
        return "too_few_trades", False
    if score.sharpe < 0.0 and score.calmar < 0.0:
        return "negative_risk_adjusted", False
    if not in_pool:
        return "outside_liquidity_universe", eligible
    return "selected", eligible


def _sleeve_weights(
    selected: tuple[str, ...], exposures: Mapping[str, _Exposure]
) -> dict[str, float]:
    directions = {
        product: float(exposures[product].direction) if product in exposures else 0.0
        for product in selected
    }
    return fixed_universe_weights(directions, universe=selected)


def run_backtest(
    *,
    bundle: Any,
    shadows: Mapping[str, ShadowResult],
    target_vol: float = TARGET_ANNUAL_VOL,
    cost_bps: float = COST_BPS,
    realized_vol_min_observations: int = VOL_OBSERVATIONS,
    selection_observations: int = SELECTION_OBSERVATIONS,
) -> BacktestResult:
    """Run the selected portfolio over one panel bundle and its shadow ledgers."""
    if not isinstance(shadows, Mapping) or not shadows:
        raise _fail("shadows", "expected a nonempty mapping of ShadowResult")
    for product, shadow in shadows.items():
        if not isinstance(shadow, ShadowResult):
            raise _fail("shadows", f"{product!r} is not a ShadowResult")
        if shadow.product != product:
            raise _fail("shadows", f"{product!r} does not match {shadow.product!r}")
    target_vol = _finite(target_vol, "target_vol", positive=True)
    cost_bps = _finite(cost_bps, "cost_bps")
    if cost_bps < 0.0:
        raise _fail("cost_bps", "expected a nonnegative value")

    bars = _normalise_bars(
        _require_frame(
            getattr(bundle, "bars", None),
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
        )
    )
    liquidity = _liquidity_universe(
        _require_frame(
            getattr(bundle, "universes", None),
            label="universes",
            required=("month_start", "product"),
        )
    )
    rolls = _require_frame(
        getattr(bundle, "roll_fills", None),
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
        signals = _normalise_signals(shadows[product])
        panel = bars.loc[bars["product"] == product].reset_index(drop=True)
        if panel.empty:
            raise _fail("bars", f"{product!r} has no rows in the bundle")
        _check_alignment(signals, panel, product)
        signals_by_product[product] = signals
        calendars[product] = SessionCalendar.from_bars(panel)

    trade_dates = sorted({day for day in bars["trade_date"]})
    months = sorted({day.replace(day=1) for day in trade_dates})

    score_daily, score_trades = _score_inputs(shadows)
    selected_by_month: dict[date, tuple[str, ...]] = {}
    selection_rows: list[dict[str, object]] = []
    for month in months:
        scores = trailing_scores(
            month_start=month,
            daily=score_daily,
            trades=score_trades,
            observations=selection_observations,
        )
        eligible = set(eligible_products(scores))
        pool = liquidity.get(month, set())
        selected = tuple(sorted(pool & eligible))
        selected_by_month[month] = selected
        for product in sorted(set(shadows) | pool):
            score = scores.get(product)
            reason, is_eligible = _selection_reason(
                in_pool=product in pool, score=score, eligible=product in eligible
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
    )
    return _run_ledgers(
        bars=bars,
        signals_by_product=signals_by_product,
        events=events,
        trade_dates=trade_dates,
        selected_by_month=selected_by_month,
        selection_rows=selection_rows,
        target_vol=target_vol,
        cost_bps=cost_bps,
        realized_vol_min_observations=realized_vol_min_observations,
    )


def _build_events(
    *,
    signals_by_product: Mapping[str, pd.DataFrame],
    calendars: Mapping[str, SessionCalendar],
    rolls: pd.DataFrame,
    shadows: Mapping[str, ShadowResult],
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
            close = _finite(row.raw_close, f"close[{product}]", positive=True)
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
            if month not in seen_months and priced:
                seen_months.add(month)
                add(
                    row.fill_time,
                    _ALIGN,
                    calendar.execution_trade_date(row.fill_time, row.trade_date),
                    "align",
                    product,
                    {
                        "contract": row.contract,
                        "price": float(row.fill_price),
                        "month": month,
                    },
                )

            if not row.action_changed:
                continue
            if pd.isna(row.fill_time):
                raise _fail(
                    "fill_time", f"{product} {row.slot_end}: a requested trade has none"
                )
            if pd.isna(row.fill_price):
                raise _fail(
                    "fill_price", f"{product} {row.slot_end}: a requested trade has none"
                )
            price = _finite(row.fill_price, f"fill_price[{product}]", positive=True)
            _finite(row.multiplier, f"multiplier[{product}]", positive=True)
            direction = int(row.target_direction)
            exposure = None
            if direction:
                exposure = _Exposure(
                    direction=direction,
                    oi_scale=_finite(row.state_oi_scale, f"oi_scale[{product}]"),
                    close=close,
                    atr=_finite(row.atr_raw, f"atr[{product}]"),
                )
                if exposure.atr < 0.0:
                    raise _fail("atr", f"{product}: expected a nonnegative ATR")
            add(
                row.fill_time,
                _TARGET,
                calendar.execution_trade_date(row.fill_time, row.trade_date),
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
        product = _text(row.product, "roll_product")
        if product not in shadows:
            continue
        add(
            pd.to_datetime(row.fill_time),
            _ROLL,
            _date_value(row.trade_date, "roll_trade_date"),
            "roll",
            product,
            {
                "old_contract": _text(row.old_contract, "roll_old_contract"),
                "new_contract": _text(row.new_contract, "roll_new_contract"),
                "old_price": _finite(row.old_price, "roll_old_price", positive=True),
                "new_price": _finite(row.new_price, "roll_new_price", positive=True),
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
    target_vol: float,
    cost_bps: float,
    realized_vol_min_observations: int,
) -> BacktestResult:
    real = EventAccount(cost_bps=cost_bps)
    prevol = EventAccount(cost_bps=cost_bps)
    real.initialize({})
    prevol.initialize({})

    latest_price: dict[str, float] = {}
    bar_contract: dict[str, str] = {}
    held_contract: dict[str, str] = {}
    exposures: dict[str, _Exposure] = {}
    real_weight: dict[str, float] = {}
    prevol_weight: dict[str, float] = {}
    leverage_of: dict[str, float] = {}
    atr_leverage_of: dict[str, float] = {}
    prevol_returns: list[tuple[date, float]] = []
    month_vol: dict[date, float | None] = {}

    events_by_date: dict[date, list[_Event]] = defaultdict(list)
    for event in events:
        events_by_date[event.trade_date].append(event)
    slots_by_date = bars.groupby("trade_date")["slot_end"].max().to_dict()

    trade_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []

    ledger_days = sorted(set(trade_dates) | set(events_by_date))
    for day in ledger_days:
        month = day.replace(day=1)
        if month not in month_vol:
            month_vol[month] = monthly_realized_volatility(
                session_date=day,
                returns_by_date=prevol_returns,
                min_observations=realized_vol_min_observations,
            )
        realized_vol = month_vol[month]
        selected = selected_by_month.get(month, ())
        previous_selected = set(
            selected_by_month.get(
                max((m for m in selected_by_month if m < month), default=None), ()
            )
        )
        unit = 1.0 / len(selected) if selected else 0.0

        day_events = events_by_date.get(day, [])
        position = 0
        while position < len(day_events):
            stamp = day_events[position].timestamp
            group: list[_Event] = []
            while position < len(day_events) and day_events[position].timestamp == stamp:
                group.append(day_events[position])
                position += 1
            _apply_group(
                group=group,
                stamp=stamp,
                day=day,
                selected=selected,
                previous_selected=previous_selected,
                unit=unit,
                realized_vol=realized_vol,
                target_vol=target_vol,
                real=real,
                prevol=prevol,
                latest_price=latest_price,
                bar_contract=bar_contract,
                held_contract=held_contract,
                exposures=exposures,
                real_weight=real_weight,
                prevol_weight=prevol_weight,
                leverage_of=leverage_of,
                atr_leverage_of=atr_leverage_of,
                trade_rows=trade_rows,
            )

        close_stamp = _close_timestamp(
            day=day,
            last_slot=slots_by_date.get(day),
            accounts=(real, prevol),
            events=day_events,
        )
        real_row = _close_account(real, day, close_stamp, latest_price, "real")
        prevol_row = _close_account(prevol, day, close_stamp, latest_price, "prevol")
        prevol_returns.append((day, prevol_row.net_return))

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
        for product in sorted(signals_by_product):
            exposure = exposures.get(product)
            position_rows.append(
                {
                    "trade_date": day,
                    "month_start": month,
                    "product": product,
                    "contract": bar_contract.get(product),
                    "selected": product in selected,
                    "universe_weight": unit if product in selected else 0.0,
                    "direction": exposure.direction if exposure else 0,
                    "oi_scale": exposure.oi_scale if exposure else 0.0,
                    "atr_leverage": atr_leverage_of.get(product, np.nan),
                    "realized_vol": np.nan if realized_vol is None else realized_vol,
                    "target_annual_vol": (
                        target_vol if realized_vol is not None else np.nan
                    ),
                    "vol_multiplier": multiplier,
                    "leverage": leverage_of.get(product, np.nan),
                    "target_weight": real_weight.get(product, 0.0),
                    "actual_weight": real_weight.get(product, 0.0),
                    "prevol_weight": prevol_weight.get(product, 0.0),
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
        daily=pd.DataFrame(daily_rows, columns=list(_DAILY_COLUMNS)),
        positions=pd.DataFrame(position_rows, columns=list(_POSITION_COLUMNS)),
        trades=pd.DataFrame(trade_rows, columns=list(_TRADE_COLUMNS)),
        signals=signals,
        selection=pd.DataFrame(selection_rows, columns=list(_SELECTION_COLUMNS)),
        data_quality=_data_quality(signals_by_product, selection_rows, month_vol),
    )


def _apply_group(
    *,
    group: list[_Event],
    stamp: pd.Timestamp,
    day: date,
    selected: tuple[str, ...],
    previous_selected: set[str],
    unit: float,
    realized_vol: float | None,
    target_vol: float,
    real: EventAccount,
    prevol: EventAccount,
    latest_price: dict[str, float],
    bar_contract: dict[str, str],
    held_contract: dict[str, str],
    exposures: dict[str, _Exposure],
    real_weight: dict[str, float],
    prevol_weight: dict[str, float],
    leverage_of: dict[str, float],
    atr_leverage_of: dict[str, float],
    trade_rows: list[dict[str, object]],
) -> None:
    touched: dict[str, str] = {}
    rolled: dict[str, tuple[str, str]] = {}
    for event in group:
        payload = event.payload
        product = event.product
        if event.kind == "price":
            latest_price[payload["contract"]] = payload["price"]
            bar_contract[product] = payload["contract"]
            continue
        if event.kind == "roll":
            latest_price[payload["old_contract"]] = payload["old_price"]
            latest_price[payload["new_contract"]] = payload["new_price"]
            bar_contract[product] = payload["new_contract"]
            rolled[product] = (payload["old_contract"], payload["new_contract"])
            touched[product] = "roll"
            continue
        latest_price[payload["contract"]] = payload["price"]
        bar_contract[product] = payload["contract"]
        if event.kind == "target":
            exposure = payload["exposure"]
            if exposure is None:
                exposures.pop(product, None)
            else:
                exposures[product] = exposure
            touched[product] = payload["reason"]
        else:
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

    if not touched:
        return

    sleeve = _sleeve_weights(selected, exposures)
    for product in touched:
        exposure = exposures.get(product)
        signed = sleeve.get(product, 0.0)
        if exposure is None or signed == 0.0:
            leverage_of[product] = 0.0
            atr_leverage_of[product] = 0.0
            real_weight[product] = 0.0
            prevol_weight[product] = 0.0
            continue
        raw = atr_leverage(close=exposure.close, atr=exposure.atr)
        scaled = final_leverage(
            close=exposure.close,
            atr=exposure.atr,
            realized_vol=realized_vol,
            target_annual_vol=target_vol,
        )
        atr_leverage_of[product] = raw
        leverage_of[product] = scaled
        real_weight[product] = signed * exposure.oi_scale * scaled
        prevol_weight[product] = signed * exposure.oi_scale * raw

    reasons: dict[str, str] = {}
    for product, reason in touched.items():
        if product in rolled:
            old_contract, new_contract = rolled[product]
            reasons[old_contract] = "roll_old"
            reasons[new_contract] = "roll_new"
            continue
        reasons[bar_contract[product]] = reason
        previous = held_contract.get(product)
        if previous is not None and previous != bar_contract[product]:
            reasons[previous] = reason

    for account, weights, label in (
        (real, real_weight, "real"),
        (prevol, prevol_weight, "prevol"),
    ):
        targets = {
            bar_contract[product]: weight
            for product, weight in weights.items()
            if weight != 0.0
        }
        if targets == {
            contract: weight for contract, weight in account.weights.items()
        }:
            continue
        prices = _event_prices(account, targets, latest_price, stamp)
        event = account.rebalance(stamp, prices, targets, reasons)
        if label != "real":
            continue
        contract_product = {
            bar_contract[product]: product for product in bar_contract
        }
        for record in event.executions:
            trade_rows.append(
                {
                    "timestamp": record.timestamp,
                    "trade_date": day,
                    "product": contract_product.get(
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
        held_contract[product] = bar_contract[product]


def _event_prices(
    account: EventAccount,
    targets: Mapping[str, float],
    latest_price: Mapping[str, float],
    stamp: pd.Timestamp,
) -> dict[str, float]:
    needed = set(targets) | {
        contract for contract, weight in account.weights.items() if weight != 0.0
    }
    prices: dict[str, float] = {}
    for contract in sorted(needed):
        if contract not in latest_price:
            raise _fail("price", f"{contract} has no causal price at {stamp}")
        prices[contract] = latest_price[contract]
    return prices


def _close_timestamp(
    *,
    day: date,
    last_slot: object,
    accounts: tuple[EventAccount, ...],
    events: list[_Event],
) -> pd.Timestamp:
    """Close a trade date just after its own last activity, never at midnight.

    Same reason as the shadow ledger: a night session is stamped on the
    previous calendar evening, so a calendar-end stamp lands after the next
    trade date's first bars.
    """
    candidates: list[pd.Timestamp] = []
    if last_slot is not None and not pd.isna(last_slot):
        candidates.append(pd.Timestamp(last_slot))
    candidates.extend(event.timestamp for event in events)
    for account in accounts:
        if account.events:
            candidates.append(pd.Timestamp(account.events[-1].timestamp))
    if not candidates:
        raise _fail("daily_activity", f"{day} has no activity to close on")
    stamp = max(candidates) + timedelta(microseconds=1)
    if stamp.date() != day:
        raise _fail("daily_time", f"{day} activity left its calendar day")
    return stamp


def _close_account(
    account: EventAccount,
    day: date,
    stamp: pd.Timestamp,
    latest_price: Mapping[str, float],
    label: str,
) -> Any:
    prices: dict[str, float] = {}
    for contract, weight in account.weights.items():
        if weight == 0.0:
            continue
        if contract not in latest_price:
            raise _fail("daily_price", f"{label} {contract} has no causal close price")
        prices[contract] = latest_price[contract]
    account.mark_close(day, stamp, prices)
    return account.drain_daily_row(day, "close")


def _data_quality(
    signals_by_product: Mapping[str, pd.DataFrame],
    selection_rows: list[dict[str, object]],
    month_vol: Mapping[date, float | None],
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
    selected_months = defaultdict(int)
    for row in selection_rows:
        if row["selected"]:
            selected_months[row["product"]] += 1
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
    return pd.DataFrame(rows, columns=list(_QUALITY_COLUMNS))
