from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cta_carry.minute_sessions import (
    DAY_SEGMENTS,
    SESSION_RULES_VERSION,
    SessionClockError,
    SessionRule,
    SessionSegment,
    build_trading_slots,
    fifteen_minute_buckets,
    load_session_rules,
    next_slots,
    resolve_session_rule,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def _with_slot_values(slots, values):
    return type(slots)(
        exchange=slots.exchange,
        product=slots.product,
        trade_date=slots.trade_date,
        previous_trade_date=slots.previous_trade_date,
        values=values,
    )


def test_monday_night_and_after_midnight_slots_follow_friday_session():
    rule = SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(
            SessionSegment(-180, 150),
            SessionSegment(540, 615),
            SessionSegment(630, 690),
            SessionSegment(810, 900),
        ),
        version="commodity-v1",
    )

    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    assert slots[0] == _dt(2024, 1, 5, 21, 0)
    assert _dt(2024, 1, 6, 0, 0) in slots
    assert _dt(2024, 1, 6, 2, 29) in slots
    assert _dt(2024, 1, 8, 0, 0) not in slots
    assert slots[-1] == _dt(2024, 1, 8, 14, 59)


def test_five_trading_minutes_skip_the_morning_recess():
    rule = SessionRule.day_only("DCE", "JD", version="commodity-v1")
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    assert next_slots(slots, _dt(2024, 1, 8, 10, 13), count=5) == (
        _dt(2024, 1, 8, 10, 13),
        _dt(2024, 1, 8, 10, 14),
        _dt(2024, 1, 8, 10, 30),
        _dt(2024, 1, 8, 10, 31),
        _dt(2024, 1, 8, 10, 32),
    )


def test_sparse_observations_do_not_compress_the_authoritative_clock():
    rule = SessionRule.day_only("DCE", "JD", version="commodity-v1")
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )
    observed = {_dt(2024, 1, 8, 9, 0), _dt(2024, 1, 8, 9, 2)}

    window = next_slots(slots, _dt(2024, 1, 8, 9, 0), count=3)

    assert window == (
        _dt(2024, 1, 8, 9, 0),
        _dt(2024, 1, 8, 9, 1),
        _dt(2024, 1, 8, 9, 2),
    )
    assert window[1] not in observed


def test_fifteen_minute_buckets_never_cross_a_recess():
    rule = SessionRule.day_only("CZCE", "AP", version="commodity-v1")
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    buckets = fifteen_minute_buckets(slots, rule)

    assert all(len(bucket) == 15 for bucket in buckets)
    assert not any(
        _dt(2024, 1, 8, 10, 14) in bucket and _dt(2024, 1, 8, 10, 30) in bucket
        for bucket in buckets
    )


@pytest.mark.parametrize(
    ("night_end", "night_segment"),
    [
        ("none", None),
        ("23:00", SessionSegment(-180, -60)),
        ("01:00", SessionSegment(-180, 60)),
        ("02:30", SessionSegment(-180, 150)),
    ],
)
def test_csv_night_end_values_translate_exactly(
    tmp_path,
    night_end,
    night_segment,
):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_end,version\n"
        f"SHFE,AU,2020-01-01,,{night_end},commodity-v1\n",
        encoding="utf-8",
    )

    rules = load_session_rules(path)

    expected_segments = tuple(SessionSegment(*item) for item in DAY_SEGMENTS)
    if night_segment is not None:
        expected_segments = (night_segment, *expected_segments)
    assert rules == (
        SessionRule(
            exchange="SHFE",
            product="AU",
            effective_start=date(2020, 1, 1),
            effective_end=None,
            segments=expected_segments,
            version=SESSION_RULES_VERSION,
        ),
    )


def test_csv_header_must_match_the_exact_ordered_schema(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "product,exchange,effective_start,effective_end,night_end,version\n"
        "AU,SHFE,2020-01-01,,02:30,commodity-v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="session_rules_csv_header"):
        load_session_rules(path)


def _rule(
    *,
    effective_start=date(2020, 1, 1),
    effective_end=None,
):
    return SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=effective_start,
        effective_end=effective_end,
        segments=tuple(SessionSegment(*item) for item in DAY_SEGMENTS),
        version=SESSION_RULES_VERSION,
    )


