from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cta_carry.minute_sessions import (
    DAY_SEGMENTS,
    SESSION_RULES_VERSION,
    SessionClockError,
    SessionRule,
    SessionSegment,
    TradingSlots,
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


def _session_rule(**overrides):
    values = {
        "exchange": "SHFE",
        "product": "AU",
        "effective_start": date(2020, 1, 1),
        "effective_end": None,
        "segments": [SessionSegment(540, 555)],
        "version": SESSION_RULES_VERSION,
    }
    values.update(overrides)
    return SessionRule(**values)


@pytest.mark.parametrize(
    ("start_minute", "end_minute"),
    [
        (540.0, 555),
        (True, 555),
        (540, "555"),
        (540, False),
    ],
)
def test_session_segment_offsets_must_be_actual_integers(start_minute, end_minute):
    with pytest.raises(ValueError, match="session_segment_offsets"):
        SessionSegment(start_minute, end_minute)


def test_session_rule_canonicalizes_segments_to_an_immutable_tuple():
    source_segments = [SessionSegment(540, 555)]

    rule = _session_rule(segments=source_segments)
    source_segments.append(SessionSegment(555, 570))

    assert isinstance(rule.segments, tuple)
    assert rule.segments == (SessionSegment(540, 555),)


@pytest.mark.parametrize("segments", [[], [object()]])
def test_session_rule_requires_nonempty_session_segments(segments):
    with pytest.raises(ValueError, match="session_rule_segments"):
        _session_rule(segments=segments)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exchange", ""),
        ("exchange", "   "),
        ("product", ""),
        ("product", "   "),
    ],
)
def test_session_rule_requires_nonempty_identity_fields(field, value):
    with pytest.raises(ValueError, match="session_rule_identity"):
        _session_rule(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effective_start", "2020-01-01"),
        ("effective_start", datetime(2020, 1, 1)),
        ("effective_end", "2020-12-31"),
        ("effective_end", datetime(2020, 12, 31)),
    ],
)
def test_session_rule_requires_concrete_dates(field, value):
    with pytest.raises(ValueError, match="session_rule_dates"):
        _session_rule(**{field: value})


def test_session_rule_rejects_reversed_effective_dates():
    with pytest.raises(ValueError, match="session_rule_date_order"):
        _session_rule(
            effective_start=date(2020, 2, 1),
            effective_end=date(2020, 1, 31),
        )


@pytest.mark.parametrize("version", ["", "   ", "commodity-v2"])
def test_session_rule_requires_the_supported_version(version):
    with pytest.raises(ValueError, match="session_rule_version"):
        _session_rule(version=version)


@pytest.mark.parametrize(
    "previous_trade_date",
    [date(2024, 1, 8), date(2024, 1, 9)],
)
def test_build_trading_slots_requires_an_earlier_previous_trade_date(
    previous_trade_date,
):
    trade_date = date(2024, 1, 8)

    with pytest.raises(SessionClockError) as exc_info:
        build_trading_slots(trade_date, previous_trade_date, _rule())

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="previous_trade_date_order",
    )


@pytest.mark.parametrize(
    ("trade_date", "effective_start", "effective_end"),
    [
        (date(2019, 12, 31), date(2020, 1, 1), None),
        (date(2021, 1, 1), date(2020, 1, 1), date(2020, 12, 31)),
    ],
)
def test_build_trading_slots_requires_trade_date_inside_rule_range(
    trade_date,
    effective_start,
    effective_end,
):
    rule = _rule(
        effective_start=effective_start,
        effective_end=effective_end,
    )

    with pytest.raises(SessionClockError) as exc_info:
        build_trading_slots(trade_date, date(2019, 12, 30), rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="session_rule_effective_date",
    )


def test_build_trading_slots_rejects_nonchronological_segment_order():
    trade_date = date(2024, 1, 8)
    rule = _session_rule(
        segments=(SessionSegment(630, 645), SessionSegment(540, 555)),
    )

    with pytest.raises(SessionClockError) as exc_info:
        build_trading_slots(trade_date, date(2024, 1, 5), rule)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="slots_strict_order",
    )


def test_next_slots_rejects_a_naive_start_with_structured_context():
    trade_date = date(2024, 1, 8)
    slots = build_trading_slots(trade_date, date(2024, 1, 5), _rule())
    naive_start = datetime(2024, 1, 8, 9, 0)

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(slots, naive_start, count=1)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="start_datetime_awareness",
    )


def test_next_slots_rejects_naive_slot_tuples_without_raw_type_errors():
    naive_slots = (datetime(2024, 1, 8, 9, 0),)

    with pytest.raises(SessionClockError, match="slot_datetime_awareness") as exc_info:
        next_slots(naive_slots, _dt(2024, 1, 8, 9, 0), count=1)

    assert exc_info.value.check == "slot_datetime_awareness"
    assert str(date(2024, 1, 8)) in str(exc_info.value)


