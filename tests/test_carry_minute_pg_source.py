from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import psycopg2.extras
import pytest

from cta_carry.minute_bars import MinuteDataError
from cta_carry.minute_pg_source import (
    MinuteCandidate,
    PublicMinuteSource,
    build_minute_batch_query,
    czce_minute_symbol,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
        elif normalized.startswith("SELECT c.trade_date"):
            self.mode = "stream"
        elif "FROM public.futures_contract_info" in normalized:
            self.mode = "metadata"

    def fetchone(self):
        assert self.mode == "plan"
        return (self.connection.plan,)

    def fetchall(self):
        assert self.mode == "metadata"
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
        metadata_rows=(),
        raise_on=None,
        rollback_error=False,
    ):
        self.plan = _safe_plan() if plan is None else plan
        self.stream_chunks = list(stream_chunks)
        self.metadata_rows = list(metadata_rows)
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
