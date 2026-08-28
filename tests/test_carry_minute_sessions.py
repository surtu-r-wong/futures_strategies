from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.sessions import (
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
    matching_session_rules,
    next_slots,
    resolve_session_rule,
    validate_capture_coverage,
)
from cta_carry.data import CarryDataSet
from cta_carry.session_authority import (
    AbsentProductDay,
    EffectiveAuthorityRange,
    SessionAuthority,
    SessionAuthorityError,
    SessionException,
    load_absent_product_days,
    matching_absent_product_day,
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
    ("night_start", "night_end", "expected"),
    [
        ("none", "none", None),
        ("21:00", "23:00", SessionSegment(-180, -60)),
        ("21:00", "23:30", SessionSegment(-180, -30)),
        ("21:00", "01:00", SessionSegment(-180, 60)),
        ("21:00", "02:30", SessionSegment(-180, 150)),
        ("22:30", "23:00", SessionSegment(-90, -60)),
    ],
)
def test_csv_night_intervals_translate_exactly(
    tmp_path, night_start, night_end, expected
):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        f"DCE,I,2019-12-26,2019-12-26,{night_start},{night_end},commodity-v1\n",
        encoding="utf-8",
    )
    rule = load_session_rules(path)[0]
    night = tuple(segment for segment in rule.segments if segment.end_minute <= 150)
    assert night == (() if expected is None else (expected,))


def test_delayed_dce_rule_starts_at_2230_on_the_previous_trade_date(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        "DCE,I,2019-12-26,2019-12-26,22:30,23:00,commodity-v1\n",
        encoding="utf-8",
    )
    rule = load_session_rules(path)[0]
    slots = build_trading_slots(date(2019, 12, 26), date(2019, 12, 25), rule)
    assert slots[0] == _dt(2019, 12, 25, 22, 30)
    assert slots[29] == _dt(2019, 12, 25, 22, 59)
    assert _dt(2019, 12, 25, 21, 0) not in slots


@pytest.mark.parametrize(
    ("night_start", "night_end"),
    [
        ("none", "23:00"),
        ("21:00", "none"),
        ("21:07", "23:00"),
        ("20:45", "23:00"),
        ("21:00", "02:45"),
        ("23:00", "22:30"),
        ("22:30", "22:30"),
        ("9:00", "23:00"),
    ],
)
def test_csv_rejects_invalid_night_intervals(tmp_path, night_start, night_end):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        f"DCE,I,2019-12-26,2019-12-26,{night_start},{night_end},commodity-v1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="session_rule_time"):
        load_session_rules(path)


def test_csv_row_translates_to_the_exact_session_rule(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        "SHFE,AU,2020-01-01,,21:00,02:30,commodity-v1\n",
        encoding="utf-8",
    )

    rules = load_session_rules(path)

    assert rules == (
        SessionRule(
            exchange="SHFE",
            product="AU",
            effective_start=date(2020, 1, 1),
            effective_end=None,
            segments=(
                SessionSegment(-180, 150),
                *(SessionSegment(*item) for item in DAY_SEGMENTS),
            ),
            version=SESSION_RULES_VERSION,
        ),
    )


def test_csv_header_must_match_the_exact_ordered_schema(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "product,exchange,effective_start,effective_end,night_start,night_end,version\n"
        "AU,SHFE,2020-01-01,,21:00,02:30,commodity-v1\n",
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


def test_matching_session_rules_returns_every_rule_covering_the_day():
    """覆盖闸与 resolve_session_rule 必须共用同一个谓词，否则两者会分叉。"""
    trade_date = date(2024, 1, 8)
    covered = _rule(effective_start=date(2020, 1, 1), effective_end=trade_date)
    later = _rule(effective_start=trade_date)

    assert matching_session_rules((covered, later), "SHFE", "AU", trade_date) == (
        covered,
        later,
    )
    assert matching_session_rules((covered,), "SHFE", "AU", trade_date) == (covered,)
    assert matching_session_rules((covered,), "SHFE", "AU", date(2019, 12, 31)) == ()
    assert matching_session_rules((covered,), "DCE", "AU", trade_date) == ()
    assert matching_session_rules((covered,), "SHFE", "CU", trade_date) == ()


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
        "exchange,product,effective_start,effective_end,"
        f"night_start,night_end,version\n{row}\n",
        encoding="utf-8",
    )
    return path


def test_csv_rejects_extra_row_cells_with_row_number(tmp_path):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2020-01-01,,none,none,commodity-v1,extra",
    )

    with pytest.raises(ValueError, match="session_rules_csv_row_width: row 2"):
        load_session_rules(path)


def test_csv_rejects_missing_row_cells_with_row_number(tmp_path):
    path = _write_session_csv(tmp_path, "SHFE,AU,2020-01-01,,none,none")

    with pytest.raises(ValueError, match="session_rules_csv_row_width: row 2"):
        load_session_rules(path)


@pytest.mark.parametrize(
    "field",
    ["exchange", "product", "effective_start", "night_start", "night_end", "version"],
)
def test_csv_rejects_empty_required_fields_with_row_and_field(tmp_path, field):
    values = {
        "exchange": "SHFE",
        "product": "AU",
        "effective_start": "2020-01-01",
        "effective_end": "",
        "night_start": "none",
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
                "night_start",
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
        "SHFE,AU,2020-01-01,,none,none,commodity-v2",
    )

    with pytest.raises(
        ValueError,
        match=("session_rules_csv_version: row 2 field version value 'commodity-v2'"),
    ):
        load_session_rules(path)


def test_csv_rejects_reversed_dates_with_row_field_and_value(tmp_path):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2020-02-01,2020-01-31,none,none,commodity-v1",
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
        ("effective_start", "SHFE,AU,not-a-date,,none,none,commodity-v1"),
        ("effective_end", "SHFE,AU,2020-01-01,not-a-date,none,none,commodity-v1"),
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


def _night_instant(previous, label):
    hour, minute = (int(part) for part in label.split(":"))
    day = previous if hour >= 21 else previous + timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=hour, minute=minute
    )


def _interval(observation):
    return (observation.night_start, observation.night_end)


def _captured_boundary(
    *,
    night_end,
    night_start="21:00",
    traded_first=None,
    traded_second=None,
    traded_flat=False,
):
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
        values.update(
            night_first=None,
            night_last=None,
            night_traded_first=None,
            night_traded_second=None,
            night_traded_first_flat=None,
        )
    else:
        first = _night_instant(previous, night_start)
        values.update(
            night_first=first,
            night_last=_night_instant(previous, night_end) - timedelta(minutes=1),
            night_traded_first=first if traded_first is None else traded_first,
            night_traded_second=(
                first + timedelta(minutes=1) if traded_second is None else traded_second
            ),
            night_traded_first_flat=traded_flat,
        )
    return values


def test_capture_classifies_dce_delayed_open_as_an_exact_pair():
    row = _captured_boundary(night_start="22:30", night_end="23:00")
    assert _interval(classify_session_boundary(row)) == ("22:30", "23:00")


def test_capture_rejects_a_non_grid_night_start():
    row = _captured_boundary(night_start="22:30", night_end="23:00")
    row["night_traded_first"] = row["night_traded_first"] + timedelta(minutes=1)
    with pytest.raises(SessionCaptureError, match="night_traded_first"):
        classify_session_boundary(row)


