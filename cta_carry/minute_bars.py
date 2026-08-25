"""Validation and deterministic aggregation for contract minute bars."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any

import numpy as np
import pandas as pd


_MULTIPLIER_COLUMNS = (
    "bar_time",
    "symbol",
    "trade_date",
    "low",
    "high",
    "volume",
    "amount",
)
_MINUTE_COLUMNS = (
    "bar_time",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
_POSITIVE_VOLUME_COLUMNS = ("open", "high", "low", "close", "amount")
_PRODUCT_PATTERN = re.compile(r"^[A-Za-z]+")


@dataclass(frozen=True)
class FifteenMinuteBar:
    start: datetime
    end: datetime
    contract: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    no_trade: bool
    traded_rows: int = 0
    missing_slots: int = 0


@dataclass(frozen=True)
class VwapFill:
    contract: str
    start: datetime
    end: datetime
    price: float
    volume: float
    amount: float
    multiplier: int
    low: float
    high: float
    traded_rows: int
    missing_slots: int = 0
    trade_date: date | None = None
    window_slots: tuple[datetime, ...] = ()
    missing_slot_times: tuple[datetime, ...] = ()
    # Which fields the price came from. A fill priced off OHLC is not a VWAP and
    # must not be read as one, so every fill carries its basis.
    pricing_basis: str = "amount_vwap"

    @property
    def window_start(self) -> datetime:
        return self.start

    @property
    def window_end(self) -> datetime:
        return self.end


@dataclass(frozen=True)
class MultiplierResolution:
    multiplier: int
    source: str
    sample_rows: int
    pass_rate: float
    sample_dates: int
    sample_start: datetime | None = None
    sample_end: datetime | None = None
    max_range_error: float = 0.0


def _stable_value(value: Any) -> str:
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: repr(item[0]))
        return (
            "{"
            + ", ".join(
                f"{_stable_value(key)}: {_stable_value(item)}" for key, item in items
            )
            + "}"
        )
    if isinstance(value, tuple):
        return "(" + ", ".join(_stable_value(item) for item in value) + ")"
    if isinstance(value, list):
        return "[" + ", ".join(_stable_value(item) for item in value) + "]"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return repr(value)


class MinuteDataError(ValueError):
    """Raised when minute data cannot safely support aggregation or execution."""

    def __init__(
        self,
        *,
        check: str,
        reason: str,
        trade_date: date | None = None,
        timestamp: datetime | None = None,
        product: str | None = None,
        contract: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.trade_date = trade_date
        self.timestamp = timestamp
        self.product = product
        self.contract = contract
        self.check = check
        self.reason = reason
        self.context = dict(context) if context is not None else None
        details = (
            ("trade_date", trade_date),
            ("timestamp", timestamp),
            ("product", product),
            ("contract", contract),
            ("check", check),
            ("reason", reason),
            ("context", self.context),
        )
        message = "minute_data_error: " + "; ".join(
            f"{name}={_stable_value(value)}"
            for name, value in details
            if value is not None
        )
        super().__init__(message)


@dataclass(frozen=True)
class _ClockContext:
    trade_date: date | None
    product: str | None


def _product_from_contract(contract: str) -> str | None:
    if not isinstance(contract, str):
        return None
    match = _PRODUCT_PATTERN.match(contract)
    return match.group(0).upper() if match is not None else None


def _clock_context(
    original_slots: Sequence[datetime],
    contract: str,
) -> _ClockContext:
    return _ClockContext(
        trade_date=getattr(original_slots, "trade_date", None),
        product=(
            getattr(original_slots, "product", None) or _product_from_contract(contract)
        ),
    )


def _as_trade_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        value = value.date()
    return value if type(value) is date else None


def _with_frame_trade_date(
    frame: pd.DataFrame,
    *,
    clock: _ClockContext,
    contract: str,
) -> _ClockContext:
    if "trade_date" not in frame.columns:
        return clock
    values = tuple(pd.unique(frame["trade_date"].dropna()))
    if not values:
        return clock
    if len(values) != 1:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="minute_trade_date",
            reason="bounded minute rows must have exactly one logical trade_date",
            context={"trade_dates": values},
        )
    trade_date = _as_trade_date(values[0])
    if trade_date is None:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="minute_trade_date",
            reason="logical trade_date must be a concrete date value",
            context={"trade_date": values[0]},
        )
    if clock.trade_date is not None and clock.trade_date != trade_date:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="minute_trade_date",
            reason="minute row trade_date conflicts with authoritative slots",
            context={
                "frame_trade_date": trade_date,
                "slot_trade_date": clock.trade_date,
            },
        )
    return _ClockContext(trade_date, clock.product)


def _minute_error(
    *,
    clock: _ClockContext,
    contract: str,
    check: str,
    reason: str,
    timestamp: datetime | None = None,
    context: Mapping[str, Any] | None = None,
) -> MinuteDataError:
    return MinuteDataError(
        trade_date=clock.trade_date,
        timestamp=timestamp,
        product=clock.product,
        contract=contract,
        check=check,
        reason=reason,
        context=context,
    )


def _is_aware_datetime(value: Any) -> bool:
    if not isinstance(value, datetime) or pd.isna(value) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _validated_slots(
    slots: Sequence[datetime],
    *,
    contract: str,
    required_count: int | None = None,
) -> tuple[tuple[datetime, ...], _ClockContext]:
    try:
        values = tuple(slots)
    except TypeError as exc:
        clock = _ClockContext(None, _product_from_contract(contract))
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="slot_cardinality",
            reason="slots must be an iterable of aware datetimes",
        ) from exc

    clock = _clock_context(slots, contract)
    if required_count is not None and len(values) != required_count:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_slots_cardinality",
            reason=f"expected exactly {required_count} authoritative slots",
            context={"actual": len(values), "expected": required_count},
        )
    if not values:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="slot_cardinality",
            reason="at least one authoritative slot is required",
            context={"actual": 0},
        )

    for index, slot in enumerate(values):
        if not _is_aware_datetime(slot):
            raise _minute_error(
                clock=clock,
                contract=contract,
                timestamp=slot if isinstance(slot, datetime) else None,
                check="slot_datetime_awareness",
                reason="each supplied slot must be an aware datetime",
                context={"index": index},
            )

    if len(set(values)) != len(values):
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="duplicate_slots",
            reason="supplied slots contain duplicate timestamps",
            context={"slot_count": len(values), "unique_count": len(set(values))},
        )
    try:
        strictly_increasing = all(
            values[index - 1] < values[index] for index in range(1, len(values))
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="slots_strict_order",
            reason="supplied slots are not mutually comparable",
        ) from exc
    if not strictly_increasing:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="slots_strict_order",
            reason="supplied slots must be strictly increasing",
        )
    return values, clock


def _require_columns(
    frame: pd.DataFrame,
    *,
    clock: _ClockContext,
    contract: str,
) -> None:
    missing = tuple(column for column in _MINUTE_COLUMNS if column not in frame.columns)
    if missing:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="minute_schema",
            reason="minute frame is missing required columns",
            context={"missing": missing},
        )


def _first_timestamp(frame: pd.DataFrame, mask: pd.Series) -> datetime | None:
    value = frame.loc[mask, "bar_time"].iloc[0]
    return value if isinstance(value, datetime) else None


def _wrong_contract_mask(symbol: pd.Series, contract: str) -> pd.Series:
    return symbol.isna() | symbol.ne(contract).fillna(True)


def _require_traded_price_order(
    frame: pd.DataFrame,
    *,
    positive: pd.Series,
    clock: _ClockContext,
    contract: str,
    price_columns: tuple[str, ...] = (),
) -> None:
    invalid_by_field = {
        "low_high": positive & frame["low"].gt(frame["high"]),
        **{
            column: positive
            & (frame[column].lt(frame["low"]) | frame[column].gt(frame["high"]))
            for column in price_columns
        },
    }
    invalid_fields = tuple(
        field for field, mask in invalid_by_field.items() if mask.any()
    )
    if not invalid_fields:
        return
    invalid_rows = pd.concat(
        [invalid_by_field[field] for field in invalid_fields],
        axis=1,
    ).any(axis=1)
    raise _minute_error(
        clock=clock,
        contract=contract,
        timestamp=_first_timestamp(frame, invalid_rows),
        check="minute_price_range",
        reason="positive-volume traded prices violate OHLC ordering",
        context={"invalid_fields": invalid_fields},
    )


def _bound_rows(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    contract: str,
    required_count: int | None = None,
) -> tuple[
    pd.DataFrame,
    tuple[datetime, ...],
    _ClockContext,
    tuple[datetime, ...],
]:
    slot_values, clock = _validated_slots(
        slots,
        contract=contract,
        required_count=required_count,
    )
    clock = _with_frame_trade_date(
        frame,
        clock=clock,
        contract=contract,
    )
    _require_columns(frame, clock=clock, contract=contract)
    working = frame.loc[:, _MINUTE_COLUMNS].copy()

    for index, timestamp in enumerate(working["bar_time"]):
        if not _is_aware_datetime(timestamp):
            raise _minute_error(
                clock=clock,
                contract=contract,
                timestamp=timestamp if isinstance(timestamp, datetime) else None,
                check="bar_time_awareness",
                reason="each minute bar_time must be an aware datetime",
                context={"row": index},
            )

    duplicates = working["bar_time"].duplicated(keep=False)
    if duplicates.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, duplicates),
            check="duplicate_bar_time",
            reason="minute rows contain duplicate bar_time values",
            context={"duplicate_rows": int(duplicates.sum())},
        )

    wrong_contract = _wrong_contract_mask(working["symbol"], contract)
    if wrong_contract.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, wrong_contract),
            check="minute_contract",
            reason="minute row symbol does not match the requested contract",
            context={
                "symbols": tuple(
                    sorted(
                        str(value) for value in working.loc[wrong_contract, "symbol"]
                    )
                )
            },
        )

    outside = ~working["bar_time"].isin(pd.Index(slot_values))
    if outside.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, outside),
            check="rows_outside_slots",
            reason="minute rows fall outside the supplied authoritative slots",
            context={"outside_count": int(outside.sum())},
        )

    working["volume"] = pd.to_numeric(working["volume"], errors="coerce")
    invalid_volume = ~np.isfinite(working["volume"].to_numpy(dtype=float))
    if invalid_volume.any():
        mask = pd.Series(invalid_volume, index=working.index)
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, mask),
            check="minute_volume",
            reason="minute volume must be finite",
        )
    negative_volume = working["volume"].lt(0)
    if negative_volume.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, negative_volume),
            check="minute_volume",
            reason="minute volume must be nonnegative",
        )

    working["amount"] = pd.to_numeric(working["amount"], errors="coerce")
    negative_amount = working["amount"].lt(0)
    if negative_amount.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, negative_amount),
            check="minute_amount",
            reason="minute amount must be nonnegative",
        )

    for column in ("open", "high", "low", "close"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    positive = working["volume"].gt(0)
    invalid_fields = tuple(
        column
        for column in _POSITIVE_VOLUME_COLUMNS
        if (
            positive
            & ~pd.Series(
                np.isfinite(working[column].to_numpy(dtype=float)),
                index=working.index,
            )
        ).any()
    )
    if invalid_fields:
        invalid_positive = positive & pd.concat(
            [
                ~pd.Series(
                    np.isfinite(working[column].to_numpy(dtype=float)),
                    index=working.index,
                )
                for column in invalid_fields
            ],
            axis=1,
        ).any(axis=1)
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, invalid_positive),
            check="positive_volume_values",
            reason="positive-volume rows require finite OHLC and amount",
            context={"invalid_fields": invalid_fields},
        )
    _require_traded_price_order(
        working,
        positive=positive,
        clock=clock,
        contract=contract,
        price_columns=("open", "close"),
    )

    slot_index = pd.Index(slot_values, name="bar_time")
    missing_slot_times = tuple(slot_index[~slot_index.isin(working["bar_time"])])
    ordered = working.set_index("bar_time").reindex(slot_index)
    return ordered, slot_values, clock, missing_slot_times


def _epsilon(low: float, high: float) -> float:
    return 1e-6 * max(1.0, abs(low), abs(high))


def _candidate_pass_rate(sample: pd.DataFrame, multiplier: int) -> float:
    price = sample["amount"] / sample["volume"] / multiplier
    low = sample["low"].to_numpy(dtype=float)
    high = sample["high"].to_numpy(dtype=float)
    tolerance = 1e-6 * np.maximum.reduce(
        (np.ones(len(sample)), np.abs(low), np.abs(high))
    )
    passed = price.ge(sample["low"] - tolerance) & price.le(sample["high"] + tolerance)
    return float(passed.mean())


def aggregate_fifteen_minute_bar(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    contract: str,
) -> FifteenMinuteBar:
    ordered, slot_values, _, missing_slot_times = _bound_rows(
        frame,
        slots=slots,
        contract=contract,
    )
    traded = ordered.loc[ordered["volume"] > 0]
    if traded.empty:
        return FifteenMinuteBar(
            start=slot_values[0],
            end=slot_values[-1] + timedelta(minutes=1),
            contract=contract,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=0.0,
            no_trade=True,
            traded_rows=0,
            missing_slots=len(missing_slot_times),
        )
    return FifteenMinuteBar(
        start=slot_values[0],
        end=slot_values[-1] + timedelta(minutes=1),
        contract=contract,
        open=float(traded.iloc[0]["open"]),
        high=float(traded["high"].max()),
        low=float(traded["low"].min()),
        close=float(traded.iloc[-1]["close"]),
        volume=float(traded["volume"].sum()),
        no_trade=False,
        traded_rows=len(traded),
        missing_slots=len(missing_slot_times),
    )


def _multiplier_clock(frame: pd.DataFrame, contract: str) -> _ClockContext:
    trade_date = None
    if "trade_date" in frame.columns:
        nonnull_dates = frame["trade_date"].dropna()
        if not nonnull_dates.empty:
            trade_date = _as_trade_date(nonnull_dates.iloc[0])
    return _ClockContext(trade_date, _product_from_contract(contract))


def _validated_multiplier_rows(
    frame: pd.DataFrame,
    *,
    contract: str,
) -> tuple[pd.DataFrame, _ClockContext]:
    clock = _multiplier_clock(frame, contract)
    missing = tuple(
        column for column in _MULTIPLIER_COLUMNS if column not in frame.columns
    )
    if missing:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="minute_schema",
            reason="multiplier frame is missing required columns",
            context={"missing": missing},
        )
    working = frame.loc[:, _MULTIPLIER_COLUMNS].copy()

    for row_number, timestamp in enumerate(working["bar_time"]):
        if not _is_aware_datetime(timestamp):
            raise _minute_error(
                clock=clock,
                contract=contract,
                timestamp=timestamp if isinstance(timestamp, datetime) else None,
                check="bar_time_awareness",
                reason="each multiplier sample bar_time must be an aware datetime",
                context={"row": row_number},
            )
    duplicates = working.duplicated(["symbol", "bar_time"], keep=False)
    if duplicates.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, duplicates),
            check="duplicate_bar_time",
            reason="multiplier rows contain duplicate symbol/bar_time values",
            context={"duplicate_rows": int(duplicates.sum())},
        )
    wrong_contract = _wrong_contract_mask(working["symbol"], contract)
    if wrong_contract.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, wrong_contract),
            check="minute_contract",
            reason="multiplier row symbol does not match the requested contract",
        )

    working["volume"] = pd.to_numeric(working["volume"], errors="coerce")
    finite_volume = np.isfinite(working["volume"].to_numpy(dtype=float))
    if not finite_volume.all():
        mask = pd.Series(~finite_volume, index=working.index)
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, mask),
            check="minute_volume",
            reason="multiplier sample volume must be finite",
        )
    negative_volume = working["volume"].lt(0)
    if negative_volume.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, negative_volume),
            check="minute_volume",
            reason="multiplier sample volume must be nonnegative",
        )

    for column in ("low", "high", "amount"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    negative_amount = working["amount"].lt(0)
    if negative_amount.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, negative_amount),
            check="minute_amount",
            reason="multiplier sample amount must be nonnegative",
        )

    positive = working["volume"].gt(0)
    invalid_fields = tuple(
        column
        for column in ("low", "high", "amount")
        if (
            positive
            & ~pd.Series(
                np.isfinite(working[column].to_numpy(dtype=float)),
                index=working.index,
            )
        ).any()
    )
    if invalid_fields:
        invalid_positive = positive & pd.concat(
            [
                ~pd.Series(
                    np.isfinite(working[column].to_numpy(dtype=float)),
                    index=working.index,
                )
                for column in invalid_fields
            ],
            axis=1,
        ).any(axis=1)
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, invalid_positive),
            check="positive_volume_values",
            reason="positive-volume multiplier rows require finite low/high/amount",
            context={"invalid_fields": invalid_fields},
        )
    normalized_dates = pd.Series(
        [
            _as_trade_date(value) if is_positive else value
            for value, is_positive in zip(
                working["trade_date"],
                positive,
                strict=True,
            )
        ],
        index=working.index,
        dtype=object,
    )
    invalid_dates = positive & normalized_dates.isna()
    if invalid_dates.any():
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=_first_timestamp(working, invalid_dates),
            check="contract_multiplier_sample",
            reason="positive-volume multiplier rows require a concrete trade_date",
        )
    working["trade_date"] = normalized_dates
    _require_traded_price_order(
        working,
        positive=positive,
        clock=clock,
        contract=contract,
    )
    return (
        working.sort_values(["symbol", "bar_time"], kind="mergesort"),
        clock,
    )


def _select_multiplier_sample(
    frame: pd.DataFrame,
    *,
    contract: str,
) -> tuple[pd.DataFrame, _ClockContext]:
    working, clock = _validated_multiplier_rows(frame, contract=contract)
    eligible = working.loc[working["volume"] > 0].reset_index(drop=True)
    eligible_rows = len(eligible)
    if eligible_rows < 10:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="contract_multiplier_sample",
            reason="at least 10 nonzero-volume rows are required",
            context={"eligible_rows": eligible_rows, "required_rows": 10},
        )

    if eligible_rows < 60:
        sample = eligible
        required_dates = 2
    else:
        positions = [index * (eligible_rows - 1) // 59 for index in range(60)]
        if len(set(positions)) != 60:
            raise RuntimeError("internal multiplier sample positions are not unique")
        sample = eligible.iloc[positions].reset_index(drop=True)
        required_dates = 3

    sample_dates = int(sample["trade_date"].nunique())
    if sample_dates < required_dates:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="contract_multiplier_sample",
            reason="multiplier sample spans too few trade dates",
            context={
                "sample_dates": sample_dates,
                "required_dates": required_dates,
                "sample_rows": len(sample),
            },
        )
    return sample, clock


def _range_diagnostics(
    sample: pd.DataFrame,
    multiplier: int,
) -> tuple[float, float]:
    price = (
        sample["amount"].to_numpy(dtype=float)
        / sample["volume"].to_numpy(dtype=float)
        / multiplier
    )
    low = sample["low"].to_numpy(dtype=float)
    high = sample["high"].to_numpy(dtype=float)
    tolerance = 1e-6 * np.maximum.reduce(
        (np.ones(len(sample)), np.abs(low), np.abs(high))
    )
    passed = (price >= low - tolerance) & (price <= high + tolerance)
    error = np.maximum(np.maximum(low - price, price - high), 0.0)
    return float(passed.mean()), float(error.max())


def _qualifying_multipliers(sample: pd.DataFrame) -> tuple[int, ...]:
    unit_amount = sample["amount"].to_numpy(dtype=float) / sample["volume"].to_numpy(
        dtype=float
    )
    low = sample["low"].to_numpy(dtype=float)
    high = sample["high"].to_numpy(dtype=float)
    tolerance = 1e-6 * np.maximum.reduce(
        (np.ones(len(sample)), np.abs(low), np.abs(high))
    )
    multipliers = np.arange(1, 10_001, dtype=float)[:, None]
    prices = unit_amount[None, :] / multipliers
    pass_rates = (
        (prices >= low[None, :] - tolerance[None, :])
        & (prices <= high[None, :] + tolerance[None, :])
    ).mean(axis=1)
    return tuple(int(value) for value in np.flatnonzero(pass_rates >= 0.99) + 1)


def _resolution(
    sample: pd.DataFrame,
    *,
    multiplier: int,
    source: str,
) -> MultiplierResolution:
    pass_rate, max_range_error = _range_diagnostics(sample, multiplier)
    return MultiplierResolution(
        multiplier=multiplier,
        source=source,
        sample_rows=len(sample),
        pass_rate=pass_rate,
        sample_dates=int(sample["trade_date"].nunique()),
        sample_start=sample.iloc[0]["bar_time"],
        sample_end=sample.iloc[-1]["bar_time"],
        max_range_error=max_range_error,
    )


def infer_contract_multiplier(
    frame: pd.DataFrame,
    *,
    contract: str,
) -> MultiplierResolution:
    sample, clock = _select_multiplier_sample(frame, contract=contract)
    candidates = _qualifying_multipliers(sample)
    if len(candidates) != 1:
        context: dict[str, Any] = {"candidate_count": len(candidates)}
        if len(candidates) <= 20:
            context["candidates"] = candidates
        elif candidates:
            context.update(
                candidate_min=candidates[0],
                candidate_max=candidates[-1],
            )
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=sample.iloc[0]["bar_time"],
            check="contract_multiplier",
            reason="expected exactly one qualifying integer multiplier",
            context=context,
        )
    return _resolution(sample, multiplier=candidates[0], source="inferred")


def validate_metadata_multiplier(
    frame: pd.DataFrame,
    *,
    contract: str,
    multiplier: int,
    source: str = "metadata",
) -> MultiplierResolution:
    clock = _multiplier_clock(frame, contract)
    if type(multiplier) is not int or multiplier <= 0:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="metadata_multiplier",
            reason="metadata multiplier must be a positive actual integer",
            context={"multiplier": multiplier},
        )
    sample, clock = _select_multiplier_sample(frame, contract=contract)
    resolution = _resolution(sample, multiplier=multiplier, source=source)
    if resolution.pass_rate < 0.99:
        raise _minute_error(
            clock=clock,
            contract=contract,
            timestamp=sample.iloc[0]["bar_time"],
            check="metadata_multiplier",
            reason="metadata multiplier failed price-range validation",
            context={
                "multiplier": multiplier,
                "pass_rate": resolution.pass_rate,
                "required_pass_rate": 0.99,
                "sample_rows": resolution.sample_rows,
            },
        )
    return resolution


_PRICING_BASES = frozenset({"amount_vwap", "ohlc_typical"})


def five_minute_vwap(
    frame: pd.DataFrame,
    *,
    slots: Sequence[datetime],
    contract: str,
    multiplier: int,
    pricing_basis: str = "amount_vwap",
) -> VwapFill:
    ordered, slot_values, clock, missing_slot_times = _bound_rows(
        frame,
        slots=slots,
        contract=contract,
        required_count=5,
    )
    if type(multiplier) is not int or multiplier <= 0:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_multiplier",
            reason="contract multiplier must be a positive actual integer",
            context={"multiplier": multiplier},
        )

    traded = ordered.loc[ordered["volume"] > 0]
    volume = float(traded["volume"].sum())
    amount = float(traded["amount"].sum())
    if not np.isfinite(volume) or volume <= 0:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_vwap",
            reason="execution window has zero traded volume",
            context={"volume": volume, "amount": amount},
        )
    if pricing_basis not in _PRICING_BASES:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_pricing_basis",
            reason=f"pricing basis must be one of {sorted(_PRICING_BASES)}",
            context={"pricing_basis": pricing_basis},
        )
    if pricing_basis == "ohlc_typical":
        # The turnover column is not trusted on this basis, so the price is the
        # volume-weighted typical price of the bars themselves. It approximates
        # a VWAP from fields that reconcile, and it is not one.
        typical = (traded["high"] + traded["low"] + traded["close"]) / 3.0
        price = float((typical * traded["volume"]).sum() / volume)
    else:
        if not np.isfinite(amount) or amount <= 0:
            raise _minute_error(
                clock=clock,
                contract=contract,
                check="execution_vwap",
                reason="execution window total amount must be finite and positive",
                context={"volume": volume, "amount": amount},
            )
        price = amount / volume / multiplier
    if not np.isfinite(price) or price <= 0:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_vwap",
            reason="VWAP must be finite and positive",
            context={
                "vwap": price,
                "volume": volume,
                "amount": amount,
                "multiplier": multiplier,
            },
        )

    low = float(traded["low"].min())
    high = float(traded["high"].max())
    epsilon = _epsilon(low, high)
    if not low - epsilon <= price <= high + epsilon:
        raise _minute_error(
            clock=clock,
            contract=contract,
            check="execution_vwap",
            reason="VWAP is outside the traded price range",
            context={"vwap": price, "low": low, "high": high},
        )
    return VwapFill(
        contract=contract,
        start=slot_values[0],
        end=slot_values[-1] + timedelta(minutes=1),
        price=price,
        volume=volume,
        amount=amount,
        multiplier=multiplier,
        low=low,
        high=high,
        traded_rows=len(traded),
        missing_slots=len(missing_slot_times),
        trade_date=clock.trade_date,
        window_slots=slot_values,
        missing_slot_times=missing_slot_times,
        pricing_basis=pricing_basis,
    )
