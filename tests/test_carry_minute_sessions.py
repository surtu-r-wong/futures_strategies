from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cta_carry.minute_sessions import (
    DAY_SEGMENTS,
    SESSION_RULES_CAPTURE_START,
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
    validate_capture_coverage,
)
from cta_carry.data import CarryDataSet
from cta_carry.session_authority import (
    EffectiveAuthorityRange,
    NoNightDate,
    SessionAuthority,
)
from scripts.carry import capture_minute_sessions as capture_module
from scripts.carry.capture_minute_sessions import (
    SessionCaptureError,
    build_audit_key_sets,
    build_default_liquidity_audit,
    classify_session_boundary,
    collapse_session_rules,
    select_audit_candidates,
    select_session_candidates,
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


def test_capture_coverage_includes_the_entire_backtest_prewarm():
    backtest_start = date(2013, 1, 4)

    assert SESSION_RULES_CAPTURE_START == date(2011, 1, 1)
    assert validate_capture_coverage(
        capture_start=date(2011, 1, 1),
        backtest_start=backtest_start,
        prewarm_calendar_days=730,
    ) == date(2011, 1, 5)
    assert validate_capture_coverage(
        capture_start=date(2011, 1, 5),
        backtest_start=backtest_start,
        prewarm_calendar_days=730,
    ) == date(2011, 1, 5)

    with pytest.raises(
        SessionClockError,
        match="session_asset_prewarm_coverage",
    ) as exc_info:
        validate_capture_coverage(
            capture_start=date(2011, 1, 6),
            backtest_start=backtest_start,
            prewarm_calendar_days=730,
        )

    assert exc_info.value.exchange == "*"
    assert exc_info.value.product == "*"
    assert exc_info.value.trade_date == backtest_start
    assert exc_info.value.check == "session_asset_prewarm_coverage"
    assert exc_info.value.reason == (
        "session asset begins after the minute-state prewarm; "
        "capture_start=2011-01-06; required_start=2011-01-05"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_start", "2011-01-01"),
        ("capture_start", datetime(2011, 1, 1)),
        ("backtest_start", "2013-01-04"),
        ("backtest_start", datetime(2013, 1, 4)),
    ],
)
def test_capture_coverage_requires_concrete_dates(field, value):
    values = {
        "capture_start": date(2011, 1, 1),
        "backtest_start": date(2013, 1, 4),
        "prewarm_calendar_days": 730,
    }
    values[field] = value

    with pytest.raises(ValueError, match="session_capture_coverage_dates"):
        validate_capture_coverage(**values)


@pytest.mark.parametrize("prewarm_calendar_days", [0, -1, True, 730.0, "730"])
def test_capture_coverage_requires_a_positive_actual_integer(
    prewarm_calendar_days,
):
    with pytest.raises(ValueError, match="session_capture_coverage_prewarm"):
        validate_capture_coverage(
            capture_start=date(2011, 1, 1),
            backtest_start=date(2013, 1, 4),
            prewarm_calendar_days=prewarm_calendar_days,
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


def test_2330_night_session_produces_ten_buckets_without_crossing_into_day():
    rule = SessionRule(
        exchange="DCE",
        product="I",
        effective_start=date(2015, 5, 11),
        effective_end=None,
        segments=(
            SessionSegment(-180, -30),
            *(SessionSegment(*item) for item in DAY_SEGMENTS),
        ),
        version=SESSION_RULES_VERSION,
    )
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )

    buckets = fifteen_minute_buckets(slots, rule)

    night_buckets = buckets[:10]
    assert len(slots) == 375
    assert len(buckets) == 25
    assert len(night_buckets) == 10
    assert all(len(bucket) == 15 for bucket in night_buckets)
    assert tuple(slot for bucket in night_buckets for slot in bucket) == slots[:150]
    assert night_buckets[0][0] == _dt(2024, 1, 5, 21, 0)
    assert night_buckets[-1][-1] == _dt(2024, 1, 5, 23, 29)
    assert buckets[10][0] == _dt(2024, 1, 8, 9, 0)