@pytest.mark.parametrize("night_end", ["none", "23:00", "23:30", "01:00", "02:30"])
def test_capture_classifies_only_supported_exact_session_boundaries(night_end):
    expected = ("none", "none") if night_end == "none" else ("21:00", night_end)
    observation = classify_session_boundary(_captured_boundary(night_end=night_end))

    assert _interval(observation) == expected


def _day_session_stripped(**kwargs):
    row = _captured_boundary(**kwargs)
    for field in (
        "day_1_first",
        "day_1_last",
        "day_2_first",
        "day_2_last",
        "day_3_first",
        "day_3_last",
    ):
        row[field] = None
    return row


def test_absent_day_session_still_fails_closed_by_default():
    row = _day_session_stripped(night_end="01:00")

    with pytest.raises(SessionCaptureError, match="day_1_first"):
        classify_session_boundary(row)


def test_authorized_absent_day_session_classifies_the_night_it_can_see():
    row = _day_session_stripped(night_end="01:00")

    observation = classify_session_boundary(row, day_session_absent=True)

    assert _interval(observation) == ("21:00", "01:00")


_ABSENT_HEADER = (
    "version,exchange,product,trade_date,absent_segment,reason,source_url\n"
)
_ABSENT_ROW = (
    "commodity-v1,SHFE,AL,2018-01-02,day,"
    "vendor archive holds no day session for this product-day,"
    "docs/research/2026-08-21-minute-archive-data-request.md\n"
)


def test_absent_product_day_matches_only_its_own_product_day(tmp_path):
    path = tmp_path / "absent.csv"
    path.write_text(_ABSENT_HEADER + _ABSENT_ROW, encoding="utf-8")

    rows = load_absent_product_days(path)

    assert [type(row) for row in rows] == [AbsentProductDay]
    assert matching_absent_product_day(rows, "SHFE", "AL", date(2018, 1, 2)) is not None
    assert matching_absent_product_day(rows, "SHFE", "AL", date(2018, 1, 3)) is None
    assert matching_absent_product_day(rows, "SHFE", "CU", date(2018, 1, 2)) is None
    assert matching_absent_product_day(rows, "DCE", "AL", date(2018, 1, 2)) is None


def test_absent_product_day_rejects_an_unsupported_segment(tmp_path):
    path = tmp_path / "absent.csv"
    path.write_text(
        _ABSENT_HEADER + _ABSENT_ROW.replace(",day,", ",night,"), encoding="utf-8"
    )

    with pytest.raises(SessionAuthorityError, match="absent_segment"):
        load_absent_product_days(path)


def test_absent_product_day_rejects_a_duplicate_key(tmp_path):
    path = tmp_path / "absent.csv"
    path.write_text(_ABSENT_HEADER + _ABSENT_ROW + _ABSENT_ROW, encoding="utf-8")

    with pytest.raises(SessionAuthorityError, match="authority_duplicate_key"):
        load_absent_product_days(path)


def test_authorized_absent_day_session_rejects_a_partially_present_day():
    row = _day_session_stripped(night_end="01:00")
    row["day_2_first"] = _dt(2024, 1, 8, 10, 30)

    with pytest.raises(SessionCaptureError, match="day_session_partially_present"):
        classify_session_boundary(row, day_session_absent=True)


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

    assert capture_module._expected_night_interval(rule) == ("21:00", "23:30")


def test_capture_treats_pandas_nat_as_an_absent_night_session():
    row = _captured_boundary(night_end="none")
    row["night_first"] = pd.NaT
    row["night_last"] = pd.NaT

    assert _interval(classify_session_boundary(row)) == ("none", "none")


def test_night_start_ignores_padded_empty_bars():
    """Padded empty bars drag night_first to 21:00; the auction print sets 22:30."""
    previous = date(2024, 1, 5)
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:30")
    row["night_traded_first_flat"] = True

    observation = classify_session_boundary(row)

    assert _interval(observation) == ("22:30", "23:00")
    assert observation.note == "night_auction_attributed"


def test_normal_night_does_not_trigger_attribution():
    observation = classify_session_boundary(_captured_boundary(night_end="23:00"))

    assert _interval(observation) == ("21:00", "23:00")
    assert observation.note is None


def test_attribution_requires_the_next_minute_to_trade():
    previous = date(2024, 1, 5)
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:31")
    row["night_traded_first_flat"] = True

    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_attribution_requires_the_shifted_minute_on_the_grid():
    previous = date(2024, 1, 5)
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    row["night_traded_first"] = _night_instant(previous, "21:05")
    row["night_traded_second"] = _night_instant(previous, "21:06")
    row["night_traded_first_flat"] = True

    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_attribution_requires_the_auction_signature():
    previous = date(2024, 1, 5)
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:30")
    row["night_traded_first_flat"] = False

    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_padded_night_without_any_trade_is_classified_as_no_night():
    row = _captured_boundary(night_end="23:00")
    row["night_traded_first"] = None
    row["night_traded_second"] = None
    row["night_traded_first_flat"] = None

    observation = classify_session_boundary(row)

    assert _interval(observation) == ("none", "none")
    assert observation.note == "night_untraded_padding"