def _assert_structured_error(error, *, trade_date, check):
    assert error.exchange == "SHFE"
    assert error.product == "AU"
    assert error.trade_date == trade_date
    assert error.check == check
    message = str(error)
    assert "SHFE" in message
    assert "AU" in message
    assert str(trade_date) in message
    assert check in message


def test_resolve_session_rule_rejects_overlapping_inclusive_ranges():
    trade_date = date(2024, 1, 8)
    rules = (
        _rule(effective_end=trade_date),
        _rule(effective_start=trade_date),
    )

    with pytest.raises(SessionClockError) as exc_info:
        resolve_session_rule(rules, "SHFE", "AU", trade_date)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_rule_cardinality",
    )


def test_resolve_session_rule_rejects_an_unmapped_product_date():
    trade_date = date(2019, 12, 31)

    with pytest.raises(SessionClockError) as exc_info:
        resolve_session_rule((_rule(),), "SHFE", "AU", trade_date)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_rule_cardinality",
    )


def test_build_trading_slots_rejects_duplicate_slots():
    trade_date = date(2024, 1, 8)
    rule = SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(SessionSegment(540, 555), SessionSegment(550, 565)),
        version=SESSION_RULES_VERSION,
    )

    with pytest.raises(SessionClockError) as exc_info:
        build_trading_slots(trade_date, date(2024, 1, 5), rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="duplicate_slots",
    )


def test_fifteen_minute_buckets_reject_a_nondivisible_segment():
    trade_date = date(2024, 1, 8)
    rule = SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(SessionSegment(540, 556),),
        version=SESSION_RULES_VERSION,
    )
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)

    with pytest.raises(SessionClockError) as exc_info:
        fifteen_minute_buckets(slots, rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="fifteen_minute_segment",
    )


def test_next_slots_rejects_a_request_past_the_final_slot():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(slots, slots[-1], count=5)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="next_slots_count",
    )


def test_next_slots_rejects_an_unsorted_clock():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)
    reversed_slots = _with_slot_values(slots, tuple(reversed(slots)))

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(reversed_slots, slots[0], count=1)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="slots_strict_order",
    )


def test_fifteen_minute_buckets_reject_a_truncated_clock():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)
    truncated_slots = _with_slot_values(slots, slots.values[:-1])

    with pytest.raises(SessionClockError) as exc_info:
        fifteen_minute_buckets(truncated_slots, rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_slots_cardinality",
    )


def test_fifteen_minute_buckets_reject_extra_slots():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)
    extra_slots = _with_slot_values(
        slots,
        values=(*slots.values, slots[-1] + timedelta(minutes=1)),
    )

    with pytest.raises(SessionClockError) as exc_info:
        fifteen_minute_buckets(extra_slots, rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_slots_cardinality",
    )


def test_fifteen_minute_buckets_reject_mismatched_slot_timestamps():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)
    mismatched_slots = _with_slot_values(
        slots,
        values=(slots[0] + timedelta(seconds=30), *slots.values[1:]),
    )

    with pytest.raises(SessionClockError) as exc_info:
        fifteen_minute_buckets(mismatched_slots, rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_slots_mapping",
    )


def test_trading_slots_are_a_real_tuple_with_structured_error_context():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)

    assert isinstance(slots, tuple)
    assert slots == tuple(slots)
    assert slots[:2] == tuple(slots)[:2]

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(slots, slots[-1], count=5)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="next_slots_count",
    )


def test_trading_slot_metadata_is_immutable_and_preserves_error_context():
    trade_date = date(2024, 1, 8)
    rule = _rule()
    slots = build_trading_slots(trade_date, date(2024, 1, 5), rule)

    with pytest.raises(AttributeError):
        slots.exchange = "DCE"
    with pytest.raises(AttributeError):
        del slots.product
    assert not hasattr(slots, "__dict__")

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(slots, slots[-1], count=5)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="next_slots_count",
    )
