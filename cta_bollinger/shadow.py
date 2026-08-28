"""Causal one-product Bollinger shadow strategy and execution ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import heapq
import math
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.indicators import atr_series
from common.commodity.panel import SessionCalendar
from common.leverage import atr_leverage
from common.minute.account import EventAccount
from cta_bollinger.indicators import bands, oi_multiplier, rolling_oi
from cta_bollinger.signals import Position, State, step

__all__ = ["ShadowResult", "run_shadow_product"]


_BAR_COLUMNS = (
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
    "fill_time",
    "fill_price",
    "fill_pending",
    "fill_unpriceable",
    "pricing_basis",
    "multiplier",
)

_ROLL_COLUMNS = (
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

_TRADE_COLUMNS = (
    "product",
    "trade_id",
    "entry_date",
    "exit_date",
    "entry_time",
    "exit_time",
    "entry_contract",
    "exit_contract",
    "entry_price",
    "exit_price",
    "direction",
    "oi_scale",
    "target_magnitude",
    "entry_signal_close",
    "exit_signal_close",
    "exit_reason",
    "roll_count",
    "gross_return",
    "cost",
    "net_return",
)

_DAILY_COLUMNS = (
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


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """One product's copied signal, logical-trade, and daily result frames."""

    product: str
    signals: pd.DataFrame
    trades: pd.DataFrame
    daily: pd.DataFrame

    def __post_init__(self) -> None:
        if not isinstance(self.product, str) or not self.product:
            raise ValueError("bollinger_shadow_product: expected nonempty string")
        for name in ("signals", "trades", "daily"):
            frame = getattr(self, name)
            if not isinstance(frame, pd.DataFrame):
                raise ValueError(f"bollinger_shadow_{name}: expected DataFrame")
            object.__setattr__(self, name, frame.copy(deep=True))


def _required_columns(
    frame: pd.DataFrame, required: tuple[str, ...], label: str
) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"bollinger_shadow_{label}_columns: missing={missing!r}")


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"bollinger_shadow_{label}: expected finite numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"bollinger_shadow_{label}: expected finite numeric value")
    if positive and numeric <= 0.0:
        raise ValueError(f"bollinger_shadow_{label}: expected finite positive value")
    return numeric