def test_absent_night_bars_remain_no_night_without_a_note():
    observation = classify_session_boundary(_captured_boundary(night_end="none"))

    assert _interval(observation) == ("none", "none")
    assert observation.note is None


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
                    "night_start": "21:00",
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
            "night_start": "21:00",
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
                "night_start": "none",
                "night_end": "none",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 9),
                "night_start": "none",
                "night_end": "none",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 10),
                "night_start": "21:00",
                "night_end": "02:30",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": date(2024, 1, 11),
                "night_start": "21:00",
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
            "night_start": "none",
            "night_end": "none",
            "version": SESSION_RULES_VERSION,
        },
        {
            "exchange": "SHFE",
            "product": "AU",
            "effective_start": date(2024, 1, 10),
            "effective_end": date(2024, 1, 11),
            "night_start": "21:00",
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


def test_session_representative_skips_a_contract_that_did_not_trade():
    """The real 2022-03-10 nickel day: SHFE halted the seven front contracts.

    Open interest survives a halt, so ranking on it alone hands the audit a
    contract with nothing to say about whether the session opened.
    """
    day = date(2022, 3, 10)
    prices = pd.DataFrame(
        [
            _audit_price(
                day, product="NI", contract="NI2204.SHF", oi=114596.0, volume=0.0
            ),
            _audit_price(
                day, product="NI", contract="NI2205.SHF", oi=52084.0, volume=0.0
            ),
            _audit_price(
                day, product="NI", contract="NI2203.SHF", oi=3906.0, volume=1506.0
            ),
            _audit_price(
                day, product="NI", contract="NI2208.SHF", oi=1998.0, volume=2509.0
            ),
        ]
    )

    index = capture_module._build_representative_index(prices)

    assert index[("SHFE", "NI", day)].daily_contract == "NI2203.SHF"


def test_session_representative_falls_back_when_nothing_traded():
    """A product-wide halt has no traded contract, so the gate must still fire."""
    day = date(2022, 3, 10)
    prices = pd.DataFrame(
        [
            _audit_price(
                day, product="NI", contract="NI2205.SHF", oi=52084.0, volume=0.0
            ),
            _audit_price(
                day, product="NI", contract="NI2204.SHF", oi=114596.0, volume=0.0
            ),
        ]
    )

    index = capture_module._build_representative_index(prices)

    assert index[("SHFE", "NI", day)].daily_contract == "NI2204.SHF"


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


def test_build_audit_accepts_a_pool_the_carry_liquidity_rule_would_not_produce():
    """采集核心必须与宇宙口径无关：注入什么池就用什么池。

    这三天的历史短到 Carry 的流动性规则会把它判成 `insufficient_since_inception`
    并踢出池（见下一条测试）。核心照单全收，才说明池子真的是外部注入的。
    """
    days = [date(2024, 1, day) for day in (2, 3, 4)]
    target = days[-1]
    key = _audit_key(target)

    seen = {}

    def _resolve_pool(*, representative_index, normalized_keys, global_calendar):
        seen["normalized"] = normalized_keys
        return frozenset({key}), {key: "lookback_complete"}

    audit = capture_module.build_audit(
        pd.DataFrame(_liquidity_prices(days)),
        resolve_pool=_resolve_pool,
        start=target,
        end=target,
    )

    # 解析器拿得到规范化后的宇宙，才能在需要时据此判定（Carry 的历史闸就靠它）。
    assert seen["normalized"] == frozenset({key})

    assert audit.key_sets.in_pool_keys == frozenset({key})
    assert audit.history_status_by_key == {key: "lookback_complete"}
    assert audit.candidates
    assert audit.global_calendar == tuple(days)


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
            history_status_by_key={},
        )

    def reject_legacy_selector(*args, **kwargs):
        raise AssertionError("legacy unfiltered selector must not be called")

    def stop_at_boundaries(source, selected, *, absent_identities=frozenset()):
        captured["boundary_source"] = source
        captured["boundary_candidates"] = selected
        captured["boundary_absent_identities"] = absent_identities
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
        "session_exception_path": capture_module.SESSION_EXCEPTIONS_PATH,
        "day_only_path": capture_module.DAY_ONLY_PATH,
        "history_exception_path": capture_module.HISTORY_EXCEPTIONS_PATH,
        "absent_product_day_path": capture_module.ABSENT_PRODUCT_DAYS_PATH,
    }
    assert captured["audit_builder_frame"] is prices
    assert captured["audit_builder"]["history_starts"] is history_starts
    assert captured["audit_builder"]["history_exceptions"] == ()
    assert captured["audit_builder"]["config"] is loader_config
    assert captured["boundary_source"] is source
    assert captured["boundary_candidates"] is candidates


def _authority(
    *,
    session_exceptions=(),
    day_only=(),
    history_exceptions=(),
    hashes=None,
):
    return SessionAuthority(
        session_exceptions=tuple(session_exceptions),
        day_only_regimes=tuple(day_only),
        liquidity_history_exceptions=tuple(history_exceptions),
        sha256_by_asset=hashes
        or {
            "session_exception": "a" * 64,
            "day_only": "b" * 64,
            "history_exception": "c" * 64,
        },
    )


def _observation(
    day,
    previous,
    *,
    night_end,
    night_start="21:00",
    exchange="SHFE",
    product="AU",
    traded_first=None,
    traded_second=None,
    traded_flat=False,
):
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
        row.update(
            night_first=None,
            night_last=None,
            night_traded_first=None,
            night_traded_second=None,
            night_traded_first_flat=None,
        )
        return row
    first = _night_instant(previous, night_start)
    row.update(
        night_first=first,
        night_last=_night_instant(previous, night_end) - timedelta(minutes=1),
        night_traded_first=first if traded_first is None else traded_first,
        night_traded_second=(
            first + timedelta(minutes=1) if traded_second is None else traded_second
        ),
        night_traded_first_flat=traded_flat,
    )
    return row


def _session_exception(**overrides):
    values = {
        "exchange": "DCE",
        "version": SESSION_RULES_VERSION,
        "trade_date": date(2019, 12, 26),
        "night_start": "22:30",
        "night_end": "23:00",
        "reason": "delayed night open notice_evening=2019-12-25",
        "source_url": "https://www.dce.com.cn/notice/6202113",
    }
    values.update(overrides)
    return SessionException(**values)


def _at_for_test(day, hour, minute):
    return datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=hour, minute=minute
    )


def test_authority_rejects_weekend_shaped_none_without_authority():
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    rows, ambiguities, _ = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="none")]),
        _authority(),
        global_calendar=(friday, monday),
    )

    assert rows.empty
    assert [item.check for item in ambiguities] == ["night_authority_conflict"]


@pytest.mark.parametrize("authority_kind", ["holiday", "day_only"])
def test_authority_accepts_declared_none(authority_kind):
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    session_exceptions = ()
    day_only = ()
    if authority_kind == "holiday":
        session_exceptions = (
            SessionException(
                exchange="SHFE",
                version=SESSION_RULES_VERSION,
                trade_date=monday,
                night_start="none",
                night_end="none",
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

    rows, ambiguities, _ = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="none")]),
        _authority(session_exceptions=session_exceptions, day_only=day_only),
        global_calendar=(friday, monday),
    )

    assert ambiguities == ()
    assert rows.to_dict("records") == [
        {
            "exchange": "SHFE",
            "product": "AU",
            "trade_date": monday,
            "night_start": "none",
            "night_end": "none",
        }
    ]


def test_authority_collects_every_unauthorized_continuous_none():
    days = [date(2024, 1, day) for day in (8, 9, 10)]
    rows, ambiguities, _ = capture_module.classify_authorized_boundaries(
        pd.DataFrame(
            [
                _observation(day, day - timedelta(days=1), night_end="none")
                for day in reversed(days)
            ]
        ),
        _authority(),
        global_calendar=tuple(days),
    )

    assert rows.empty
    assert [item.trade_date for item in ambiguities] == days
    assert all(item.check == "night_authority_conflict" for item in ambiguities)


def test_authority_rejects_observed_night_inside_declared_none():
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    declared = SessionException(
        exchange="SHFE",
        version=SESSION_RULES_VERSION,
        trade_date=monday,
        night_start="none",
        night_end="none",
        reason="notice_evening=2024-01-05 holiday halt",
        source_url="https://www.shfe.com.cn/example",
    )

    rows, ambiguities, _ = capture_module.classify_authorized_boundaries(
        pd.DataFrame([_observation(monday, friday, night_end="23:00")]),
        _authority(session_exceptions=(declared,)),
        global_calendar=(friday, monday),
    )

    assert rows.empty
    assert [(item.product, item.check) for item in ambiguities] == [
        ("*", "session_exception_unconsumed"),
        ("AU", "night_authority_conflict"),
    ]


def test_authorized_delayed_open_is_classified_for_every_dce_product():
    """The real 2019-12-25 night: padding starts 21:00, the auction prints 22:29."""
    day = date(2019, 12, 26)
    previous = date(2019, 12, 25)
    exception = _session_exception()
    boundaries = pd.DataFrame(
        [
            _observation(
                day,
                previous,
                night_start="21:00",
                night_end="23:00",
                exchange="DCE",
                product=product,
                traded_first=_night_instant(previous, "22:29"),
                traded_second=_night_instant(previous, "22:30"),
                traded_flat=True,
            )
            for product in ("I", "J")
        ]
    )
    classified, ambiguities, notes = capture_module.classify_authorized_boundaries(
        boundaries,
        _authority(session_exceptions=(exception,)),
        global_calendar=(day,),
    )
    assert ambiguities == ()
    assert set(classified["night_start"]) == {"22:30"}
    assert set(classified["night_end"]) == {"23:00"}
    assert notes == (
        "night_auction_attributed=2019-12-26 DCE I",
        "night_auction_attributed=2019-12-26 DCE J",
    )


