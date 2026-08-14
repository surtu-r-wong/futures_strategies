from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from pathlib import Path

import pytest

from cta_carry.minute_sessions import SESSION_RULES_VERSION
from cta_carry.session_authority import (
    AUTHORITY_VERSION,
    EffectiveAuthorityRange,
    NoNightDate,
    SessionAuthority,
    SessionAuthorityError,
    authorize_night_observation,
    load_authority_ranges,
    load_no_night_dates,
    load_session_authority,
    matching_no_night_dates,
    matching_ranges,
    validate_no_night_calendar,
)


NO_NIGHT_HEADER = "version,exchange,trade_date,reason,source_url\n"
RANGE_HEADER = (
    "version,exchange,product,effective_start,effective_end,reason,source_url\n"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _no_night_row(**overrides) -> NoNightDate:
    values = {
        "version": SESSION_RULES_VERSION,
        "exchange": "SHFE",
        "trade_date": date(2024, 2, 19),
        "reason": "Spring Festival notice_evening=2024-02-08",
        "source_url": "https://www.shfe.com.cn/notice/holiday",
    }
    values.update(overrides)
    return NoNightDate(**values)


def _range_row(**overrides) -> EffectiveAuthorityRange:
    values = {
        "version": SESSION_RULES_VERSION,
        "exchange": "SHFE",
        "product": "AU",
        "effective_start": date(2011, 1, 1),
        "effective_end": date(2013, 7, 4),
        "reason": "night trading not yet introduced",
        "source_url": "https://www.shfe.com.cn/notice/night-launch",
    }
    values.update(overrides)
    return EffectiveAuthorityRange(**values)


def _authority(
    *,
    no_night_dates=(),
    day_only_regimes=(),
    liquidity_history_exceptions=(),
) -> SessionAuthority:
    return SessionAuthority(
        no_night_dates=tuple(no_night_dates),
        day_only_regimes=tuple(day_only_regimes),
        liquidity_history_exceptions=tuple(liquidity_history_exceptions),
        sha256_by_asset={},
    )


def test_authority_records_are_immutable_and_share_session_rule_version():
    assert AUTHORITY_VERSION == SESSION_RULES_VERSION == "commodity-v1"

    with pytest.raises(FrozenInstanceError):
        _no_night_row().exchange = "DCE"
    with pytest.raises(FrozenInstanceError):
        _range_row().product = "AG"

    authority = _authority()
    with pytest.raises(TypeError):
        authority.sha256_by_asset["new"] = "0" * 64


@pytest.mark.parametrize(
    ("loader", "header"),
    [
        (load_no_night_dates, "exchange,version,trade_date,reason,source_url\n"),
        (
            load_authority_ranges,
            "version,exchange,product,effective_start,reason,source_url,effective_end\n",
        ),
    ],
)
def test_loaders_require_exact_ordered_headers(tmp_path, loader, header):
    path = _write(tmp_path / "authority.csv", header)

    with pytest.raises(SessionAuthorityError, match="authority_csv_header") as exc:
        loader(path)

    assert exc.value.check == "authority_csv_header"
    assert exc.value.reason == "CSV header does not match the authority schema"
    assert exc.value.context["path"] == str(path)


@pytest.mark.parametrize(
    ("loader", "header", "row"),
    [
        (
            load_no_night_dates,
            NO_NIGHT_HEADER,
            "commodity-v1,SHFE,2024-02-19,,https://www.shfe.com.cn/\n",
        ),
        (
            load_authority_ranges,
            RANGE_HEADER,
            "commodity-v1,SHFE,AU,2011-01-01,,day only,\n",
        ),
    ],
)
def test_loaders_reject_empty_required_values(tmp_path, loader, header, row):
    path = _write(tmp_path / "authority.csv", header + row)

    with pytest.raises(SessionAuthorityError, match="authority_csv_required") as exc:
        loader(path)

    assert exc.value.check == "authority_csv_required"
    assert exc.value.row_identity["row_number"] == 2


@pytest.mark.parametrize(
    ("loader", "header", "row"),
    [
        (
            load_no_night_dates,
            NO_NIGHT_HEADER,
            "commodity-v1,SHFE,2024-02-30,holiday,https://www.shfe.com.cn/\n",
        ),
        (
            load_authority_ranges,
            RANGE_HEADER,
            "commodity-v1,SHFE,AU,2011-13-01,,day only,https://www.shfe.com.cn/\n",
        ),
        (
            load_authority_ranges,
            RANGE_HEADER,
            "commodity-v1,SHFE,AU,2011-01-01,2011-02-30,day only,https://www.shfe.com.cn/\n",
        ),
    ],
)
def test_loaders_reject_invalid_iso_dates(tmp_path, loader, header, row):
    path = _write(tmp_path / "authority.csv", header + row)

    with pytest.raises(SessionAuthorityError, match="authority_csv_date") as exc:
        loader(path)

    assert exc.value.check == "authority_csv_date"


@pytest.mark.parametrize(
    ("loader", "header", "row"),
    [
        (
            load_no_night_dates,
            NO_NIGHT_HEADER,
            "commodity-v2,SHFE,2024-02-19,holiday,https://www.shfe.com.cn/\n",
        ),
        (
            load_authority_ranges,
            RANGE_HEADER,
            "commodity-v2,SHFE,AU,2011-01-01,,day only,https://www.shfe.com.cn/\n",
        ),
    ],
)
def test_loaders_reject_unknown_versions(tmp_path, loader, header, row):
    path = _write(tmp_path / "authority.csv", header + row)

    with pytest.raises(SessionAuthorityError, match="authority_csv_version") as exc:
        loader(path)

    assert exc.value.check == "authority_csv_version"


def test_no_night_loader_rejects_duplicate_keys(tmp_path):
    row = (
        "commodity-v1,SHFE,2024-02-19,holiday notice_evening=2024-02-08,"
        "https://www.shfe.com.cn/\n"
    )
    path = _write(tmp_path / "no-night.csv", NO_NIGHT_HEADER + row + row)

    with pytest.raises(SessionAuthorityError, match="authority_duplicate_key") as exc:
        load_no_night_dates(path)

    assert exc.value.check == "authority_duplicate_key"
    assert exc.value.row_identity == {
        "version": "commodity-v1",
        "exchange": "SHFE",
        "trade_date": "2024-02-19",
    }


@pytest.mark.parametrize(
    "second_row",
    [
        "commodity-v1,SHFE,AU,2011-02-01,2011-04-01,second,https://www.shfe.com.cn/\n",
        "commodity-v1,SHFE,AU,2012-01-01,,second,https://www.shfe.com.cn/\n",
    ],
)
def test_range_loader_rejects_closed_and_open_ended_overlaps(
    tmp_path, second_row
):
    first = (
        "commodity-v1,SHFE,AU,2011-01-01,2011-03-01,first,"
        "https://www.shfe.com.cn/\n"
    )
    if second_row.startswith("commodity-v1,SHFE,AU,2012"):
        first = (
            "commodity-v1,SHFE,AU,2011-01-01,,first,"
            "https://www.shfe.com.cn/\n"
        )
    path = _write(tmp_path / "ranges.csv", RANGE_HEADER + first + second_row)

    with pytest.raises(SessionAuthorityError, match="authority_range_overlap") as exc:
        load_authority_ranges(path)

    assert exc.value.check == "authority_range_overlap"


def test_range_loader_rejects_duplicate_start_keys_before_overlap(tmp_path):
    first = (
        "commodity-v1,SHFE,AU,2011-01-01,2011-03-01,first,"
        "https://www.shfe.com.cn/\n"
    )
    second = (
        "commodity-v1,SHFE,AU,2011-01-01,2011-04-01,second,"
        "https://www.shfe.com.cn/\n"
    )
    path = _write(tmp_path / "ranges.csv", RANGE_HEADER + first + second)

    with pytest.raises(SessionAuthorityError, match="authority_duplicate_key") as exc:
        load_authority_ranges(path)

    assert exc.value.check == "authority_duplicate_key"


def test_range_loader_rejects_inverted_intervals(tmp_path):
    row = (
        "commodity-v1,SHFE,AU,2011-03-01,2011-01-01,bad order,"
        "https://www.shfe.com.cn/\n"
    )
    path = _write(tmp_path / "ranges.csv", RANGE_HEADER + row)

    with pytest.raises(SessionAuthorityError, match="authority_range_order") as exc:
        load_authority_ranges(path)

    assert exc.value.check == "authority_range_order"


def test_header_only_history_exception_asset_is_valid(tmp_path):
    path = _write(tmp_path / "history.csv", RANGE_HEADER)

    assert load_authority_ranges(path) == ()


def test_load_session_authority_hashes_exact_asset_bytes(tmp_path):
    no_night_path = _write(tmp_path / "no-night.csv", NO_NIGHT_HEADER)
    day_only_path = _write(tmp_path / "day-only.csv", RANGE_HEADER)
    history_path = _write(tmp_path / "history.csv", RANGE_HEADER)

    authority = load_session_authority(
        no_night_path=no_night_path,
        day_only_path=day_only_path,
        history_exception_path=history_path,
    )

    assert authority.no_night_dates == ()
    assert authority.day_only_regimes == ()
    assert authority.liquidity_history_exceptions == ()
    assert set(authority.sha256_by_asset) == {
        "no_night",
        "day_only",
        "history_exception",
    }
    assert all(
        len(digest) == 64
        and digest == digest.lower()
        and set(digest) <= set("0123456789abcdef")
        for digest in authority.sha256_by_asset.values()
    )


def test_notice_evening_maps_to_the_next_target_trade_date():
    calendar = (date(2024, 2, 8), date(2024, 2, 19), date(2024, 2, 20))
    row = _no_night_row()

    validate_no_night_calendar((row,), calendar)

    bad = replace(row, trade_date=date(2024, 2, 8))
    with pytest.raises(
        SessionAuthorityError, match="notice_target_trade_date"
    ) as exc:
        validate_no_night_calendar((bad,), calendar)

    assert exc.value.check == "notice_target_trade_date"
    assert exc.value.row is bad
    assert exc.value.context == {"expected_trade_date": date(2024, 2, 19)}


@pytest.mark.parametrize(
    "reason",
    [
        "holiday notice has no token",
        "holiday notice_evening=2024-02-08 notice_evening=2024-02-09",
        "holiday notice_evening=2024-02-30",
    ],
)
def test_notice_evening_requires_exactly_one_valid_machine_readable_date(reason):
    row = _no_night_row(reason=reason)

    with pytest.raises(SessionAuthorityError, match="notice_evening"):
        validate_no_night_calendar((row,), (date(2024, 2, 19),))


@pytest.mark.parametrize(
    "calendar",
    [
        (datetime(2024, 2, 19),),
        ("2024-02-19",),
    ],
)
def test_notice_calendar_requires_actual_date_values(calendar):
    with pytest.raises(SessionAuthorityError, match="notice_calendar_date"):
        validate_no_night_calendar((_no_night_row(),), calendar)


def test_matching_helpers_are_deterministic_and_inclusive():
    no_night = (
        _no_night_row(exchange="DCE"),
        _no_night_row(exchange="SHFE"),
    )
    ranges = (
        _range_row(exchange="DCE", product="I"),
        _range_row(exchange="SHFE", product="AU"),
    )

    assert matching_no_night_dates(
        reversed(no_night), "SHFE", date(2024, 2, 19)
    ) == (no_night[1],)
    assert matching_ranges(ranges, "SHFE", "AU", date(2011, 1, 1)) == (
        ranges[1],
    )
    assert matching_ranges(ranges, "SHFE", "AU", date(2013, 7, 4)) == (
        ranges[1],
    )


def test_matching_helpers_reject_multiplicity_even_for_unloaded_rows():
    duplicate_halts = (_no_night_row(), _no_night_row(source_url="https://other/"))
    overlapping_ranges = (
        _range_row(),
        _range_row(
            effective_start=date(2013, 1, 1),
            effective_end=None,
            reason="overlap",
        ),
    )

    with pytest.raises(SessionAuthorityError, match="authority_match_cardinality"):
        matching_no_night_dates(duplicate_halts, "SHFE", date(2024, 2, 19))
    with pytest.raises(SessionAuthorityError, match="authority_match_cardinality"):
        matching_ranges(overlapping_ranges, "SHFE", "AU", date(2013, 2, 1))


@pytest.mark.parametrize("observed_night_end", ["23:00", "23:30", "01:00", "02:30"])
def test_observed_night_requires_both_none_authorities_to_be_absent(
    observed_night_end,
):
    authorize_night_observation(
        _authority(),
        exchange="SHFE",
        product="AU",
        trade_date=date(2024, 2, 19),
        observed_night_end=observed_night_end,
    )

    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(no_night_dates=(_no_night_row(),)),
            exchange="SHFE",
            product="AU",
            trade_date=date(2024, 2, 19),
            observed_night_end=observed_night_end,
        )


