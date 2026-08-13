"""Versioned commodity-session rules and authoritative trading-minute clocks."""

import csv
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_RULES_VERSION = "commodity-v1"
DAY_SEGMENTS = ((540, 615), (630, 690), (810, 900))

_SESSION_RULE_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_end",
    "version",
)


class SessionClockError(ValueError):
    def __init__(
        self,
        *,
        exchange: str,
        product: str,
        trade_date: date,
        check: str,
        reason: str,
    ) -> None:
        self.exchange = exchange
        self.product = product
        self.trade_date = trade_date
        self.check = check
        self.reason = reason
        super().__init__(f"{trade_date} {exchange} {product} {check}: {reason}")


@dataclass(frozen=True, order=True)
class SessionSegment:
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if self.end_minute <= self.start_minute:
            raise ValueError("session segment end must be after start")
        if self.start_minute < -180 or self.end_minute > 900:
            raise ValueError("session segment is outside the commodity clock")


_NIGHT_SEGMENTS = {
    "none": None,
    "23:00": SessionSegment(-180, -60),
    "01:00": SessionSegment(-180, 60),
    "02:30": SessionSegment(-180, 150),
}


@dataclass(frozen=True)
class SessionRule:
    exchange: str
    product: str
    effective_start: date
    effective_end: date | None
    segments: tuple[SessionSegment, ...]
    version: str

    @classmethod
    def day_only(cls, exchange: str, product: str, *, version: str) -> "SessionRule":
        return cls(
            exchange=exchange,
            product=product,
            effective_start=date(2000, 1, 1),
            effective_end=None,
            segments=tuple(SessionSegment(*item) for item in DAY_SEGMENTS),
            version=version,
        )


def load_session_rules(path: Path) -> tuple[SessionRule, ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != _SESSION_RULE_COLUMNS:
            raise ValueError(
                "session_rules_csv_header: "
                f"expected {_SESSION_RULE_COLUMNS!r}; got {actual_columns!r}"
            )

        rules: list[SessionRule] = []
        day_segments = tuple(SessionSegment(*item) for item in DAY_SEGMENTS)
        for row in reader:
            night_end = row["night_end"]
            if night_end not in _NIGHT_SEGMENTS:
                raise ValueError(
                    f"session_rules_night_end: unsupported night_end {night_end!r}"
                )
            night_segment = _NIGHT_SEGMENTS[night_end]
            segments = day_segments
            if night_segment is not None:
                segments = (night_segment, *segments)
            effective_end_text = row["effective_end"]
            rules.append(
                SessionRule(
                    exchange=row["exchange"],
                    product=row["product"],
                    effective_start=date.fromisoformat(row["effective_start"]),
                    effective_end=(
                        date.fromisoformat(effective_end_text)
                        if effective_end_text
                        else None
                    ),
                    segments=segments,
                    version=row["version"],
                )
            )
    return tuple(rules)


def resolve_session_rule(
    rules: Sequence[SessionRule],
    exchange: str,
    product: str,
    trade_date: date,
) -> SessionRule:
    matches = tuple(
        rule
        for rule in rules
        if rule.exchange == exchange
        and rule.product == product
        and rule.effective_start <= trade_date
        and (rule.effective_end is None or trade_date <= rule.effective_end)
    )
    if len(matches) != 1:
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="session_rule_cardinality",
            reason=f"expected exactly one matching rule; found {len(matches)}",
        )
    return matches[0]


class _FrozenTradingSlotsType(type):
    def __setattr__(cls, name: str, value: object) -> None:
        raise AttributeError("TradingSlots type is immutable")

    def __delattr__(cls, name: str) -> None:
        raise AttributeError("TradingSlots type is immutable")


def _constant_property(value):
    return property(lambda instance: value)


