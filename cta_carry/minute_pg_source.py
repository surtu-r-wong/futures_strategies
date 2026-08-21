"""Bounded PostgreSQL access for Carry contract minute bars."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import math
from numbers import Integral
import re
import sys
from typing import Any, Callable

import numpy as np
import pandas as pd
from psycopg2 import extras, sql

from common.config import load_config, resolve_settings_path
from common.db import get_connection, pg_config_from

from .minute_bars import MinuteDataError, validate_metadata_multiplier


_DAILY_TO_MINUTE_EXCHANGE = {
    "SHF": "SHFE",
    "DCE": "DCE",
    "CZC": "CZCE",
    "INE": "INE",
    "GFE": "GFEX",
}
_CONCRETE_CONTRACT = re.compile(
    r"^(?P<product>[A-Za-z]+)(?P<delivery>\d{3,4})\.(?P<suffix>[A-Za-z]+)$"
)
_CHUNK_RELATION = re.compile(r"_hyper_\d+_\d+_chunk")
_LEGACY_CANDIDATE_COLUMNS = (
    "trade_date",
    "product",
    "daily_contract",
    "minute_symbol",
    "exchange",
    "window_start",
    "window_end",
)
_CANDIDATE_COLUMNS = (
    *_LEGACY_CANDIDATE_COLUMNS,
    "candidate_role",
    "causal_in_pool_date",
    "selection_source",
)
_STREAM_COLUMNS = (
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
)
_BOUNDARY_COLUMNS = (
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
)
_TRADED_FIRST_INDEX = _BOUNDARY_COLUMNS.index("night_traded_first")
_TRADED_SECOND_INDEX = _BOUNDARY_COLUMNS.index("night_traded_second")
_TRADED_FLAT_INDEX = _BOUNDARY_COLUMNS.index("night_traded_first_flat")
_TRANSACTION_SETTINGS = (
    "SET LOCAL max_parallel_workers_per_gather = 0;",
    "SET LOCAL work_mem = '32MB';",
    "SET LOCAL statement_timeout = '300s';",
    "SET LOCAL enable_hashjoin = off;",
    "SET LOCAL enable_mergejoin = off;",
)
_CREATE_CANDIDATES = """
CREATE TEMP TABLE _carry_minute_candidates (
    trade_date DATE NOT NULL,
    product TEXT NOT NULL,
    daily_contract TEXT NOT NULL,
    minute_symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (minute_symbol, trade_date)
) ON COMMIT DROP;
"""
_INSERT_CANDIDATES = """
INSERT INTO _carry_minute_candidates (
    trade_date, product, daily_contract, minute_symbol, exchange,
    window_start, window_end
) VALUES %s
"""
_METADATA_QUERY = """
SELECT "交易日期", "合约乘数"
FROM public.futures_contract_info
WHERE "合约代码" = %s
  AND "交易日期" <= %s