@pytest.mark.parametrize(
    ("night_end", "night_segment"),
    [
        ("none", None),
        ("23:00", SessionSegment(-180, -60)),
        ("23:30", SessionSegment(-180, -30)),
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


def test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products():
    rules = load_session_rules(Path("config/carry_minute_sessions.csv"))

    assert rules
    for product in ("RB", "AU", "SC", "AP", "JD"):
        assert any(rule.product == product for rule in rules)

    grouped = {}
    for rule in rules:
        grouped.setdefault((rule.exchange, rule.product), []).append(rule)
    for product_rules in grouped.values():
        ordered = sorted(product_rules, key=lambda item: item.effective_start)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            assert previous.effective_end is not None
            assert previous.effective_end < current.effective_start


def _captured_boundary(*, night_end):
    trade_date = date(2024, 1, 8)
    previous = date(2024, 1, 5)
    values = {
        "trade_date": trade_date,
        "previous_trade_date": previous,
        "exchange": "SHFE",
        "product": "AU",
        "daily_contract": "AU2406.SHF",
        "day_1_first": _dt(2024, 1, 8, 9, 0),
        "day_1_last": _dt(2024, 1, 8, 10, 14),
        "day_2_first": _dt(2024, 1, 8, 10, 30),
        "day_2_last": _dt(2024, 1, 8, 11, 29),
        "day_3_first": _dt(2024, 1, 8, 13, 30),
        "day_3_last": _dt(2024, 1, 8, 14, 59),
    }
    if night_end == "none":
        values.update(night_first=None, night_last=None)
    else:
        last = {
            "23:00": _dt(2024, 1, 5, 22, 59),
            "23:30": _dt(2024, 1, 5, 23, 29),
            "01:00": _dt(2024, 1, 6, 0, 59),
            "02:30": _dt(2024, 1, 6, 2, 29),
        }[night_end]
        values.update(
            night_first=_dt(2024, 1, 5, 21, 0),
            night_last=last,
        )
    return values


@pytest.mark.parametrize(
    "night_end", ["none", "23:00", "23:30", "01:00", "02:30"]
)
def test_capture_classifies_only_supported_exact_session_boundaries(night_end):
    assert classify_session_boundary(_captured_boundary(night_end=night_end)) == (
        night_end
    )


def test_capture_reverse_maps_2330_session_segment():
    rule = SessionRule(
        exchange="DCE",
        product="I",
        effective_start=date(2015, 5, 11),
        effective_end=None,
        segments=(
            SessionSegment(-180, -30),
            *(SessionSegment(*item) for item in DAY_SEGMENTS),
        ),
        version=SESSION_RULES_VERSION,
    )

    assert capture_module._expected_night_end(rule) == "23:30"


def test_capture_treats_pandas_nat_as_an_absent_night_session():
    row = _captured_boundary(night_end="none")
    row["night_first"] = pd.NaT
    row["night_last"] = pd.NaT

    assert classify_session_boundary(row) == "none"


def test_capture_rejects_a_missing_standard_day_segment():
    row = _captured_boundary(night_end="none")
    row["day_2_last"] = None

    with pytest.raises(SessionCaptureError, match="day_2_last"):
        classify_session_boundary(row)


def test_capture_collapse_accepts_2330_as_an_allowed_boundary():
    trade_date = date(2015, 5, 11)

    assert collapse_session_rules(
        pd.DataFrame(
            [
                {
                    "exchange": "DCE",
                    "product": "I",
                    "trade_date": trade_date,
                    "night_end": "23:30",
                }
            ]
        ),
        global_calendar=[trade_date],
        audit_keys={("DCE", "I", trade_date)},
    ) == [
        {
            "exchange": "DCE",
            "product": "I",
            "effective_start": trade_date,
            "effective_end": trade_date,
            "night_end": "23:30",
            "version": SESSION_RULES_VERSION,
        }
    ]


def test_capture_collapses_every_observed_rule_change_even_inside_a_month():
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 8),
                "night_end": "none",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 9),
                "night_end": "none",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 10),
                "night_end": "02:30",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 11),
                "night_end": "02:30",
            },
        ]
    )

    audit_keys = frozenset(
        (row.exchange, row.product, row.trade_date)
        for row in classified.itertuples(index=False)
    )

    assert collapse_session_rules(
        classified,
        global_calendar=classified["trade_date"].tolist(),
        audit_keys=audit_keys,
    ) == [
        {
            "exchange": "SHFE",
            "product": "AU",
            "effective_start": date(2024, 1, 8),
            "effective_end": date(2024, 1, 9),
            "night_end": "none",
            "version": SESSION_RULES_VERSION,
        },
        {
            "exchange": "SHFE",
            "product": "AU",
            "effective_start": date(2024, 1, 10),
            "effective_end": date(2024, 1, 11),
            "night_end": "02:30",
            "version": SESSION_RULES_VERSION,
        },
    ]


def test_capture_selects_highest_oi_deterministically_and_uses_trading_lag():
    prices = pd.DataFrame(
        [
            {
                "trade_date": date(2024, 1, 5),
                "product": "RB",
                "contract": "RB2405.SHF",
                "oi": 10.0,
                "volume": 10.0,
            },
            {
                "trade_date": date(2024, 1, 8),
                "product": "RB",
                "contract": "RB2405.SHF",
                "oi": 100.0,
                "volume": 50.0,
            },
            {
                "trade_date": date(2024, 1, 8),
                "product": "RB",
                "contract": "RB2410.SHF",
                "oi": 100.0,
                "volume": 40.0,
            },
        ]
    )

    selected = select_session_candidates(
        prices,
        start=date(2024, 1, 8),
        end=date(2024, 1, 8),
    )

    assert len(selected) == 1
    assert selected[0].previous_trade_date == date(2024, 1, 5)
    assert selected[0].candidate.daily_contract == "RB2405.SHF"
    assert selected[0].candidate.minute_symbol == "RB2405"
    assert selected[0].candidate.window_start == _dt(2024, 1, 5, 21, 0)


def _audit_key(day):
    return ("SHFE", "RB", day)


def _audit_price(
    day,
    *,
    product="RB",
    contract="RB2405.SHF",
    oi=100.0,
    volume=50.0,
    turnover=5_000_000_000.0,
):
    return {
        "trade_date": day,
        "product": product,
        "contract": contract,
        "oi": oi,
        "volume": volume,
        "turnover": turnover,
    }


def test_audit_envelope_emits_across_the_requested_start_boundary():
    calendar = [date(2023, 12, 28), date(2023, 12, 29), date(2024, 1, 2)]
    pool = {_audit_key(date(2023, 12, 28))}

    envelope = build_audit_key_sets(
        normalized_keys={_audit_key(date(2024, 1, 2))},
        in_pool_keys=pool,
        global_calendar=calendar,
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )

    target = _audit_key(date(2024, 1, 2))
    assert envelope.normalized_keys == frozenset({target})
    assert envelope.in_pool_keys == frozenset()
    assert envelope.audit_universe_keys == frozenset({target})
    assert envelope.audit_keys == frozenset({target})


@pytest.mark.parametrize("pool_offset", [0, 1, 2])
def test_audit_envelope_includes_pool_membership_at_t_and_two_lags(pool_offset):
    calendar = [date(2024, 1, day) for day in (2, 3, 4)]
    target = calendar[2]

    envelope = build_audit_key_sets(
        normalized_keys={_audit_key(target)},
        in_pool_keys={_audit_key(calendar[2 - pool_offset])},
        global_calendar=calendar,
        start=target,
        end=target,
    )

    assert envelope.audit_keys == frozenset({_audit_key(target)})


def test_audit_envelope_with_no_pool_membership_has_no_audit_keys():
    target = date(2024, 1, 4)
    envelope = build_audit_key_sets(
        normalized_keys={_audit_key(target)},
        in_pool_keys=set(),
        global_calendar=[date(2024, 1, day) for day in (2, 3, 4)],
        start=target,
        end=target,
    )

    assert envelope.in_pool_keys == frozenset()
    assert envelope.audit_keys == frozenset()
    assert envelope.audit_universe_keys == envelope.normalized_keys


