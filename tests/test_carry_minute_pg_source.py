from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import psycopg2.extras
import pytest

from cta_carry.minute_bars import MinuteDataError
from cta_carry.minute_pg_source import (
    _BOUNDARY_COLUMNS,
    MinuteCandidate,
    MinuteSourceAudit,
    PublicMinuteSource,
    build_minute_batch_query,
    build_session_boundary_query,
    czce_minute_symbol,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_UNSET = object()


def test_czce_three_digit_month_maps_on_the_small_side():
    assert czce_minute_symbol("AP605.CZC", date(2025, 12, 1)) == "AP2605"
    assert czce_minute_symbol("TA1701.CZC", date(2016, 9, 1)) == "TA1701"


def test_czce_symbol_helper_rejects_a_non_czce_venue_directly():
    with pytest.raises(MinuteDataError) as exc_info:
        czce_minute_symbol("RB2405.SHF", date(2024, 1, 8))

    assert exc_info.value.check == "czce_contract_mapping"
    assert exc_info.value.contract == "RB2405.SHF"


def test_batch_query_keeps_minute_symbol_bare_and_has_literal_bounds():
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=SHANGHAI)

    query = build_minute_batch_query(lower=lower, upper=upper).as_string(None)

    assert "m.symbol = c.minute_symbol" in query
    assert "m.bar_time >= '2024-01-01T20:00:00+08:00'" in query
    assert "m.bar_time < '2024-02-01T15:01:00+08:00'" in query
    assert "regexp_replace(m.symbol" not in query
    assert "CASE WHEN m.exchange" not in query
    assert "ORDER BY m.bar_time, m.symbol" in query


def test_session_boundary_query_is_bounded_and_keeps_symbol_bare():
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=SHANGHAI)

    query = build_session_boundary_query(lower=lower, upper=upper).as_string(None)

    assert "FROM public.futures_minute m" in query
    assert "m.symbol = c.minute_symbol" in query
    assert "min(m.bar_time)" in query.lower()
    assert "max(m.bar_time)" in query.lower()
    assert "'2024-01-01T20:00:00+08:00'" in query
    assert "'2024-02-01T15:01:00+08:00'" in query


def test_session_boundary_query_seeks_once_per_candidate():
    """One indexed seek per candidate, not one decompression of the whole month."""
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=SHANGHAI)

    query = build_session_boundary_query(lower=lower, upper=upper).as_string(None)

    assert "LEFT JOIN LATERAL" in query
    # a per-candidate aggregate subquery needs no outer grouping
    assert "GROUP BY" not in query
    assert "LEFT JOIN public.futures_minute" not in query


def test_session_boundary_query_exposes_traded_night_columns():
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=SHANGHAI)

    query = build_session_boundary_query(lower=lower, upper=upper).as_string(None)

    assert "AS night_traded_first" in query
    assert "AS night_traded_second" in query
    assert "AS night_traded_first_flat" in query
    assert "m.volume > 0" in query
    assert "m.symbol = c.minute_symbol" in query
    assert "regexp_replace(m.symbol" not in query
    assert "'2024-01-01T20:00:00+08:00'" in query


def test_boundary_columns_append_traded_columns_after_observed_rows():
    assert _BOUNDARY_COLUMNS[15] == "observed_rows"
    assert _BOUNDARY_COLUMNS[16:] == (
        "night_traded_first",
        "night_traded_second",
        "night_traded_first_flat",
    )


@pytest.mark.parametrize(
    ("daily_contract", "trade_date", "minute_symbol", "exchange"),
    [
        ("rb2405.shf", date(2024, 1, 8), "RB2405", "SHFE"),
        ("i2405.dce", date(2024, 1, 8), "I2405", "DCE"),
        ("AP605.CZC", date(2025, 12, 1), "AP2605", "CZCE"),
        ("sc2405.ine", date(2024, 1, 8), "SC2405", "INE"),
        ("si2405.gfe", date(2024, 1, 8), "SI2405", "GFEX"),
    ],
)
def test_candidate_canonicalizes_every_supported_daily_suffix(
    daily_contract, trade_date, minute_symbol, exchange
):
    start = datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI)
    candidate = MinuteCandidate(
        trade_date=trade_date,
        product="  " + minute_symbol.rstrip("0123456789").lower() + " ",
        daily_contract=daily_contract,
        minute_symbol=minute_symbol.lower(),
        exchange=exchange.lower(),
        window_start=start,
        window_end=start + timedelta(hours=20),
    )

    assert candidate.product == minute_symbol.rstrip("0123456789")
    assert candidate.daily_contract == daily_contract.upper()
    assert candidate.minute_symbol == minute_symbol
    assert candidate.exchange == exchange


@pytest.mark.parametrize(
    ("contract", "trade_date"),
    [
        ("RB.SHF", date(2024, 1, 8)),
        ("RB2413.SHF", date(2024, 1, 8)),
        ("RB405.SHF", date(2024, 1, 8)),
        ("RB2405.CFF", date(2024, 1, 8)),
    ],
)
def test_malformed_or_unsupported_daily_contract_is_structured(contract, trade_date):
    start = datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI)
    with pytest.raises(MinuteDataError) as exc_info:
        MinuteCandidate(
            trade_date=trade_date,
            product="RB",
            daily_contract=contract,
            minute_symbol="RB2405",
            exchange="SHFE",
            window_start=start,
            window_end=start + timedelta(hours=20),
        )

    error = exc_info.value
    assert error.trade_date == trade_date
    assert error.contract == contract
    assert error.check == "minute_contract_mapping"


def test_ambiguous_czce_delivery_year_has_exact_structured_error():
    with pytest.raises(MinuteDataError) as exc_info:
        czce_minute_symbol("AP105.CZC", date(2025, 12, 1))

    error = exc_info.value
    assert error.trade_date == date(2025, 12, 1)
    assert error.contract == "AP105.CZC"
    assert error.check == "czce_contract_mapping"
    assert error.reason == ("delivery year is more than three years after trade date")


@pytest.mark.parametrize("trade_date", [True, datetime(2024, 1, 8, 0, 0)])
def test_candidate_rejects_bool_and_datetime_trade_dates(trade_date):
    start = datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI)
    with pytest.raises(MinuteDataError, match="trade_date"):
        MinuteCandidate(
            trade_date=trade_date,
            product="RB",
            daily_contract="RB2405.SHF",
            minute_symbol="RB2405",
            exchange="SHFE",
            window_start=start,
            window_end=start + timedelta(hours=20),
        )