def test_untraded_padding_is_counted_when_authority_declares_day_only():
    day = date(2024, 1, 8)
    previous = date(2024, 1, 5)
    row = _observation(day, previous, night_end="23:00", exchange="DCE", product="JD")
    row["night_traded_first"] = None
    row["night_traded_second"] = None
    row["night_traded_first_flat"] = None

    classified, ambiguities, notes = capture_module.classify_authorized_boundaries(
        pd.DataFrame([row]),
        _authority(
            day_only=(
                EffectiveAuthorityRange(
                    version=SESSION_RULES_VERSION,
                    exchange="DCE",
                    product="JD",
                    effective_start=day,
                    effective_end=day,
                    reason="documented day-only regime",
                    source_url="https://www.dce.com.cn/example",
                ),
            )
        ),
        global_calendar=(day,),
    )

    assert ambiguities == ()
    assert set(classified["night_start"]) == {"none"}
    assert set(classified["night_end"]) == {"none"}
    assert notes == ("night_untraded_padding=2024-01-08 DCE JD",)


def test_untraded_padding_is_counted_even_when_authorization_fails():
    """The real 2022-03-10 SHFE NI night: full padding, zero trades, no authority."""
    day = date(2022, 3, 10)
    previous = date(2022, 3, 9)
    row = _observation(day, previous, night_end="01:00", exchange="SHFE", product="NI")
    row["night_traded_first"] = None
    row["night_traded_second"] = None
    row["night_traded_first_flat"] = None

    classified, ambiguities, notes = capture_module.classify_authorized_boundaries(
        pd.DataFrame([row]),
        _authority(),
        global_calendar=(day,),
    )

    assert classified.empty
    assert [(item.exchange, item.product, item.check) for item in ambiguities] == [
        ("SHFE", "NI", "night_authority_conflict")
    ]
    assert notes == ("night_untraded_padding=2022-03-10 SHFE NI",)


def test_loaded_but_unconsumed_exception_blocks_capture():
    day = date(2019, 12, 26)
    classified, ambiguities, _ = capture_module.classify_authorized_boundaries(
        pd.DataFrame(
            [
                _observation(
                    day,
                    date(2019, 12, 25),
                    night_start="21:00",
                    night_end="23:00",
                    exchange="SHFE",
                    product="RB",
                )
            ]
        ),
        _authority(session_exceptions=(_session_exception(),)),
        global_calendar=(day,),
    )
    assert classified.empty is False
    assert [(item.exchange, item.product, item.check) for item in ambiguities] == [
        ("DCE", "*", "session_exception_unconsumed")
    ]


def test_capture_calendar_validation_filters_outside_authority_and_calls_once(
    monkeypatch,
):
    calendar = (date(2024, 1, 5), date(2024, 1, 8))
    outside = SessionException(
        exchange="SHFE",
        version=SESSION_RULES_VERSION,
        trade_date=date(2024, 2, 19),
        night_start="none",
        night_end="none",
        reason="notice_evening=2024-02-08 holiday halt",
        source_url="https://www.shfe.com.cn/outside",
    )
    calls = []

    def record(rows, actual_calendar):
        calls.append((tuple(rows), tuple(actual_calendar)))

    monkeypatch.setattr(capture_module, "validate_session_exception_calendar", record)

    capture_module.validate_capture_session_exception_calendar((outside,), calendar)

    assert calls == [((), calendar)]


def test_capture_calendar_validation_keeps_in_range_off_by_one_fail_closed():
    calendar = (date(2024, 1, 5), date(2024, 1, 8))
    wrong_target = SessionException(
        exchange="SHFE",
        version=SESSION_RULES_VERSION,
        trade_date=date(2024, 1, 8),
        night_start="none",
        night_end="none",
        reason="notice_evening=2024-01-04 holiday halt",
        source_url="https://www.shfe.com.cn/in-range",
    )

    with pytest.raises(ValueError, match="notice_target_trade_date"):
        capture_module.validate_capture_session_exception_calendar(
            (wrong_target,),
            calendar,
        )


def _sibling_boundary_frame(rows):
    return pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "exchange": exchange,
                "product": product,
                "daily_contract": contract,
                "night_traded_first": traded_first,
            }
            for trade_date, exchange, product, contract, traded_first in rows
        ]
    )


def test_earliest_sibling_night_start_keeps_the_minimum_and_drops_the_silent():
    day = date(2019, 12, 23)
    frame = _sibling_boundary_frame(
        [
            (day, "SHFE", "AL", "AL2001.SHF", _dt(2019, 12, 20, 21, 4)),
            (day, "SHFE", "AL", "AL2003.SHF", _dt(2019, 12, 20, 21, 0)),
            (day, "SHFE", "AL", "AL2010.SHF", None),
            (day, "SHFE", "CU", "CU2002.SHF", _dt(2019, 12, 20, 21, 7)),
        ]
    )

    assert capture_module.earliest_sibling_night_starts(frame) == {
        (day, "SHFE", "AL"): _dt(2019, 12, 20, 21, 0),
        (day, "SHFE", "CU"): _dt(2019, 12, 20, 21, 7),
    }


def test_sibling_candidates_cover_only_unsettled_days_and_skip_the_representative():
    day = date(2019, 12, 23)
    previous = date(2019, 12, 20)

    def _row(product, contract):
        return {
            "trade_date": day,
            "exchange": "SHFE",
            "product": product,
            "daily_contract": contract,
            "window_start": _dt(2019, 12, 20, 21, 0),
            "window_end": _dt(2019, 12, 23, 15, 1),
            "previous_trade_date": previous,
        }

    boundaries = pd.DataFrame([_row("AL", "AL2002.SHF"), _row("CU", "CU2002.SHF")])
    ambiguities = (
        capture_module.AmbiguityRecord(
            trade_date=day,
            exchange="SHFE",
            product="AL",
            check="night_authority_conflict",
            reason="unsettled",
        ),
    )
    prices = pd.DataFrame(
        [
            {"trade_date": day, "contract": "AL2002.SHF"},
            {"trade_date": day, "contract": "AL2003.SHF"},
            {"trade_date": day, "contract": "CU2002.SHF"},
            {"trade_date": day, "contract": "CU2003.SHF"},
        ]
    )

    selected = capture_module.sibling_night_candidates(boundaries, ambiguities, prices)

    assert [item.candidate.daily_contract for item in selected] == ["AL2003.SHF"]
    assert selected[0].previous_trade_date == previous


def _day_only_rule(product="A"):
    return SessionRule.day_only("DCE", product, version=SESSION_RULES_VERSION)