def test_synthetic_exit_candidate_keeps_target_day_without_daily_rows():
    source_day = date(2024, 1, 2)
    target_day = date(2024, 1, 4)
    prices = pd.DataFrame([_audit_price(source_day)])

    selected = select_audit_candidates(
        prices,
        audit_keys={_audit_key(target_day)},
        in_pool_source_keys={_audit_key(source_day)},
        global_calendar=[source_day, date(2024, 1, 3), target_day],
    )

    assert len(selected) == 1
    assert selected[0].candidate.trade_date == target_day
    assert selected[0].candidate.daily_contract == "RB2405.SHF"
    assert selected[0].candidate.candidate_role == "session_representative"
    assert selected[0].candidate.causal_in_pool_date == source_day
    assert selected[0].candidate.selection_source == "causal_in_pool_main"
    assert selected[0].causal_in_pool_date == source_day
    assert selected[0].selection_source == "causal_in_pool_main"


@pytest.mark.parametrize(
    ("outer_causal_date", "outer_selection_source"),
    [
        (date(2024, 1, 3), "causal_in_pool_main"),
        (date(2024, 1, 2), "target_day_main"),
        ("2024-01-02", "causal_in_pool_main"),
        (date(2024, 1, 2), ""),
    ],
)
def test_captured_representative_rejects_outer_provenance_mismatch_or_bad_types(
    outer_causal_date,
    outer_selection_source,
):
    source_day = date(2024, 1, 2)
    target_day = date(2024, 1, 4)
    selected = select_audit_candidates(
        pd.DataFrame([_audit_price(source_day)]),
        audit_keys={_audit_key(target_day)},
        in_pool_source_keys={_audit_key(source_day)},
        global_calendar=[source_day, date(2024, 1, 3), target_day],
    )[0]

    with pytest.raises(SessionCaptureError, match="captured_candidate_metadata"):
        capture_module.CapturedCandidate(
            candidate=selected.candidate,
            previous_trade_date=selected.previous_trade_date,
            causal_in_pool_date=outer_causal_date,
            selection_source=outer_selection_source,
        )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            [
                _audit_price(
                    date(2024, 1, 4),
                    contract="RB2405.SHF",
                    oi=101,
                    volume=1,
                ),
                _audit_price(
                    date(2024, 1, 4),
                    contract="RB2410.SHF",
                    oi=100,
                    volume=999,
                ),
            ],
            "RB2405.SHF",
        ),
        (
            [
                _audit_price(
                    date(2024, 1, 4),
                    contract="RB2405.SHF",
                    volume=20,
                ),
                _audit_price(
                    date(2024, 1, 4),
                    contract="RB2410.SHF",
                    volume=30,
                ),
            ],
            "RB2410.SHF",
        ),
        (
            [
                _audit_price(date(2024, 1, 4), contract="RB2410.SHF"),
                _audit_price(date(2024, 1, 4), contract="RB2405.SHF"),
            ],
            "RB2405.SHF",
        ),
    ],
)
def test_audit_representative_uses_oi_volume_contract_tie_breaks(rows, expected):
    target = date(2024, 1, 4)
    selected = select_audit_candidates(
        pd.DataFrame([_audit_price(date(2024, 1, 3)), *rows]),
        audit_keys={_audit_key(target)},
        in_pool_source_keys={_audit_key(target)},
        global_calendar=[date(2024, 1, 3), target],
    )

    assert selected[0].candidate.daily_contract == expected
    assert selected[0].candidate.candidate_role == "session_representative"
    assert selected[0].candidate.causal_in_pool_date == target
    assert selected[0].candidate.selection_source == "target_day_main"
    assert selected[0].selection_source == "target_day_main"


def _liquidity_prices(days, *, product="RB", contract="RB2405.SHF", turnover=None):
    values = turnover or [5_000_000_000.0] * len(days)
    return [
        _audit_price(
            day,
            product=product,
            contract=contract,
            turnover=value,
        )
        for day, value in zip(days, values, strict=True)
    ]


def test_default_liquidity_envelope_uses_threshold_equality_and_per_product_means():
    days = [date(2023, 9, 1) + timedelta(days=offset) for offset in range(121)]
    prices = pd.DataFrame(
        [
            *_liquidity_prices(days),
            *_liquidity_prices(
                days,
                product="AU",
                contract="AU2406.SHF",
                turnover=[6_000_000_000.0] * len(days),
            ),
        ]
    )

    audit = build_default_liquidity_audit(
        prices,
        history_starts=pd.DataFrame(
            [
                {"product": "AU", "first_trade_date": days[0]},
                {"product": "RB", "first_trade_date": days[0]},
            ]
        ),
        history_exceptions=(),
        start=days[-1],
        end=days[-1],
    )

    assert audit.config.prewarm_calendar_days == 730
    assert audit.config.liquidity_window == 120
    assert audit.history_status_by_key == {
        ("SHFE", "AU", days[-1]): "finite",
        ("SHFE", "RB", days[-1]): "finite",
    }
    assert audit.key_sets.in_pool_keys == frozenset(
        {
            ("SHFE", "AU", days[-1]),
            _audit_key(days[-1]),
        }
    )


def test_default_liquidity_envelope_marks_short_new_listing_out_of_pool():
    days = [date(2024, 1, day) for day in (2, 3, 4)]
    audit = build_default_liquidity_audit(
        pd.DataFrame(_liquidity_prices(days)),
        history_starts=pd.DataFrame([{"product": "RB", "first_trade_date": days[0]}]),
        history_exceptions=(),
        start=days[-1],
        end=days[-1],
    )

    key = _audit_key(days[-1])
    assert audit.history_status_by_key[key] == "insufficient_since_inception"
    assert key not in audit.key_sets.in_pool_keys


def test_default_liquidity_envelope_rejects_unexplained_old_history_gap():
    days = [date(2024, 1, day) for day in (2, 3, 4)]

    with pytest.raises(SessionCaptureError, match="liquidity_history_incomplete"):
        build_default_liquidity_audit(
            pd.DataFrame(_liquidity_prices(days)),
            history_starts=pd.DataFrame(
                [{"product": "RB", "first_trade_date": date(2004, 8, 25)}]
            ),
            history_exceptions=(),
            start=days[-1],
            end=days[-1],
        )


