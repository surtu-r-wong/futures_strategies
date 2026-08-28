"""Versioned session rules and authoritative trading-minute clocks.

A market's particulars -- which day segments it runs, whether it has a night
session, how wide its clock is -- live on a :class:`SessionRuleset` and are
passed in. Nothing here is allowed to assume one market.
"""

import csv
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")

# Structural bounds on the trade-date minute offset this module counts in: a
# negative offset is the previous evening, 0..539 the small hours that still
# belong to the trade date, and 540 or more the day session. These are the
# outer limits any market has to fit inside; each market's own, tighter window
# is declared on its ruleset and enforced when rules are built.
CLOCK_MIN_MINUTE = -180
CLOCK_MAX_MINUTE = 960

_SESSION_RULE_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_start",
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


def validate_capture_coverage(
    *,
    capture_start: date,
    backtest_start: date,
    prewarm_calendar_days: int,
) -> date:
    if type(capture_start) is not date or type(backtest_start) is not date:
        raise ValueError(
            "session_capture_coverage_dates: capture_start and backtest_start "
            "must be concrete date values"
        )
    if type(prewarm_calendar_days) is not int or prewarm_calendar_days <= 0:
        raise ValueError(
            "session_capture_coverage_prewarm: prewarm_calendar_days must be a "
            "positive actual integer"
        )

    required = backtest_start - timedelta(days=prewarm_calendar_days)
    if capture_start > required:
        raise SessionClockError(
            exchange="*",
            product="*",
            trade_date=backtest_start,
            check="session_asset_prewarm_coverage",
            reason=(
                "session asset begins after the minute-state prewarm; "
                f"capture_start={capture_start.isoformat()}; "
                f"required_start={required.isoformat()}"
            ),
        )
    return required


@dataclass(frozen=True, order=True)
class SessionSegment:
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if type(self.start_minute) is not int or type(self.end_minute) is not int:
            raise ValueError("session_segment_offsets: offsets must be actual integers")
        if self.end_minute <= self.start_minute:
            raise ValueError("session segment end must be after start")
        if (
            self.start_minute < CLOCK_MIN_MINUTE
            or self.end_minute > CLOCK_MAX_MINUTE
        ):
            raise ValueError("session segment is outside the trading clock")


