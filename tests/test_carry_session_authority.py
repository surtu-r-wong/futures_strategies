from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
import hashlib
from pathlib import Path

import pytest

from cta_carry.minute_sessions import SESSION_RULES_VERSION
from cta_carry.session_authority import (
    AUTHORITY_VERSION,
    EffectiveAuthorityRange,
    SessionAuthority,
    SessionAuthorityError,
    SessionException,
    authorize_night_observation,
    load_authority_ranges,
    load_session_authority,
    load_session_exceptions,
    matching_ranges,
    matching_session_exceptions,
    validate_session_exception_calendar,
)


SESSION_EXCEPTION_HEADER = (
    "exchange,version,trade_date,night_start,night_end,reason,source_url\n"
)
RANGE_HEADER = (
    "version,exchange,product,effective_start,effective_end,reason,source_url\n"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class _AtomicReplaceOnClose:
    def __init__(self, handle, owner, replacement_payload: bytes) -> None:
        self._handle = handle
        self._owner = owner
        self._replacement_payload = replacement_payload

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self._handle.__exit__(*args)
        finally:
            target = Path(str(self._owner))
            replacement = target.with_name(f"{target.name}.next")
            replacement.write_bytes(self._replacement_payload)
            replacement.replace(target)
            self._owner._replacement_pending = False

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __iter__(self):
        return iter(self._handle)


class _AtomicReplacingPath(type(Path())):
    __slots__ = ("_replacement_payload", "_replacement_pending")

    def arm_atomic_replacement(self, payload: bytes):
        self._replacement_payload = payload
        self._replacement_pending = True
        return self

    def open(self, *args, **kwargs):
        handle = super().open(*args, **kwargs)
        mode = kwargs.get("mode", args[0] if args else "r")
        if self._replacement_pending and "r" in mode:
            return _AtomicReplaceOnClose(
                handle,
                self,
                self._replacement_payload,
            )
        return handle


def _session_exception(**overrides) -> SessionException:
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


def _holiday_exception(**overrides) -> SessionException:
    values = {
        "exchange": "SHFE",
        "trade_date": date(2024, 2, 19),
        "night_start": "none",
        "night_end": "none",
        "reason": "Spring Festival notice_evening=2024-02-08",
        "source_url": "https://www.shfe.com.cn/notice/holiday",
    }
    values.update(overrides)
    return _session_exception(**values)


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
    session_exceptions=(),
    day_only_regimes=(),
    liquidity_history_exceptions=(),
) -> SessionAuthority:
    return SessionAuthority(
        session_exceptions=tuple(session_exceptions),
        day_only_regimes=tuple(day_only_regimes),
        liquidity_history_exceptions=tuple(liquidity_history_exceptions),
        sha256_by_asset={},
    )


def test_authority_records_are_immutable_and_share_session_rule_version():
    assert AUTHORITY_VERSION == SESSION_RULES_VERSION == "commodity-v1"

    with pytest.raises(FrozenInstanceError):
        _range_row().product = "AG"

    authority = _authority()
    with pytest.raises(TypeError):
        authority.sha256_by_asset["new"] = "0" * 64


def test_session_exception_is_immutable_and_uses_the_rule_version():
    row = _session_exception()
    assert row.version == AUTHORITY_VERSION == SESSION_RULES_VERSION
    with pytest.raises(FrozenInstanceError):
        row.night_start = "21:00"


def test_session_exception_loader_reads_exact_schema(tmp_path):
    path = _write(
        tmp_path / "exceptions.csv",
        SESSION_EXCEPTION_HEADER
        + "DCE,commodity-v1,2019-12-26,22:30,23:00,delayed night open"
        " notice_evening=2019-12-25,https://www.dce.com.cn/notice/6202113\n",
    )
    assert load_session_exceptions(path) == (_session_exception(),)