def _padded_no_night_row():
    # 2017-03-31 shape: the exchange ran no night session, but the archive still
    # wrote a full set of flat bars for the evening before.
    trade_date = date(2017, 3, 31)
    previous = date(2017, 3, 30)
    row = _captured_boundary(night_end="none")
    row.update(
        trade_date=trade_date,
        previous_trade_date=previous,
        exchange="DCE",
        product="A",
        daily_contract="A1709.DCE",
        day_1_first=_dt(2017, 3, 31, 9, 0),
        day_1_last=_dt(2017, 3, 31, 10, 14),
        day_2_first=_dt(2017, 3, 31, 10, 30),
        day_2_last=_dt(2017, 3, 31, 11, 29),
        day_3_first=_dt(2017, 3, 31, 13, 30),
        day_3_last=_dt(2017, 3, 31, 14, 59),
        night_first=_night_instant(previous, "21:00"),
        night_last=_night_instant(previous, "23:30") - timedelta(minutes=1),
        night_traded_first=None,
        night_traded_second=None,
        night_traded_first_flat=None,
    )
    return row


def test_replay_ignores_padded_night_bars_on_a_night_that_did_not_run():
    frame = pd.DataFrame([_padded_no_night_row()])

    capture_module.validate_audited_boundaries(frame, (_day_only_rule(),))


def test_replay_ignores_bars_padded_before_a_delayed_open():
    # 2019-12-26 shape: the session opened 22:30, but the archive writes bars on
    # the normal schedule, so the first bar present sits at 21:00.
    trade_date = date(2019, 12, 26)
    previous = date(2019, 12, 25)
    row = _captured_boundary(night_start="22:30", night_end="23:00")
    row.update(
        trade_date=trade_date,
        previous_trade_date=previous,
        exchange="DCE",
        product="A",
        daily_contract="A2005.DCE",
        day_1_first=_dt(2019, 12, 26, 9, 0),
        day_1_last=_dt(2019, 12, 26, 10, 14),
        day_2_first=_dt(2019, 12, 26, 10, 30),
        day_2_last=_dt(2019, 12, 26, 11, 29),
        day_3_first=_dt(2019, 12, 26, 13, 30),
        day_3_last=_dt(2019, 12, 26, 14, 59),
        night_first=_night_instant(previous, "21:00"),
        night_last=_night_instant(previous, "23:00") - timedelta(minutes=1),
        night_traded_first=_night_instant(previous, "22:30"),
        night_traded_second=_night_instant(previous, "22:30") + timedelta(minutes=1),
        night_traded_first_flat=False,
    )
    rule = SessionRule(
        exchange="DCE",
        product="A",
        effective_start=trade_date,
        effective_end=trade_date,
        segments=(
            SessionSegment(-90, -60),
            *(SessionSegment(*item) for item in DAY_SEGMENTS),
        ),
        version=SESSION_RULES_VERSION,
    )

    capture_module.validate_audited_boundaries(pd.DataFrame([row]), (rule,))


def test_replay_still_checks_the_night_close_against_the_published_slots():
    row = _captured_boundary(night_end="23:00")
    row["night_last"] = row["night_last"] + timedelta(minutes=7)

    rule = SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=date(2024, 1, 8),
        effective_end=date(2024, 1, 8),
        segments=(
            SessionSegment(-180, -60),
            *(SessionSegment(*item) for item in DAY_SEGMENTS),
        ),
        version=SESSION_RULES_VERSION,
    )

    with pytest.raises(SessionCaptureError, match="night_last"):
        capture_module.validate_audited_boundaries(pd.DataFrame([row]), (rule,))


def test_replay_honours_a_registered_absent_day_session():
    row = _padded_no_night_row()
    for field in (
        "day_1_first",
        "day_1_last",
        "day_2_first",
        "day_2_last",
        "day_3_first",
        "day_3_last",
    ):
        row[field] = None
    registered = (
        AbsentProductDay(
            version=SESSION_RULES_VERSION,
            exchange="DCE",
            product="A",
            trade_date=date(2017, 3, 31),
            absent_segment="day",
            reason="archive holds no day session",
            source_url="docs/research/x.md",
        ),
    )

    capture_module.validate_audited_boundaries(
        pd.DataFrame([row]), (_day_only_rule(),), absent_product_days=registered
    )


def test_replay_still_rejects_an_unregistered_missing_day_session():
    row = _padded_no_night_row()
    row["day_2_first"] = None

    with pytest.raises(SessionCaptureError, match="day_2_first"):
        capture_module.validate_audited_boundaries(
            pd.DataFrame([row]), (_day_only_rule(),)
        )


def test_replay_still_rejects_a_night_bar_the_published_rule_cannot_explain():
    row = _padded_no_night_row()
    # A traded night makes the observation a real session, which a day-only rule
    # must not be allowed to publish.
    row["night_traded_first"] = _night_instant(date(2017, 3, 30), "21:00")
    row["night_traded_second"] = _night_instant(date(2017, 3, 30), "21:00") + timedelta(
        minutes=1
    )
    row["night_traded_first_flat"] = False

    with pytest.raises(SessionCaptureError, match="session_rule_replay"):
        capture_module.validate_audited_boundaries(
            pd.DataFrame([row]), (_day_only_rule(),)
        )


def test_night_start_takes_the_earliest_trade_across_the_products_contracts():
    rep = _dt(2020, 5, 18, 21, 1)
    sibling = _dt(2020, 5, 18, 21, 0)

    assert capture_module.widened_night_traded_first(rep, sibling) == sibling
    assert capture_module.widened_night_traded_first(sibling, rep) == sibling


def test_night_start_uses_a_sibling_when_the_representative_never_traded():
    sibling = _dt(2019, 12, 20, 21, 0)

    assert capture_module.widened_night_traded_first(None, sibling) == sibling


def test_night_start_stays_absent_when_no_contract_traded():
    # Fail-closed is preserved: a night nobody traded still reaches the
    # padding gate rather than being handed a start it never had.
    assert capture_module.widened_night_traded_first(None, None) is None


def test_consumed_absent_day_sessions_reads_the_frame_not_the_registry():
    frame = pd.DataFrame(
        [
            {
                "trade_date": date(2018, 1, 2),
                "exchange": "SHFE",
                "product": "AL",
                "daily_contract": "AL1803.SHF",
                "day_1_first": None,
                "day_1_last": None,
                "day_2_first": None,
                "day_2_last": None,
                "day_3_first": None,
                "day_3_last": None,
            },
            {
                "trade_date": date(2018, 1, 3),
                "exchange": "SHFE",
                "product": "CU",
                "daily_contract": "CU1803.SHF",
                "day_1_first": _dt(2018, 1, 3, 9, 0),
                "day_1_last": _dt(2018, 1, 3, 10, 14),
                "day_2_first": _dt(2018, 1, 3, 10, 30),
                "day_2_last": _dt(2018, 1, 3, 11, 29),
                "day_3_first": _dt(2018, 1, 3, 13, 30),
                "day_3_last": _dt(2018, 1, 3, 14, 59),
            },
        ]
    )

    assert capture_module.consumed_absent_day_sessions(frame) == frozenset(
        {(date(2018, 1, 2), "SHFE", "AL", "AL1803.SHF")}
    )


def test_authorized_absent_day_session_lines_name_every_consumed_row():
    rows = (
        AbsentProductDay(
            version="commodity-v1",
            exchange="SHFE",
            product="AL",
            trade_date=date(2018, 1, 2),
            absent_segment="day",
            reason="archive holds no day session",
            source_url="docs/research/x.md",
        ),
        AbsentProductDay(
            version="commodity-v1",
            exchange="SHFE",
            product="CU",
            trade_date=date(2019, 1, 2),
            absent_segment="day",
            reason="archive holds no day session",
            source_url="docs/research/x.md",
        ),
    )

    lines = capture_module.authorized_absent_day_session_lines(
        rows, frozenset({(date(2018, 1, 2), "SHFE", "AL", "AL1803.SHF")})
    )

    assert lines == (
        "authorized_absent_day_session key=SHFE/AL/2018-01-02 "
        "contract=AL1803.SHF source_url=docs/research/x.md",
        "authorized_absent_day_session_count=1 registered=2",
    )