def _date_value(value: object, label: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError(f"bollinger_shadow_{label}: expected timezone-naive date")
        if value.time() != time.min:
            raise ValueError(f"bollinger_shadow_{label}: expected midnight date")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp != timestamp.normalize():
            raise ValueError(f"bollinger_shadow_{label}: expected midnight date")
        return timestamp.date()
    raise ValueError(f"bollinger_shadow_{label}: expected date values")


def _aware_timestamps(
    series: pd.Series, label: str, *, allow_missing: bool
) -> pd.Series:
    if not allow_missing and series.isna().any():
        raise ValueError(f"bollinger_shadow_{label}: timestamp is required")
    try:
        converted = pd.to_datetime(series, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bollinger_shadow_{label}: invalid timestamp") from exc
    if not isinstance(converted.dtype, pd.DatetimeTZDtype):
        raise ValueError(
            f"bollinger_shadow_{label}: expected timezone-aware timestamps"
        )
    return converted


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"bollinger_shadow_{label}: expected nonempty string")
    return value


def _prepare_bars(value: object, product: str) -> tuple[pd.DataFrame, object | None]:
    embedded_rolls: object | None = None
    if isinstance(value, pd.DataFrame):
        source = value
    else:
        source = getattr(value, "bars", None)
        embedded_rolls = getattr(value, "roll_fills", None)
    if not isinstance(source, pd.DataFrame):
        raise ValueError("bollinger_shadow_bars: expected DataFrame or bundle")
    _required_columns(source, _BAR_COLUMNS, "bars")

    frame = source.loc[source["product"] == product, list(_BAR_COLUMNS)].copy()
    if frame.empty:
        raise ValueError(f"bollinger_shadow_product: no rows for {product!r}")
    frame["slot_end"] = _aware_timestamps(
        frame["slot_end"], "slot_end", allow_missing=False
    )
    frame["fill_time"] = _aware_timestamps(
        frame["fill_time"], "fill_time", allow_missing=True
    )
    if frame.duplicated(["product", "slot_end"]).any():
        raise ValueError("bollinger_shadow_duplicate: duplicate (product, slot_end)")
    frame = frame.sort_values("slot_end", kind="stable").reset_index(drop=True)
    frame["trade_date"] = [
        _date_value(value, "trade_date") for value in frame["trade_date"]
    ]

    for column in ("no_trade", "fill_pending", "fill_unpriceable"):
        if not frame[column].map(lambda item: isinstance(item, (bool, np.bool_))).all():
            raise ValueError(f"bollinger_shadow_{column}: expected boolean values")
        frame[column] = frame[column].astype(bool)
    for row in frame.itertuples(index=False):
        _finite(row.multiplier, "multiplier", positive=True)
        _finite(row.adj_factor, "adj_factor", positive=True)

    traded = frame.loc[~frame["no_trade"]]
    for row in traded.itertuples(index=False):
        _nonempty_string(row.contract, "contract")
        _nonempty_string(row.pricing_basis, "pricing_basis")
        for column in ("open", "high", "low", "close", "volume", "open_interest"):
            _finite(getattr(row, column), column)
        if row.fill_time is not pd.NaT and not pd.isna(row.fill_time):
            if row.fill_time <= row.slot_end:
                raise ValueError(
                    "bollinger_shadow_fill_time: fill must be after signal"
                )
        if not pd.isna(row.fill_price):
            _finite(row.fill_price, "fill_price", positive=True)
        elif not row.fill_pending and not row.fill_unpriceable:
            raise ValueError(
                "bollinger_shadow_fill_price: expected finite numeric value"
            )
        if pd.isna(row.fill_time) and not row.fill_pending and not row.fill_unpriceable:
            raise ValueError("bollinger_shadow_fill_time: timestamp is required")
    return frame, embedded_rolls


def _prepare_rolls(value: object | None, product: str) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame(columns=_ROLL_COLUMNS)
    if not isinstance(value, pd.DataFrame):
        raise ValueError("bollinger_shadow_roll_fills: expected DataFrame")
    if value.empty:
        return pd.DataFrame(columns=_ROLL_COLUMNS)
    _required_columns(value, _ROLL_COLUMNS, "roll_fills")
    frame = value.loc[value["product"] == product, list(_ROLL_COLUMNS)].copy()
    if frame.empty:
        return frame
    frame["trade_date"] = [
        _date_value(item, "roll_trade_date") for item in frame["trade_date"]
    ]
    frame["fill_time"] = _aware_timestamps(
        frame["fill_time"], "roll_fill_time", allow_missing=False
    )
    if frame.duplicated(["trade_date", "product"]).any():
        raise ValueError("bollinger_shadow_roll_duplicate: one roll per product-day")
    for row in frame.itertuples(index=False):
        old_contract = _nonempty_string(row.old_contract, "roll_old_contract")
        new_contract = _nonempty_string(row.new_contract, "roll_new_contract")
        if old_contract == new_contract:
            raise ValueError("bollinger_shadow_roll_legs: old and new must differ")
        _nonempty_string(row.old_pricing_basis, "roll_old_pricing_basis")
        _nonempty_string(row.new_pricing_basis, "roll_new_pricing_basis")
        _finite(row.old_price, "roll_old_price", positive=True)
        _finite(row.new_price, "roll_new_price", positive=True)
    return frame.sort_values("fill_time", kind="stable").reset_index(drop=True)


def _blank_signal(row: Any) -> dict[str, object]:
    return {
        "product": str(row.product),
        "trade_date": row.trade_date,
        "slot_end": row.slot_end,
        "contract": str(row.contract),
        "no_trade": bool(row.no_trade),
        "raw_open": row.open,
        "raw_high": row.high,
        "raw_low": row.low,
        "raw_close": row.close,
        "adj_factor": row.adj_factor,
        "signal_open": np.nan,
        "signal_high": np.nan,
        "signal_low": np.nan,
        "signal_close": np.nan,
        "middle": np.nan,
        "std": np.nan,
        "upper": np.nan,
        "lower": np.nan,
        "atr_adjusted": np.nan,
        "atr_raw": np.nan,
        "oi_short": np.nan,
        "oi_long": np.nan,
        "oi_scale_input": np.nan,
        "state_position": Position.FLAT.value,
        "state_take_profit": np.nan,
        "state_oi_scale": 0.0,
        "action": "no_trade" if row.no_trade else "warmup",
        "action_changed": False,
        "target_direction": 0,
        "target_magnitude": 0.0,
        "target_weight": 0.0,
        "fill_time": row.fill_time,
        "fill_price": row.fill_price,
        "fill_pending": bool(row.fill_pending),
        "fill_unpriceable": bool(row.fill_unpriceable),
        "pricing_basis": row.pricing_basis,
        "multiplier": row.multiplier,
        "roll_fill_time": pd.NaT,
        "roll_old_contract": None,
        "roll_new_contract": None,
        "roll_old_price": np.nan,
        "roll_new_price": np.nan,
        "roll_old_pricing_basis": None,
        "roll_new_pricing_basis": None,
        "roll_execution_count": 0,
        "roll_turnover": 0.0,
        "roll_cost": 0.0,
    }


def run_shadow_product(
    bars: object,
    *,
    product: str,
    roll_fills: pd.DataFrame | None = None,
    band_length: int = 300,
    atr_window: int = 20,
    oi_short: int = 150,
    oi_long: int = 300,
    beta: float = 1.5,
    ddof: int = 0,
    cost_bps: float = 1.3,
) -> ShadowResult:
    """Run one product without the portfolio-level volatility multiplier."""
    _nonempty_string(product, "product")
    if type(atr_window) is not int or atr_window < 1:
        raise ValueError("bollinger_shadow_atr_window: expected positive integer")
    frame, embedded_rolls = _prepare_bars(bars, product)
    rolls = _prepare_rolls(
        roll_fills if roll_fills is not None else embedded_rolls,
        product,
    )

    traded = frame.loc[~frame["no_trade"]].copy()
    signal_rows = [_blank_signal(row) for row in frame.itertuples(index=False)]
    if traded.empty:
        daily = pd.DataFrame(
            (
                {
                    "product": product,
                    "trade_date": day,
                    **{column: 0.0 for column in _DAILY_COLUMNS[2:]},
                    "gross_equity": 1.0,
                    "equity": 1.0,
                }
                for day in sorted(frame["trade_date"].unique())
            ),
            columns=_DAILY_COLUMNS,
        )
        return ShadowResult(
            product,
            pd.DataFrame(signal_rows),
            pd.DataFrame(columns=_TRADE_COLUMNS),
            daily,
        )

    adjusted_open = traded["open"].to_numpy(dtype="float64") * traded[
        "adj_factor"
    ].to_numpy(dtype="float64")
    adjusted_high = traded["high"].to_numpy(dtype="float64") * traded[
        "adj_factor"
    ].to_numpy(dtype="float64")
    adjusted_low = traded["low"].to_numpy(dtype="float64") * traded[
        "adj_factor"
    ].to_numpy(dtype="float64")
    adjusted_close = traded["close"].to_numpy(dtype="float64") * traded[
        "adj_factor"
    ].to_numpy(dtype="float64")
    band_path = bands(adjusted_close, length=band_length, beta=beta, ddof=ddof)
    oi_path = rolling_oi(
        traded["open_interest"].to_numpy(dtype="float64"),
        short=oi_short,
        long=oi_long,
    )
    adjusted_atr = atr_series(
        adjusted_high, adjusted_low, adjusted_close, window=atr_window
    )
    adjusted_atr[: min(atr_window - 1, len(adjusted_atr))] = np.nan

    traded_positions = list(traded.index)
    for position, frame_index in enumerate(traded_positions):
        output = signal_rows[frame_index]
        factor = float(traded.loc[frame_index, "adj_factor"])
        output.update(
            signal_open=adjusted_open[position],
            signal_high=adjusted_high[position],
            signal_low=adjusted_low[position],
            signal_close=adjusted_close[position],
            middle=band_path.middle[position],
            std=band_path.std[position],
            upper=band_path.upper[position],
            lower=band_path.lower[position],
            atr_adjusted=adjusted_atr[position],
            atr_raw=adjusted_atr[position] / factor,
            oi_short=oi_path.short[position],
            oi_long=oi_path.long[position],
        )

    calendar = SessionCalendar.from_bars(frame)

    account = EventAccount(cost_bps=cost_bps)
    first = traded.iloc[0]
    current_contract = str(first["contract"])
    account.initialize({current_contract: float(first["close"])})
    state = State(Position.FLAT, take_profit=None, oi_scale=0.0)
    current_target = 0.0
    previous_contract: str | None = None
    previous_pricing_basis: str | None = None
    used_rolls: set[int] = set()
    logical_trades: list[dict[str, object]] = []
    open_trade: dict[str, object] | None = None
    trade_number = 0
    daily_rows: list[dict[str, object]] = []
    traded_number_by_index = {
        frame_index: number for number, frame_index in enumerate(traded_positions)
    }
    pending_fills: list[tuple[pd.Timestamp, int, dict[str, object]]] = []
    fill_sequence = 0
    latest_prices: dict[str, tuple[pd.Timestamp, float]] = {}
    closed_dates: set[date] = set()

    def execute_pending_fill(payload: dict[str, object]) -> None:
        nonlocal current_contract
        nonlocal current_target
        nonlocal open_trade
        nonlocal trade_number

        fill_time = pd.Timestamp(payload["fill_time"])
        fill_contract = str(payload["contract"])
        if fill_contract != current_contract:
            raise ValueError(
                "bollinger_shadow_fill_contract: pending fill must match active contract"
            )
        fill_price = float(payload["fill_price"])
        next_target = float(payload["next_target"])
        reason = str(payload["reason"])
        output = payload["output"]
        assert isinstance(output, dict)
        equity_before = account.equity
        gross_before = account.gross_equity
        execution_start = len(account.executions)
        account.rebalance(
            fill_time,
            {current_contract: fill_price},
            {current_contract: next_target} if next_target else {},
            {current_contract: reason},
        )
        output["action_changed"] = True
        current_target = next_target
        latest_prices[current_contract] = (fill_time, fill_price)

        previous_position = payload["previous_position"]
        desired_state = payload["desired_state"]
        assert isinstance(previous_position, Position)
        assert isinstance(desired_state, State)
        if previous_position is Position.FLAT:
            trade_number += 1
            open_trade = {
                "product": product,
                "trade_id": f"{product}-{trade_number:06d}",
                "entry_date": fill_time.date(),
                "entry_time": fill_time,
                "entry_contract": current_contract,
                "entry_price": fill_price,
                "direction": int(payload["direction"]),
                "oi_scale": desired_state.oi_scale,
                "target_magnitude": abs(next_target),
                "entry_signal_close": float(payload["signal_close"]),
                "roll_count": 0,
                "equity_before": equity_before,
                "gross_before": gross_before,
                "execution_start": execution_start,
            }
            return

        assert open_trade is not None
        costs = math.fsum(
            record.cost
            for record in account.executions[int(open_trade["execution_start"]) :]
        )
        logical_trades.append(
            {
                **{key: open_trade[key] for key in _TRADE_COLUMNS if key in open_trade},
                "exit_date": fill_time.date(),
                "exit_time": fill_time,
                "exit_contract": current_contract,
                "exit_price": fill_price,
                "exit_signal_close": float(payload["signal_close"]),
                "exit_reason": reason,
                "gross_return": account.gross_equity / float(open_trade["gross_before"])
                - 1.0,
                "cost": costs,
                "net_return": account.equity / float(open_trade["equity_before"]) - 1.0,
            }
        )
        open_trade = None

    def drain_pending(until: pd.Timestamp) -> None:
        while pending_fills and pending_fills[0][0] <= until:
            _, _, payload = heapq.heappop(pending_fills)
            execute_pending_fill(payload)

    def close_account_day(
        trade_day: date, last_slot_end: pd.Timestamp | None
    ) -> None:
        """Close one trade date just after that trade date's own last activity.

        A calendar-end stamp cannot serve here. ``sessions._slot_timestamp``
        puts a trade date's night session on the **previous** calendar evening,
        so the next trade date's first bars carry timestamps before midnight of
        this one; stamping the close at ``time.max`` would put it after them and
        the account's strictly increasing clock would reject the next fill.
        Anchoring on this trade date's last bar or execution keeps the boundary
        inside the gap that separates two sessions.
        """
        if trade_day in closed_dates:
            raise ValueError(f"bollinger_shadow_daily_duplicate: {trade_day}")
        while (
            pending_fills
            and pending_fills[0][2]["execution_trade_date"] <= trade_day
        ):
            _, _, payload = heapq.heappop(pending_fills)
            execute_pending_fill(payload)
        candidates: list[pd.Timestamp] = []
        if last_slot_end is not None:
            candidates.append(pd.Timestamp(last_slot_end))
        if account.events:
            candidates.append(pd.Timestamp(account.events[-1].timestamp))
        if not candidates:
            raise ValueError(
                f"bollinger_shadow_daily_activity: {trade_day} has no activity"
            )
        close_timestamp = max(candidates) + timedelta(microseconds=1)
        if close_timestamp.date() != trade_day:
            raise ValueError(
                "bollinger_shadow_daily_time: trade date activity left its calendar day"
            )

        prices: dict[str, float] = {}
        if current_target:
            latest = latest_prices.get(current_contract)
            if latest is None or latest[0] > close_timestamp:
                raise ValueError(
                    "bollinger_shadow_daily_price: held contract needs a causal price"
                )
            prices[current_contract] = latest[1]
        account.mark_close(trade_day, close_timestamp, prices)
        daily = account.drain_daily_row(trade_day, "close")
        daily_rows.append(
            {
                "product": product,
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
        closed_dates.add(trade_day)

    def close_pending_dates_before(next_day: date | None) -> None:
        while pending_fills:
            pending_day = pending_fills[0][2]["execution_trade_date"]
            if next_day is not None and pending_day >= next_day:
                return
            close_account_day(pending_day, None)

    for trade_date, day_frame in frame.groupby("trade_date", sort=True):
        close_pending_dates_before(trade_date)
        for frame_index, row in day_frame.iterrows():
            output = signal_rows[frame_index]
            if bool(row["no_trade"]):
                drain_pending(row["slot_end"])
                output.update(
                    state_position=state.position.value,
                    state_take_profit=state.take_profit
                    if state.take_profit is not None
                    else np.nan,
                    state_oi_scale=state.oi_scale,
                    target_direction=int(math.copysign(1, current_target))
                    if current_target
                    else 0,
                    target_magnitude=abs(current_target),
                    target_weight=current_target,
                )
                continue

            contract = str(row["contract"])
            if previous_contract is not None and contract != previous_contract:
                matches = rolls.loc[
                    (rolls["trade_date"] == trade_date)
                    & (rolls["old_contract"] == previous_contract)
                    & (rolls["new_contract"] == contract)
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "bollinger_shadow_roll_missing: exact contract transition fill required"
                    )
                roll_index = int(matches.index[0])
                roll = matches.iloc[0]
                if roll["fill_time"] >= row["slot_end"]:
                    raise ValueError(
                        "bollinger_shadow_roll_time: roll must precede bar signal"
                    )
                if (
                    roll["old_pricing_basis"] != previous_pricing_basis
                    or roll["new_pricing_basis"] != row["pricing_basis"]
                ):
                    raise ValueError(
                        "bollinger_shadow_roll_pricing_basis: both legs must match panel provenance"
                    )
                used_rolls.add(roll_index)
                drain_pending(roll["fill_time"])
                output.update(
                    roll_fill_time=roll["fill_time"],
                    roll_old_contract=previous_contract,
                    roll_new_contract=contract,
                    roll_old_price=float(roll["old_price"]),
                    roll_new_price=float(roll["new_price"]),
                    roll_old_pricing_basis=roll["old_pricing_basis"],
                    roll_new_pricing_basis=roll["new_pricing_basis"],
                )
                if current_target != 0.0:
                    event = account.rebalance(
                        roll["fill_time"],
                        {
                            previous_contract: roll["old_price"],
                            contract: roll["new_price"],
                        },
                        {contract: current_target},
                        {previous_contract: "roll_old", contract: "roll_new"},
                    )
                    output.update(
                        roll_execution_count=len(event.executions),
                        roll_turnover=event.turnover,
                        roll_cost=event.cost,
                    )
                    if open_trade is not None:
                        open_trade["roll_count"] = int(open_trade["roll_count"]) + 1
                current_contract = contract
                latest_prices[contract] = (roll["fill_time"], float(roll["new_price"]))
            elif previous_contract is None:
                boundary = rolls.loc[
                    (rolls["trade_date"] == trade_date)
                    & (rolls["new_contract"] == contract)
                    & ~rolls.index.isin(used_rolls)
                ]
                if len(boundary) > 1:
                    raise ValueError(
                        "bollinger_shadow_roll_boundary: duplicate boundary fills"
                    )
                if len(boundary) == 1:
                    roll_index = int(boundary.index[0])
                    roll = boundary.iloc[0]
                    if roll["fill_time"] >= row["slot_end"]:
                        raise ValueError(
                            "bollinger_shadow_roll_time: boundary roll must precede bar signal"
                        )
                    if roll["new_pricing_basis"] != row["pricing_basis"]:
                        raise ValueError(
                            "bollinger_shadow_roll_pricing_basis: boundary new leg must match panel provenance"
                        )
                    drain_pending(roll["fill_time"])
                    old_contract = str(roll["old_contract"])
                    output.update(
                        roll_fill_time=roll["fill_time"],
                        roll_old_contract=old_contract,
                        roll_new_contract=contract,
                        roll_old_price=float(roll["old_price"]),
                        roll_new_price=float(roll["new_price"]),
                        roll_old_pricing_basis=roll["old_pricing_basis"],
                        roll_new_pricing_basis=roll["new_pricing_basis"],
                    )
                    if current_target != 0.0:
                        if current_contract != old_contract:
                            raise ValueError(
                                "bollinger_shadow_roll_boundary: held old leg mismatches boundary"
                            )
                        event = account.rebalance(
                            roll["fill_time"],
                            {
                                old_contract: roll["old_price"],
                                contract: roll["new_price"],
                            },
                            {contract: current_target},
                            {old_contract: "roll_old", contract: "roll_new"},
                        )
                        output.update(
                            roll_execution_count=len(event.executions),
                            roll_turnover=event.turnover,
                            roll_cost=event.cost,
                        )
                        if open_trade is not None:
                            open_trade["roll_count"] = int(open_trade["roll_count"]) + 1
                    used_rolls.add(roll_index)
                    latest_prices[contract] = (
                        roll["fill_time"],
                        float(roll["new_price"]),
                    )
                current_contract = contract
            previous_contract = contract
            previous_pricing_basis = str(row["pricing_basis"])
            drain_pending(row["slot_end"])
            latest_prices[contract] = (row["slot_end"], float(row["close"]))

            number = traded_number_by_index[frame_index]
            ready_values = (
                output["middle"],
                output["std"],
                output["upper"],
                output["lower"],
                output["atr_adjusted"],
                output["oi_short"],
                output["oi_long"],
            )
            previous_ready = number > 0 and all(
                math.isfinite(float(signal_rows[traded_positions[number - 1]][column]))
                for column in ("signal_close", "middle", "upper", "lower")
            )
            if previous_ready and all(
                math.isfinite(float(value)) for value in ready_values
            ):
                previous = signal_rows[traded_positions[number - 1]]
                supplied_scale = oi_multiplier(
                    short_oi=float(output["oi_short"]),
                    long_oi=float(output["oi_long"]),
                )
                output["oi_scale_input"] = supplied_scale
                desired = step(
                    state,
                    previous_close=float(previous["signal_close"]),
                    previous_middle=float(previous["middle"]),
                    previous_upper=float(previous["upper"]),
                    previous_lower=float(previous["lower"]),
                    close=float(output["signal_close"]),
                    middle=float(output["middle"]),
                    upper=float(output["upper"]),
                    lower=float(output["lower"]),
                    std=float(output["std"]),
                    oi_scale=supplied_scale,
                )
                output["action"] = desired.reason
                if desired.changed:
                    last_traded_index = traded_positions[-1]
                    if bool(row["fill_pending"]) and frame_index == last_traded_index:
                        output["action"] = "fill_pending"
                    else:
                        if bool(row["fill_pending"]) or bool(row["fill_unpriceable"]):
                            raise ValueError(
                                "bollinger_shadow_required fill is unavailable"
                            )
                        if pd.isna(row["fill_time"]) or pd.isna(row["fill_price"]):
                            raise ValueError(
                                "bollinger_shadow_required fill is missing"
                            )
                        fill_time = row["fill_time"]
                        fill_price = _finite(
                            row["fill_price"], "required fill", positive=True
                        )
                        next_direction = desired.target_direction
                        next_target = 0.0
                        if next_direction:
                            raw_atr = float(output["atr_raw"])
                            next_target = (
                                next_direction
                                * desired.state.oi_scale
                                * atr_leverage(close=float(row["close"]), atr=raw_atr)
                            )
                        execution_date = calendar.execution_trade_date(
                            fill_time, trade_date
                        )
                        if execution_date > trade_date:
                            previous_state = state
                            state = desired.state
                            fill_sequence += 1
                            heapq.heappush(
                                pending_fills,
                                (
                                    pd.Timestamp(fill_time),
                                    fill_sequence,
                                    {
                                        "execution_trade_date": execution_date,
                                        "fill_time": fill_time,
                                        "fill_price": fill_price,
                                        "contract": current_contract,
                                        "next_target": next_target,
                                        "reason": desired.reason,
                                        "output": output,
                                        "previous_position": previous_state.position,
                                        "desired_state": desired.state,
                                        "direction": desired.target_direction,
                                        "signal_close": float(output["signal_close"]),
                                    },
                                ),
                            )
                            output.update(
                                state_position=state.position.value,
                                state_take_profit=state.take_profit
                                if state.take_profit is not None
                                else np.nan,
                                state_oi_scale=state.oi_scale,
                                target_direction=desired.target_direction,
                                target_magnitude=abs(next_target),
                                target_weight=next_target,
                            )
                            continue
                        equity_before = account.equity
                        gross_before = account.gross_equity
                        execution_start = len(account.executions)
                        event = account.rebalance(
                            fill_time,
                            {current_contract: fill_price},
                            {current_contract: next_target} if next_target else {},
                            {current_contract: desired.reason},
                        )
                        output["action_changed"] = True
                        latest_prices[current_contract] = (fill_time, fill_price)
                        previous_state = state
                        state = desired.state
                        current_target = next_target
                        if previous_state.position is Position.FLAT:
                            trade_number += 1
                            open_trade = {
                                "product": product,
                                "trade_id": f"{product}-{trade_number:06d}",
                                "entry_date": pd.Timestamp(fill_time).date(),
                                "entry_time": fill_time,
                                "entry_contract": current_contract,
                                "entry_price": fill_price,
                                "direction": desired.target_direction,
                                "oi_scale": state.oi_scale,
                                "target_magnitude": abs(next_target),
                                "entry_signal_close": float(output["signal_close"]),
                                "roll_count": 0,
                                "equity_before": equity_before,
                                "gross_before": gross_before,
                                "execution_start": execution_start,
                            }
                        else:
                            assert open_trade is not None
                            costs = math.fsum(
                                record.cost
                                for record in account.executions[
                                    int(open_trade["execution_start"]) :
                                ]
                            )
                            logical_trades.append(
                                {
                                    **{
                                        key: open_trade[key]
                                        for key in _TRADE_COLUMNS
                                        if key in open_trade
                                    },
                                    "exit_date": pd.Timestamp(fill_time).date(),
                                    "exit_time": fill_time,
                                    "exit_contract": current_contract,
                                    "exit_price": fill_price,
                                    "exit_signal_close": float(output["signal_close"]),
                                    "exit_reason": desired.reason,
                                    "gross_return": account.gross_equity
                                    / float(open_trade["gross_before"])
                                    - 1.0,
                                    "cost": costs,
                                    "net_return": account.equity
                                    / float(open_trade["equity_before"])
                                    - 1.0,
                                }
                            )
                            open_trade = None

            output.update(
                state_position=state.position.value,
                state_take_profit=state.take_profit
                if state.take_profit is not None
                else np.nan,
                state_oi_scale=state.oi_scale,
                target_direction=int(math.copysign(1, current_target))
                if current_target
                else 0,
                target_magnitude=abs(current_target),
                target_weight=current_target,
            )

        close_account_day(trade_date, day_frame["slot_end"].max())

    close_pending_dates_before(None)

    if len(used_rolls) != len(rolls):
        raise ValueError(
            "bollinger_shadow_roll_mismatch: unused or mismatched roll fills"
        )

    signals_output = pd.DataFrame(signal_rows)
    trades_output = pd.DataFrame(logical_trades, columns=_TRADE_COLUMNS)
    daily_output = (
        pd.DataFrame(daily_rows, columns=_DAILY_COLUMNS)
        .sort_values("trade_date", kind="stable")
        .reset_index(drop=True)
    )
    return ShadowResult(product, signals_output, trades_output, daily_output)