@dataclass(frozen=True)
class SessionRuleset:
    """Everything about one market's clock that the rules themselves don't carry.

    A rule row in the versioned CSV asset says who and when, plus the night
    interval. The day segments, the width of the clock, and whether a night
    session is legal at all are properties of the market, so they live here and
    are passed to the loader rather than read off a module-level constant.

    ``day_segment_schedule`` is a schedule, not one tuple, because a market can
    move its own hours: CFFEX shortened the stock-index day session on
    2016-01-01. Each entry is the date its segments take effect, earliest first.
    """

    version: str
    capture_start: date
    day_segment_schedule: tuple[tuple[date, tuple[SessionSegment, ...]], ...]
    clock_start_minute: int
    clock_end_minute: int
    allows_night: bool

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("session_ruleset_version: version must be a nonempty string")
        if type(self.capture_start) is not date:
            raise ValueError(
                "session_ruleset_capture_start: capture_start must be a concrete date"
            )
        if type(self.clock_start_minute) is not int or type(self.clock_end_minute) is not int:
            raise ValueError(
                "session_ruleset_clock: clock bounds must be actual integers"
            )
        if self.clock_start_minute < CLOCK_MIN_MINUTE or self.clock_end_minute > CLOCK_MAX_MINUTE:
            raise ValueError(
                "session_ruleset_clock: clock bounds must fall inside "
                f"[{CLOCK_MIN_MINUTE}, {CLOCK_MAX_MINUTE}]"
            )
        if self.clock_end_minute <= self.clock_start_minute:
            raise ValueError(
                "session_ruleset_clock: clock end must be after clock start"
            )
        if type(self.allows_night) is not bool:
            raise ValueError("session_ruleset_night: allows_night must be a bool")

        try:
            schedule = tuple(
                (effective_start, tuple(segments))
                for effective_start, segments in self.day_segment_schedule
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "session_ruleset_schedule: schedule must be pairs of a date and "
                "its segments"
            ) from exc
        if not schedule:
            raise ValueError(
                "session_ruleset_schedule: require at least one dated entry"
            )
        for effective_start, segments in schedule:
            if type(effective_start) is not date:
                raise ValueError(
                    "session_ruleset_schedule: entry dates must be concrete dates"
                )
            if not segments or not all(
                isinstance(item, SessionSegment) for item in segments
            ):
                raise ValueError(
                    "session_ruleset_schedule: require nonempty SessionSegment values"
                )
            for segment in segments:
                if (
                    segment.start_minute < self.clock_start_minute
                    or segment.end_minute > self.clock_end_minute
                ):
                    raise ValueError(
                        f"session_ruleset_schedule: segment {segment!r} falls "
                        f"outside the {self.version} clock "
                        f"[{self.clock_start_minute}, {self.clock_end_minute}]"
                    )
        if any(
            schedule[index - 1][0] >= schedule[index][0]
            for index in range(1, len(schedule))
        ):
            raise ValueError(
                "session_ruleset_schedule: entry dates must strictly increase"
            )
        object.__setattr__(self, "day_segment_schedule", schedule)

    def day_segments_for(self, effective_start: date) -> tuple[SessionSegment, ...]:
        """The day segments this market ran on ``effective_start``."""
        if type(effective_start) is not date:
            raise ValueError(
                "session_ruleset_lookup: effective_start must be a concrete date"
            )
        chosen: tuple[SessionSegment, ...] | None = None
        for entry_start, segments in self.day_segment_schedule:
            if entry_start > effective_start:
                break
            chosen = segments
        if chosen is None:
            raise ValueError(
                f"session_ruleset_lookup: {self.version} has no day segments in "
                f"effect on {effective_start}; earliest entry is "
                f"{self.day_segment_schedule[0][0]}"
            )
        return chosen


COMMODITY_V1 = SessionRuleset(
    version="commodity-v1",
    capture_start=date(2011, 1, 1),
    day_segment_schedule=(
        (
            date(2000, 1, 1),
            (
                SessionSegment(540, 615),
                SessionSegment(630, 690),
                SessionSegment(810, 900),
            ),
        ),
    ),
    clock_start_minute=-180,
    clock_end_minute=900,
    allows_night=True,
)

# Stock-index futures. Two eras: CFFEX ran 09:15-11:30 / 13:00-15:15 until it
# shortened the day session on 2016-01-01 to 09:30-11:30 / 13:00-15:00. Both
# entries are the exchange's official hours; what the local minute archive
# actually holds for the early era is 15 minutes shorter, and that shortfall is
# registered as a known gap by the consumer rather than shaved off here.
CFFEX_V1 = SessionRuleset(
    version="cffex-v1",
    capture_start=date(2010, 4, 16),
    day_segment_schedule=(
        (
            date(2000, 1, 1),
            (
                SessionSegment(555, 690),
                SessionSegment(780, 915),
            ),
        ),
        (
            date(2016, 1, 1),
            (
                SessionSegment(570, 690),
                SessionSegment(780, 900),
            ),
        ),
    ),
    clock_start_minute=540,
    clock_end_minute=915,
    allows_night=False,
)

SESSION_RULESETS: dict[str, SessionRuleset] = {
    COMMODITY_V1.version: COMMODITY_V1,
    CFFEX_V1.version: CFFEX_V1,
}

_DAY_ONLY_EFFECTIVE_START = date(2000, 1, 1)