@pytest.mark.parametrize(
    "field",
    [
        "trade_date",
        "product",
        "daily_contract",
        "minute_symbol",
        "exchange",
        "window_start",
        "window_end",
    ],
)
def test_candidate_rejects_sql_adaptable_builtin_subclasses(field):
    class AdaptableText(str):
        def __conform__(self, protocol):
            return "'); SELECT pg_sleep(9); --"

    class AdaptableDate(date):
        def __conform__(self, protocol):
            return "'); SELECT pg_sleep(9); --"

    class AdaptableDatetime(datetime):
        def __conform__(self, protocol):
            return "'); SELECT pg_sleep(9); --"

        def isoformat(self, *args, **kwargs):
            return "2024-01-01T00:00:00+08:00'; SELECT pg_sleep(9); --"

    values = {
        "trade_date": date(2024, 1, 8),
        "product": "RB",
        "daily_contract": "RB2405.SHF",
        "minute_symbol": "RB2405",
        "exchange": "SHFE",
        "window_start": datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI),
        "window_end": datetime(2024, 1, 8, 15, 1, tzinfo=SHANGHAI),
    }
    if field == "trade_date":
        values[field] = AdaptableDate(2024, 1, 8)
    elif field in {"window_start", "window_end"}:
        source = values[field]
        values[field] = AdaptableDatetime(
            source.year,
            source.month,
            source.day,
            source.hour,
            source.minute,
            tzinfo=SHANGHAI,
        )
    else:
        values[field] = AdaptableText(values[field])

    with pytest.raises(MinuteDataError):
        MinuteCandidate(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product", "TA"),
        ("minute_symbol", "RB2410"),
        ("exchange", "DCE"),
    ],
)
def test_candidate_rejects_inconsistent_mapping(field, value):
    values = {
        "trade_date": date(2024, 1, 8),
        "product": "RB",
        "daily_contract": "RB2405.SHF",
        "minute_symbol": "RB2405",
        "exchange": "SHFE",
        "window_start": datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI),
        "window_end": datetime(2024, 1, 8, 15, 1, tzinfo=SHANGHAI),
    }
    values[field] = value

    with pytest.raises(MinuteDataError) as exc_info:
        MinuteCandidate(**values)

    assert exc_info.value.check == "minute_candidate"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2024, 1, 1), datetime(2024, 1, 2)),
        (
            datetime(2024, 1, 2, tzinfo=SHANGHAI),
            datetime(2024, 1, 1, tzinfo=SHANGHAI),
        ),
    ],
)
def test_candidate_requires_an_aware_ordered_window(start, end):
    with pytest.raises(MinuteDataError) as exc_info:
        MinuteCandidate(
            trade_date=date(2024, 1, 8),
            product="RB",
            daily_contract="RB2405.SHF",
            minute_symbol="RB2405",
            exchange="SHFE",
            window_start=start,
            window_end=end,
        )

    assert exc_info.value.check == "minute_candidate"


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (datetime(2024, 1, 1), datetime(2024, 2, 1)),
        (
            datetime(2024, 2, 1, tzinfo=SHANGHAI),
            datetime(2024, 1, 1, tzinfo=SHANGHAI),
        ),
    ],
)
def test_batch_query_requires_aware_ordered_bounds(lower, upper):
    with pytest.raises(MinuteDataError) as exc_info:
        build_minute_batch_query(lower=lower, upper=upper)

    assert exc_info.value.check == "minute_query_bounds"


def test_batch_query_rejects_datetime_subclasses_that_can_override_isoformat():
    class InjectedDatetime(datetime):
        def isoformat(self, *args, **kwargs):
            return "2024-01-01T00:00:00+08:00'; SELECT pg_sleep(9); --"

    lower = InjectedDatetime(2024, 1, 1, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, tzinfo=SHANGHAI)

    with pytest.raises(MinuteDataError) as exc_info:
        build_minute_batch_query(lower=lower, upper=upper)

    assert exc_info.value.check == "minute_query_bounds"


def test_batch_query_selects_the_exact_stream_columns():
    lower = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, tzinfo=SHANGHAI)
    normalized = " ".join(
        build_minute_batch_query(lower=lower, upper=upper).as_string(None).split()
    )

    assert normalized.startswith(
        "SELECT c.trade_date, c.product, c.daily_contract, "
        "m.bar_time, m.symbol, m.exchange, m.open, m.high, m.low, m.close, "
        "m.volume, m.amount, m.open_interest "
    )
    assert "m.bar_time >= c.window_start" in normalized
    assert "m.bar_time < c.window_end" in normalized
    assert "'2024-01-01T00:00:00+08:00'::TIMESTAMPTZ" in normalized


STREAM_COLUMNS = [
    "trade_date",
    "product",
    "daily_contract",
    "bar_time",
    "symbol",
    "exchange",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "open_interest",
]
BOUNDARY_COLUMNS = [
    "trade_date",
    "product",
    "daily_contract",
    "minute_symbol",
    "exchange",
    "window_start",
    "window_end",
    "night_first",
    "night_last",
    "day_1_first",
    "day_1_last",
    "day_2_first",
    "day_2_last",
    "day_3_first",
    "day_3_last",
    "observed_rows",
    "night_traded_first",
    "night_traded_second",
    "night_traded_first_flat",
]
SETTINGS = [
    "SET LOCAL max_parallel_workers_per_gather = 0;",
    "SET LOCAL work_mem = '32MB';",
    "SET LOCAL statement_timeout = '300s';",
    "SET LOCAL enable_hashjoin = off;",
    "SET LOCAL enable_mergejoin = off;",
]


def _candidate(contract="RB2405.SHF", trade_date=date(2024, 1, 8)):
    start = datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI)
    product = contract.rstrip(".SHFDCECZCINEGFE0123456789").upper()
    suffix = contract.rsplit(".", 1)[1].upper()
    exchange = {
        "SHF": "SHFE",
        "DCE": "DCE",
        "CZC": "CZCE",
        "INE": "INE",
        "GFE": "GFEX",
    }[suffix]
    minute_symbol = (
        czce_minute_symbol(contract, trade_date)
        if suffix == "CZC"
        else contract.rsplit(".", 1)[0].upper()
    )
    return MinuteCandidate(
        trade_date=trade_date,
        product=product,
        daily_contract=contract,
        minute_symbol=minute_symbol,
        exchange=exchange,
        window_start=start,
        window_end=start + timedelta(hours=19),
    )


def _legacy_candidate_frame(candidate):
    return pd.DataFrame(
        [
            {
                column: getattr(candidate, column)
                for column in (
                    "trade_date",
                    "product",
                    "daily_contract",
                    "minute_symbol",
                    "exchange",
                    "window_start",
                    "window_end",
                )
            }
        ]
    )


def _safe_plan(*, rows=10, chunks=1, node_type="Nested Loop"):
    children = [
        {
            "Node Type": "Index Scan",
            "Plan Rows": rows,
            "Schema": "_timescaledb_internal",
            "Relation Name": f"_hyper_1_{number}_chunk",
        }
        for number in range(chunks)
    ]
    return [{"Plan": {"Node Type": node_type, "Plan Rows": rows, "Plans": children}}]