@pytest.mark.parametrize(
    ("loader", "header"),
    [
        (
            load_session_exceptions,
            "version,exchange,trade_date,night_start,night_end,reason,source_url\n",
        ),
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
            load_session_exceptions,
            SESSION_EXCEPTION_HEADER,
            "SHFE,commodity-v1,2024-02-19,none,none,,https://www.shfe.com.cn/\n",
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
            load_session_exceptions,
            SESSION_EXCEPTION_HEADER,
            "SHFE,commodity-v1,2024-02-30,none,none,holiday,"
            "https://www.shfe.com.cn/\n",
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
            load_session_exceptions,
            SESSION_EXCEPTION_HEADER,
            "SHFE,commodity-v2,2024-02-19,none,none,holiday,"
            "https://www.shfe.com.cn/\n",
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


def test_session_exception_loader_rejects_duplicate_keys(tmp_path):
    row = (
        "SHFE,commodity-v1,2024-02-19,none,none,"
        "holiday notice_evening=2024-02-08,https://www.shfe.com.cn/\n"
    )
    path = _write(tmp_path / "exceptions.csv", SESSION_EXCEPTION_HEADER + row + row)

    with pytest.raises(SessionAuthorityError, match="authority_duplicate_key") as exc:
        load_session_exceptions(path)

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
    exception_path = _write(tmp_path / "exceptions.csv", SESSION_EXCEPTION_HEADER)
    day_only_path = _write(tmp_path / "day-only.csv", RANGE_HEADER)
    history_path = _write(tmp_path / "history.csv", RANGE_HEADER)

    authority = load_session_authority(
        session_exception_path=exception_path,
        day_only_path=day_only_path,
        history_exception_path=history_path,
    )

    assert authority.session_exceptions == ()
    assert authority.day_only_regimes == ()
    assert authority.liquidity_history_exceptions == ()
    assert set(authority.sha256_by_asset) == {
        "session_exception",
        "day_only",
        "history_exception",
    }
    assert all(
        len(digest) == 64
        and digest == digest.lower()
        and set(digest) <= set("0123456789abcdef")
        for digest in authority.sha256_by_asset.values()
    )


def test_session_authority_parses_the_same_snapshot_bound_to_each_digest(tmp_path):
    original = SESSION_EXCEPTION_HEADER.encode()
    raw_exception_path = _write(tmp_path / "exceptions.csv", SESSION_EXCEPTION_HEADER)
    day_only_path = _write(tmp_path / "day-only.csv", RANGE_HEADER)
    history_path = _write(tmp_path / "history.csv", RANGE_HEADER)
    replacement = (
        SESSION_EXCEPTION_HEADER
        + "DCE,commodity-v1,2024-02-19,none,none,"
        "holiday notice_evening=2024-02-08,https://www.dce.com.cn/\n"
    ).encode()
    exception_path = _AtomicReplacingPath(raw_exception_path).arm_atomic_replacement(
        replacement
    )

    authority = load_session_authority(
        session_exception_path=exception_path,
        day_only_path=day_only_path,
        history_exception_path=history_path,
    )

    assert Path(exception_path).read_bytes() == replacement
    assert authority.session_exceptions == ()
    assert authority.sha256_by_asset["session_exception"] == hashlib.sha256(
        original
    ).hexdigest()


def test_notice_evening_maps_to_the_next_target_trade_date():
    calendar = (date(2024, 2, 8), date(2024, 2, 19), date(2024, 2, 20))
    row = _holiday_exception()

    validate_session_exception_calendar((row,), calendar)

    bad = replace(row, trade_date=date(2024, 2, 8))
    with pytest.raises(
        SessionAuthorityError, match="notice_target_trade_date"
    ) as exc:
        validate_session_exception_calendar((bad,), calendar)

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
    row = _holiday_exception(reason=reason)

    with pytest.raises(SessionAuthorityError, match="notice_evening"):
        validate_session_exception_calendar((row,), (date(2024, 2, 19),))


@pytest.mark.parametrize(
    "calendar",
    [
        (datetime(2024, 2, 19),),
        ("2024-02-19",),
    ],
)
def test_notice_calendar_requires_actual_date_values(calendar):
    with pytest.raises(SessionAuthorityError, match="notice_calendar_date"):
        validate_session_exception_calendar((_holiday_exception(),), calendar)


def test_matching_helpers_are_deterministic_and_inclusive():
    exceptions = (
        _session_exception(exchange="DCE"),
        _session_exception(exchange="SHFE"),
    )
    ranges = (
        _range_row(exchange="DCE", product="I"),
        _range_row(exchange="SHFE", product="AU"),
    )

    assert matching_session_exceptions(
        reversed(exceptions), "SHFE", date(2019, 12, 26)
    ) == (exceptions[1],)
    assert matching_ranges(ranges, "SHFE", "AU", date(2011, 1, 1)) == (
        ranges[1],
    )
    assert matching_ranges(ranges, "SHFE", "AU", date(2013, 7, 4)) == (
        ranges[1],
    )


def test_matching_helpers_reject_multiplicity_even_for_unloaded_rows():
    duplicates = (
        _session_exception(),
        _session_exception(source_url="https://other/"),
    )
    overlapping_ranges = (
        _range_row(),
        _range_row(
            effective_start=date(2013, 1, 1),
            effective_end=None,
            reason="overlap",
        ),
    )

    with pytest.raises(SessionAuthorityError, match="authority_match_cardinality"):
        matching_session_exceptions(duplicates, "DCE", date(2019, 12, 26))
    with pytest.raises(SessionAuthorityError, match="authority_match_cardinality"):
        matching_ranges(overlapping_ranges, "SHFE", "AU", date(2013, 2, 1))


@pytest.mark.parametrize("observed_night_end", ["23:00", "23:30", "01:00", "02:30"])
def test_observed_night_requires_both_none_authorities_to_be_absent(
    observed_night_end,
):
    assert (
        authorize_night_observation(
            _authority(),
            exchange="SHFE",
            product="AU",
            trade_date=date(2024, 2, 19),
            observed_night_start="21:00",
            observed_night_end=observed_night_end,
        )
        is None
    )

    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(session_exceptions=(_holiday_exception(),)),
            exchange="SHFE",
            product="AU",
            trade_date=date(2024, 2, 19),
            observed_night_start="21:00",
            observed_night_end=observed_night_end,
        )


def test_observed_none_requires_exactly_one_authority():
    day_only = _range_row(effective_end=None)
    halt = _holiday_exception()
    values = {
        "exchange": "SHFE",
        "product": "AU",
        "trade_date": date(2024, 2, 19),
        "observed_night_start": "none",
        "observed_night_end": "none",
    }

    assert (
        authorize_night_observation(
            _authority(day_only_regimes=(day_only,)), **values
        )
        is None
    )
    assert (
        authorize_night_observation(_authority(session_exceptions=(halt,)), **values)
        == halt
    )

    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(_authority(), **values)


def test_delayed_open_requires_an_exact_exception():
    exception = _session_exception()
    authority = _authority(session_exceptions=(exception,))
    values = {
        "exchange": "DCE",
        "product": "I",
        "trade_date": date(2019, 12, 26),
        "observed_night_start": "22:30",
        "observed_night_end": "23:00",
    }
    assert authorize_night_observation(authority, **values) == exception
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(_authority(), **values)
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(session_exceptions=(exception,)),
            **{**values, "observed_night_start": "21:00"},
        )