ORDER BY "交易日期" DESC, "合约乘数" ASC NULLS LAST
"""
_TABLE_BOUNDS_QUERY = """
SELECT min(bar_time), max(bar_time) FROM public.futures_minute
"""


def _require_trade_date(value: object, *, contract: str | None = None) -> date:
    if type(value) is not date:
        raise MinuteDataError(
            contract=contract,
            check="minute_contract_mapping",
            reason="trade_date must be a concrete date value",
        )
    return value


def _parse_daily_contract(
    contract: object,
    trade_date: date,
) -> tuple[str, str, str]:
    _require_trade_date(
        trade_date, contract=contract if type(contract) is str else None
    )
    if type(contract) is not str:
        raise MinuteDataError(
            trade_date=trade_date,
            check="minute_contract_mapping",
            reason="daily contract must be a concrete month contract",
        )
    match = _CONCRETE_CONTRACT.fullmatch(contract.strip())
    if match is None:
        raise MinuteDataError(
            trade_date=trade_date,
            contract=contract,
            check="minute_contract_mapping",
            reason="daily contract must include a concrete three- or four-digit delivery month and venue suffix",
        )
    product = match.group("product").upper()
    delivery = match.group("delivery")
    suffix = match.group("suffix").upper()
    if suffix not in _DAILY_TO_MINUTE_EXCHANGE:
        raise MinuteDataError(
            trade_date=trade_date,
            contract=contract,
            product=product,
            check="minute_contract_mapping",
            reason="daily contract has an unsupported venue suffix",
            context={"suffix": suffix},
        )
    if len(delivery) == 3 and suffix != "CZC":
        raise MinuteDataError(
            trade_date=trade_date,
            contract=contract,
            product=product,
            check="minute_contract_mapping",
            reason="three-digit delivery codes are supported only for CZCE",
            context={"delivery": delivery, "suffix": suffix},
        )
    month = int(delivery[-2:])
    if not 1 <= month <= 12:
        raise MinuteDataError(
            trade_date=trade_date,
            contract=contract,
            product=product,
            check="minute_contract_mapping",
            reason="daily contract delivery month must be between 01 and 12",
            context={"delivery": delivery},
        )
    return product, delivery, suffix


def czce_minute_symbol(contract: str, trade_date: date) -> str:
    """Return the bare CZCE minute symbol for a concrete daily contract."""
    product, delivery, suffix = _parse_daily_contract(contract, trade_date)
    if suffix != "CZC":
        raise MinuteDataError(
            trade_date=trade_date,
            product=product,
            contract=contract,
            check="czce_contract_mapping",
            reason="CZCE mapping requires a CZC daily contract suffix",
            context={"suffix": suffix},
        )
    if len(delivery) == 4:
        return f"{product}{delivery}"

    delivery_year_digit = int(delivery[0])
    years_after = (delivery_year_digit - trade_date.year % 10 + 10) % 10
    if years_after > 3:
        raise MinuteDataError(
            trade_date=trade_date,
            product=product,
            contract=contract,
            check="czce_contract_mapping",
            reason="delivery year is more than three years after trade date",
        )
    delivery_year = trade_date.year + years_after
    return f"{product}{delivery_year % 100:02d}{delivery[-2:]}"


def _minute_contract(contract: str, trade_date: date) -> tuple[str, str, str]:
    product, delivery, suffix = _parse_daily_contract(contract, trade_date)
    minute_symbol = (
        czce_minute_symbol(contract, trade_date)
        if suffix == "CZC"
        else f"{product}{delivery}"
    )
    return product, minute_symbol, _DAILY_TO_MINUTE_EXCHANGE[suffix]


def minute_contract_identity(contract: str, trade_date: date) -> tuple[str, str, str]:
    """Map one normalized daily contract to its small-side minute identity."""
    return _minute_contract(contract, trade_date)


@dataclass(frozen=True)
class MinuteSourceAudit:
    """Immutable runtime provenance for one minute-source instance."""

    minute_table_min: datetime | None = None
    minute_table_max: datetime | None = None
    minute_query_months: int = 0
    minute_rows: int = 0
    minute_candidate_contract_days: int = 0


@dataclass(frozen=True)
class MinutePlanSummary:
    """Immutable, JSON-serializable facts from one safe minute query plan."""

    query_kind: str
    lower_bound: str
    upper_bound: str
    candidate_contract_days: int
    referenced_chunks: tuple[str, ...]
    maximum_plan_rows: int | float
    node_types: tuple[str, ...]


@dataclass(frozen=True)
class MinuteCandidate:
    """Validated daily-contract candidate and its physical minute window."""

    trade_date: date
    product: str
    daily_contract: str
    minute_symbol: str
    exchange: str
    window_start: datetime
    window_end: datetime
    candidate_role: str = "unspecified"
    causal_in_pool_date: date | None = None
    selection_source: str = "unspecified"

    def __post_init__(self) -> None:
        contract = self.daily_contract if type(self.daily_contract) is str else None
        trade_date = _require_trade_date(
            self.trade_date,
            contract=contract,
        )
        for name, value in {
            "product": self.product,
            "daily_contract": self.daily_contract,
            "minute_symbol": self.minute_symbol,
            "exchange": self.exchange,
        }.items():
            if type(value) is not str:
                raise MinuteDataError(
                    trade_date=trade_date,
                    contract=contract,
                    check="minute_candidate",
                    reason=f"{name} must be a concrete built-in string",
                )
        for name, value in {
            "window_start": self.window_start,
            "window_end": self.window_end,
        }.items():
            if type(value) is not datetime:
                raise MinuteDataError(
                    trade_date=trade_date,
                    contract=contract,
                    check="minute_candidate",
                    reason=f"{name} must be a concrete built-in datetime",
                )
        for name, value in {
            "candidate_role": self.candidate_role,
            "selection_source": self.selection_source,
        }.items():
            if type(value) is not str or not value.strip():
                raise MinuteDataError(
                    trade_date=trade_date,
                    contract=contract,
                    check="minute_candidate",
                    reason=f"{name} must be a nonempty concrete built-in string",
                )
        if (
            self.causal_in_pool_date is not None
            and type(self.causal_in_pool_date) is not date
        ):
            raise MinuteDataError(
                trade_date=trade_date,
                contract=contract,
                check="minute_candidate",
                reason="causal_in_pool_date must be a concrete date or None",
            )
        expected_product, expected_symbol, expected_exchange = _minute_contract(
            self.daily_contract,
            trade_date,
        )
        values = {
            "product": self.product,
            "minute_symbol": self.minute_symbol,
            "exchange": self.exchange,
        }
        normalized: dict[str, str] = {}
        for name, value in values.items():
            if not value.strip():
                raise MinuteDataError(
                    trade_date=trade_date,
                    product=expected_product,
                    contract=self.daily_contract,
                    check="minute_candidate",
                    reason=f"{name} must be a nonempty identifier",
                )
            normalized[name] = value.strip().upper()

        expected = {
            "product": expected_product,
            "minute_symbol": expected_symbol,
            "exchange": expected_exchange,
        }
        mismatches = {
            name: {"actual": normalized[name], "expected": value}
            for name, value in expected.items()
            if normalized[name] != value
        }
        if mismatches:
            raise MinuteDataError(
                trade_date=trade_date,
                product=expected_product,
                contract=self.daily_contract,
                check="minute_candidate",
                reason="candidate identifiers conflict with the daily contract mapping",
                context={"mismatches": mismatches},
            )
        try:
            valid_window = (
                _is_aware(self.window_start)
                and _is_aware(self.window_end)
                and self.window_start < self.window_end
            )
        except (TypeError, ValueError, OverflowError):
            valid_window = False
        if not valid_window:
            raise MinuteDataError(
                trade_date=trade_date,
                product=expected_product,
                contract=self.daily_contract,
                check="minute_candidate",
                reason="candidate window must contain ordered aware datetimes",
            )

        object.__setattr__(self, "product", normalized["product"])
        object.__setattr__(self, "daily_contract", self.daily_contract.strip().upper())
        object.__setattr__(self, "minute_symbol", normalized["minute_symbol"])
        object.__setattr__(self, "exchange", normalized["exchange"])
        candidate_role = self.candidate_role.strip()
        if candidate_role.casefold() == "session_representative":
            candidate_role = "session_representative"
        object.__setattr__(self, "candidate_role", candidate_role)
        object.__setattr__(self, "selection_source", self.selection_source.strip())


def _is_aware(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _validated_bounds(lower: datetime, upper: datetime) -> tuple[datetime, datetime]:
    if (
        type(lower) is not datetime
        or type(upper) is not datetime
        or not _is_aware(lower)
        or not _is_aware(upper)
    ):
        raise MinuteDataError(
            check="minute_query_bounds",
            reason="physical query bounds must be aware datetimes",
        )
    try:
        ordered = lower < upper
    except (TypeError, ValueError, OverflowError) as exc:
        raise MinuteDataError(
            check="minute_query_bounds",
            reason="physical query bounds must be comparable",
        ) from exc
    if not ordered:
        raise MinuteDataError(
            check="minute_query_bounds",
            reason="physical lower bound must precede upper bound",
        )
    return lower, upper


def build_minute_batch_query(*, lower: datetime, upper: datetime) -> sql.SQL:
    """Build a chunk-prunable SELECT with validated timestamp literals."""
    lower, upper = _validated_bounds(lower, upper)
    lower_literal = lower.isoformat()
    upper_literal = upper.isoformat()
    return sql.SQL(
        f"""
        SELECT
            c.trade_date,
            c.product,
            c.daily_contract,
            m.bar_time,
            m.symbol,
            m.exchange,
            m.open,
            m.high,
            m.low,
            m.close,
            m.volume,
            m.amount,
            m.open_interest
        FROM _carry_minute_candidates c
        JOIN public.futures_minute m
          ON m.symbol = c.minute_symbol
         AND m.bar_time >= c.window_start
         AND m.bar_time < c.window_end
        WHERE m.bar_time >= '{lower_literal}'::TIMESTAMPTZ
          AND m.bar_time < '{upper_literal}'::TIMESTAMPTZ
        ORDER BY m.bar_time, m.symbol
        """
    )


def build_session_boundary_query(*, lower: datetime, upper: datetime) -> sql.SQL:
    """Build one grouped session-boundary row per minute candidate.

    The aggregate sits in a LATERAL subquery so the planner seeks the minute
    table once per candidate on the bare symbol. A plain LEFT JOIN made it
    decompress every contract in the month and filter afterwards, which at 2025
    pool sizes cost 356 s per month against a 300 s statement timeout; the
    per-candidate shape returns identical rows in 0.7 s.
    """
    lower, upper = _validated_bounds(lower, upper)
    lower_literal = lower.isoformat()
    upper_literal = upper.isoformat()
    night = (
        "m.bar_time < (c.trade_date::timestamp + TIME '09:00')"
        " AT TIME ZONE 'Asia/Shanghai'"
    )
    night_traded = f"m.volume > 0 AND {night}"

    def segment(name: str, opening: str, closing: str) -> str:
        window = (
            f"m.bar_time >= (c.trade_date::timestamp + TIME '{opening}')"
            " AT TIME ZONE 'Asia/Shanghai'"
            f" AND m.bar_time < (c.trade_date::timestamp + TIME '{closing}')"
            " AT TIME ZONE 'Asia/Shanghai'"
        )
        return (
            f"                min(m.bar_time) FILTER (WHERE {window})"
            f" AS {name}_first,\n"
            f"                max(m.bar_time) FILTER (WHERE {window})"
            f" AS {name}_last,"
        )

    return sql.SQL(
        f"""
        SELECT
            c.trade_date,
            c.product,
            c.daily_contract,
            c.minute_symbol,
            c.exchange,
            c.window_start,
            c.window_end,
            b.night_first,
            b.night_last,
            b.day_1_first,
            b.day_1_last,
            b.day_2_first,
            b.day_2_last,
            b.day_3_first,
            b.day_3_last,
            b.observed_rows,
            b.night_traded_first,
            b.night_traded_second,
            b.night_traded_first_flat
        FROM _carry_minute_candidates c
        LEFT JOIN LATERAL (
            SELECT
                min(m.bar_time) FILTER (WHERE {night}) AS night_first,
                max(m.bar_time) FILTER (WHERE {night}) AS night_last,
{segment("day_1", "09:00", "10:15")}
{segment("day_2", "10:30", "11:30")}
{segment("day_3", "13:30", "15:00")}
                count(m.bar_time) AS observed_rows,
                (array_agg(m.bar_time ORDER BY m.bar_time)
                    FILTER (WHERE {night_traded}))[1] AS night_traded_first,
                (array_agg(m.bar_time ORDER BY m.bar_time)
                    FILTER (WHERE {night_traded}))[2] AS night_traded_second,
                (array_agg(m.high ORDER BY m.bar_time)
                    FILTER (WHERE {night_traded}))[1]
                  = (array_agg(m.low ORDER BY m.bar_time)
                    FILTER (WHERE {night_traded}))[1] AS night_traded_first_flat
            FROM public.futures_minute m
            WHERE m.symbol = c.minute_symbol
              AND m.bar_time >= c.window_start
              AND m.bar_time < c.window_end
              AND m.bar_time >= '{lower_literal}'::TIMESTAMPTZ
              AND m.bar_time < '{upper_literal}'::TIMESTAMPTZ
        ) b ON TRUE
        ORDER BY c.trade_date, c.product, c.daily_contract
        """
    )


def _fresh_date(value: date) -> date:
    if type(value) is not date:
        raise TypeError("expected an exact built-in date")
    return date(value.year, value.month, value.day)


def _fresh_datetime(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError("expected an exact built-in datetime")
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=value.tzinfo,
        fold=value.fold,
    )


def _fresh_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("expected an exact built-in string")
    return value.encode("utf-8").decode("utf-8")


def _dataframe_text(value: Any, *, column: str, row: int) -> str:
    if type(value) is str:
        return _fresh_text(value)
    if type(value) is np.str_:
        primitive = value.item()
        if type(primitive) is str:
            return _fresh_text(primitive)
    raise MinuteDataError(
        check="minute_candidates",
        reason="candidate identifier must be trusted text data",
        context={"column": column, "row": row, "type": type(value).__name__},
    )


def _trusted_timestamp(value: Any, *, column: str, row: int) -> pd.Timestamp:
    if type(value) is pd.Timestamp:
        timestamp = value
    elif type(value) is np.datetime64:
        timestamp = pd.Timestamp(value)
    else:
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidate time value must use a trusted scalar type",
            context={"column": column, "row": row, "type": type(value).__name__},
        )
    if pd.isna(timestamp) or timestamp.nanosecond != 0:
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidate time value must be concrete at microsecond precision",
            context={"column": column, "row": row},
        )
    return timestamp


def _dataframe_date(value: Any, *, column: str, row: int) -> date:
    if type(value) is date:
        return _fresh_date(value)
    timestamp = _trusted_timestamp(value, column=column, row=row)
    try:
        return date(timestamp.year, timestamp.month, timestamp.day)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidate date is outside the supported Python range",
            context={"column": column, "row": row, "value": str(value)},
        ) from exc


def _dataframe_optional_date(value: Any, *, column: str, row: int) -> date | None:
    if value is None:
        return None
    return _dataframe_date(value, column=column, row=row)


def _dataframe_datetime(value: Any, *, column: str, row: int) -> datetime:
    if type(value) is datetime:
        return _fresh_datetime(value)
    timestamp = _trusted_timestamp(value, column=column, row=row)
    try:
        return datetime(
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
            timestamp.microsecond,
            tzinfo=timestamp.tzinfo,
            fold=timestamp.fold,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidate timestamp is outside the supported Python range",
            context={"column": column, "row": row, "value": str(value)},
        ) from exc


def _dataframe_candidate(raw: dict[str, Any], *, row: int) -> MinuteCandidate:
    return MinuteCandidate(
        trade_date=_dataframe_date(raw["trade_date"], column="trade_date", row=row),
        product=_dataframe_text(raw["product"], column="product", row=row),
        daily_contract=_dataframe_text(
            raw["daily_contract"],
            column="daily_contract",
            row=row,
        ),
        minute_symbol=_dataframe_text(
            raw["minute_symbol"],
            column="minute_symbol",
            row=row,
        ),
        exchange=_dataframe_text(raw["exchange"], column="exchange", row=row),
        window_start=_dataframe_datetime(
            raw["window_start"],
            column="window_start",
            row=row,
        ),
        window_end=_dataframe_datetime(
            raw["window_end"],
            column="window_end",
            row=row,
        ),
        candidate_role=_dataframe_text(
            raw.get("candidate_role", "unspecified"),
            column="candidate_role",
            row=row,
        ),
        causal_in_pool_date=_dataframe_optional_date(
            raw.get("causal_in_pool_date"),
            column="causal_in_pool_date",
            row=row,
        ),
        selection_source=_dataframe_text(
            raw.get("selection_source", "unspecified"),
            column="selection_source",
            row=row,
        ),
    )


def _canonical_candidates(
    candidate_frame: pd.DataFrame | Sequence[MinuteCandidate],
    *,
    lower: datetime,
    upper: datetime,
) -> tuple[MinuteCandidate, ...]:
    lower, upper = _validated_bounds(lower, upper)
    if isinstance(candidate_frame, pd.DataFrame):
        actual = tuple(candidate_frame.columns)
        actual_set = set(actual)
        supported_sets = {
            frozenset(_LEGACY_CANDIDATE_COLUMNS),
            frozenset(_CANDIDATE_COLUMNS),
        }
        if (
            len(actual) != len(actual_set)
            or frozenset(actual_set) not in supported_sets
        ):
            raise MinuteDataError(
                check="minute_candidates",
                reason="candidate frame must contain exactly a supported MinuteCandidate schema",
                context={
                    "columns": actual,
                    "required": _CANDIDATE_COLUMNS,
                    "legacy": _LEGACY_CANDIDATE_COLUMNS,
                },
            )
        selected_columns = (
            _CANDIDATE_COLUMNS
            if actual_set == set(_CANDIDATE_COLUMNS)
            else _LEGACY_CANDIDATE_COLUMNS
        )
        raw_candidates: Sequence[Any] = tuple(
            candidate_frame.loc[:, selected_columns].to_dict("records")
        )
    elif isinstance(candidate_frame, Sequence) and not isinstance(
        candidate_frame, (str, bytes)
    ):
        raw_candidates = candidate_frame
    else:
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidates must be a DataFrame or a sequence of MinuteCandidate values",
        )

    candidates: list[MinuteCandidate] = []
    for index, raw in enumerate(raw_candidates):
        if isinstance(raw, MinuteCandidate):
            candidate = raw
        elif isinstance(raw, dict) and isinstance(candidate_frame, pd.DataFrame):
            candidate = _dataframe_candidate(raw, row=index)
        else:
            raise MinuteDataError(
                check="minute_candidates",
                reason="candidate sequences may contain only MinuteCandidate values",
                context={"index": index, "type": type(raw).__name__},
            )
        if candidate.window_start < lower or candidate.window_end > upper:
            raise MinuteDataError(
                trade_date=candidate.trade_date,
                product=candidate.product,
                contract=candidate.daily_contract,
                check="minute_candidates",
                reason="candidate window must be contained by the physical batch bounds",
                context={
                    "lower": lower,
                    "upper": upper,
                    "window_start": candidate.window_start,
                    "window_end": candidate.window_end,
                },
            )
        candidates.append(candidate)
    if not candidates:
        raise MinuteDataError(
            check="minute_candidates",
            reason="at least one minute candidate is required",
        )
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.minute_symbol,
                item.trade_date,
                item.daily_contract,
            ),
        )
    )
    keys = [(item.minute_symbol, item.trade_date) for item in ordered]
    if len(keys) != len(set(keys)):
        raise MinuteDataError(
            check="minute_candidates",
            reason="candidate primary keys must be unique",
            context={"candidate_count": len(keys), "unique_keys": len(set(keys))},
        )
    return ordered


def _session_candidate_metadata_error(
    candidate: MinuteCandidate,
    *,
    reason: str,
) -> MinuteDataError:
    return MinuteDataError(
        trade_date=candidate.trade_date,
        product=candidate.product,
        contract=candidate.daily_contract,
        check="session_candidate_metadata",
        reason=reason,
        context={
            "candidate_role": candidate.candidate_role,
            "causal_in_pool_date": candidate.causal_in_pool_date,
            "selection_source": candidate.selection_source,
        },
    )


def _validate_session_candidate_provenance(
    candidates: Sequence[MinuteCandidate],
) -> None:
    for candidate in candidates:
        role = candidate.candidate_role
        if (
            type(role) is not str
            or not role.strip()
            or role.casefold() == "unspecified"
        ):
            raise _session_candidate_metadata_error(
                candidate,
                reason="boundary candidate_role must be explicit",
            )
        if role != "session_representative":
            continue
        if type(candidate.causal_in_pool_date) is not date:
            raise _session_candidate_metadata_error(
                candidate,
                reason=(
                    "session representative causal_in_pool_date must be a concrete date"
                ),
            )
        source = candidate.selection_source
        if (
            type(source) is not str
            or not source.strip()
            or source.casefold() == "unspecified"
        ):
            raise _session_candidate_metadata_error(
                candidate,
                reason="session representative selection_source must be explicit",
            )


def _insert_candidates(cursor, candidates: Sequence[MinuteCandidate]) -> None:
    rows = [
        (
            _fresh_date(candidate.trade_date),
            _fresh_text(candidate.product),
            _fresh_text(candidate.daily_contract),
            _fresh_text(candidate.minute_symbol),
            _fresh_text(candidate.exchange),
            _fresh_datetime(candidate.window_start),
            _fresh_datetime(candidate.window_end),
        )
        for candidate in candidates
    ]
    extras.execute_values(cursor, _INSERT_CANDIDATES, rows, page_size=len(rows))


def _plan_error(
    reason: str,
    *,
    context: dict[str, Any] | None = None,
) -> MinuteDataError:
    return MinuteDataError(
        check="minute_query_plan",
        reason=reason,
        context=context,
    )


def _plan_root(payload: Any) -> dict[str, Any]:
    if isinstance(payload, tuple):
        if len(payload) != 1:
            raise _plan_error(
                "EXPLAIN row must contain exactly one JSON value",
                context={"column_count": len(payload)},
            )
        payload = payload[0]
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise _plan_error("EXPLAIN returned malformed JSON") from exc
    if isinstance(payload, list):
        if len(payload) != 1 or not isinstance(payload[0], dict):
            raise _plan_error(
                "EXPLAIN JSON must contain exactly one statement plan",
                context={"statement_count": len(payload)},
            )
        payload = payload[0]
    if not isinstance(payload, dict):
        raise _plan_error(
            "EXPLAIN JSON must be an object or one-element array",
            context={"type": type(payload).__name__},
        )
    root = payload.get("Plan")
    if not isinstance(root, dict):
        raise _plan_error("EXPLAIN JSON is missing its Plan object")
    return root


def _plan_nodes(root: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    nodes: list[dict[str, Any]] = []

    def visit(node: Any, path: tuple[int, ...]) -> None:
        if not isinstance(node, dict):
            raise _plan_error(
                "EXPLAIN plan nodes must be JSON objects",
                context={"path": path, "type": type(node).__name__},
            )
        node_type = node.get("Node Type")
        plan_rows = node.get("Plan Rows")
        if not isinstance(node_type, str) or not node_type.strip():
            raise _plan_error(
                "EXPLAIN plan node is missing Node Type",
                context={"path": path},
            )
        valid_plan_rows = (type(plan_rows) is int and plan_rows >= 0) or (
            type(plan_rows) is float and math.isfinite(plan_rows) and plan_rows >= 0
        )
        if not valid_plan_rows:
            raise _plan_error(
                "EXPLAIN plan node has invalid Plan Rows",
                context={"path": path, "plan_rows": plan_rows},
            )
        relation = node.get("Relation Name")
        schema = node.get("Schema")
        if relation is not None and not isinstance(relation, str):
            raise _plan_error(
                "EXPLAIN relation name must be text",
                context={"path": path},
            )
        if schema is not None and not isinstance(schema, str):
            raise _plan_error(
                "EXPLAIN schema name must be text",
                context={"path": path},
            )
        children = node.get("Plans", [])
        if not isinstance(children, list):
            raise _plan_error(
                "EXPLAIN Plans must be an array",
                context={"path": path},
            )
        nodes.append(node)
        for index, child in enumerate(children):
            visit(child, (*path, index))

    visit(root, ())
    return tuple(nodes)


def _validate_plan(
    payload: Any,
) -> tuple[tuple[str, ...], int | float, tuple[str, ...]]:
    nodes = _plan_nodes(_plan_root(payload))
    chunks = {
        relation
        for node in nodes
        if isinstance((relation := node.get("Relation Name")), str)
        and _CHUNK_RELATION.fullmatch(relation)
    }
    if len(chunks) > 3:
        raise _plan_error(
            "bounded minute query references more than three Timescale chunks",
            context={"chunk_count": len(chunks), "chunks": tuple(sorted(chunks))},
        )
    for node in nodes:
        plan_rows = node["Plan Rows"]
        if plan_rows >= 10_000_000:
            raise _plan_error(
                "bounded minute query has an unsafe row estimate",
                context={
                    "node_type": node["Node Type"],
                    "plan_rows": plan_rows,
                },
            )
        node_type = node["Node Type"].strip().lower()
        relation = str(node.get("Relation Name", "")).strip().lower()
        schema = str(node.get("Schema", "")).strip().lower()
        full_relation = relation.removeprefix("public.")
        if (
            node_type.endswith("seq scan")
            and full_relation == "futures_minute"
            and schema in {"", "public"}
        ):
            raise _plan_error(
                "bounded minute query would sequentially scan the full hypertable",
                context={"relation": relation, "schema": schema or None},
            )
    return (
        tuple(sorted(chunks)),
        max(node["Plan Rows"] for node in nodes),
        tuple(sorted({node["Node Type"].strip() for node in nodes})),
    )


def _minute_plan_summary(
    payload: Any,
    *,
    query_kind: str,
    candidates: Sequence[MinuteCandidate],
    lower: datetime,
    upper: datetime,
) -> MinutePlanSummary:
    chunks, maximum_plan_rows, node_types = _validate_plan(payload)
    return MinutePlanSummary(
        query_kind=query_kind,
        lower_bound=lower.isoformat(),
        upper_bound=upper.isoformat(),
        candidate_contract_days=len(candidates),
        referenced_chunks=chunks,
        maximum_plan_rows=maximum_plan_rows,
        node_types=node_types,
    )


def _validate_stream_frame(
    rows: Sequence[Sequence[Any]],
    previous_key: tuple[datetime, str] | None,
) -> tuple[pd.DataFrame, tuple[datetime, str]]:
    try:
        frame = pd.DataFrame.from_records(rows, columns=_STREAM_COLUMNS)
    except (TypeError, ValueError) as exc:
        raise MinuteDataError(
            check="minute_row_schema",
            reason="streamed minute rows do not match the SELECT column shape",
        ) from exc
    current_previous = previous_key
    for row_number, (bar_time, symbol) in enumerate(
        zip(frame["bar_time"], frame["symbol"], strict=True)
    ):
        if not _is_aware(bar_time) or not isinstance(symbol, str) or not symbol:
            raise MinuteDataError(
                timestamp=bar_time if isinstance(bar_time, datetime) else None,
                contract=symbol if isinstance(symbol, str) else None,
                check="minute_row_order",
                reason="stream ordering keys must be an aware bar_time and nonempty symbol",
                context={"row": row_number},
            )
        key = (bar_time, symbol)
        try:
            regressed = current_previous is not None and key < current_previous
        except (TypeError, ValueError, OverflowError) as exc:
            raise MinuteDataError(
                timestamp=bar_time,
                contract=symbol,
                check="minute_row_order",
                reason="stream ordering keys must be mutually comparable",
                context={"row": row_number},
            ) from exc
        if regressed:
            raise MinuteDataError(
                timestamp=bar_time,
                contract=symbol,
                check="minute_row_order",
                reason="streamed minute rows regressed across deterministic query order",
                context={"previous": current_previous, "current": key},
            )
        current_previous = key
    if current_previous is None:
        raise RuntimeError("internal stream validation received an empty chunk")
    return frame, current_previous


def _candidate_boundary_identity(candidate: MinuteCandidate) -> tuple[Any, ...]:
    return (
        candidate.trade_date,
        candidate.product,
        candidate.daily_contract,
        candidate.minute_symbol,
        candidate.exchange,
        candidate.window_start,
        candidate.window_end,
    )


def _missing_candidate_minutes(candidate: MinuteCandidate) -> MinuteDataError:
    if candidate.candidate_role == "session_representative":
        return MinuteDataError(
            trade_date=candidate.trade_date,
            product=candidate.product,
            contract=candidate.daily_contract,
            check="session_representative_missing_minutes",
            reason="session representative has no minute observations in its target window",
            context={
                "candidate_role": candidate.candidate_role,
                "causal_in_pool_date": candidate.causal_in_pool_date,
                "selection_source": candidate.selection_source,
            },
        )
    return MinuteDataError(
        trade_date=candidate.trade_date,
        product=candidate.product,
        contract=candidate.daily_contract,
        check="session_boundaries",
        reason="candidate has no classifiable minute observations",
        context={"candidate_role": candidate.candidate_role},
    )


def _returned_boundary_identity(
    row: Sequence[Any],
    *,
    row_number: int,
) -> tuple[Any, ...]:
    if (
        not isinstance(row, Sequence)
        or isinstance(row, (str, bytes))
        or len(row) != len(_BOUNDARY_COLUMNS)
    ):
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary row does not match the grouped SELECT shape",
            context={"row": row_number},
        )
    identity = tuple(row[:7])
    if (
        type(identity[0]) is not date
        or any(type(value) is not str for value in identity[1:5])
        or not _is_aware(identity[5])
        or not _is_aware(identity[6])
    ):
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary candidate identity has invalid scalar types",
            context={"row": row_number},
        )
    try:
        hash(identity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MinuteDataError(
            trade_date=identity[0],
            product=identity[1],
            contract=identity[2],
            check="session_boundaries",
            reason="boundary candidate identity is not hashable",
            context={"row": row_number},
        ) from exc
    return identity


def _validate_boundary_frame(
    rows: Sequence[Sequence[Any]],
    candidates: Sequence[MinuteCandidate],
    *,
    absent_identities: frozenset[tuple[date, str, str]] = frozenset(),
) -> pd.DataFrame:
    # Keyed by (trade_date, exchange, product): an absence is registered for a
    # product-day, and which contract happened to represent it is incidental.
    candidate_by_identity = {
        _candidate_boundary_identity(candidate): candidate for candidate in candidates
    }
    expected = sorted(candidate_by_identity)
    if len(rows) != len(expected):
        returned_identities = {
            _returned_boundary_identity(row, row_number=row_number)
            for row_number, row in enumerate(rows)
        }
        missing = [
            candidate_by_identity[identity]
            for identity in expected
            if identity not in returned_identities
        ]
        if missing and all(
            candidate.candidate_role == "session_representative"
            for candidate in missing
        ):
            raise _missing_candidate_minutes(missing[0])
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary query must return exactly one row per candidate",
            context={"expected_rows": len(expected), "actual_rows": len(rows)},
        )

    identities: list[tuple[Any, ...]] = []
    normalized_rows: list[Sequence[Any]] = []
    boundary_indexes = range(7, 15)
    for row_number, row in enumerate(rows):
        identity = _returned_boundary_identity(row, row_number=row_number)
        for column_index in boundary_indexes:
            value = row[column_index]
            if value is not None and not _is_aware(value):
                raise MinuteDataError(
                    trade_date=identity[0],
                    product=identity[1],
                    contract=identity[2],
                    check="session_boundaries",
                    reason="observed session boundaries must be aware datetimes",
                    context={
                        "row": row_number,
                        "column": _BOUNDARY_COLUMNS[column_index],
                    },
                )
        for column_index in (_TRADED_FIRST_INDEX, _TRADED_SECOND_INDEX):
            value = row[column_index]
            if value is not None and not _is_aware(value):
                raise MinuteDataError(
                    trade_date=identity[0],
                    product=identity[1],
                    contract=identity[2],
                    check="session_boundaries",
                    reason="observed session boundaries must be aware datetimes",
                    context={
                        "row": row_number,
                        "column": _BOUNDARY_COLUMNS[column_index],
                    },
                )
        flat = row[_TRADED_FLAT_INDEX]
        if flat is not None and not isinstance(flat, bool):
            raise MinuteDataError(
                trade_date=identity[0],
                product=identity[1],
                contract=identity[2],
                check="session_boundaries",
                reason="night_traded_first_flat must be boolean or null",
                context={"row": row_number, "value": repr(flat)},
            )
        observed_rows = row[15]
        candidate = candidate_by_identity.get(identity)
        empty_observation = (
            isinstance(observed_rows, Integral)
            and not isinstance(observed_rows, bool)
            and int(observed_rows) == 0
            and all(value is None for value in row[7:15])
        )
        # A registered absence says this product-day's archive is known to hold
        # nothing here. The row is kept with every boundary missing so the
        # classifier can see the absence rather than a fabricated session.
        authorized_absent = (
            candidate is not None
            and empty_observation
            and (candidate.trade_date, candidate.exchange, candidate.product)
            in absent_identities
        )
        if candidate is not None and empty_observation and not authorized_absent:
            raise _missing_candidate_minutes(candidate)
        if not authorized_absent and (
            isinstance(observed_rows, bool)
            or not isinstance(observed_rows, Integral)
            or int(observed_rows) <= 0
        ):
            raise MinuteDataError(
                trade_date=identity[0],
                product=identity[1],
                contract=identity[2],
                check="session_boundaries",
                reason="candidate has no classifiable minute observations",
                context={"row": row_number, "observed_rows": observed_rows},
            )
        identities.append(identity)
        normalized_rows.append(
            (
                *row[:15],
                int(observed_rows),
                row[_TRADED_FIRST_INDEX],
                row[_TRADED_SECOND_INDEX],
                flat,
            )
        )

    if len(identities) != len(set(identities)):
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary query returned a candidate more than once",
        )
    if identities != expected:
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary rows do not exactly match ordered candidates",
            context={"expected": expected, "actual": identities},
        )
    try:
        return pd.DataFrame.from_records(normalized_rows, columns=_BOUNDARY_COLUMNS)
    except (TypeError, ValueError) as exc:
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary rows cannot be represented as a table",
        ) from exc


def _metadata_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        result = int(value)
        return result if result > 0 else None
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        result = int(value)
        return result if result > 0 else None
    return None


def _rollback_preserving(connection, original: BaseException) -> None:
    try:
        connection.rollback()
    except Exception as rollback_error:
        original.add_note(f"rollback also failed: {rollback_error!r}")


def _close_cursor_preserving(cursor) -> None:
    close = getattr(cursor, "close", None)
    if close is None:
        return
    if sys.exc_info()[0] is None:
        close()
        return
    try:
        close()
    except Exception:
        pass


class _ConnectionCleanup(BaseException):
    """Bypass connection-manager rollback after an owned rollback attempt."""


def _exit_connection_scope_preserving(scope, connection, original) -> None:
    cleanup = _ConnectionCleanup()
    try:
        scope.__exit__(type(cleanup), cleanup, cleanup.__traceback__)
    except BaseException as cleanup_error:
        if cleanup_error is not cleanup:
            original.add_note(f"connection cleanup also failed: {cleanup_error!r}")
            close = getattr(connection, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as close_error:
                    original.add_note(
                        f"direct connection close also failed: {close_error!r}"
                    )


@contextmanager
def _managed_connection(factory, pg):
    scope = factory(pg)
    connection = scope.__enter__()
    try:
        yield connection
    except BaseException as original:
        _rollback_preserving(connection, original)
        _exit_connection_scope_preserving(scope, connection, original)
        raise
    else:
        scope.__exit__(None, None, None)


class PublicMinuteSource:
    """Open one public-schema transaction for each bounded monthly operation."""

    def __init__(
        self,
        *,
        pg: dict[str, Any] | None = None,
        config_path=None,
        use_test: bool = False,
        connection_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        if pg is None:
            settings_path = (
                config_path if config_path is not None else resolve_settings_path()
            )
            settings = load_config(settings_path)
            pg = pg_config_from(settings, use_test=use_test)
        self._pg = dict(pg)
        self._pg["schema"] = "public"
        self._connection_factory = connection_factory or get_connection
        self._minute_table_min: datetime | None = None
        self._minute_table_max: datetime | None = None
        self._minute_query_months = 0
        self._minute_rows = 0
        self._candidate_contract_days: set[tuple[date, str]] = set()
        self._plan_summaries: list[MinutePlanSummary] = []

    @property
    def audit(self) -> MinuteSourceAudit:
        """Return a stable snapshot of source coverage and query counters."""
        return MinuteSourceAudit(
            minute_table_min=self._minute_table_min,
            minute_table_max=self._minute_table_max,
            minute_query_months=self._minute_query_months,
            minute_rows=self._minute_rows,
            minute_candidate_contract_days=len(self._candidate_contract_days),
        )

    @property
    def plan_audit(self) -> tuple[MinutePlanSummary, ...]:
        """Return stable snapshots of successfully validated query plans."""
        return tuple(self._plan_summaries)

    def load_table_bounds(self) -> tuple[datetime, datetime]:
        """Load and cache the exact first and last physical minute timestamps."""
        if self._minute_table_min is not None and self._minute_table_max is not None:
            return self._minute_table_min, self._minute_table_max

        with _managed_connection(
            self._connection_factory,
            self._pg.copy(),
        ) as connection:
            cursor = connection.cursor()
            try:
                for setting in _TRANSACTION_SETTINGS:
                    cursor.execute(setting)
                cursor.execute(_TABLE_BOUNDS_QUERY)
                row = cursor.fetchone()
            finally:
                _close_cursor_preserving(cursor)

        if not isinstance(row, Sequence) or len(row) != 2:
            raise MinuteDataError(
                check="minute_table_bounds",
                reason="minute table bounds query must return exactly two columns",
            )
        table_min, table_max = row
        if (
            type(table_min) is not datetime
            or type(table_max) is not datetime
            or not _is_aware(table_min)
            or not _is_aware(table_max)
        ):
            raise MinuteDataError(
                check="minute_table_bounds",
                reason="minute table bounds must be two aware datetimes",
                context={"minute_table_min": table_min, "minute_table_max": table_max},
            )
        try:
            regressed = table_min > table_max
        except (TypeError, ValueError, OverflowError) as exc:
            raise MinuteDataError(
                check="minute_table_bounds",
                reason="minute table bounds must be mutually comparable",
            ) from exc
        if regressed:
            raise MinuteDataError(
                check="minute_table_bounds",
                reason="minute table minimum cannot follow its maximum",
                context={"minute_table_min": table_min, "minute_table_max": table_max},
            )
        self._minute_table_min = table_min
        self._minute_table_max = table_max
        return table_min, table_max

    def explain_month(
        self,
        candidate_frame: pd.DataFrame | Sequence[MinuteCandidate],
        lower: datetime,
        upper: datetime,
    ) -> MinutePlanSummary:
        """Validate and summarize a bounded monthly plan without selecting rows."""
        candidates = _canonical_candidates(
            candidate_frame,
            lower=lower,
            upper=upper,
        )
        select_text = build_minute_batch_query(
            lower=lower,
            upper=upper,
        ).as_string(None)
        explain_text = "EXPLAIN (FORMAT JSON) " + select_text
        with _managed_connection(
            self._connection_factory,
            self._pg.copy(),
        ) as connection:
            cursor = connection.cursor()
            try:
                for setting in _TRANSACTION_SETTINGS:
                    cursor.execute(setting)
                cursor.execute(_CREATE_CANDIDATES)
                _insert_candidates(cursor, candidates)
                cursor.execute(explain_text)
                summary = _minute_plan_summary(
                    cursor.fetchone(),
                    query_kind="explain_only",
                    candidates=candidates,
                    lower=lower,
                    upper=upper,
                )
            finally:
                _close_cursor_preserving(cursor)
        self._plan_summaries.append(summary)
        return summary

    def iter_month(
        self,
        candidate_frame: pd.DataFrame | Sequence[MinuteCandidate],
        lower: datetime,
        upper: datetime,
    ):
        """Yield ordered DataFrames while retaining transaction ownership."""
        candidates = _canonical_candidates(
            candidate_frame,
            lower=lower,
            upper=upper,
        )
        self._minute_query_months += 1
        self._candidate_contract_days.update(
            (candidate.trade_date, candidate.daily_contract) for candidate in candidates
        )
        select_text = build_minute_batch_query(
            lower=lower,
            upper=upper,
        ).as_string(None)
        explain_text = "EXPLAIN (FORMAT JSON) " + select_text
        with _managed_connection(
            self._connection_factory,
            self._pg.copy(),
        ) as connection:
            control_cursor = connection.cursor()
            stream_cursor = None
            try:
                for setting in _TRANSACTION_SETTINGS:
                    control_cursor.execute(setting)
                control_cursor.execute(_CREATE_CANDIDATES)
                _insert_candidates(control_cursor, candidates)
                control_cursor.execute(explain_text)
                summary = _minute_plan_summary(
                    control_cursor.fetchone(),
                    query_kind="iter_month",
                    candidates=candidates,
                    lower=lower,
                    upper=upper,
                )
                self._plan_summaries.append(summary)
                stream_cursor = connection.cursor(name="_carry_minute_stream")
                stream_cursor.itersize = 100_000
                stream_cursor.execute(select_text)
                previous_key = None
                while True:
                    rows = stream_cursor.fetchmany(100_000)
                    if not rows:
                        break
                    frame, previous_key = _validate_stream_frame(
                        rows,
                        previous_key,
                    )
                    self._minute_rows += len(frame)
                    yield frame
            finally:
                if stream_cursor is not None:
                    _close_cursor_preserving(stream_cursor)
                _close_cursor_preserving(control_cursor)

    def iter_session_boundaries(
        self,
        candidate_frame: pd.DataFrame | Sequence[MinuteCandidate],
        lower: datetime,
        upper: datetime,
        absent_identities: frozenset[tuple[date, str, str]] = frozenset(),
    ) -> pd.DataFrame:
        """Return one validated grouped boundary row per candidate."""
        candidates = _canonical_candidates(
            candidate_frame,
            lower=lower,
            upper=upper,
        )
        _validate_session_candidate_provenance(candidates)
        select_text = build_session_boundary_query(
            lower=lower,
            upper=upper,
        ).as_string(None)
        explain_text = "EXPLAIN (FORMAT JSON) " + select_text
        with _managed_connection(
            self._connection_factory,
            self._pg.copy(),
        ) as connection:
            cursor = connection.cursor()
            try:
                for setting in _TRANSACTION_SETTINGS:
                    cursor.execute(setting)
                cursor.execute(_CREATE_CANDIDATES)
                _insert_candidates(cursor, candidates)
                cursor.execute(explain_text)
                _validate_plan(cursor.fetchone())
                cursor.execute(select_text)
                frame = _validate_boundary_frame(
                    cursor.fetchall(),
                    candidates,
                    absent_identities=absent_identities,
                )
            finally:
                _close_cursor_preserving(cursor)
        return frame

    def resolve_metadata_multiplier(
        self,
        *,
        daily_contract: str,
        trade_date: date,
        frame: pd.DataFrame | None = None,
    ):
        """Resolve latest effective contract metadata, optionally validate it."""
        trade_date = _require_trade_date(trade_date, contract=daily_contract)
        _, minute_symbol, _ = _minute_contract(daily_contract, trade_date)
        with _managed_connection(
            self._connection_factory,
            self._pg.copy(),
        ) as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(_METADATA_QUERY, (minute_symbol, trade_date))
                rows = cursor.fetchall()
                dated_values: list[tuple[date, Any]] = []
                for row_number, row in enumerate(rows):
                    if not isinstance(row, Sequence) or len(row) < 2:
                        raise MinuteDataError(
                            trade_date=trade_date,
                            contract=daily_contract,
                            check="metadata_multiplier",
                            reason="contract metadata row has an invalid shape",
                            context={"row": row_number},
                        )
                    effective_date = row[0]
                    if isinstance(effective_date, datetime):
                        effective_date = effective_date.date()
                    if type(effective_date) is not date or effective_date > trade_date:
                        raise MinuteDataError(
                            trade_date=trade_date,
                            contract=daily_contract,
                            check="metadata_multiplier",
                            reason="contract metadata effective date is invalid",
                            context={"row": row_number, "effective_date": row[0]},
                        )
                    dated_values.append((effective_date, row[1]))
                if not dated_values:
                    raise MinuteDataError(
                        trade_date=trade_date,
                        contract=daily_contract,
                        check="metadata_multiplier",
                        reason="no contract multiplier metadata exists on or before trade_date",
                    )
                latest_date = max(item[0] for item in dated_values)
                latest_values = [
                    value
                    for effective_date, value in dated_values
                    if effective_date == latest_date
                ]
                normalized = [_metadata_integer(value) for value in latest_values]
                if any(value is None for value in normalized):
                    raise MinuteDataError(
                        trade_date=trade_date,
                        contract=daily_contract,
                        check="metadata_multiplier",
                        reason="latest contract multiplier metadata must be a positive integer",
                        context={
                            "effective_date": latest_date,
                            "values": tuple(latest_values),
                        },
                    )
                distinct = set(normalized)
                if len(distinct) != 1:
                    raise MinuteDataError(
                        trade_date=trade_date,
                        contract=daily_contract,
                        check="metadata_multiplier",
                        reason="latest contract metadata date has conflicting multipliers",
                        context={
                            "effective_date": latest_date,
                            "multipliers": tuple(sorted(distinct)),
                        },
                    )
                multiplier = distinct.pop()
                if frame is None:
                    return multiplier
                return validate_metadata_multiplier(
                    frame,
                    contract=minute_symbol,
                    multiplier=multiplier,
                )
            finally:
                _close_cursor_preserving(cursor)