def _minute_row(at, *, symbol="RB2405"):
    return (
        date(2024, 1, 8),
        "RB",
        "RB2405.SHF",
        at,
        symbol,
        "SHFE",
        100.0,
        101.0,
        99.0,
        100.5,
        10.0,
        10_000.0,
        20.0,
    )


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.mode = None
        self.stream_index = 0

    @staticmethod
    def _text(statement):
        as_string = getattr(statement, "as_string", None)
        return as_string(None) if as_string is not None else str(statement)

    def execute(self, statement, params=None):
        text = self._text(statement)
        self.connection.events.append(("execute", text, params))
        if self.connection.raise_on and self.connection.raise_on in text:
            raise RuntimeError("cursor exploded")
        normalized = " ".join(text.split())
        if normalized.startswith("EXPLAIN (FORMAT JSON)"):
            self.mode = "plan"
        elif normalized.startswith("SELECT c.trade_date") and "LATERAL" in normalized:
            self.mode = "boundary"
        elif normalized.startswith("SELECT c.trade_date"):
            self.mode = "stream"
        elif "FROM public.futures_contract_info" in normalized:
            self.mode = "metadata"
        elif "FROM public.futures_daily" in normalized:
            self.mode = "daily"
        elif "SELECT min(bar_time), max(bar_time)" in normalized:
            self.mode = "table_bounds"

    def fetchone(self):
        if self.mode == "table_bounds":
            return self.connection.table_bounds
        assert self.mode == "plan"
        return (self.connection.plan,)

    def fetchall(self):
        assert self.mode in {"metadata", "boundary", "daily"}
        if self.mode == "boundary":
            return list(self.connection.boundary_rows)
        if self.mode == "daily":
            return list(self.connection.daily_rows)
        return list(self.connection.metadata_rows)

    def fetchmany(self, size):
        assert self.mode == "stream"
        assert size == 100_000
        if self.connection.raise_on == "fetchmany":
            raise RuntimeError("fetch exploded")
        chunks = self.connection.stream_chunks
        if self.stream_index >= len(chunks):
            return []
        chunk = chunks[self.stream_index]
        self.stream_index += 1
        return chunk

    def close(self):
        self.connection.events.append(("cursor_close",))


class FakeConnection:
    def __init__(
        self,
        *,
        plan=None,
        stream_chunks=(),
        boundary_rows=(),
        metadata_rows=(),
        daily_rows=(),
        table_bounds=(
            datetime(2005, 1, 4, 9, 0, tzinfo=SHANGHAI),
            datetime(2026, 8, 5, 23, 59, tzinfo=SHANGHAI),
        ),
        raise_on=None,
        rollback_error=False,
    ):
        self.plan = _safe_plan() if plan is None else plan
        self.stream_chunks = list(stream_chunks)
        self.boundary_rows = list(boundary_rows)
        self.metadata_rows = list(metadata_rows)
        self.daily_rows = list(daily_rows)
        self.table_bounds = table_bounds
        self.raise_on = raise_on
        self.rollback_error = rollback_error
        self.events = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self, name=None):
        self.events.append(("cursor_open", name))
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error:
            raise RuntimeError("rollback exploded")

    def close(self):
        self.closes += 1


class FakeConnectionScope:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()
        return False


def _source(connection, captured_pg=None):
    def factory(pg):
        if captured_pg is not None:
            captured_pg.update(pg)
        return FakeConnectionScope(connection)

    return PublicMinuteSource(pg={"schema": "ignored"}, connection_factory=factory)