def test_day_only_and_session_exception_cannot_both_authorize_one_product_day():
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(
                session_exceptions=(
                    _session_exception(night_start="none", night_end="none"),
                ),
                day_only_regimes=(
                    _range_row(
                        exchange="DCE",
                        product="I",
                        effective_start=date(2019, 12, 26),
                        effective_end=date(2019, 12, 26),
                    ),
                ),
            ),
            exchange="DCE",
            product="I",
            trade_date=date(2019, 12, 26),
            observed_night_start="none",
            observed_night_end="none",
        )


def test_authorization_rejects_unknown_observations_and_keeps_error_context():
    trade_date = date(2024, 2, 19)

    with pytest.raises(SessionAuthorityError, match="night_observation_value") as exc:
        authorize_night_observation(
            _authority(),
            exchange="SHFE",
            product="AU",
            trade_date=trade_date,
            observed_night_start="21:00",
            observed_night_end="23:17",
        )

    assert exc.value.check == "night_observation_value"
    assert "session_rule_time" in exc.value.reason
    assert exc.value.row_identity == {
        "exchange": "SHFE",
        "product": "AU",
        "trade_date": "2024-02-19",
    }
    assert exc.value.context == {
        "observed_night_start": "21:00",
        "observed_night_end": "23:17",
    }


def test_repository_authority_assets_have_exact_header_only_contracts():
    repository = Path(__file__).resolve().parents[1]

    assert (
        repository / "config/carry_minute_session_exceptions.csv"
    ).read_text(encoding="utf-8") == SESSION_EXCEPTION_HEADER
    assert (
        repository / "config/carry_minute_day_only_regimes.csv"
    ).read_text(encoding="utf-8") == RANGE_HEADER
    assert (
        repository / "config/carry_liquidity_history_exceptions.csv"
    ).read_text(encoding="utf-8") == RANGE_HEADER