def test_exact_history_exception_authorizes_gap_but_does_not_enter_pool():
    days = [date(2024, 1, day) for day in (2, 3, 4)]
    target = days[-1]
    exception = EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="RB",
        effective_start=target,
        effective_end=target,
        reason="documented suspension",
        source_url="https://www.shfe.com.cn/official-notice",
    )

    audit = build_default_liquidity_audit(
        pd.DataFrame(_liquidity_prices(days)),
        history_starts=pd.DataFrame(
            [{"product": "RB", "first_trade_date": date(2004, 8, 25)}]
        ),
        history_exceptions=(exception,),
        start=target,
        end=target,
    )

    key = _audit_key(target)
    assert audit.history_status_by_key[key] == "authorized_history_gap"
    assert key not in audit.key_sets.in_pool_keys


def _history_exception(target):
    return EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="RB",
        effective_start=target,
        effective_end=target,
        reason="documented suspension",
        source_url="https://www.shfe.com.cn/official-notice",
    )


def _empty_history_starts():
    return pd.DataFrame(columns=["product", "first_trade_date"])


def test_default_liquidity_build_constructs_one_reduced_representative_index(
    monkeypatch,
):
    days = [date(2023, 9, 1) + timedelta(days=offset) for offset in range(121)]
    prices = pd.DataFrame(
        [
            *_liquidity_prices(days),
            *_liquidity_prices(
                days,
                contract="RB2410.SHF",
                turnover=[1_000_000_000.0] * len(days),
            ),
        ]
    )
    original_builder = getattr(capture_module, "_build_representative_index")
    calls = 0
    representative_size = None

    def spy_builder(frame):
        nonlocal calls, representative_size
        calls += 1
        assert frame is prices
        result = original_builder(frame)
        representative_size = len(result)
        return result

    original_to_dict = pd.DataFrame.to_dict

    def reject_full_contract_expansion(frame, *args, **kwargs):
        if frame is prices:
            raise AssertionError("full contract frame must not expand to records")
        return original_to_dict(frame, *args, **kwargs)

    monkeypatch.setattr(
        capture_module,
        "_build_representative_index",
        spy_builder,
    )
    monkeypatch.setattr(pd.DataFrame, "to_dict", reject_full_contract_expansion)

    audit = build_default_liquidity_audit(
        prices,
        history_starts=pd.DataFrame([{"product": "RB", "first_trade_date": days[0]}]),
        history_exceptions=(),
        start=days[-1],
        end=days[-1],
    )

    assert calls == 1
    assert representative_size == len(days)
    assert audit.key_sets.in_pool_keys == frozenset({_audit_key(days[-1])})


def test_missing_history_start_cannot_be_authorized_by_an_exact_exception():
    days = [date(2024, 1, day) for day in (2, 3, 4)]
    target = days[-1]

    with pytest.raises(
        SessionCaptureError,
        match=r"liquidity_history_incomplete: .*product=RB.*missing_history_start",
    ):
        build_default_liquidity_audit(
            pd.DataFrame(_liquidity_prices(days)),
            history_starts=_empty_history_starts(),
            history_exceptions=(_history_exception(target),),
            start=target,
            end=target,
        )


def test_finite_liquidity_still_requires_a_history_start_row():
    days = [date(2023, 9, 1) + timedelta(days=offset) for offset in range(121)]

    with pytest.raises(
        SessionCaptureError,
        match=r"liquidity_history_incomplete: .*product=RB.*missing_history_start",
    ):
        build_default_liquidity_audit(
            pd.DataFrame(_liquidity_prices(days)),
            history_starts=_empty_history_starts(),
            history_exceptions=(),
            start=days[-1],
            end=days[-1],
        )


def test_history_start_after_loaded_product_data_is_rejected_with_dates():
    days = [date(2024, 1, day) for day in (2, 3, 4)]

    with pytest.raises(
        SessionCaptureError,
        match=(
            r"liquidity_history_incomplete: .*product=RB.*"
            r"first_trade_date=2024-01-05.*loaded_first_trade_date=2024-01-02"
        ),
    ):
        build_default_liquidity_audit(
            pd.DataFrame(_liquidity_prices(days)),
            history_starts=pd.DataFrame(
                [{"product": "RB", "first_trade_date": date(2024, 1, 5)}]
            ),
            history_exceptions=(),
            start=days[-1],
            end=days[-1],
        )


def test_synthetic_exit_uses_the_latest_of_two_causal_pool_producers():
    first_source = date(2024, 1, 2)
    latest_source = date(2024, 1, 3)
    target = date(2024, 1, 4)
    prices = pd.DataFrame(
        [
            _audit_price(
                first_source,
                contract="RB2405.SHF",
                oi=500,
            ),
            _audit_price(
                latest_source,
                contract="RB2405.SHF",
                oi=100,
            ),
            _audit_price(
                latest_source,
                contract="RB2410.SHF",
                oi=200,
            ),
        ]
    )

    selected = select_audit_candidates(
        prices,
        audit_keys={_audit_key(target)},
        in_pool_source_keys={
            _audit_key(first_source),
            _audit_key(latest_source),
        },
        global_calendar=[first_source, latest_source, target],
    )

    assert selected[0].candidate.trade_date == target
    assert selected[0].candidate.daily_contract == "RB2410.SHF"
    assert selected[0].causal_in_pool_date == latest_source
    assert selected[0].selection_source == "causal_in_pool_main"