class TradingSlots(tuple, metaclass=_FrozenTradingSlotsType):
    """Tuple of trading minutes with immutable clock context metadata."""

    __slots__ = ()

    exchange: str
    product: str
    trade_date: date
    previous_trade_date: date

    def __new__(
        cls,
        *,
        exchange: str,
        product: str,
        trade_date: date,
        previous_trade_date: date,
        values: Sequence[datetime],
    ) -> "TradingSlots":
        contextual_type = _FrozenTradingSlotsType(
            "TradingSlots",
            (TradingSlots,),
            {
                "__slots__": (),
                "exchange": _constant_property(exchange),
                "product": _constant_property(product),
                "trade_date": _constant_property(trade_date),
                "previous_trade_date": _constant_property(previous_trade_date),
            },
        )
        return tuple.__new__(contextual_type, values)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("TradingSlots is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("TradingSlots is immutable")

    @property
    def values(self) -> tuple[datetime, ...]:
        return self


def _clock_context(
    slots: Sequence[datetime],
    *,
    rule: SessionRule | None = None,
    fallback_date: date,
) -> tuple[str, str, date]:
    exchange = getattr(slots, "exchange", None)
    product = getattr(slots, "product", None)
    slot_trade_date = getattr(slots, "trade_date", None)
    if rule is not None:
        exchange = exchange or rule.exchange
        product = product or rule.product
        if slot_trade_date is None:
            day_segments = tuple(
                segment for segment in rule.segments if segment.start_minute >= 540
            )
            for slot in reversed(slots):
                local = slot.astimezone(SHANGHAI)
                minute_of_day = local.hour * 60 + local.minute
                if any(
                    segment.start_minute <= minute_of_day < segment.end_minute
                    for segment in day_segments
                ):
                    slot_trade_date = local.date()
                    break
    return (
        exchange or "<unknown>",
        product or "<unknown>",
        slot_trade_date or fallback_date,
    )


def _require_unique_slots(
    slots: Sequence[datetime],
    *,
    rule: SessionRule | None = None,
    fallback_date: date,
) -> None:
    if len(set(slots)) == len(slots):
        return
    exchange, product, trade_date = _clock_context(
        slots,
        rule=rule,
        fallback_date=fallback_date,
    )
    raise SessionClockError(
        exchange=exchange,
        product=product,
        trade_date=trade_date,
        check="duplicate_slots",
        reason="authoritative clock contains duplicate timestamps",
    )


def _require_strict_order(
    slots: Sequence[datetime],
    *,
    rule: SessionRule | None = None,
    fallback_date: date,
) -> None:
    if all(slots[index - 1] < slots[index] for index in range(1, len(slots))):
        return
    exchange, product, trade_date = _clock_context(
        slots,
        rule=rule,
        fallback_date=fallback_date,
    )
    raise SessionClockError(
        exchange=exchange,
        product=product,
        trade_date=trade_date,
        check="slots_strict_order",
        reason="authoritative clock must be strictly increasing",
    )


def _validate_slots_for_rule(
    slots: Sequence[datetime],
    rule: SessionRule,
) -> None:
    fallback_date = getattr(slots, "trade_date", rule.effective_start)
    _require_unique_slots(slots, rule=rule, fallback_date=fallback_date)
    _require_strict_order(slots, rule=rule, fallback_date=fallback_date)
    exchange, product, trade_date = _clock_context(
        slots,
        rule=rule,
        fallback_date=fallback_date,
    )

    if isinstance(slots, TradingSlots) and (
        slots.exchange != rule.exchange or slots.product != rule.product
    ):
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="session_slots_rule",
            reason=(
                "clock identity does not match rule; "
                f"rule={rule.exchange}/{rule.product}"
            ),
        )

    segments = tuple(sorted(rule.segments))
    expected_count = sum(
        segment.end_minute - segment.start_minute for segment in segments
    )
    if len(slots) != expected_count:
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="session_slots_cardinality",
            reason=f"expected {expected_count} slots; found {len(slots)}",
        )

    minute_offsets = tuple(
        minute_offset
        for segment in segments
        for minute_offset in range(segment.start_minute, segment.end_minute)
    )
    if isinstance(slots, TradingSlots):
        previous_trade_date = slots.previous_trade_date
    else:
        previous_trade_date = trade_date - timedelta(days=1)
        for index, minute_offset in enumerate(minute_offsets):
            local_date = slots[index].astimezone(SHANGHAI).date()
            if minute_offset < 0:
                previous_trade_date = local_date
                break
            if minute_offset < 540:
                previous_trade_date = local_date - timedelta(days=1)
                break

    expected_slots = tuple(
        sorted(
            _slot_timestamp(
                minute_offset,
                trade_date=trade_date,
                previous_trade_date=previous_trade_date,
            )
            for minute_offset in minute_offsets
        )
    )
    if tuple(slots) != expected_slots:
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="session_slots_mapping",
            reason="clock timestamps do not match the resolved session rule",
        )


