from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from cta_carry.minute_bars import (
    MinuteDataError,
    aggregate_fifteen_minute_bar,
    five_minute_vwap,
    infer_contract_multiplier,
    validate_metadata_multiplier,
)
from cta_carry.minute_sessions import (
    SESSION_RULES_VERSION,
    SessionRule,
    SessionSegment,
    build_trading_slots,
    next_slots,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CONTRACT = "RB2405"


def _rows(prices, volumes, multiplier=10):
    start = datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)
    records = []
    for index, (price, volume) in enumerate(zip(prices, volumes, strict=True)):
        records.append(
            {
                "bar_time": start + timedelta(minutes=index),
                "symbol": CONTRACT,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "amount": price * volume * multiplier,
            }
        )
    return pd.DataFrame(records)


def test_zero_volume_prices_do_not_enter_fifteen_minute_extremes():
    frame = _rows([100.0, 1.0, 101.0], [2.0, 0.0, 3.0])

    bar = aggregate_fifteen_minute_bar(
        frame,
        slots=tuple(frame["bar_time"]),
        contract=CONTRACT,
    )

    assert (bar.open, bar.high, bar.low, bar.close) == (
        100.0,
        101.0,
        100.0,
        101.0,
    )
    assert bar.volume == 5.0


def test_vwap_uses_amount_volume_and_multiplier():
    frame = _rows([100.0, 102.0, 104.0, 106.0, 108.0], [1, 2, 3, 4, 5])

    result = five_minute_vwap(
        frame,
        slots=tuple(frame["bar_time"]),
        contract=CONTRACT,
        multiplier=10,
    )

    expected = sum(frame["amount"]) / sum(frame["volume"]) / 10
    assert result.price == pytest.approx(expected)
    assert result.volume == 15.0


def test_multiplier_is_uniquely_inferred_from_price_ranges():
    frame = _rows([100.0] * 60, [1.0] * 60, multiplier=10)
    frame["trade_date"] = frame["bar_time"].dt.date
    frame.loc[20:39, "trade_date"] = frame.loc[20:39, "trade_date"].map(
        lambda value: value + timedelta(days=1)
    )
    frame.loc[40:59, "trade_date"] = frame.loc[40:59, "trade_date"].map(
        lambda value: value + timedelta(days=2)
    )

    result = infer_contract_multiplier(frame, contract=CONTRACT)

    assert result.multiplier == 10
    assert result.source == "inferred"
    assert result.sample_rows == 60


def test_zero_volume_execution_window_is_a_hard_failure():
    frame = _rows([100.0] * 5, [0.0] * 5)

    with pytest.raises(MinuteDataError, match="execution_vwap") as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=10,
        )

    assert exc_info.value.check == "execution_vwap"


def _assert_error(exc_info, check):
    error = exc_info.value
    assert error.check == check
    assert check in str(error)
    assert CONTRACT in str(error)


def test_duplicate_minute_timestamps_are_rejected():
    frame = _rows([100.0, 101.0], [1.0, 1.0])
    frame.loc[1, "bar_time"] = frame.loc[0, "bar_time"]

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(
            frame,
            slots=(frame.loc[0, "bar_time"],),
            contract=CONTRACT,
        )

    _assert_error(exc_info, "duplicate_bar_time")


@pytest.mark.parametrize(
    ("column", "check"),
    [("volume", "minute_volume"), ("amount", "minute_amount")],
)
def test_negative_volume_or_amount_is_rejected(column, check):
    frame = _rows([100.0] * 5, [1.0] * 5)
    frame.loc[2, column] = -1.0

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, check)


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
def test_nonfinite_positive_volume_ohlc_is_rejected(column):
    frame = _rows([100.0, 101.0, 102.0], [1.0, 1.0, 1.0])
    frame.loc[1, column] = np.inf

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
        )

    _assert_error(exc_info, "positive_volume_values")
    assert column in str(exc_info.value)


def test_nonfinite_positive_volume_amount_is_rejected():
    frame = _rows([100.0, 101.0, 102.0], [1.0, 1.0, 1.0])
    frame.loc[1, "amount"] = np.nan

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
        )

    _assert_error(exc_info, "positive_volume_values")
    assert "amount" in str(exc_info.value)