def test_capture_entry_wires_default_config_history_and_audit_candidates(
    monkeypatch,
    tmp_path,
):
    class BoundaryReached(RuntimeError):
        pass

    captured = {}
    start = date(2024, 1, 2)
    end = date(2024, 1, 4)
    prices = object()
    history_starts = object()
    candidates = (object(),)
    empty_keys = capture_module.AuditKeySets(
        normalized_keys=frozenset(),
        in_pool_keys=frozenset(),
        audit_universe_keys=frozenset(),
        audit_keys=frozenset(),
    )
    empty_quality = pd.DataFrame(
        columns=["object_type", "object_id", "trade_date", "status", "action"]
    )

    def fake_daily_loader(**kwargs):
        captured["daily_loader"] = kwargs
        return SimpleNamespace(prices=prices, data_quality=empty_quality)

    def fake_history_loader(**kwargs):
        captured["history_loader"] = kwargs
        return history_starts

    def fake_authority_loader(**kwargs):
        captured["authority_paths"] = kwargs
        return _authority()

    def fake_audit_builder(frame, **kwargs):
        captured["audit_builder_frame"] = frame
        captured["audit_builder"] = kwargs
        return SimpleNamespace(
            key_sets=empty_keys,
            candidates=candidates,
            global_calendar=(start,),
        )

    def reject_legacy_selector(*args, **kwargs):
        raise AssertionError("legacy unfiltered selector must not be called")

    def stop_at_boundaries(source, selected):
        captured["boundary_source"] = source
        captured["boundary_candidates"] = selected
        raise BoundaryReached

    source = object()
    monkeypatch.setattr(capture_module, "load_public_carry_data", fake_daily_loader)
    monkeypatch.setattr(
        capture_module,
        "load_public_product_history_starts",
        fake_history_loader,
    )
    monkeypatch.setattr(
        capture_module,
        "load_session_authority",
        fake_authority_loader,
    )
    monkeypatch.setattr(
        capture_module,
        "_build_default_liquidity_audit",
        fake_audit_builder,
    )
    monkeypatch.setattr(
        capture_module,
        "select_session_candidates",
        reject_legacy_selector,
    )
    monkeypatch.setattr(
        capture_module,
        "PublicMinuteSource",
        lambda **kwargs: source,
    )
    monkeypatch.setattr(
        capture_module,
        "capture_session_boundaries",
        stop_at_boundaries,
    )

    settings = tmp_path / "settings.yaml"
    with pytest.raises(BoundaryReached):
        capture_module.capture_and_publish(
            start=start,
            end=end,
            backtest_start=date(2027, 1, 1),
            inventory_output=tmp_path / "inventory.csv",
            audit_report=tmp_path / "audit.txt",
            output=tmp_path / "sessions.csv",
            settings=settings,
            use_test=True,
        )

    loader_config = captured["daily_loader"]["config"]
    assert loader_config == capture_module.CarryConfig()
    assert loader_config.prewarm_calendar_days == 730
    assert captured["daily_loader"] == {
        "start": start,
        "end": end,
        "config": loader_config,
        "config_path": settings,
        "use_test": True,
    }
    assert captured["history_loader"] == {
        "config_path": settings,
        "use_test": True,
    }
    assert captured["authority_paths"] == {
        "no_night_path": capture_module.NO_NIGHT_PATH,
        "day_only_path": capture_module.DAY_ONLY_PATH,
        "history_exception_path": capture_module.HISTORY_EXCEPTIONS_PATH,
    }
    assert captured["audit_builder_frame"] is prices
    assert captured["audit_builder"]["history_starts"] is history_starts
    assert captured["audit_builder"]["history_exceptions"] == ()
    assert captured["audit_builder"]["config"] is loader_config
    assert captured["boundary_source"] is source
    assert captured["boundary_candidates"] is candidates


def _authority(*, no_night=(), day_only=(), hashes=None):
    return SessionAuthority(
        no_night_dates=tuple(no_night),
        day_only_regimes=tuple(day_only),
        liquidity_history_exceptions=(),
        sha256_by_asset=hashes
        or {
            "no_night": "a" * 64,
            "day_only": "b" * 64,
            "history_exception": "c" * 64,
        },
    )


def _observation(day, previous, *, night_end, exchange="SHFE", product="AU"):
    row = {
        "trade_date": day,
        "previous_trade_date": previous,
        "exchange": exchange,
        "product": product,
        "daily_contract": f"{product}2406.SHF",
        "day_1_first": _at_for_test(day, 9, 0),
        "day_1_last": _at_for_test(day, 10, 14),
        "day_2_first": _at_for_test(day, 10, 30),
        "day_2_last": _at_for_test(day, 11, 29),
        "day_3_first": _at_for_test(day, 13, 30),
        "day_3_last": _at_for_test(day, 14, 59),
    }
    if night_end == "none":
        row.update(night_first=None, night_last=None)
        return row
    after_midnight = previous + timedelta(days=1)
    end_day, hour, minute = {
        "23:00": (previous, 22, 59),
        "23:30": (previous, 23, 29),
        "01:00": (after_midnight, 0, 59),
        "02:30": (after_midnight, 2, 29),
    }[night_end]
    row.update(
        night_first=_at_for_test(previous, 21, 0),
        night_last=_at_for_test(end_day, hour, minute),
    )
    return row


def _at_for_test(day, hour, minute):
    return datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=hour, minute=minute
    )


def test_authority_rejects_weekend_shaped_none_without_authority():
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    rows, ambiguities = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="none")]),
        _authority(),
    )

    assert rows.empty
    assert [item.check for item in ambiguities] == ["night_authority_conflict"]


@pytest.mark.parametrize("authority_kind", ["holiday", "day_only"])
def test_authority_accepts_declared_none(authority_kind):
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    no_night = ()
    day_only = ()
    if authority_kind == "holiday":
        no_night = (
            NoNightDate(
                version=SESSION_RULES_VERSION,
                exchange="SHFE",
                trade_date=monday,
                reason="notice_evening=2024-01-05 holiday halt",
                source_url="https://www.shfe.com.cn/example",
            ),
        )
    else:
        day_only = (
            EffectiveAuthorityRange(
                version=SESSION_RULES_VERSION,
                exchange="SHFE",
                product="AU",
                effective_start=monday,
                effective_end=monday,
                reason="documented day-only regime",
                source_url="https://www.shfe.com.cn/example",
            ),
        )

    rows, ambiguities = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="none")]),
        _authority(no_night=no_night, day_only=day_only),
    )

    assert ambiguities == ()
    assert rows.to_dict("records") == [
        {
            "exchange": "SHFE",
            "product": "AU",
            "trade_date": monday,
            "night_end": "none",
        }
    ]


def test_authority_collects_every_unauthorized_continuous_none():
    days = [date(2024, 1, day) for day in (8, 9, 10)]
    rows, ambiguities = capture_module.classify_authorized_boundaries(
        pd.DataFrame(
            [
                _observation(day, day - timedelta(days=1), night_end="none")
                for day in reversed(days)
            ]
        ),
        _authority(),
    )

    assert rows.empty
    assert [item.trade_date for item in ambiguities] == days
    assert all(item.check == "night_authority_conflict" for item in ambiguities)


def test_authority_rejects_observed_night_inside_declared_none():
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    declared = NoNightDate(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        trade_date=monday,
        reason="notice_evening=2024-01-05 holiday halt",
        source_url="https://www.shfe.com.cn/example",
    )

    rows, ambiguities = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="23:00")]),
        _authority(no_night=(declared,)),
    )

    assert rows.empty
    assert [item.check for item in ambiguities] == ["night_authority_conflict"]