@pytest.mark.parametrize("legacy_dataframe", [False, True])
def test_session_boundaries_reject_unspecified_provenance_before_db_access(
    legacy_dataframe,
):
    candidate = _candidate()
    supplied = _legacy_candidate_frame(candidate) if legacy_dataframe else [candidate]
    connection_calls = []

    def forbidden_factory(pg):
        connection_calls.append(pg)
        raise AssertionError("database access must follow candidate provenance checks")

    source = PublicMinuteSource(pg={}, connection_factory=forbidden_factory)
    with pytest.raises(MinuteDataError) as exc_info:
        source.iter_session_boundaries(
            supplied,
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    error = exc_info.value
    assert error.check == "session_candidate_metadata"
    assert error.trade_date == candidate.trade_date
    assert error.product == candidate.product
    assert error.contract == candidate.daily_contract
    assert error.context == {
        "candidate_role": "unspecified",
        "causal_in_pool_date": None,
        "selection_source": "unspecified",
    }
    assert connection_calls == []


@pytest.mark.parametrize(
    ("causal_in_pool_date", "selection_source"),
    [
        (None, "target_day_main"),
        (date(2024, 1, 5), "unspecified"),
    ],
)
def test_session_representative_requires_complete_provenance_before_db_access(
    causal_in_pool_date,
    selection_source,
):
    candidate = replace(
        _candidate(),
        candidate_role="session_representative",
        causal_in_pool_date=causal_in_pool_date,
        selection_source=selection_source,
    )
    connection_calls = []

    def forbidden_factory(pg):
        connection_calls.append(pg)
        raise AssertionError("database access must follow candidate provenance checks")

    source = PublicMinuteSource(pg={}, connection_factory=forbidden_factory)
    with pytest.raises(MinuteDataError) as exc_info:
        source.iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    assert exc_info.value.check == "session_candidate_metadata"
    assert exc_info.value.context == {
        "candidate_role": "session_representative",
        "causal_in_pool_date": causal_in_pool_date,
        "selection_source": selection_source,
    }
    assert connection_calls == []


def test_iter_month_preserves_legacy_dataframe_compatibility(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _candidate()
    captured = []
    monkeypatch.setattr(
        minute_pg_source,
        "_insert_candidates",
        lambda cursor, candidates: captured.extend(candidates),
    )

    assert (
        list(
            _source(FakeConnection()).iter_month(
                _legacy_candidate_frame(candidate),
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )
    assert len(captured) == 1
    assert captured[0].candidate_role == "unspecified"
    assert captured[0].causal_in_pool_date is None
    assert captured[0].selection_source == "unspecified"


def test_iter_month_sets_up_transaction_and_uses_the_same_bounded_select(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    connection = FakeConnection()
    inserted = []

    def fake_insert(cursor, candidates):
        connection.events.append(("insert",))
        inserted.extend(candidates)

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", fake_insert)
    candidates = [_candidate("TA2405.CZC"), _candidate()]

    assert (
        list(
            _source(connection).iter_month(
                candidates,
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )

    executed = [event[1] for event in connection.events if event[0] == "execute"]
    assert [text.strip() for text in executed[:5]] == SETTINGS
    explain_index = next(
        index
        for index, text in enumerate(executed)
        if text.lstrip().startswith("EXPLAIN")
    )
    create_event_index = next(
        index
        for index, event in enumerate(connection.events)
        if event[0] == "execute" and "CREATE TEMP TABLE" in event[1]
    )
    insert_event_index = connection.events.index(("insert",))
    explain_event_index = next(
        index
        for index, event in enumerate(connection.events)
        if event[0] == "execute" and event[1].lstrip().startswith("EXPLAIN")
    )
    assert create_event_index < insert_event_index < explain_event_index
    assert [event[1] for event in connection.events if event[0] == "cursor_open"] == [
        None,
        "_carry_minute_stream",
    ]
    explain = executed[explain_index]
    select = next(text for text in executed if text.lstrip().startswith("SELECT"))
    assert explain.removeprefix("EXPLAIN (FORMAT JSON) ") == select
    assert [(item.minute_symbol, item.trade_date) for item in inserted] == [
        ("RB2405", date(2024, 1, 8)),
        ("TA2405", date(2024, 1, 8)),
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_explain_month_returns_an_immutable_serializable_summary_without_select(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    lower = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, tzinfo=SHANGHAI)
    connection = FakeConnection(plan=_safe_plan(rows=123, chunks=2))
    inserted = []
    monkeypatch.setattr(
        minute_pg_source,
        "_insert_candidates",
        lambda cursor, candidates: inserted.extend(candidates),
    )
    source = _source(connection)

    summary = source.explain_month(
        [_candidate("TA2405.CZC"), _candidate()],
        lower=lower,
        upper=upper,
    )

    assert summary.query_kind == "explain_only"
    assert summary.lower_bound == lower.isoformat()
    assert summary.upper_bound == upper.isoformat()
    assert summary.candidate_contract_days == 2
    assert summary.referenced_chunks == (
        "_hyper_1_0_chunk",
        "_hyper_1_1_chunk",
    )
    assert summary.maximum_plan_rows == 123
    assert summary.node_types == ("Index Scan", "Nested Loop")
    assert json.loads(json.dumps(asdict(summary)))["candidate_contract_days"] == 2
    with pytest.raises(FrozenInstanceError):
        summary.maximum_plan_rows = 999
    assert source.plan_audit == (summary,)
    assert source.audit.minute_query_months == 0
    assert source.audit.minute_candidate_contract_days == 0
    assert [(item.minute_symbol, item.trade_date) for item in inserted] == [
        ("RB2405", date(2024, 1, 8)),
        ("TA2405", date(2024, 1, 8)),
    ]
    executed = [event[1] for event in connection.events if event[0] == "execute"]
    assert [text.strip() for text in executed[:5]] == SETTINGS
    assert any(text.lstrip().startswith("EXPLAIN") for text in executed)
    assert not any(text.lstrip().startswith("SELECT") for text in executed)
    assert [event for event in connection.events if event[0] == "cursor_open"] == [
        ("cursor_open", None)
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_explain_month_rejects_an_unsafe_plan_without_select_and_cleans_up(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    connection = FakeConnection(plan=_safe_plan(chunks=4))
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    source = _source(connection)

    with pytest.raises(MinuteDataError) as exc_info:
        source.explain_month(
            [_candidate()],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    assert exc_info.value.check == "minute_query_plan"
    assert source.plan_audit == ()
    executed = [event[1] for event in connection.events if event[0] == "execute"]
    assert not any(text.lstrip().startswith("SELECT") for text in executed)
    assert [event for event in connection.events if event[0] == "cursor_open"] == [
        ("cursor_open", None)
    ]
    assert connection.commits == 0
    assert connection.rollbacks >= 1
    assert connection.closes == 1


def test_iter_month_exposes_stable_plan_audit_snapshots(monkeypatch):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    source = _source(FakeConnection(plan=_safe_plan(rows=17, chunks=1)))
    lower = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, tzinfo=SHANGHAI)

    assert list(source.iter_month([_candidate()], lower=lower, upper=upper)) == []
    before = source.plan_audit

    assert len(before) == 1
    assert before[0].query_kind == "iter_month"
    assert before[0].candidate_contract_days == 1
    assert before[0].maximum_plan_rows == 17
    assert list(source.iter_month([_candidate()], lower=lower, upper=upper)) == []
    assert len(source.plan_audit) == 2
    assert before == (source.plan_audit[0],)


def test_source_audit_tracks_exact_table_bounds_and_integer_query_counters(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    lower = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, tzinfo=SHANGHAI)
    table_min = datetime(2005, 1, 4, 9, 0, tzinfo=SHANGHAI)
    table_max = datetime(2026, 8, 5, 23, 59, tzinfo=SHANGHAI)
    first = _candidate()
    second = _candidate("TA2405.CZC")
    rows = [
        _minute_row(datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)),
        _minute_row(datetime(2024, 1, 8, 9, 1, tzinfo=SHANGHAI)),
    ]
    source = _source(
        FakeConnection(
            stream_chunks=[rows],
            table_bounds=(table_min, table_max),
        )
    )

    assert source.load_table_bounds() == (table_min, table_max)
    streamed = list(
        source.iter_month(
            [first, second],
            lower=lower,
            upper=upper,
        )
    )

    assert sum(len(frame) for frame in streamed) == 2
    assert source.audit == MinuteSourceAudit(
        minute_table_min=table_min,
        minute_table_max=table_max,
        minute_query_months=1,
        minute_rows=2,
        minute_candidate_contract_days=2,
    )


def test_table_bounds_are_cached_and_connection_resources_close_once():
    connection = FakeConnection()
    source = _source(connection)

    first = source.load_table_bounds()
    second = source.load_table_bounds()

    assert second == first
    bound_queries = [
        event
        for event in connection.events
        if event[0] == "execute" and "SELECT min(bar_time), max(bar_time)" in event[1]
    ]
    assert len(bound_queries) == 1
    assert connection.events.count(("cursor_close",)) == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


@pytest.mark.parametrize(
    "bounds",
    [
        (),
        (datetime(2024, 1, 1, tzinfo=SHANGHAI),),
        (
            datetime(2024, 1, 1, tzinfo=SHANGHAI),
            datetime(2024, 1, 2, tzinfo=SHANGHAI),
            datetime(2024, 1, 3, tzinfo=SHANGHAI),
        ),
    ],
)
def test_table_bounds_reject_malformed_row_shape_and_close_resources(bounds):
    connection = FakeConnection(table_bounds=bounds)
    source = _source(connection)

    with pytest.raises(MinuteDataError) as exc_info:
        source.load_table_bounds()

    assert exc_info.value.check == "minute_table_bounds"
    assert connection.events.count(("cursor_close",)) == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_table_bounds_query_failure_rolls_back_and_closes_resources():
    connection = FakeConnection(raise_on="SELECT min(bar_time), max(bar_time)")
    source = _source(connection)

    with pytest.raises(RuntimeError, match="cursor exploded"):
        source.load_table_bounds()

    assert connection.events.count(("cursor_close",)) == 1
    assert connection.commits == 0
    assert connection.rollbacks >= 1
    assert connection.closes == 1


def test_source_audit_snapshots_are_frozen_and_do_not_change_retroactively(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    connection = FakeConnection(
        stream_chunks=[[_minute_row(datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI))]]
    )
    source = _source(connection)
    before = source.audit

    with pytest.raises(FrozenInstanceError):
        before.minute_rows = 999

    source.load_table_bounds()
    list(
        source.iter_month(
            [_candidate()],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )
    )

    assert before == MinuteSourceAudit()
    assert source.audit.minute_rows == 1
    assert source.audit.minute_query_months == 1
    assert source.audit.minute_table_min is not None


def test_source_audit_counts_repeated_queries_but_deduplicates_candidates(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    row = _minute_row(datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI))
    source = _source(FakeConnection(stream_chunks=[[row]]))
    bounds = {
        "lower": datetime(2024, 1, 1, tzinfo=SHANGHAI),
        "upper": datetime(2024, 2, 1, tzinfo=SHANGHAI),
    }

    list(source.iter_month([_candidate()], **bounds))
    list(source.iter_month([_candidate()], **bounds))

    assert source.audit.minute_query_months == 2
    assert source.audit.minute_rows == 2
    assert source.audit.minute_candidate_contract_days == 1


def test_source_audit_records_started_failed_query_without_rows(monkeypatch):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    connection = FakeConnection(raise_on="EXPLAIN")
    source = _source(connection)

    with pytest.raises(RuntimeError, match="cursor exploded"):
        list(
            source.iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert source.audit.minute_query_months == 1
    assert source.audit.minute_rows == 0
    assert source.audit.minute_candidate_contract_days == 1
    assert connection.rollbacks >= 1
    assert connection.closes == 1


def test_source_audit_partial_consumption_counts_only_yielded_rows(monkeypatch):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    first_rows = [_minute_row(datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI))]
    second_rows = [_minute_row(datetime(2024, 1, 8, 9, 1, tzinfo=SHANGHAI))]
    connection = FakeConnection(stream_chunks=[first_rows, second_rows])
    source = _source(connection)
    stream = source.iter_month(
        [_candidate()],
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
    )

    assert len(next(stream)) == 1
    stream.close()

    assert source.audit.minute_query_months == 1
    assert source.audit.minute_rows == 1
    assert source.audit.minute_candidate_contract_days == 1
    assert connection.rollbacks >= 1
    assert connection.closes == 1


@pytest.mark.parametrize(
    "bounds",
    [
        (None, None),
        (datetime(2024, 1, 1), datetime(2024, 1, 2)),
        (
            datetime(2024, 1, 2, tzinfo=SHANGHAI),
            datetime(2024, 1, 1, tzinfo=SHANGHAI),
        ),
        (datetime(2024, 1, 1, tzinfo=SHANGHAI), None),
    ],
)
def test_table_bounds_reject_empty_naive_regressed_or_partial_metadata(bounds):
    source = _source(FakeConnection(table_bounds=bounds))

    with pytest.raises(MinuteDataError) as exc_info:
        source.load_table_bounds()

    assert exc_info.value.check == "minute_table_bounds"


def test_iter_month_accepts_an_exact_candidate_dataframe(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _candidate()
    frame = pd.DataFrame([candidate.__dict__])
    captured = []
    monkeypatch.setattr(
        minute_pg_source,
        "_insert_candidates",
        lambda cursor, candidates: captured.extend(candidates),
    )

    assert (
        list(
            _source(FakeConnection()).iter_month(
                frame,
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )
    assert captured == [candidate]


def test_dataframe_ingestion_normalizes_trusted_pandas_and_numpy_scalars(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    start = pd.Timestamp("2024-01-07 21:00:00", tz=SHANGHAI)
    frame = pd.DataFrame(
        {
            "trade_date": pd.Series(
                [pd.Timestamp("2024-01-08")],
                dtype=object,
            ),
            "product": pd.Series([np.str_("rb")], dtype=object),
            "daily_contract": pd.Series([np.str_("rb2405.shf")], dtype=object),
            "minute_symbol": pd.Series([np.str_("rb2405")], dtype=object),
            "exchange": pd.Series([np.str_("shfe")], dtype=object),
            "window_start": pd.Series([start], dtype=object),
            "window_end": pd.Series(
                [start + pd.Timedelta(hours=19)],
                dtype=object,
            ),
        }
    )
    captured = []
    monkeypatch.setattr(
        minute_pg_source,
        "_insert_candidates",
        lambda cursor, candidates: captured.extend(candidates),
    )

    assert (
        list(
            _source(FakeConnection()).iter_month(
                frame,
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )
    assert len(captured) == 1
    candidate = captured[0]
    assert type(candidate.trade_date) is date
    assert all(
        type(getattr(candidate, column)) is str
        for column in ("product", "daily_contract", "minute_symbol", "exchange")
    )
    assert type(candidate.window_start) is datetime
    assert type(candidate.window_end) is datetime


def test_dataframe_out_of_python_range_numpy_date_is_structured(monkeypatch):
    from cta_carry import minute_pg_source

    frame = pd.DataFrame([_candidate().__dict__], dtype=object)
    frame.at[0, "trade_date"] = np.datetime64("10000-01-01")
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        list(
            _source(FakeConnection()).iter_month(
                frame,
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert exc_info.value.check == "minute_candidates"


def test_real_candidate_insert_uses_safe_normalized_rows_and_required_ddl(
    monkeypatch,
):
    captured = {}

    def fake_execute_values(cursor, template, rows, *, page_size):
        captured.update(
            cursor=cursor,
            template=template,
            rows=rows,
            page_size=page_size,
        )

    monkeypatch.setattr(psycopg2.extras, "execute_values", fake_execute_values)
    connection = FakeConnection()
    start = pd.Timestamp("2024-01-07 21:00:00", tz=SHANGHAI)
    candidate_frame = pd.DataFrame(
        {
            "trade_date": pd.Series(
                [pd.Timestamp("2024-01-08"), pd.Timestamp("2024-01-08")],
                dtype=object,
            ),
            "product": pd.Series([np.str_("ta"), np.str_("rb")], dtype=object),
            "daily_contract": pd.Series(
                [np.str_("ta2405.czc"), np.str_("rb2405.shf")],
                dtype=object,
            ),
            "minute_symbol": pd.Series(
                [np.str_("ta2405"), np.str_("rb2405")],
                dtype=object,
            ),
            "exchange": pd.Series(
                [np.str_("czce"), np.str_("shfe")],
                dtype=object,
            ),
            "window_start": pd.Series([start, start], dtype=object),
            "window_end": pd.Series(
                [
                    start + pd.Timedelta(hours=19),
                    start + pd.Timedelta(hours=19),
                ],
                dtype=object,
            ),
        }
    )

    assert (
        list(
            _source(connection).iter_month(
                candidate_frame,
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )

    ddl = next(
        event[1]
        for event in connection.events
        if event[0] == "execute" and "CREATE TEMP TABLE" in event[1]
    )
    normalized_ddl = " ".join(ddl.split())
    assert "PRIMARY KEY (minute_symbol, trade_date)" in normalized_ddl
    assert "ON COMMIT DROP" in normalized_ddl
    assert " ".join(captured["template"].split()) == (
        "INSERT INTO _carry_minute_candidates "
        "( trade_date, product, daily_contract, minute_symbol, exchange, "
        "window_start, window_end ) VALUES %s"
    )
    assert captured["page_size"] == 2
    rows = captured["rows"]
    assert [(row[3], row[0]) for row in rows] == [
        ("RB2405", date(2024, 1, 8)),
        ("TA2405", date(2024, 1, 8)),
    ]
    expected_start = datetime(2024, 1, 7, 21, 0, tzinfo=SHANGHAI)
    expected_end = datetime(2024, 1, 8, 16, 0, tzinfo=SHANGHAI)
    assert rows == [
        (
            date(2024, 1, 8),
            "RB",
            "RB2405.SHF",
            "RB2405",
            "SHFE",
            expected_start,
            expected_end,
        ),
        (
            date(2024, 1, 8),
            "TA",
            "TA2405.CZC",
            "TA2405",
            "CZCE",
            expected_start,
            expected_end,
        ),
    ]
    assert all(type(row[0]) is date for row in rows)
    assert all(type(value) is str for row in rows for value in row[1:5])
    assert all(type(value) is datetime for row in rows for value in row[5:7])


@pytest.mark.parametrize(
    "plan",
    [
        _safe_plan(chunks=4),
        _safe_plan(rows=10_000_000),
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Plan Rows": 1,
                    "Schema": "public",
                    "Relation Name": "futures_minute",
                }
            }
        ],
        [
            {
                "Plan": {
                    "Node Type": "Parallel Seq Scan",
                    "Plan Rows": 1,
                    "Schema": "public",
                    "Relation Name": "futures_minute",
                }
            }
        ],
        [{"Plan": {"Plan Rows": 1}}],
    ],
    ids=[
        "too-many-chunks",
        "ten-million-rows",
        "full-seq-scan",
        "full-parallel-seq-scan",
        "malformed",
    ],
)
def test_iter_month_rejects_unsafe_or_malformed_plans(monkeypatch, plan):
    from cta_carry import minute_pg_source

    connection = FakeConnection(plan=plan)
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        list(
            _source(connection).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert exc_info.value.check == "minute_query_plan"
    assert connection.rollbacks >= 1
    assert connection.commits == 0
    assert connection.closes == 1


@pytest.mark.parametrize(
    "plan_rows",
    [10**1000, float("nan"), float("inf"), "10", True],
    ids=["huge-int", "nan", "infinity", "string", "bool"],
)
def test_plan_rows_extremes_raise_a_structured_plan_error(
    monkeypatch,
    plan_rows,
):
    from cta_carry import minute_pg_source

    plan = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Plan Rows": plan_rows,
            }
        }
    ]
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        list(
            _source(FakeConnection(plan=plan)).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert exc_info.value.check == "minute_query_plan"


@pytest.mark.parametrize(
    "payload",
    [
        '{"Plan": {"Node Type": "Index Scan", "Plan Rows": 2}}',
        {"Plan": {"Node Type": "Index Scan", "Plan Rows": 2}},
        [{"Plan": {"Node Type": "Index Scan", "Plan Rows": 2}}],
    ],
    ids=["json-string", "dict", "list"],
)
def test_iter_month_accepts_driver_plan_payload_forms(monkeypatch, payload):
    from cta_carry import minute_pg_source

    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    connection = FakeConnection(plan=payload)

    assert (
        list(
            _source(connection).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )


def test_plan_counts_logical_chunks_not_compressed_backing_relations(monkeypatch):
    from cta_carry import minute_pg_source

    plans = []
    for chunk_number in (10, 11):
        plans.append(
            {
                "Node Type": "Custom Scan",
                "Custom Plan Provider": "DecompressChunk",
                "Plan Rows": 20,
                "Relation Name": f"_hyper_1_{chunk_number}_chunk",
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Plan Rows": 20,
                        "Schema": "_timescaledb_internal",
                        "Relation Name": (f"compress_hyper_2_{chunk_number}_chunk"),
                    }
                ],
            }
        )
    plan = [{"Plan": {"Node Type": "Append", "Plan Rows": 40, "Plans": plans}}]
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    assert (
        list(
            _source(FakeConnection(plan=plan)).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )
        == []
    )


def test_iter_month_streams_exact_columns_and_global_order(monkeypatch):
    from cta_carry import minute_pg_source

    start = datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)
    connection = FakeConnection(
        plan=_safe_plan(rows=9_999_999, chunks=3),
        stream_chunks=[
            [_minute_row(start), _minute_row(start + timedelta(minutes=1))],
            [_minute_row(start + timedelta(minutes=2))],
        ],
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    frames = list(
        _source(connection).iter_month(
            [_candidate()],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )
    )

    assert [list(frame.columns) for frame in frames] == [
        STREAM_COLUMNS,
        STREAM_COLUMNS,
    ]
    assert pd.concat(frames, ignore_index=True)["bar_time"].tolist() == [
        start,
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
    ]


def test_iter_month_rejects_order_regression_between_fetches(monkeypatch):
    from cta_carry import minute_pg_source

    start = datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)
    connection = FakeConnection(
        stream_chunks=[
            [_minute_row(start + timedelta(minutes=1))],
            [_minute_row(start)],
        ]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        list(
            _source(connection).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert exc_info.value.check == "minute_row_order"
    assert connection.rollbacks >= 1


def test_iter_month_rolls_back_cursor_failure(monkeypatch):
    from cta_carry import minute_pg_source

    connection = FakeConnection(raise_on="fetchmany")
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(RuntimeError, match="fetch exploded"):
        list(
            _source(connection).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert connection.rollbacks >= 1
    assert connection.commits == 0
    assert connection.closes == 1


def test_iter_month_preserves_cursor_failure_when_rollback_also_fails(monkeypatch):
    from cta_carry import minute_pg_source

    connection = FakeConnection(raise_on="fetchmany", rollback_error=True)
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(RuntimeError, match="fetch exploded") as exc_info:
        list(
            _source(connection).iter_month(
                [_candidate()],
                lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            )
        )

    assert "rollback exploded" in " ".join(exc_info.value.__notes__)
    assert connection.rollbacks >= 1
    assert connection.commits == 0
    assert connection.closes == 1


def test_iter_month_early_close_rolls_back_and_closes(monkeypatch):
    from cta_carry import minute_pg_source

    start = datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)
    connection = FakeConnection(stream_chunks=[[_minute_row(start)]])
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)
    iterator = _source(connection).iter_month(
        [_candidate()],
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
    )

    assert next(iterator).shape == (1, len(STREAM_COLUMNS))
    iterator.close()

    assert connection.rollbacks >= 1
    assert connection.commits == 0
    assert connection.closes == 1


def test_constructor_forces_public_schema(monkeypatch):
    from cta_carry import minute_pg_source

    captured_pg = {}
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    list(
        _source(FakeConnection(), captured_pg).iter_month(
            [_candidate()],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )
    )

    assert captured_pg["schema"] == "public"


def test_metadata_multiplier_uses_latest_record_on_or_before_trade_date():
    connection = FakeConnection(
        metadata_rows=[
            (date(2024, 1, 2), 20),
            (date(2024, 1, 5), 10),
            (date(2024, 1, 4), 15),
        ]
    )

    multiplier = _source(connection).resolve_metadata_multiplier(
        daily_contract="RB2405.SHF",
        trade_date=date(2024, 1, 8),
    )

    assert multiplier == 10
    metadata_event = next(
        event
        for event in connection.events
        if event[0] == "execute" and "futures_contract_info" in event[1]
    )
    normalized = " ".join(metadata_event[1].split())
    assert '"合约代码" = %s' in normalized
    assert '"交易日期" <= %s' in normalized
    assert 'ORDER BY "交易日期" DESC, "合约乘数" ASC NULLS LAST' in normalized
    assert metadata_event[2] == ("RB2405", date(2024, 1, 8))


def _inferable_frame(contract="RB2405", multiplier=10):
    start = datetime(2024, 1, 8, 9, 0, tzinfo=SHANGHAI)
    records = []
    for index in range(60):
        price = 100.0
        records.append(
            {
                "bar_time": start + timedelta(minutes=index),
                "symbol": contract,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1.0,
                "amount": price * 1.0 * multiplier,
                "trade_date": (start + timedelta(days=index // 20)).date(),
            }
        )
    return pd.DataFrame(records)


def test_metadata_multiplier_falls_back_to_inference_when_history_is_absent():
    # futures_contract_info only reaches back to 2025-12-22, so every historical
    # product-day has no metadata row. The minute bars still carry enough to
    # settle the multiplier on their own.
    connection = FakeConnection(metadata_rows=[])

    resolution = _source(connection).resolve_metadata_multiplier(
        daily_contract="RB2405.SHF",
        trade_date=date(2018, 6, 19),
        frame=_inferable_frame(),
    )

    assert resolution.multiplier == 10
    assert resolution.source == "inferred"


def _daily_rows(multiplier, close=2500.0, n=40):
    # turnover = volume * close_of_day * multiplier, with the day's close standing
    # in for its VWAP, which is where the small spread comes from in real data.
    return [
        (float(1000 + index), float(1000 + index) * close * multiplier, close)
        for index in range(n)
    ]


def test_multiplier_comes_from_the_daily_turnover_before_any_minute_inference():
    # Zhengzhou's minute turnover is synthetic, so the minute bars cannot settle
    # its multiplier. The daily record reconciles and can.
    connection = FakeConnection(metadata_rows=[], daily_rows=_daily_rows(10))

    resolution = _source(connection).resolve_metadata_multiplier(
        daily_contract="RM909.CZC",
        trade_date=date(2019, 6, 3),
        frame=_inferable_frame(contract="RM1909"),
    )

    assert resolution.multiplier == 10
    assert resolution.source == "daily_turnover"


def test_daily_turnover_multiplier_refuses_a_non_integer_result():
    connection = FakeConnection(metadata_rows=[], daily_rows=_daily_rows(7.4))

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).resolve_metadata_multiplier(
            daily_contract="RM909.CZC",
            trade_date=date(2019, 6, 3),
        )

    assert exc_info.value.check == "daily_turnover_multiplier"


def test_daily_turnover_multiplier_refuses_too_few_days():
    connection = FakeConnection(metadata_rows=[], daily_rows=_daily_rows(10, n=4))

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).resolve_metadata_multiplier(
            daily_contract="RM909.CZC",
            trade_date=date(2019, 6, 3),
        )

    assert exc_info.value.check == "daily_turnover_multiplier"


def test_metadata_multiplier_infers_from_the_wider_sample_when_given_one():
    # The day's own window spans one trade date, which is too narrow for the
    # inference sample. The month's bars for that contract are not.
    single_day = _inferable_frame().iloc[:20].copy()
    single_day["trade_date"] = date(2018, 6, 19)
    connection = FakeConnection(metadata_rows=[])

    resolution = _source(connection).resolve_metadata_multiplier(
        daily_contract="RB2405.SHF",
        trade_date=date(2018, 6, 19),
        frame=single_day,
        inference_frame=_inferable_frame(),
    )

    assert resolution.multiplier == 10
    assert resolution.source == "inferred"
    assert resolution.sample_dates >= 3


def test_metadata_multiplier_without_metadata_or_bars_still_fails_closed():
    connection = FakeConnection(metadata_rows=[])

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).resolve_metadata_multiplier(
            daily_contract="RB2405.SHF",
            trade_date=date(2018, 6, 19),
        )

    assert exc_info.value.check == "metadata_multiplier"


def test_metadata_multiplier_prefers_metadata_over_inference():
    # Metadata is a recorded fact and inference is a conclusion drawn from
    # turnover, so the fact wins wherever it exists.
    connection = FakeConnection(metadata_rows=[(date(2024, 1, 5), 10)])

    resolution = _source(connection).resolve_metadata_multiplier(
        daily_contract="RB2405.SHF",
        trade_date=date(2024, 1, 8),
        frame=_inferable_frame(),
    )

    assert resolution.multiplier == 10
    assert resolution.source == "metadata"


def test_metadata_multiplier_rejects_conflict_on_latest_date():
    connection = FakeConnection(
        metadata_rows=[
            (date(2024, 1, 5), 10),
            (date(2024, 1, 4), 15),
            (date(2024, 1, 5), 20),
        ]
    )

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).resolve_metadata_multiplier(
            daily_contract="RB2405.SHF",
            trade_date=date(2024, 1, 8),
        )

    assert exc_info.value.check == "metadata_multiplier"
    assert connection.rollbacks >= 1
    assert connection.commits == 0


@pytest.mark.parametrize(
    "rows", [[], [(date(2024, 1, 5), 0)], [(date(2024, 1, 5), 10.5)]]
)
def test_metadata_multiplier_rejects_missing_or_noninteger_values(rows):
    with pytest.raises(MinuteDataError) as exc_info:
        _source(FakeConnection(metadata_rows=rows)).resolve_metadata_multiplier(
            daily_contract="RB2405.SHF",
            trade_date=date(2024, 1, 8),
        )

    assert exc_info.value.check == "metadata_multiplier"


def test_metadata_multiplier_delegates_frame_validation(monkeypatch):
    from cta_carry import minute_pg_source

    sentinel = object()
    captured = {}

    def fake_validate(frame, *, contract, multiplier):
        captured.update(frame=frame, contract=contract, multiplier=multiplier)
        return sentinel

    monkeypatch.setattr(
        minute_pg_source,
        "validate_metadata_multiplier",
        fake_validate,
    )
    frame = pd.DataFrame({"symbol": ["RB2405"]})

    result = _source(
        FakeConnection(metadata_rows=[(date(2024, 1, 5), 10)])
    ).resolve_metadata_multiplier(
        daily_contract="RB2405.SHF",
        trade_date=date(2024, 1, 8),
        frame=frame,
    )

    assert result is sentinel
    assert captured == {"frame": frame, "contract": "RB2405", "multiplier": 10}


def _session_candidate(
    trade_date,
    *,
    candidate_role="session_representative",
    causal_in_pool_date=None,
    selection_source="target_day_main",
):
    previous = trade_date - timedelta(days=1)
    return MinuteCandidate(
        trade_date=trade_date,
        product="RB",
        daily_contract="RB2405.SHF",
        minute_symbol="RB2405",
        exchange="SHFE",
        window_start=datetime(
            previous.year,
            previous.month,
            previous.day,
            21,
            tzinfo=SHANGHAI,
        ),
        window_end=datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            16,
            tzinfo=SHANGHAI,
        ),
        candidate_role=candidate_role,
        causal_in_pool_date=causal_in_pool_date or trade_date,
        selection_source=selection_source,
    )


def test_session_representative_role_is_canonicalized_without_touching_future_roles():
    representative = _session_candidate(
        date(2024, 1, 8),
        candidate_role="SESSION_REPRESENTATIVE",
    )
    future_role = _session_candidate(
        date(2024, 1, 8),
        candidate_role="Signal_Main",
    )

    assert representative.candidate_role == "session_representative"
    assert future_role.candidate_role == "Signal_Main"


def _boundary_row(
    candidate,
    *,
    observed_rows=345,
    traded_first=_UNSET,
    traded_second=_UNSET,
    traded_flat=False,
):
    previous = candidate.trade_date - timedelta(days=1)
    identity = (
        candidate.trade_date,
        candidate.product,
        candidate.daily_contract,
        candidate.minute_symbol,
        candidate.exchange,
        candidate.window_start,
        candidate.window_end,
    )
    if observed_rows == 0:
        return (*identity, *(None for _ in range(8)), observed_rows, None, None, None)
    return (
        *identity,
        datetime(
            previous.year,
            previous.month,
            previous.day,
            21,
            tzinfo=SHANGHAI,
        ),
        datetime(
            previous.year,
            previous.month,
            previous.day,
            22,
            59,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            9,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            10,
            14,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            10,
            30,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            11,
            29,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            13,
            30,
            tzinfo=SHANGHAI,
        ),
        datetime(
            candidate.trade_date.year,
            candidate.trade_date.month,
            candidate.trade_date.day,
            14,
            59,
            tzinfo=SHANGHAI,
        ),
        observed_rows,
        _night_at(previous, 21, 0) if traded_first is _UNSET else traded_first,
        _night_at(previous, 21, 1) if traded_second is _UNSET else traded_second,
        traded_flat,
    )


def _night_at(previous, hour, minute):
    return datetime(
        previous.year,
        previous.month,
        previous.day,
        hour,
        minute,
        tzinfo=SHANGHAI,
    )


def test_boundary_frame_keeps_the_traded_night_columns(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, traded_flat=True)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    frame = _source(connection).iter_session_boundaries(
        [candidate],
        lower=datetime(2024, 1, 1, 0, 0, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, 0, 0, tzinfo=SHANGHAI),
    )

    assert frame.loc[0, "night_traded_first"] == _night_at(date(2024, 1, 7), 21, 0)
    assert frame.loc[0, "night_traded_second"] == _night_at(date(2024, 1, 7), 21, 1)
    assert bool(frame.loc[0, "night_traded_first_flat"]) is True


def test_boundary_frame_rejects_a_naive_traded_night_timestamp(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    connection = FakeConnection(
        boundary_rows=[
            _boundary_row(candidate, traded_first=datetime(2024, 1, 7, 21, 0))
        ]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError, match="aware datetimes"):
        _source(connection).iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, 0, 0, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, 0, 0, tzinfo=SHANGHAI),
        )


def test_boundary_frame_rejects_a_non_boolean_auction_flag(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, traded_flat="yes")]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError, match="night_traded_first_flat"):
        _source(connection).iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, 0, 0, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, 0, 0, tzinfo=SHANGHAI),
        )


def test_iter_session_boundaries_returns_one_ordered_row_per_candidate(monkeypatch):
    from cta_carry import minute_pg_source

    first = _session_candidate(date(2024, 1, 8))
    second = _session_candidate(date(2024, 1, 9))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(first), _boundary_row(second)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    frame = _source(connection).iter_session_boundaries(
        [second, first],
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
    )

    assert list(frame.columns) == BOUNDARY_COLUMNS
    assert frame["trade_date"].tolist() == [date(2024, 1, 8), date(2024, 1, 9)]
    executed = [event[1] for event in connection.events if event[0] == "execute"]
    assert [text.strip() for text in executed[:5]] == SETTINGS
    explain = next(text for text in executed if text.lstrip().startswith("EXPLAIN"))
    select = next(text for text in executed if text.lstrip().startswith("SELECT"))
    assert explain.removeprefix("EXPLAIN (FORMAT JSON) ") == select
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize("as_dataframe", [False, True])
def test_candidate_metadata_survives_sequence_and_dataframe_canonicalization(
    monkeypatch, as_dataframe
):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(
        date(2024, 1, 8),
        causal_in_pool_date=date(2024, 1, 5),
        selection_source="causal_in_pool_main",
    )
    supplied = pd.DataFrame([candidate.__dict__]) if as_dataframe else [candidate]
    captured = []
    monkeypatch.setattr(
        minute_pg_source,
        "_insert_candidates",
        lambda cursor, candidates: captured.extend(candidates),
    )

    _source(
        FakeConnection(boundary_rows=[_boundary_row(candidate)])
    ).iter_session_boundaries(
        supplied,
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
    )

    assert len(captured) == 1
    assert captured[0].candidate_role == "session_representative"
    assert captured[0].causal_in_pool_date == date(2024, 1, 5)
    assert captured[0].selection_source == "causal_in_pool_main"


def test_zero_row_case_variant_representative_keeps_role_specific_failure(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(
        date(2024, 1, 8),
        candidate_role="SESSION_REPRESENTATIVE",
        causal_in_pool_date=date(2024, 1, 5),
        selection_source="causal_in_pool_main",
    )
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, observed_rows=0)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    assert exc_info.value.check == "session_representative_missing_minutes"
    assert exc_info.value.context == {
        "candidate_role": "session_representative",
        "causal_in_pool_date": date(2024, 1, 5),
        "selection_source": "causal_in_pool_main",
    }


def test_zero_row_session_representative_has_exact_role_specific_failure(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(
        date(2024, 1, 8),
        causal_in_pool_date=date(2024, 1, 5),
        selection_source="causal_in_pool_main",
    )
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, observed_rows=0)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    error = exc_info.value
    assert error.check == "session_representative_missing_minutes"
    assert error.trade_date == date(2024, 1, 8)
    assert error.product == "RB"
    assert error.contract == "RB2405.SHF"
    assert error.context == {
        "candidate_role": "session_representative",
        "causal_in_pool_date": date(2024, 1, 5),
        "selection_source": "causal_in_pool_main",
    }
    assert error.check != "dynamic_execution_leg_missing_minutes"


def test_boundary_cardinality_mismatch_structures_unhashable_identity(monkeypatch):
    from cta_carry import minute_pg_source

    first = _session_candidate(date(2024, 1, 8))
    second = _session_candidate(date(2024, 1, 9))
    malformed = list(_boundary_row(first))
    malformed[1] = ["RB"]
    connection = FakeConnection(boundary_rows=[tuple(malformed)])
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).iter_session_boundaries(
            [first, second],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    assert exc_info.value.check == "session_boundaries"
    assert "identity" in exc_info.value.reason
    assert connection.rollbacks >= 1
    assert connection.commits == 0


def test_iter_session_boundaries_keeps_an_authorized_absent_candidate(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, observed_rows=0)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    frame = _source(connection).iter_session_boundaries(
        [candidate],
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        absent_identities=frozenset(
            {(candidate.trade_date, candidate.exchange, candidate.product)}
        ),
    )

    assert len(frame) == 1
    for column in ("night_first", "night_last", "day_1_first", "day_3_last"):
        assert frame[column].isna().all()


def test_iter_session_boundaries_tolerates_empty_rows_for_a_supplementary_look(
    monkeypatch,
):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, observed_rows=0)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    frame = _source(connection).iter_session_boundaries(
        [candidate],
        lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
        upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        tolerate_empty=True,
    )

    assert len(frame) == 1
    assert frame["night_traded_first"].isna().all()


def test_iter_session_boundaries_still_rejects_an_unregistered_absence(monkeypatch):
    from cta_carry import minute_pg_source

    candidate = _session_candidate(date(2024, 1, 8))
    other = _session_candidate(date(2024, 1, 9))
    connection = FakeConnection(
        boundary_rows=[_boundary_row(candidate, observed_rows=0)]
    )
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).iter_session_boundaries(
            [candidate],
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
            absent_identities=frozenset({(other.trade_date, other.exchange, "ZZ")}),
        )

    assert exc_info.value.check == "session_representative_missing_minutes"


@pytest.mark.parametrize(
    ("failure", "expected_check"),
    [
        ("missing", "session_representative_missing_minutes"),
        ("duplicate", "session_boundaries"),
        ("no_bars", "session_representative_missing_minutes"),
    ],
)
def test_iter_session_boundaries_rejects_unclassified_candidates(
    monkeypatch, failure, expected_check
):
    from cta_carry import minute_pg_source

    first = _session_candidate(date(2024, 1, 8))
    second = _session_candidate(date(2024, 1, 9))
    candidates = [first, second]
    rows = [_boundary_row(first), _boundary_row(second)]
    if failure == "missing":
        rows.pop()
    elif failure == "duplicate":
        rows[1] = _boundary_row(first)
    else:
        candidates = [first]
        rows = [_boundary_row(first, observed_rows=0)]
    connection = FakeConnection(boundary_rows=rows)
    monkeypatch.setattr(minute_pg_source, "_insert_candidates", lambda *args: None)

    with pytest.raises(MinuteDataError) as exc_info:
        _source(connection).iter_session_boundaries(
            candidates,
            lower=datetime(2024, 1, 1, tzinfo=SHANGHAI),
            upper=datetime(2024, 2, 1, tzinfo=SHANGHAI),
        )

    assert exc_info.value.check == expected_check
    assert exc_info.value.check != "dynamic_execution_leg_missing_minutes"
    assert connection.rollbacks >= 1
    assert connection.commits == 0