def test_rows_outside_authoritative_slots_are_rejected_with_context():
    frame = _rows([100.0, 101.0, 102.0], [1.0, 1.0, 1.0])
    slots = tuple(frame.loc[:1, "bar_time"])
    frame["trade_date"] = slots[0].date()

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(frame, slots=slots, contract=CONTRACT)

    _assert_error(exc_info, "rows_outside_slots")
    error = exc_info.value
    assert error.trade_date == slots[0].date()
    assert error.timestamp == frame.loc[2, "bar_time"]
    assert error.product == "RB"
    message = str(error)
    assert str(error.trade_date) in message
    assert error.timestamp.isoformat() in message
    assert error.reason in message
    assert "outside_count" in message


def test_missing_slot_rows_do_not_compress_the_execution_clock():
    complete = _rows([100.0, 101.0, 102.0, 103.0, 104.0], [1.0] * 5)
    slots = tuple(complete["bar_time"])
    complete["trade_date"] = date(2024, 1, 8)
    sparse = complete.drop(index=1)

    fill = five_minute_vwap(
        sparse,
        slots=slots,
        contract=CONTRACT,
        multiplier=10,
    )

    assert fill.start == slots[0]
    assert fill.end == slots[-1] + timedelta(minutes=1)
    assert fill.traded_rows == 4
    assert fill.missing_slots == 1
    assert fill.trade_date == date(2024, 1, 8)
    assert fill.window_slots == slots
    assert fill.missing_slot_times == (slots[1],)


def test_execution_vwap_requires_exactly_five_supplied_slots():
    frame = _rows([100.0] * 4, [1.0] * 4)

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "execution_slots_cardinality")


@pytest.mark.parametrize(
    ("slots", "check"),
    [
        (
            (
                datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI),
                datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI),
            ),
            "duplicate_slots",
        ),
        (
            (
                datetime(2024, 1, 8, 9, 0),
                datetime(2024, 1, 8, 9, 1),
            ),
            "slot_datetime_awareness",
        ),
        (
            (
                datetime(2024, 1, 8, 9, 1, tzinfo=SHANGHAI),
                datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI),
            ),
            "slots_strict_order",
        ),
    ],
)
def test_invalid_authoritative_slots_are_rejected(slots, check):
    frame = _rows([], [])

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(frame, slots=slots, contract=CONTRACT)

    _assert_error(exc_info, check)


def test_vwap_outside_the_traded_range_is_rejected():
    frame = _rows([100.0] * 5, [1.0] * 5)
    frame["amount"] = 110.0 * frame["volume"] * 10

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "execution_vwap")
    assert exc_info.value.reason == "VWAP is outside the traded price range"
    assert exc_info.value.context == {"vwap": 110.0, "low": 100.0, "high": 100.0}