def test_collapse_keeps_night_none_night_as_three_rules():
    days = [date(2024, 1, day) for day in (8, 9, 10)]
    audit_keys = frozenset(("SHFE", "AU", day) for day in days)
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": day,
                "night_end": value,
            }
            for day, value in zip(days, ("23:00", "none", "23:00"), strict=True)
        ]
    )

    rules = collapse_session_rules(
        classified,
        global_calendar=days,
        audit_keys=audit_keys,
    )

    assert [(row["effective_start"], row["effective_end"]) for row in rules] == [
        (days[0], days[0]),
        (days[1], days[1]),
        (days[2], days[2]),
    ]


@pytest.mark.parametrize("right_night_end", ["23:00", "02:30"])
def test_collapse_never_bridges_an_unaudited_global_trading_day(
    right_night_end, tmp_path
):
    days = [date(2024, 1, day) for day in (8, 9, 10)]
    audit_keys = frozenset(
        {
            ("SHFE", "AU", days[0]),
            ("SHFE", "AU", days[2]),
        }
    )
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": days[0],
                "night_end": "23:00",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": days[2],
                "night_end": right_night_end,
            },
        ]
    )

    rows = collapse_session_rules(
        classified,
        global_calendar=days,
        audit_keys=audit_keys,
    )
    staged = capture_module._stage_session_rules(tmp_path / "rules.csv", rows)
    rules = load_session_rules(staged)

    assert len(rules) == 2
    with pytest.raises(SessionClockError, match="session_rule_cardinality"):
        resolve_session_rule(rules, "SHFE", "AU", days[1])


def _key(day, product="RB"):
    return ("SHFE", product, day)


def test_coverage_report_has_every_requested_year_and_independent_counts():
    day = date(2024, 1, 2)
    key_sets = capture_module.AuditKeySets(
        normalized_keys=frozenset({_key(day), _key(day, "AU")}),
        in_pool_keys=frozenset({_key(day)}),
        audit_universe_keys=frozenset(
            {_key(day), _key(day, "AU"), _key(day, "CU")}
        ),
        audit_keys=frozenset({_key(day), _key(day, "CU")}),
    )
    quality = pd.DataFrame(
        [
            {
                "object_type": "contract_bar",
                "object_id": "RB2405.SHF",
                "trade_date": day,
                "check": "ohlc_integrity",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "bad",
            },
            {
                "object_type": "contract_bar",
                "object_id": "CU2405.SHF",
                "trade_date": day,
                "check": "activity_fields",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "bad",
            },
            {
                "object_type": "contract_bar",
                "object_id": "BAD",
                "trade_date": day,
                "check": "contract_parse",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "bad",
            },
            {
                "object_type": "contract_bar",
                "object_id": "RB2405.SHF",
                "trade_date": pd.NaT,
                "check": "trade_date",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "unparseable_trade_date",
            },
        ]
    )

    report = capture_module.coverage_report(
        data_quality=quality,
        key_sets=key_sets,
        start=date(2023, 12, 31),
        end=date(2025, 1, 1),
    )

    assert [row["coverage_year"] for row in report.rows] == [2023, 2024, 2025]
    assert report.rows[0]["in_pool_ratio"] == "0.000000"
    assert report.rows[0]["audited_ratio"] == "0.000000"
    assert report.rows[1] == {
        "coverage_year": 2024,
        "all_product_days": 2,
        "in_pool_days": 1,
        "in_pool_ratio": "0.500000",
        "audit_universe_days": 3,
        "audited_days": 2,
        "audited_ratio": "0.666667",
        "normalization_excluded_product_days": 1,
        "normalization_unkeyable_rows": 1,
    }
    assert report.unknown_date_unkeyable_rows == 1
    assert report.has_unkeyable


def _authority_files(tmp_path):
    payloads = {
        "no_night": b"no-night-authority",
        "day_only": b"day-only-authority",
        "history_exception": b"history-exception-authority",
    }
    paths = {}
    for name, payload in payloads.items():
        path = tmp_path / f"{name}.csv"
        path.write_bytes(payload)
        paths[name] = path
    hashes = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
    }
    return paths, hashes


def _single_rule_capture(tmp_path):
    previous = date(2024, 1, 5)
    day = date(2024, 1, 8)
    audit_keys = frozenset({("SHFE", "AU", day)})
    boundaries = pd.DataFrame(
        [_observation(day, previous, night_end="23:00")]
    )
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": day,
                "night_end": "23:00",
            }
        ]
    )
    rows = collapse_session_rules(
        classified,
        global_calendar=[previous, day],
        audit_keys=audit_keys,
    )
    paths, hashes = _authority_files(tmp_path)
    return boundaries, rows, [previous, day], audit_keys, paths, hashes


@pytest.mark.parametrize("problem", ["zero", "missing", "duplicate"])
def test_boundary_key_validation_fails_closed(problem, tmp_path):
    previous = date(2024, 1, 5)
    day = date(2024, 1, 8)
    other = date(2024, 1, 9)
    row = _observation(day, previous, night_end="23:00")
    if problem == "zero":
        boundaries = pd.DataFrame(columns=row)
    elif problem == "missing":
        boundaries = pd.DataFrame([row])
    else:
        boundaries = pd.DataFrame([row, row])
    expected = (
        frozenset({("SHFE", "AU", other)})
        if problem == "missing"
        else frozenset({("SHFE", "AU", day)})
    )

    with pytest.raises(SessionCaptureError, match=f"boundary_{problem}"):
        capture_module.validate_boundary_keys(boundaries, expected)

    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    paths, hashes = _authority_files(tmp_path)
    with pytest.raises(SessionCaptureError, match=f"boundary_{problem}"):
        capture_module.publish_session_rules(
            output=output,
            rule_rows=(),
            boundaries=boundaries,
            global_calendar=[previous, day, other],
            audit_keys=expected,
            authority=_authority(hashes=hashes),
            authority_paths=paths,
        )

    assert output.read_bytes() == b"old\n"
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []


def test_boundary_keys_equal_audit_keys_on_success():
    day = date(2024, 1, 8)
    boundaries = pd.DataFrame(
        [_observation(day, date(2024, 1, 5), night_end="23:00")]
    )
    audit_keys = frozenset({("SHFE", "AU", day)})

    assert capture_module.validate_boundary_keys(boundaries, audit_keys) == audit_keys


@pytest.mark.parametrize(
    "failure",
    ["hash_mismatch", "loader_replay", "reverse_key_mismatch", "replace"],
)
def test_atomic_publisher_preserves_old_asset_and_removes_temporary_files(
    failure, monkeypatch, tmp_path
):
    (
        boundaries,
        rows,
        calendar,
        audit_keys,
        paths,
        hashes,
    ) = _single_rule_capture(tmp_path)
    output = tmp_path / "sessions.csv"
    original = b"old-authoritative-bytes\n"
    output.write_bytes(original)
    authority = _authority(hashes=hashes)

    if failure == "hash_mismatch":
        authority = _authority(hashes={**hashes, "day_only": "0" * 64})
    elif failure == "loader_replay":
        monkeypatch.setattr(capture_module, "load_session_rules", lambda path: ())
    elif failure == "reverse_key_mismatch":
        monkeypatch.setattr(
            capture_module,
            "expand_rule_keys",
            lambda *args, **kwargs: frozenset(),
        )
    else:
        monkeypatch.setattr(
            capture_module.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("replace failed")
            ),
        )

    with pytest.raises((OSError, SessionCaptureError, SessionClockError)):
        capture_module.publish_session_rules(
            output=output,
            rule_rows=rows,
            boundaries=boundaries,
            global_calendar=calendar,
            audit_keys=audit_keys,
            authority=authority,
            authority_paths=paths,
        )

    assert output.read_bytes() == original
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []


def test_atomic_publisher_replays_and_reverse_expands_before_replace(tmp_path):
    (
        boundaries,
        rows,
        calendar,
        audit_keys,
        paths,
        hashes,
    ) = _single_rule_capture(tmp_path)
    output = tmp_path / "sessions.csv"

    loaded = capture_module.publish_session_rules(
        output=output,
        rule_rows=rows,
        boundaries=boundaries,
        global_calendar=calendar,
        audit_keys=audit_keys,
        authority=_authority(hashes=hashes),
        authority_paths=paths,
    )

    assert output.exists()
    assert len(loaded) == 1
    assert capture_module.expand_rule_keys(
        loaded,
        global_calendar=calendar,
    ) == audit_keys


def test_unkeyable_normalization_refuses_publication():
    report = capture_module.CoverageReport(
        rows=(
            {
                "coverage_year": 2024,
                "normalization_unkeyable_rows": 1,
            },
        ),
        unknown_date_unkeyable_rows=0,
    )

    with pytest.raises(SessionCaptureError, match="normalization_unkeyable"):
        capture_module.require_publishable_coverage(report)