def test_authorized_history_gap_lines_are_sorted_exact_and_do_not_leak_misses():
    day = date(2024, 1, 8)
    au = EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="AU",
        effective_start=day,
        effective_end=day,
        reason="documented AU gap",
        source_url="https://www.shfe.com.cn/au-gap",
    )
    missed = EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="CU",
        effective_start=day,
        effective_end=day,
        reason="must not leak",
        source_url="https://www.shfe.com.cn/missed",
    )

    assert capture_module.authorized_history_gap_lines(
        {("SHFE", "AU", day): "authorized_history_gap"},
        (missed, au),
    ) == (
        "authorized_history_gap key=SHFE/AU/2024-01-08 "
        "authority_effective_start=2024-01-08 authority_effective_end=2024-01-08 "
        "reason='documented AU gap' "
        "source_url='https://www.shfe.com.cn/au-gap'",
    )


def test_collapse_keeps_night_none_night_as_three_rules():
    days = [date(2024, 1, day) for day in (8, 9, 10)]
    audit_keys = frozenset(("SHFE", "AU", day) for day in days)
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": day,
                "night_start": "none" if value == "none" else "21:00",
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


@pytest.mark.parametrize(
    "right_night", [("21:00", "23:00"), ("21:00", "02:30"), ("22:30", "23:00")]
)
def test_collapse_never_bridges_an_unaudited_global_trading_day(right_night, tmp_path):
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
                "night_start": "21:00",
                "night_end": "23:00",
            },
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": days[2],
                "night_start": right_night[0],
                "night_end": right_night[1],
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


def test_collapse_breaks_when_only_night_start_changes():
    days = (date(2019, 12, 25), date(2019, 12, 26))
    classified = pd.DataFrame(
        [
            {
                "exchange": "DCE",
                "product": "I",
                "trade_date": days[0],
                "night_start": "21:00",
                "night_end": "23:00",
            },
            {
                "exchange": "DCE",
                "product": "I",
                "trade_date": days[1],
                "night_start": "22:30",
                "night_end": "23:00",
            },
        ]
    )
    keys = frozenset(("DCE", "I", day) for day in days)
    rules = collapse_session_rules(classified, global_calendar=days, audit_keys=keys)
    assert [(row["night_start"], row["night_end"]) for row in rules] == [
        ("21:00", "23:00"),
        ("22:30", "23:00"),
    ]


def _key(day, product="RB"):
    return ("SHFE", product, day)


@pytest.mark.parametrize(
    ("object_id", "expected"),
    [
        ("L2602F.DCE", ("DCE", "L", date(2026, 1, 5))),
        ("SC2003TAS.INE", ("INE", "SC", date(2026, 1, 5))),
        ("TA405.CZC", ("CZCE", "TA", date(2026, 1, 5))),
    ],
)
def test_raw_exclusion_identity_accepts_nonstandard_contract_markers(
    object_id, expected
):
    assert (
        capture_module.raw_exclusion_product_day_identity(
            object_id,
            date(2026, 1, 5),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("object_id", "trade_date"),
    [
        ("BAD", date(2026, 1, 5)),
        ("RB2605.UNKNOWN", date(2026, 1, 5)),
        ("2605.DCE", date(2026, 1, 5)),
        ("RB2605XYZ.DCE", date(2026, 1, 5)),
        ("RB26.DCE", date(2026, 1, 5)),
        ("RB26055.DCE", date(2026, 1, 5)),
        ("L2602F.DCE", datetime(2026, 1, 5)),
    ],
)
def test_raw_exclusion_identity_rejects_unkeyable_rows(object_id, trade_date):
    with pytest.raises(SessionCaptureError, match="raw_exclusion_identity"):
        capture_module.raw_exclusion_product_day_identity(object_id, trade_date)


def test_coverage_keys_nonstandard_exclusions_by_product_day():
    day = date(2026, 1, 5)
    survivor = ("DCE", "L", day)
    key_sets = capture_module.AuditKeySets(
        normalized_keys=frozenset({survivor}),
        in_pool_keys=frozenset(),
        audit_universe_keys=frozenset({survivor}),
        audit_keys=frozenset(),
    )
    quality = pd.DataFrame(
        [
            {
                "object_type": "contract_bar",
                "object_id": "L2602F.DCE",
                "trade_date": day,
                "status": "excluded",
                "action": "exclude_candidate",
            },
            {
                "object_type": "contract_bar",
                "object_id": "SC2003TAS.INE",
                "trade_date": day,
                "status": "excluded",
                "action": "exclude_candidate",
            },
        ]
    )

    report = capture_module.coverage_report(
        data_quality=quality,
        key_sets=key_sets,
        start=day,
        end=day,
    )

    assert report.rows[0]["normalization_excluded_product_days"] == 1
    assert report.rows[0]["normalization_unkeyable_rows"] == 0
    assert not report.has_unkeyable


def test_coverage_report_has_every_requested_year_and_independent_counts():
    day = date(2024, 1, 2)
    key_sets = capture_module.AuditKeySets(
        normalized_keys=frozenset({_key(day), _key(day, "AU")}),
        in_pool_keys=frozenset({_key(day)}),
        audit_universe_keys=frozenset({_key(day), _key(day, "AU"), _key(day, "CU")}),
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
        "session_exception": b"session-exception-authority",
        "day_only": b"day-only-authority",
        "history_exception": b"history-exception-authority",
        "absent_product_day": b"absent-product-day-authority",
    }
    paths = {}
    for name, payload in payloads.items():
        path = tmp_path / f"{name}.csv"
        path.write_bytes(payload)
        paths[name] = path
    hashes = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
    }
    return paths, hashes