def _multiplier_rows(count=60, *, date_count=3, low=100.0, high=100.0):
    frame = _rows([100.0] * count, [1.0] * count, multiplier=10)
    first_date = frame.loc[0, "bar_time"].date()
    frame["trade_date"] = [
        first_date + timedelta(days=min(index * date_count // count, date_count - 1))
        for index in range(count)
    ]
    frame["low"] = low
    frame["high"] = high
    return frame


def test_multiplier_inference_rejects_only_nine_nonzero_samples():
    frame = _multiplier_rows(count=9, date_count=2)

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert "eligible_rows" in str(exc_info.value)


def test_multiplier_inference_rejects_fewer_than_two_dates():
    frame = _multiplier_rows(count=10, date_count=1)

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert "sample_dates" in str(exc_info.value)


def test_sixty_row_multiplier_sample_requires_three_dates():
    frame = _multiplier_rows(count=60, date_count=2)

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert exc_info.value.context["required_dates"] == 3


def test_multiplier_inference_rejects_two_accepted_candidates():
    frame = _multiplier_rows(count=60, date_count=3, low=90.0, high=101.0)
    frame["amount"] = 1000.0

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "contract_multiplier")
    assert exc_info.value.context["candidate_count"] == 2
    assert exc_info.value.context["candidates"] == (10, 11)


def test_fewer_than_sixty_multiplier_rows_use_every_eligible_row():
    frame = _multiplier_rows(count=10, date_count=2)

    result = infer_contract_multiplier(frame, contract=CONTRACT)

    assert result.sample_rows == 10
    assert result.sample_dates == 2
    assert result.sample_start == frame["bar_time"].min()
    assert result.sample_end == frame["bar_time"].max()


def test_large_multiplier_sample_is_exactly_sixty_and_spans_range():
    frame = _multiplier_rows(count=121, date_count=3).sample(
        frac=1.0,
        random_state=42,
    )

    result = infer_contract_multiplier(frame, contract=CONTRACT)

    assert result.sample_rows == 60
    assert result.sample_dates == 3
    assert result.sample_start == frame["bar_time"].min()
    assert result.sample_end == frame["bar_time"].max()


@pytest.mark.parametrize("multiplier", [True, 10.5, "10", 0, -1])
def test_metadata_multiplier_must_be_a_positive_actual_integer(multiplier):
    frame = _multiplier_rows()

    with pytest.raises(MinuteDataError) as exc_info:
        validate_metadata_multiplier(
            frame,
            contract=CONTRACT,
            multiplier=multiplier,
        )

    _assert_error(exc_info, "metadata_multiplier")


def test_metadata_multiplier_must_pass_the_same_range_validation():
    frame = _multiplier_rows()

    with pytest.raises(MinuteDataError) as exc_info:
        validate_metadata_multiplier(frame, contract=CONTRACT, multiplier=11)

    _assert_error(exc_info, "metadata_multiplier")
    assert exc_info.value.context["pass_rate"] == 0.0


def test_valid_metadata_multiplier_returns_auditable_resolution():
    frame = _multiplier_rows()

    result = validate_metadata_multiplier(frame, contract=CONTRACT, multiplier=10)

    assert result.multiplier == 10
    assert result.source == "metadata"
    assert result.sample_rows == 60
    assert result.sample_dates == 3
    assert result.pass_rate == 1.0


def test_all_zero_volume_bar_is_no_trade_with_null_ohlc():
    frame = _rows([100.0, 101.0, 102.0], [0.0, 0.0, 0.0])

    bar = aggregate_fifteen_minute_bar(
        frame,
        slots=tuple(frame["bar_time"]),
        contract=CONTRACT,
    )

    assert bar.no_trade is True
    assert (bar.open, bar.high, bar.low, bar.close) == (None, None, None, None)
    assert bar.volume == 0.0


def test_zero_volume_carried_price_does_not_enter_vwap_audit_range():
    frame = _rows([100.0, 1.0, 102.0, 103.0, 104.0], [1, 0, 1, 1, 1])

    fill = five_minute_vwap(
        frame,
        slots=tuple(frame["bar_time"]),
        contract=CONTRACT,
        multiplier=10,
    )

    assert fill.low == 100.0
    assert fill.high == 104.0
    assert fill.volume == 4.0


def test_execution_vwap_rejects_nonpositive_total_amount():
    frame = _rows([100.0] * 5, [1.0] * 5)
    frame["amount"] = 0.0

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "execution_vwap")
    assert "finite and positive" in exc_info.value.reason


@pytest.mark.parametrize("multiplier", [True, 10.5, "10", 0, -1])
def test_execution_multiplier_must_be_a_positive_actual_integer(multiplier):
    frame = _rows([100.0] * 5, [1.0] * 5)

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
            multiplier=multiplier,
        )

    _assert_error(exc_info, "execution_multiplier")


def test_missing_minute_columns_raise_a_structured_schema_error():
    frame = _rows([100.0], [1.0]).drop(columns="amount")

    with pytest.raises(MinuteDataError) as exc_info:
        aggregate_fifteen_minute_bar(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
        )

    _assert_error(exc_info, "minute_schema")
    assert exc_info.value.context == {"missing": ("amount",)}