# Shorthand for the commodity consumer, which predates the ruleset seam. New
# consumers pass a SessionRuleset instead of reading these.
SESSION_RULES_VERSION = COMMODITY_V1.version
SESSION_RULES_CAPTURE_START = COMMODITY_V1.capture_start
DAY_SEGMENTS = tuple(
    (segment.start_minute, segment.end_minute)
    for segment in COMMODITY_V1.day_segments_for(_DAY_ONLY_EFFECTIVE_START)
)


def ruleset_for_version(version: str) -> SessionRuleset:
    """Look up a registered ruleset, or fail loudly naming what is registered."""
    try:
        return SESSION_RULESETS[version]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"session_rule_version: expected one of {tuple(SESSION_RULESETS)!r}; "
            f"got {version!r}"
        ) from exc


def night_label_to_offset(value: str) -> int:
    """Convert an exact HH:MM night label into a trade-date minute offset."""
    if type(value) is not str or len(value) != 5 or value[2] != ":":
        raise ValueError(f"session_rule_time: invalid night label {value!r}")
    try:
        hour = int(value[:2])
        minute = int(value[3:])
    except ValueError as exc:
        raise ValueError(f"session_rule_time: invalid night label {value!r}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or minute % 15:
        raise ValueError(f"session_rule_time: invalid night label {value!r}")
    clock_minute = hour * 60 + minute
    if 21 * 60 <= clock_minute < 24 * 60:
        return clock_minute - 24 * 60
    if 0 <= clock_minute <= 150:
        return clock_minute
    raise ValueError(
        f"session_rule_time: night label outside commodity clock {value!r}"
    )


def night_offset_to_label(value: int) -> str:
    """Convert a trade-date minute offset back into its exact HH:MM label."""
    if type(value) is not int or value < -180 or value > 150 or value % 15:
        raise ValueError(f"session_rule_time: invalid night offset {value!r}")
    clock_minute = value + 24 * 60 if value < 0 else value
    return f"{clock_minute // 60:02d}:{clock_minute % 60:02d}"


def parse_night_interval(
    night_start: str,
    night_end: str,
) -> SessionSegment | None:
    """Translate an exact night label pair into one segment, or None."""
    if night_start == night_end == "none":
        return None
    if "none" in {night_start, night_end}:
        raise ValueError(
            "session_rule_time: night_start and night_end must both be none"
        )
    start = night_label_to_offset(night_start)
    end = night_label_to_offset(night_end)
    if start >= end:
        raise ValueError("session_rule_time: night_start must precede night_end")
    return SessionSegment(start, end)


@dataclass(frozen=True)
class SessionRule:
    exchange: str
    product: str
    effective_start: date
    effective_end: date | None
    segments: tuple[SessionSegment, ...]
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, str) or not self.exchange.strip():
            raise ValueError(
                "session_rule_identity: exchange must be a nonempty string"
            )
        if not isinstance(self.product, str) or not self.product.strip():
            raise ValueError("session_rule_identity: product must be a nonempty string")
        if type(self.effective_start) is not date or (
            self.effective_end is not None and type(self.effective_end) is not date
        ):
            raise ValueError(
                "session_rule_dates: effective dates must be concrete date values"
            )
        if self.effective_end is not None and self.effective_start > self.effective_end:
            raise ValueError(
                "session_rule_date_order: effective_start must not exceed effective_end"
            )
        try:
            segments = tuple(self.segments)
        except TypeError as exc:
            raise ValueError(
                "session_rule_segments: segments must be iterable"
            ) from exc
        object.__setattr__(self, "segments", segments)
        if not segments or not all(
            isinstance(item, SessionSegment) for item in segments
        ):
            raise ValueError(
                "session_rule_segments: require nonempty SessionSegment values"
            )
        ruleset_for_version(self.version)

    @classmethod
    def day_only(cls, exchange: str, product: str, *, version: str) -> "SessionRule":
        return cls(
            exchange=exchange,
            product=product,
            effective_start=_DAY_ONLY_EFFECTIVE_START,
            effective_end=None,
            segments=ruleset_for_version(version).day_segments_for(
                _DAY_ONLY_EFFECTIVE_START
            ),
            version=version,
        )