def _slot_timestamp(
    minute_offset: int,
    *,
    trade_date: date,
    previous_trade_date: date,
) -> datetime:
    if minute_offset < 0:
        slot_date = previous_trade_date
        minute_of_day = 1440 + minute_offset
    elif minute_offset < 540:
        slot_date = previous_trade_date + timedelta(days=1)
        minute_of_day = minute_offset
    else:
        slot_date = trade_date
        minute_of_day = minute_offset
    midnight = datetime.combine(slot_date, time(), tzinfo=SHANGHAI)
    return midnight + timedelta(minutes=minute_of_day)


def build_trading_slots(
    trade_date: date,
    previous_trade_date: date,
    rule: SessionRule,
) -> TradingSlots:
    values = tuple(
        sorted(
            _slot_timestamp(
                minute_offset,
                trade_date=trade_date,
                previous_trade_date=previous_trade_date,
            )
            for segment in sorted(rule.segments)
            for minute_offset in range(segment.start_minute, segment.end_minute)
        )
    )
    slots = TradingSlots(
        exchange=rule.exchange,
        product=rule.product,
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        values=values,
    )
    _require_unique_slots(
        slots,
        rule=rule,
        fallback_date=trade_date,
    )
    return slots


def next_slots(
    slots: Sequence[datetime],
    start: datetime,
    count: int,
) -> tuple[datetime, ...]:
    fallback_date = start.date()
    _require_unique_slots(slots, fallback_date=fallback_date)
    _require_strict_order(
        slots,
        fallback_date=fallback_date,
    )
    start_index = bisect_left(slots, start)
    window = tuple(slots[start_index : start_index + count])
    if len(window) != count:
        exchange, product, trade_date = _clock_context(
            slots,
            fallback_date=fallback_date,
        )
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="next_slots_count",
            reason=f"expected {count} slots; found {len(window)}",
        )
    return window


def fifteen_minute_buckets(
    slots: Sequence[datetime],
    rule: SessionRule,
) -> tuple[tuple[datetime, ...], ...]:
    _validate_slots_for_rule(slots, rule)
    fallback_date = getattr(slots, "trade_date", rule.effective_start)
    exchange, product, trade_date = _clock_context(
        slots,
        rule=rule,
        fallback_date=fallback_date,
    )
    buckets: list[tuple[datetime, ...]] = []
    slot_index = 0
    for segment in sorted(rule.segments):
        segment_length = segment.end_minute - segment.start_minute
        if segment_length % 15:
            raise SessionClockError(
                exchange=exchange,
                product=product,
                trade_date=trade_date,
                check="fifteen_minute_segment",
                reason=(
                    "session segment length must be divisible by 15; "
                    f"segment={segment!r}"
                ),
            )
        segment_slots = slots[slot_index : slot_index + segment_length]
        buckets.extend(
            tuple(segment_slots[index : index + 15])
            for index in range(0, segment_length, 15)
        )
        slot_index += segment_length
    return tuple(buckets)