def test_exact_even_sample_does_not_replace_positions_to_chase_a_date():
    frame = _multiplier_rows(count=121, date_count=2)
    first_date = frame.loc[0, "trade_date"]
    frame.loc[1, "trade_date"] = first_date + timedelta(days=2)
    frame.loc[2, "amount"] = 900.0

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert exc_info.value.context["sample_dates"] == 2


def test_frame_trade_date_overrides_a_night_window_physical_date():
    friday_night = datetime(2024, 1, 5, 21, 0, tzinfo=SHANGHAI)
    slots = tuple(friday_night + timedelta(minutes=index) for index in range(5))
    frame = _rows([100.0] * 5, [1.0] * 5)
    frame["bar_time"] = slots
    frame["trade_date"] = date(2024, 1, 8)
    frame.loc[0, "amount"] = -1.0

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=slots,
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "minute_amount")
    assert exc_info.value.trade_date == date(2024, 1, 8)
    assert "trade_date=2024-01-08" in str(exc_info.value)


def test_plain_night_slots_do_not_invent_a_physical_trade_date():
    friday_night = datetime(2024, 1, 5, 21, 0, tzinfo=SHANGHAI)
    slots = tuple(friday_night + timedelta(minutes=index) for index in range(5))
    frame = _rows([100.0] * 5, [1.0] * 5)
    frame["bar_time"] = slots
    frame.loc[0, "amount"] = -1.0

    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            frame,
            slots=slots,
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "minute_amount")
    assert exc_info.value.trade_date is None
    assert "trade_date=" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["aggregation", "vwap"])
def test_nullable_null_symbol_is_a_structured_contract_mismatch(operation):
    count = 5 if operation == "vwap" else 3
    frame = _rows([100.0] * count, [1.0] * count)
    frame["symbol"] = frame["symbol"].astype("string")
    frame.loc[1, "symbol"] = pd.NA

    with pytest.raises(MinuteDataError) as exc_info:
        if operation == "aggregation":
            aggregate_fifteen_minute_bar(
                frame,
                slots=tuple(frame["bar_time"]),
                contract=CONTRACT,
            )
        else:
            five_minute_vwap(
                frame,
                slots=tuple(frame["bar_time"]),
                contract=CONTRACT,
                multiplier=10,
            )

    _assert_error(exc_info, "minute_contract")
    assert exc_info.value.timestamp == frame.loc[1, "bar_time"]


def test_nullable_null_symbol_is_rejected_during_multiplier_validation():
    frame = _multiplier_rows()
    frame["symbol"] = frame["symbol"].astype("string")
    frame.loc[1, "symbol"] = pd.NA

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "minute_contract")
    assert exc_info.value.timestamp == frame.loc[1, "bar_time"]


def test_nullable_symbols_do_not_distort_missing_slot_audit():
    complete = _rows([100.0] * 5, [1.0] * 5)
    slots = tuple(complete["bar_time"])
    sparse = complete.drop(index=1)
    sparse["symbol"] = sparse["symbol"].astype("string")

    fill = five_minute_vwap(
        sparse,
        slots=slots,
        contract=CONTRACT,
        multiplier=10,
    )

    assert fill.missing_slots == 1
    assert fill.missing_slot_times == (slots[1],)
    assert len(fill.missing_slot_times) == fill.missing_slots


def _resolve_multiplier(frame, source):
    if source == "inferred":
        return infer_contract_multiplier(frame, contract=CONTRACT)
    return validate_metadata_multiplier(frame, contract=CONTRACT, multiplier=10)


@pytest.mark.parametrize("source", ["inferred", "metadata"])
def test_intraday_trade_date_timestamps_count_as_one_calendar_date(source):
    frame = _multiplier_rows(count=10, date_count=2)
    frame["trade_date"] = [
        pd.Timestamp("2024-01-08") + pd.Timedelta(minutes=index)
        for index in range(10)
    ]

    with pytest.raises(MinuteDataError) as exc_info:
        _resolve_multiplier(frame, source)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert exc_info.value.context["sample_dates"] == 1
    assert exc_info.value.context["required_dates"] == 2