_REQUIRED_SESSION_RULE_FIELDS = (
    "exchange",
    "product",
    "effective_start",
    "night_start",
    "night_end",
    "version",
)


def _parse_csv_date(*, row_number: int, field: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"session_rules_csv_date: row {row_number} field {field} "
            f"value {value!r}: invalid ISO date"
        ) from exc


def load_session_rules(
    path: Path,
    *,
    ruleset: SessionRuleset = COMMODITY_V1,
) -> tuple[SessionRule, ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != _SESSION_RULE_COLUMNS:
            raise ValueError(
                "session_rules_csv_header: "
                f"expected {_SESSION_RULE_COLUMNS!r}; got {actual_columns!r}"
            )

        rules: list[SessionRule] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(
                    f"session_rules_csv_row_width: row {row_number}: "
                    f"expected {len(_SESSION_RULE_COLUMNS)} cells"
                )
            for field in _REQUIRED_SESSION_RULE_FIELDS:
                value = row[field]
                if not value.strip():
                    raise ValueError(
                        f"session_rules_csv_required: row {row_number} field "
                        f"{field} value {value!r}: required nonempty field"
                    )

            version = row["version"]
            if version != ruleset.version:
                raise ValueError(
                    f"session_rules_csv_version: row {row_number} field version "
                    f"value {version!r}: expected {ruleset.version!r}"
                )
            night_start = row["night_start"]
            night_end = row["night_end"]
            try:
                night_segment = parse_night_interval(night_start, night_end)
            except ValueError as exc:
                raise ValueError(
                    f"session_rules_night_interval: row {row_number} fields "
                    f"night_start/night_end values "
                    f"{night_start!r}/{night_end!r}: {exc}"
                ) from exc

            effective_start = _parse_csv_date(
                row_number=row_number,
                field="effective_start",
                value=row["effective_start"],
            )
            effective_end_text = row["effective_end"]
            effective_end = (
                _parse_csv_date(
                    row_number=row_number,
                    field="effective_end",
                    value=effective_end_text,
                )
                if effective_end_text
                else None
            )
            if effective_end is not None and effective_start > effective_end:
                raise ValueError(
                    f"session_rules_csv_date_order: row {row_number} "
                    f"field effective_end value {effective_end_text!r}: "
                    "effective_start must not exceed effective_end"
                )

            if night_segment is not None and not ruleset.allows_night:
                raise ValueError(
                    f"session_rules_csv_night: row {row_number} fields "
                    f"night_start/night_end values "
                    f"{night_start!r}/{night_end!r}: ruleset {ruleset.version!r} "
                    "has no night session"
                )
            segments = ruleset.day_segments_for(effective_start)
            if night_segment is not None:
                segments = (night_segment, *segments)
            rules.append(
                SessionRule(
                    exchange=row["exchange"],
                    product=row["product"],
                    effective_start=effective_start,
                    effective_end=effective_end,
                    segments=segments,
                    version=version,
                )
            )
    return tuple(rules)


def matching_session_rules(
    rules: Sequence[SessionRule],
    exchange: str,
    product: str,
    trade_date: date,
) -> tuple[SessionRule, ...]:
    """Every rule covering this product-day, in the order the rules were given.

    ``resolve_session_rule`` demands exactly one; a coverage gate wants to count
    them without raising. Both go through here so the two can never disagree on
    what "covered" means.
    """
    return tuple(
        rule
        for rule in rules
        if rule.exchange == exchange
        and rule.product == product
        and rule.effective_start <= trade_date
        and (rule.effective_end is None or trade_date <= rule.effective_end)
    )


def resolve_session_rule(
    rules: Sequence[SessionRule],
    exchange: str,
    product: str,
    trade_date: date,
) -> SessionRule:
    matches = matching_session_rules(rules, exchange, product, trade_date)
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


def _is_aware_datetime(value) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _metadata_clock_context(
    slots: Sequence[datetime],
    *,
    rule: SessionRule | None = None,
    fallback_date: date,
) -> tuple[str, str, date]:
    return (
        getattr(slots, "exchange", None)
        or (rule.exchange if rule is not None else "<unknown>"),
        getattr(slots, "product", None)
        or (rule.product if rule is not None else "<unknown>"),
        getattr(slots, "trade_date", None) or fallback_date,
    )


def _require_aware_slots(
    slots: Sequence[datetime],
    *,
    rule: SessionRule | None = None,
    fallback_date: date,
) -> None:
    for index, slot in enumerate(slots):
        if _is_aware_datetime(slot):
            continue
        exchange, product, trade_date = _metadata_clock_context(
            slots,
            rule=rule,
            fallback_date=fallback_date,
        )
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="slot_datetime_awareness",
            reason=f"slot {index} must be an aware datetime",
        )


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
    _require_aware_slots(
        slots,
        rule=rule,
        fallback_date=fallback_date,
    )
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

    segments = rule.segments
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
        _slot_timestamp(
            minute_offset,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
        )
        for minute_offset in minute_offsets
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
    if not (
        rule.effective_start <= trade_date
        and (rule.effective_end is None or trade_date <= rule.effective_end)
    ):
        raise SessionClockError(
            exchange=rule.exchange,
            product=rule.product,
            trade_date=trade_date,
            check="session_rule_effective_date",
            reason="trade date is outside the session rule effective range",
        )
    if previous_trade_date >= trade_date:
        raise SessionClockError(
            exchange=rule.exchange,
            product=rule.product,
            trade_date=trade_date,
            check="previous_trade_date_order",
            reason="previous trade date must be earlier than trade date",
        )
    values = tuple(
        _slot_timestamp(
            minute_offset,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
        )
        for segment in rule.segments
        for minute_offset in range(segment.start_minute, segment.end_minute)
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
    _require_strict_order(
        slots,
        rule=rule,
        fallback_date=trade_date,
    )
    return slots


def _preserve_slot_context(
    slots: Sequence[datetime],
    values: Sequence[datetime],
) -> tuple[datetime, ...]:
    if isinstance(slots, TradingSlots):
        return TradingSlots(
            exchange=slots.exchange,
            product=slots.product,
            trade_date=slots.trade_date,
            previous_trade_date=slots.previous_trade_date,
            values=values,
        )
    return tuple(values)


def next_slots(
    slots: Sequence[datetime],
    start: datetime,
    count: int,
) -> tuple[datetime, ...]:
    fallback_date = getattr(slots, "trade_date", None) or (
        start.date() if isinstance(start, datetime) else date.min
    )
    if not _is_aware_datetime(start):
        exchange, product, trade_date = _metadata_clock_context(
            slots,
            fallback_date=fallback_date,
        )
        raise SessionClockError(
            exchange=exchange,
            product=product,
            trade_date=trade_date,
            check="start_datetime_awareness",
            reason="start must be an aware datetime",
        )
    fallback_date = (
        getattr(slots, "trade_date", None) or start.astimezone(SHANGHAI).date()
    )
    _require_aware_slots(
        slots,
        fallback_date=fallback_date,
    )
    _require_unique_slots(slots, fallback_date=fallback_date)
    _require_strict_order(
        slots,
        fallback_date=fallback_date,
    )
    start_index = bisect_left(slots, start)
    window = _preserve_slot_context(
        slots,
        slots[start_index : start_index + count],
    )
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
    for segment in rule.segments:
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
            _preserve_slot_context(
                slots,
                segment_slots[index : index + 15],
            )
            for index in range(0, segment_length, 15)
        )
        slot_index += segment_length
    return tuple(buckets)