def _single_rule_capture(tmp_path, *, night_start="21:00", night_end="23:00"):
    previous = date(2024, 1, 5)
    day = date(2024, 1, 8)
    audit_keys = frozenset({("SHFE", "AU", day)})
    boundaries = pd.DataFrame(
        [_observation(day, previous, night_start=night_start, night_end=night_end)]
    )
    classified = pd.DataFrame(
        [
            {
                "exchange": "SHFE",
                "product": "AU",
                "trade_date": day,
                "night_start": night_start,
                "night_end": night_end,
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
    boundaries = pd.DataFrame([_observation(day, date(2024, 1, 5), night_end="23:00")])
    audit_keys = frozenset({("SHFE", "AU", day)})

    assert capture_module.validate_boundary_keys(boundaries, audit_keys) == audit_keys


@pytest.mark.parametrize(
    "failure",
    [
        "hash_mismatch",
        "session_exception_mutation",
        "loader_replay",
        "reverse_key_mismatch",
        "diagnostics",
        "replace",
    ],
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
    validated_callback = None

    if failure == "hash_mismatch":
        authority = _authority(hashes={**hashes, "day_only": "0" * 64})
    elif failure == "session_exception_mutation":
        paths["session_exception"].write_bytes(b"mutated-after-load\n")
    elif failure == "loader_replay":
        monkeypatch.setattr(capture_module, "load_session_rules", lambda path: ())
    elif failure == "reverse_key_mismatch":
        monkeypatch.setattr(
            capture_module,
            "expand_rule_keys",
            lambda *args, **kwargs: frozenset(),
        )
    elif failure == "diagnostics":

        def fail_diagnostics(rules):
            raise OSError("diagnostics failed")

        validated_callback = fail_diagnostics
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
            validated_callback=validated_callback,
        )

    assert output.read_bytes() == original
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []


def test_atomic_publisher_runs_diagnostics_after_validation_and_before_replace(
    monkeypatch, tmp_path
):
    boundaries, rows, calendar, audit_keys, paths, hashes = _single_rule_capture(
        tmp_path
    )
    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    events = []

    original_hashes = capture_module._validate_authority_hashes
    original_loader = capture_module.load_session_rules
    original_boundaries = capture_module.validate_audited_boundaries
    original_expand = capture_module.expand_rule_keys
    original_replace = capture_module.os.replace

    def track_hashes(*args, **kwargs):
        events.append("hashes")
        return original_hashes(*args, **kwargs)

    def track_loader(*args, **kwargs):
        events.append("loader")
        return original_loader(*args, **kwargs)

    def track_boundaries(*args, **kwargs):
        events.append("boundaries")
        return original_boundaries(*args, **kwargs)

    def track_expand(*args, **kwargs):
        events.append("reverse")
        return original_expand(*args, **kwargs)

    def diagnostics(loaded_rules):
        events.append("diagnostics")
        assert len(loaded_rules) == 1
        assert output.read_bytes() == b"old\n"

    def track_replace(*args, **kwargs):
        events.append("replace")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(capture_module, "_validate_authority_hashes", track_hashes)
    monkeypatch.setattr(capture_module, "load_session_rules", track_loader)
    monkeypatch.setattr(
        capture_module,
        "validate_audited_boundaries",
        track_boundaries,
    )
    monkeypatch.setattr(capture_module, "expand_rule_keys", track_expand)
    monkeypatch.setattr(capture_module.os, "replace", track_replace)

    capture_module.publish_session_rules(
        output=output,
        rule_rows=rows,
        boundaries=boundaries,
        global_calendar=calendar,
        audit_keys=audit_keys,
        authority=_authority(hashes=hashes),
        authority_paths=paths,
        validated_callback=diagnostics,
    )

    assert events == [
        "hashes",
        "loader",
        "boundaries",
        "reverse",
        "diagnostics",
        "replace",
    ]


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
    assert (
        capture_module.expand_rule_keys(
            loaded,
            global_calendar=calendar,
        )
        == audit_keys
    )


def test_atomic_publisher_installs_the_exact_interval_schema(tmp_path):
    (
        boundaries,
        rows,
        calendar,
        audit_keys,
        paths,
        hashes,
    ) = _single_rule_capture(tmp_path, night_start="22:30", night_end="23:00")
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

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "exchange,product,effective_start,effective_end,night_start,night_end,version"
    )
    assert lines[1] == "SHFE,AU,2024-01-08,2024-01-08,22:30,23:00,commodity-v1"
    assert loaded[0].segments[0] == SessionSegment(-90, -60)
    assert load_session_rules(output)[0].segments[0] == SessionSegment(-90, -60)


def test_atomic_publisher_rejects_a_mutated_staged_night_start(monkeypatch, tmp_path):
    (
        boundaries,
        rows,
        calendar,
        audit_keys,
        paths,
        hashes,
    ) = _single_rule_capture(tmp_path, night_start="22:30", night_end="23:00")
    output = tmp_path / "sessions.csv"
    original = b"old-authoritative-bytes\n"
    output.write_bytes(original)
    real_stage = capture_module._stage_session_rules

    def tamper(target, rule_rows):
        temporary = real_stage(target, rule_rows)
        temporary.write_text(
            temporary.read_text(encoding="utf-8").replace(",22:30,", ",21:00,"),
            encoding="utf-8",
        )
        return temporary

    monkeypatch.setattr(capture_module, "_stage_session_rules", tamper)

    with pytest.raises(SessionCaptureError, match="session_rule_replay"):
        capture_module.publish_session_rules(
            output=output,
            rule_rows=rows,
            boundaries=boundaries,
            global_calendar=calendar,
            audit_keys=audit_keys,
            authority=_authority(hashes=hashes),
            authority_paths=paths,
        )

    assert output.read_bytes() == original
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []


def test_publisher_requires_all_three_authority_hash_names(tmp_path):
    (
        boundaries,
        rows,
        calendar,
        audit_keys,
        paths,
        hashes,
    ) = _single_rule_capture(tmp_path)
    output = tmp_path / "sessions.csv"
    call = {
        "rule_rows": rows,
        "boundaries": boundaries,
        "global_calendar": calendar,
        "audit_keys": audit_keys,
    }

    with pytest.raises(SessionCaptureError, match="authority_hash_paths"):
        capture_module.publish_session_rules(
            output=output,
            authority=_authority(hashes=hashes),
            authority_paths={
                "no_night": paths["session_exception"],
                "day_only": paths["day_only"],
                "history_exception": paths["history_exception"],
            },
            **call,
        )

    with pytest.raises(SessionCaptureError, match="authority_hash_manifest"):
        capture_module.publish_session_rules(
            output=output,
            authority=_authority(
                hashes={
                    "no_night": hashes["session_exception"],
                    "day_only": hashes["day_only"],
                    "history_exception": hashes["history_exception"],
                }
            ),
            authority_paths=paths,
            **call,
        )

    assert not output.exists()
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []


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


def _install_capture_flow(
    monkeypatch,
    *,
    night_ends,
    data_quality=None,
    authority=None,
    history_status_by_key=None,
):
    previous = date(2024, 1, 5)
    days = [
        date(2024, 1, 8) + timedelta(days=index) for index in range(len(night_ends))
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
        history_status_by_key=history_status_by_key or {},
    )
    boundaries = pd.DataFrame(
        [
            _observation(
                day,
                calendar[index],
                night_end=night_end,
            )
            for index, (day, night_end) in enumerate(zip(days, night_ends, strict=True))
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
    authority = _authority() if authority is None else authority
    calls = {"calendar_validation": 0}

    monkeypatch.setattr(
        capture_module,
        "load_public_carry_data",
        # The night-start widening reads the daily contracts of an unsettled
        # product-day, so prices is a real frame here. It holds only the
        # representative, which leaves this harness with no sibling to widen to.
        lambda **kwargs: CarryDataSet(
            prices=pd.DataFrame(
                [{"trade_date": day, "contract": "AU2406.SHF"} for day in days]
            ),
            data_quality=quality,
        ),
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
        calls["calendar_rows"] = tuple(rows)
        assert tuple(actual_calendar) == tuple(calendar)

    monkeypatch.setattr(
        capture_module,
        "validate_session_exception_calendar",
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
        lambda source, candidates, *, absent_identities=frozenset(): boundaries,
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


def test_capture_success_uses_checked_publisher_and_marks_report_validated(
    monkeypatch, tmp_path, capsys
):
    authority, calls, audit_keys = _install_capture_flow(
        monkeypatch,
        night_ends=("23:00",),
    )
    published = {}

    def fake_publish(**kwargs):
        published.update(kwargs)
        loaded_rules = (object(),)
        kwargs["validated_callback"](loaded_rules)
        kwargs["output"].write_bytes(b"published\n")
        return loaded_rules

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
    assert "publication_status=validated" in report.read_text()
    assert "products=1 rules=1 checked_days=1 ambiguous=0" in captured.out


def test_capture_logs_only_actual_authorized_history_gap_matches(
    monkeypatch, tmp_path, capsys
):
    day = date(2024, 1, 8)
    hit = EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="AU",
        effective_start=day,
        effective_end=day,
        reason="documented AU gap",
        source_url="https://www.shfe.com.cn/au-gap",
    )
    miss = EffectiveAuthorityRange(
        version=SESSION_RULES_VERSION,
        exchange="SHFE",
        product="CU",
        effective_start=day,
        effective_end=day,
        reason="must not leak",
        source_url="https://www.shfe.com.cn/missed",
    )
    authority = _authority(history_exceptions=(miss, hit))
    _install_capture_flow(
        monkeypatch,
        night_ends=("none",),
        authority=authority,
        history_status_by_key={
            ("SHFE", "AU", day): "authorized_history_gap",
        },
    )

    capture_module.capture_and_publish(
        start=day,
        end=day,
        backtest_start=date(2026, 1, 8),
        output=tmp_path / "sessions.csv",
        inventory_output=tmp_path / "inventory.csv",
        audit_report=tmp_path / "audit.txt",
        settings=None,
        use_test=True,
    )

    captured = capsys.readouterr()
    expected = (
        "authorized_history_gap key=SHFE/AU/2024-01-08 "
        "authority_effective_start=2024-01-08 authority_effective_end=2024-01-08 "
        "reason='documented AU gap' "
        "source_url='https://www.shfe.com.cn/au-gap'"
    )
    assert expected in captured.out
    assert expected in (tmp_path / "audit.txt").read_text()
    assert "must not leak" not in captured.out
    assert "https://www.shfe.com.cn/missed" not in captured.out


@pytest.mark.parametrize("failure", ["diagnostics", "replace"])
def test_capture_success_failures_preserve_old_output_and_honest_diagnostics(
    failure, monkeypatch, tmp_path
):
    paths, hashes = _authority_files(tmp_path)
    _install_capture_flow(
        monkeypatch,
        night_ends=("23:00",),
        authority=_authority(hashes=hashes),
    )
    monkeypatch.setattr(
        capture_module, "SESSION_EXCEPTIONS_PATH", paths["session_exception"]
    )
    monkeypatch.setattr(capture_module, "DAY_ONLY_PATH", paths["day_only"])
    monkeypatch.setattr(
        capture_module,
        "HISTORY_EXCEPTIONS_PATH",
        paths["history_exception"],
    )
    monkeypatch.setattr(
        capture_module,
        "ABSENT_PRODUCT_DAYS_PATH",
        paths["absent_product_day"],
    )
    if failure == "diagnostics":
        monkeypatch.setattr(
            capture_module,
            "write_capture_diagnostics",
            lambda **kwargs: (_ for _ in ()).throw(OSError("diagnostics failed")),
        )
    else:
        monkeypatch.setattr(
            capture_module.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("replace failed")
            ),
        )
    output = tmp_path / "sessions.csv"
    output.write_bytes(b"old\n")
    report = tmp_path / "audit.txt"

    with pytest.raises(OSError, match=failure):
        capture_module.capture_and_publish(
            start=date(2024, 1, 8),
            end=date(2024, 1, 8),
            backtest_start=date(2026, 1, 8),
            output=output,
            inventory_output=tmp_path / "inventory.csv",
            audit_report=report,
            settings=None,
            use_test=True,
        )

    assert output.read_bytes() == b"old\n"
    assert list(tmp_path.glob(".sessions.csv.*.tmp")) == []
    if failure == "replace":
        assert "publication_status=validated" in report.read_text()
        assert "publication_status=published" not in report.read_text()
    else:
        assert not report.exists()


def test_capture_unkeyable_uses_the_single_gate_without_querying_minutes(
    monkeypatch, tmp_path, capsys
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

    assert capture_module.capture_and_publish(**kwargs) == (1, 0, 0, 0)
    assert output.read_bytes() == b"old\n"
    report_text = report.read_text()
    assert "publication_status=blocked" in report_text
    assert "products=1 rules=0 checked_days=0 ambiguous=0" in report_text
    assert inventory.read_text().splitlines() == [
        "exchange,product,trade_date,check,reason"
    ]
    assert (
        capture_module.main(
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
        )
        == 1
    )
    assert output.read_bytes() == b"old\n"
    assert gate_calls == 2
    captured = capsys.readouterr()
    assert "ambiguous_session=normalization_unkeyable" not in captured.err
    assert captured.out.count("products=1 rules=0 checked_days=0 ambiguous=0") == 2


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


def test_capture_uses_an_injected_audit_builder_instead_of_the_carry_pool(
    monkeypatch, tmp_path
):
    """连续策略要按自己的宇宙采集，所以发布路径必须能被注入 audit builder。

    只断言「注入的被调了」不够 —— 两个都被调也能通过。这里让 Carry 的池子一旦被
    调用就直接 fail，注入才算真的取代了它。
    """
    _install_capture_flow(monkeypatch, night_ends=("none", "none"))
    # 上一行装的替身忽略入参，所以可以就地取回它要返回的 audit。
    audit = capture_module._build_default_liquidity_audit()
    monkeypatch.setattr(
        capture_module,
        "_build_default_liquidity_audit",
        lambda *args, **kwargs: pytest.fail(
            "Carry pool was used despite an injected audit builder"
        ),
    )
    seen = []

    def _builder(prices, **kwargs):
        seen.append((prices, kwargs))
        return audit

    capture_module.capture_and_publish(
        start=date(2024, 1, 8),
        end=date(2024, 1, 9),
        backtest_start=date(2026, 1, 9),
        output=tmp_path / "sessions.csv",
        inventory_output=tmp_path / "inventory.csv",
        audit_report=tmp_path / "audit.txt",
        settings=None,
        use_test=True,
        audit_builder=_builder,
    )

    assert len(seen) == 1
    _prices, kwargs = seen[0]
    assert set(kwargs) == {
        "history_starts",
        "history_exceptions",
        "start",
        "end",
        "config",
    }


def test_capture_falls_back_to_the_carry_pool_when_no_builder_is_injected(
    monkeypatch, tmp_path
):
    """不注入时必须仍走 Carry 的池子 —— 默认行为一个字节都不能变。"""
    _install_capture_flow(monkeypatch, night_ends=("none", "none"))
    audit = capture_module._build_default_liquidity_audit()
    calls = []
    monkeypatch.setattr(
        capture_module,
        "_build_default_liquidity_audit",
        lambda *args, **kwargs: calls.append(kwargs) or audit,
    )

    capture_module.capture_and_publish(
        start=date(2024, 1, 8),
        end=date(2024, 1, 9),
        backtest_start=date(2026, 1, 9),
        output=tmp_path / "sessions.csv",
        inventory_output=tmp_path / "inventory.csv",
        audit_report=tmp_path / "audit.txt",
        settings=None,
        use_test=True,
    )

    assert len(calls) == 1