@pytest.mark.parametrize("source", ["inferred", "metadata"])
def test_sixty_timestamp_values_on_two_calendar_dates_fail_date_gate(source):
    frame = _multiplier_rows(count=60, date_count=3)
    frame["trade_date"] = [
        pd.Timestamp("2024-01-08") + pd.Timedelta(minutes=index)
        if index < 30
        else pd.Timestamp("2024-01-09") + pd.Timedelta(minutes=index - 30)
        for index in range(60)
    ]

    with pytest.raises(MinuteDataError) as exc_info:
        _resolve_multiplier(frame, source)

    _assert_error(exc_info, "contract_multiplier_sample")
    assert exc_info.value.context["sample_dates"] == 2
    assert exc_info.value.context["required_dates"] == 3


def test_empty_monday_night_window_preserves_logical_clock_context():
    trade_date = date(2024, 1, 8)
    rule = SessionRule(
        exchange="SHFE",
        product="RB",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(SessionSegment(-180, -165),),
        version=SESSION_RULES_VERSION,
    )
    session_slots = build_trading_slots(
        trade_date=trade_date,
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )
    window = next_slots(session_slots, session_slots[0], count=5)
    empty = _rows([100.0] * 5, [1.0] * 5).iloc[0:0]

    assert all(timestamp.date() == date(2024, 1, 5) for timestamp in window)
    with pytest.raises(MinuteDataError) as exc_info:
        five_minute_vwap(
            empty,
            slots=window,
            contract=CONTRACT,
            multiplier=10,
        )

    _assert_error(exc_info, "execution_vwap")
    assert exc_info.value.trade_date == trade_date
    assert exc_info.value.product == "RB"
    assert "trade_date=2024-01-08" in str(exc_info.value)


def _run_bar_operation(frame, operation):
    if operation == "aggregation":
        return aggregate_fifteen_minute_bar(
            frame,
            slots=tuple(frame["bar_time"]),
            contract=CONTRACT,
        )
    return five_minute_vwap(
        frame,
        slots=tuple(frame["bar_time"]),
        contract=CONTRACT,
        multiplier=10,
    )


@pytest.mark.parametrize("operation", ["aggregation", "vwap"])
def test_positive_volume_low_above_high_is_rejected(operation):
    count = 5 if operation == "vwap" else 3
    frame = _rows([100.0] * count, [1.0] * count)
    frame.loc[1, ["low", "high"]] = [101.0, 99.0]

    with pytest.raises(MinuteDataError) as exc_info:
        _run_bar_operation(frame, operation)

    _assert_error(exc_info, "minute_price_range")
    assert exc_info.value.timestamp == frame.loc[1, "bar_time"]


@pytest.mark.parametrize("operation", ["aggregation", "vwap"])
@pytest.mark.parametrize("column", ["open", "close"])
def test_positive_volume_open_and_close_must_be_within_range(operation, column):
    count = 5 if operation == "vwap" else 3
    frame = _rows([100.0] * count, [1.0] * count)
    frame.loc[1, ["low", "high"]] = [99.0, 101.0]
    frame.loc[1, column] = 102.0

    with pytest.raises(MinuteDataError) as exc_info:
        _run_bar_operation(frame, operation)

    _assert_error(exc_info, "minute_price_range")
    assert column in exc_info.value.context["invalid_fields"]


@pytest.mark.parametrize("operation", ["aggregation", "vwap"])
def test_zero_volume_carried_rows_ignore_malformed_ohlc_order(operation):
    frame = _rows([100.0] * 5, [1.0, 0.0, 1.0, 1.0, 1.0])
    frame.loc[1, ["open", "high", "low", "close"]] = [
        300.0,
        100.0,
        200.0,
        50.0,
    ]

    result = _run_bar_operation(frame, operation)

    assert result.low == 100.0
    assert result.high == 100.0


def test_multiplier_rejects_a_positive_volume_reversed_price_range():
    frame = _multiplier_rows()
    frame.loc[1, ["low", "high"]] = [101.0, 99.0]

    with pytest.raises(MinuteDataError) as exc_info:
        infer_contract_multiplier(frame, contract=CONTRACT)

    _assert_error(exc_info, "minute_price_range")
    assert exc_info.value.timestamp == frame.loc[1, "bar_time"]