def test_capture_parser_requires_backtest_and_diagnostic_destinations():
    parser = capture_module.build_parser()
    base = [
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-02",
        "--output",
        "sessions.csv",
    ]
    valid = [
        *base,
        "--backtest-start",
        "2026-01-02",
        "--inventory-output",
        "inventory.csv",
        "--audit-report",
        "audit.md",
    ]

    parsed = parser.parse_args(valid)
    assert parsed.backtest_start == date(2026, 1, 2)

    for missing in ("--backtest-start", "--inventory-output", "--audit-report"):
        complete = [
            *base,
            "--backtest-start",
            "2025-01-01",
            "--inventory-output",
            "inventory.csv",
            "--audit-report",
            "audit.md",
        ]
        index = complete.index(missing)
        del complete[index : index + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(complete)


def test_capture_request_enforces_repository_start_and_backtest_prewarm():
    with pytest.raises(SessionCaptureError, match="repository_capture_start"):
        capture_module.validate_capture_request(
            start=SESSION_RULES_CAPTURE_START + timedelta(days=1),
            backtest_start=date(2013, 1, 4),
            output=capture_module.SESSION_RULES_PATH,
            prewarm_calendar_days=730,
        )

    with pytest.raises(SessionClockError, match="session_asset_prewarm_coverage"):
        capture_module.validate_capture_request(
            start=date(2024, 1, 2),
            backtest_start=date(2025, 1, 1),
            output=Path("/tmp/temporary-session-rules.csv"),
            prewarm_calendar_days=730,
        )


def _install_capture_flow(monkeypatch, *, night_ends, data_quality=None):
    previous = date(2024, 1, 5)
    days = [
        date(2024, 1, 8) + timedelta(days=index)
        for index in range(len(night_ends))
    ]
    calendar = [previous, *days]
    keys = frozenset(("SHFE", "AU", day) for day in days)
    key_sets = capture_module.AuditKeySets(
        normalized_keys=keys,
        in_pool_keys=keys,
        audit_universe_keys=keys,
        audit_keys=keys,
    )
    audit = SimpleNamespace(
        key_sets=key_sets,
        candidates=(object(),),
        global_calendar=tuple(calendar),
    )
    boundaries = pd.DataFrame(
        [
            _observation(
                day,
                calendar[index],
                night_end=night_end,
            )
            for index, (day, night_end) in enumerate(
                zip(days, night_ends, strict=True)
            )
        ]
    )
    quality = (
        pd.DataFrame(
            columns=[
                "object_type",
                "object_id",
                "trade_date",
                "check",
                "status",
                "action",
                "reason",
            ]
        )
        if data_quality is None
        else data_quality
    )
    authority = _authority()
    calls = {"calendar_validation": 0}

    monkeypatch.setattr(
        capture_module,
        "load_public_carry_data",
        lambda **kwargs: CarryDataSet(prices=object(), data_quality=quality),
    )
    monkeypatch.setattr(
        capture_module,
        "load_public_product_history_starts",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        capture_module,
        "_build_default_liquidity_audit",
        lambda *args, **kwargs: audit,
    )
    monkeypatch.setattr(
        capture_module,
        "load_session_authority",
        lambda **kwargs: authority,
        raising=False,
    )

    def validate_calendar(rows, actual_calendar):
        calls["calendar_validation"] += 1
        assert tuple(actual_calendar) == tuple(calendar)

    monkeypatch.setattr(
        capture_module,
        "validate_no_night_calendar",
        validate_calendar,
        raising=False,
    )
    monkeypatch.setattr(
        capture_module,
        "PublicMinuteSource",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        capture_module,
        "capture_session_boundaries",
        lambda source, candidates: boundaries,
    )
    return authority, calls, keys


def test_capture_aggregates_all_authority_ambiguities_and_writes_diagnostics(
    monkeypatch, tmp_path, capsys
):
    _, calls, _ = _install_capture_flow(
        monkeypatch,
        night_ends=("none", "none"),
    )
    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    inventory = tmp_path / "inventory.csv"
    report = tmp_path / "audit.txt"

    result = capture_module.capture_and_publish(
        start=date(2024, 1, 8),
        end=date(2024, 1, 9),
        backtest_start=date(2026, 1, 9),
        output=output,
        inventory_output=inventory,
        audit_report=report,
        settings=None,
        use_test=True,
    )

    captured = capsys.readouterr()
    assert result == (1, 0, 2, 2)
    assert calls["calendar_validation"] == 1
    assert output.read_bytes() == b"old\n"
    inventory_rows = pd.read_csv(inventory)
    assert inventory_rows["check"].tolist() == [
        "night_authority_conflict",
        "night_authority_conflict",
    ]
    assert "publication_status=blocked" in report.read_text()
    assert "session_authority version=commodity-v1" in captured.out
    assert (
        "eligibility_config liquidity_window=120 "
        "liquidity_threshold=5000000000.0 prewarm_calendar_days=730"
    ) in captured.out
    assert (
        "requested_range start=2024-01-08 end=2024-01-09 "
        "daily_load_start=2022-01-08 backtest_start=2026-01-09"
    ) in captured.out
    assert "coverage_year=2024" in captured.out
    assert "normalization_unkeyable_unknown_date_rows=0" in captured.out
    assert captured.err.count("ambiguous_session=") == 2


def test_capture_success_uses_checked_publisher_and_marks_report_published(
    monkeypatch, tmp_path, capsys
):
    authority, calls, audit_keys = _install_capture_flow(
        monkeypatch,
        night_ends=("23:00",),
    )
    published = {}

    def fake_publish(**kwargs):
        published.update(kwargs)
        kwargs["output"].write_bytes(b"published\n")
        return (object(),)

    monkeypatch.setattr(capture_module, "publish_session_rules", fake_publish)
    output = tmp_path / "sessions.csv"
    inventory = tmp_path / "inventory.csv"
    report = tmp_path / "audit.txt"

    result = capture_module.capture_and_publish(
        start=date(2024, 1, 8),
        end=date(2024, 1, 8),
        backtest_start=date(2026, 1, 8),
        output=output,
        inventory_output=inventory,
        audit_report=report,
        settings=None,
        use_test=True,
    )

    captured = capsys.readouterr()
    assert result == (1, 1, 1, 0)
    assert calls["calendar_validation"] == 1
    assert published["audit_keys"] == audit_keys
    assert published["authority"] is authority
    assert inventory.read_text().splitlines() == [
        "exchange,product,trade_date,check,reason"
    ]
    assert "publication_status=published" in report.read_text()
    assert "products=1 rules=1 checked_days=1 ambiguous=0" in captured.out


def test_capture_unkeyable_uses_the_single_gate_without_querying_minutes(
    monkeypatch, tmp_path
):
    day = date(2024, 1, 8)
    quality = pd.DataFrame(
        [
            {
                "object_type": "contract_bar",
                "object_id": "BAD",
                "trade_date": day,
                "check": "contract_parse",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "bad identity",
            },
            {
                "object_type": "contract_bar",
                "object_id": "AU2406.SHF",
                "trade_date": pd.NaT,
                "check": "trade_date",
                "status": "excluded",
                "action": "exclude_candidate",
                "reason": "bad date",
            },
        ]
    )
    _install_capture_flow(
        monkeypatch,
        night_ends=("23:00",),
        data_quality=quality,
    )
    monkeypatch.setattr(
        capture_module,
        "PublicMinuteSource",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unkeyable capture must not construct a minute source")
        ),
    )
    original_gate = capture_module.require_publishable_coverage
    gate_calls = 0

    def counted_gate(report):
        nonlocal gate_calls
        gate_calls += 1
        return original_gate(report)

    monkeypatch.setattr(
        capture_module,
        "require_publishable_coverage",
        counted_gate,
    )
    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    inventory = tmp_path / "inventory.csv"
    report = tmp_path / "audit.txt"
    kwargs = {
        "start": date(2024, 1, 8),
        "end": date(2024, 1, 8),
        "backtest_start": date(2026, 1, 8),
        "output": output,
        "inventory_output": inventory,
        "audit_report": report,
        "settings": None,
        "use_test": True,
    }

    assert capture_module.capture_and_publish(**kwargs) == (1, 0, 0, 2)
    assert output.read_bytes() == b"old\n"
    assert "publication_status=blocked" in report.read_text()
    assert capture_module.main(
        [
            "--start",
            "2024-01-08",
            "--end",
            "2024-01-08",
            "--backtest-start",
            "2026-01-08",
            "--output",
            str(output),
            "--inventory-output",
            str(inventory),
            "--audit-report",
            str(report),
        ]
    ) == 1
    assert output.read_bytes() == b"old\n"
    assert gate_calls == 2


@pytest.mark.parametrize(
    "collision",
    ["output_inventory", "output_report", "inventory_report"],
)
def test_capture_rejects_colliding_output_paths_before_any_write_or_load(
    collision, monkeypatch, tmp_path
):
    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    inventory = tmp_path / "inventory.csv"
    report = tmp_path / "audit.txt"
    if collision == "output_inventory":
        inventory = output
    elif collision == "output_report":
        report = output
    else:
        report = inventory
    monkeypatch.setattr(
        capture_module,
        "load_session_authority",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("path validation must precede authority loading")
        ),
    )

    with pytest.raises(SessionCaptureError, match="capture_output_path_collision"):
        capture_module.capture_and_publish(
            start=date(2024, 1, 8),
            end=date(2024, 1, 8),
            backtest_start=date(2026, 1, 8),
            output=output,
            inventory_output=inventory,
            audit_report=report,
            settings=None,
            use_test=False,
        )

    assert output.read_bytes() == b"old\n"