def test_next_slots_rejects_mixed_aware_and_naive_slots():
    trade_date = date(2024, 1, 8)
    slots = build_trading_slots(trade_date, date(2024, 1, 5), _rule())
    mixed_slots = _with_slot_values(
        slots,
        (slots[0], slots[1].replace(tzinfo=None), *slots[2:]),
    )

    with pytest.raises(SessionClockError) as exc_info:
        next_slots(mixed_slots, slots[0], count=1)

    _assert_structured_error(
        exc_info.value,
        trade_date=trade_date,
        check="slot_datetime_awareness",
    )


def test_equivalent_aware_utc_start_and_slots_compare_by_instant():
    trade_date = date(2024, 1, 8)
    slots = build_trading_slots(trade_date, date(2024, 1, 5), _rule())
    utc = ZoneInfo("UTC")
    utc_slots = _with_slot_values(
        slots,
        tuple(slot.astimezone(utc) for slot in slots),
    )
    start_utc = slots[0].astimezone(utc)

    assert next_slots(slots, start_utc, count=2) == slots[:2]
    assert next_slots(utc_slots, slots[0], count=2) == utc_slots[:2]
    assert fifteen_minute_buckets(utc_slots, _rule())[0] == utc_slots[:15]


def _write_session_csv(tmp_path, row):
    path = tmp_path / "sessions.csv"
    path.write_text(
        f"exchange,product,effective_start,effective_end,night_end,version\n{row}\n",
        encoding="utf-8",
    )
    return path


def test_csv_rejects_extra_row_cells_with_row_number(tmp_path):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2020-01-01,,none,commodity-v1,extra",
    )

    with pytest.raises(ValueError, match="session_rules_csv_row_width: row 2"):
        load_session_rules(path)


def test_csv_rejects_missing_row_cells_with_row_number(tmp_path):
    path = _write_session_csv(tmp_path, "SHFE,AU,2020-01-01,,none")

    with pytest.raises(ValueError, match="session_rules_csv_row_width: row 2"):
        load_session_rules(path)


@pytest.mark.parametrize(
    "field",
    ["exchange", "product", "effective_start", "night_end", "version"],
)
def test_csv_rejects_empty_required_fields_with_row_and_field(tmp_path, field):
    values = {
        "exchange": "SHFE",
        "product": "AU",
        "effective_start": "2020-01-01",
        "effective_end": "",
        "night_end": "none",
        "version": SESSION_RULES_VERSION,
    }
    values[field] = ""
    path = _write_session_csv(
        tmp_path,
        ",".join(
            values[column]
            for column in (
                "exchange",
                "product",
                "effective_start",
                "effective_end",
                "night_end",
                "version",
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match=rf"session_rules_csv_required: row 2 field {field}",
    ):
        load_session_rules(path)


def test_csv_rejects_an_unsupported_version_with_row_field_and_value(tmp_path):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2020-01-01,,none,commodity-v2",
    )

    with pytest.raises(
        ValueError,
        match=("session_rules_csv_version: row 2 field version value 'commodity-v2'"),
    ):
        load_session_rules(path)


def test_csv_rejects_reversed_dates_with_row_field_and_value(tmp_path):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2020-02-01,2020-01-31,none,commodity-v1",
    )

    with pytest.raises(
        ValueError,
        match=(
            "session_rules_csv_date_order: row 2 field effective_end value '2020-01-31'"
        ),
    ):
        load_session_rules(path)


@pytest.mark.parametrize(
    ("field", "row"),
    [
        ("effective_start", "SHFE,AU,not-a-date,,none,commodity-v1"),
        ("effective_end", "SHFE,AU,2020-01-01,not-a-date,none,commodity-v1"),
    ],
)
def test_csv_wraps_malformed_dates_with_row_field_and_value(tmp_path, field, row):
    path = _write_session_csv(tmp_path, row)

    with pytest.raises(
        ValueError,
        match=rf"session_rules_csv_date: row 2 field {field} value 'not-a-date'",
    ):
        load_session_rules(path)


def _assert_same_clock_context(actual, expected):
    assert isinstance(actual, tuple)
    assert isinstance(actual, TradingSlots)
    assert actual.exchange == expected.exchange
    assert actual.product == expected.product
    assert actual.trade_date == expected.trade_date
    assert actual.previous_trade_date == expected.previous_trade_date


def test_next_slots_preserves_authoritative_clock_context():
    rule = _rule()
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    window = next_slots(slots, slots[0], count=5)

    assert window == tuple(slots[:5])
    _assert_same_clock_context(window, slots)


def test_every_fifteen_minute_bucket_preserves_authoritative_clock_context():
    rule = _rule()
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    buckets = fifteen_minute_buckets(slots, rule)

    assert buckets
    assert all(bucket == tuple(bucket) for bucket in buckets)
    for bucket in buckets:
        _assert_same_clock_context(bucket, slots)