def test_observed_none_requires_one_authority_with_day_only_priority():
    day_only = _range_row(effective_end=None)
    halt = _no_night_row()
    values = {
        "exchange": "SHFE",
        "product": "AU",
        "trade_date": date(2024, 2, 19),
        "observed_night_end": "none",
    }

    authorize_night_observation(_authority(day_only_regimes=(day_only,)), **values)
    authorize_night_observation(_authority(no_night_dates=(halt,)), **values)
    authorize_night_observation(
        _authority(no_night_dates=(halt,), day_only_regimes=(day_only,)), **values
    )

    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(_authority(), **values)


def test_authorization_rejects_unknown_observations_and_keeps_error_context():
    trade_date = date(2024, 2, 19)

    with pytest.raises(SessionAuthorityError, match="night_observation_value") as exc:
        authorize_night_observation(
            _authority(),
            exchange="SHFE",
            product="AU",
            trade_date=trade_date,
            observed_night_end="23:15",
        )

    assert exc.value.check == "night_observation_value"
    assert exc.value.reason == "unsupported observed night-session endpoint"
    assert exc.value.row_identity == {
        "exchange": "SHFE",
        "product": "AU",
        "trade_date": "2024-02-19",
    }
    assert exc.value.context == {"observed_night_end": "23:15"}


def test_repository_authority_assets_have_exact_header_only_contracts():
    repository = Path(__file__).resolve().parents[1]

    assert (
        repository / "config/carry_minute_no_night_dates.csv"
    ).read_text(encoding="utf-8") == NO_NIGHT_HEADER
    assert (
        repository / "config/carry_minute_day_only_regimes.csv"
    ).read_text(encoding="utf-8") == RANGE_HEADER
    assert (
        repository / "config/carry_liquidity_history_exceptions.csv"
    ).read_text(encoding="utf-8") == RANGE_HEADER
