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
_CANDIDATE_COLUMNS = (
    "trade_date",
    "product",
    "daily_contract",
    "minute_symbol",
    "exchange",
    "window_start",
    "window_end",
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
)
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
class MinuteCandidate:
    """Validated daily-contract candidate and its physical minute window."""

    trade_date: date
    product: str
    daily_contract: str
    minute_symbol: str
    exchange: str
    window_start: datetime
    window_end: datetime

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
    """Build one grouped session-boundary row per minute candidate."""
    lower, upper = _validated_bounds(lower, upper)
    lower_literal = lower.isoformat()
    upper_literal = upper.isoformat()
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
            min(m.bar_time) FILTER (
                WHERE m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS night_first,
            max(m.bar_time) FILTER (
                WHERE m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS night_last,
            min(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '09:00'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '10:15'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_1_first,
            max(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '09:00'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '10:15'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_1_last,
            min(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '10:30'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '11:30'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_2_first,
            max(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '10:30'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '11:30'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_2_last,
            min(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '13:30'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '15:00'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_3_first,
            max(m.bar_time) FILTER (
                WHERE m.bar_time >= (
                    c.trade_date::timestamp + TIME '13:30'
                ) AT TIME ZONE 'Asia/Shanghai'
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '15:00'
                ) AT TIME ZONE 'Asia/Shanghai'
            ) AS day_3_last,
            count(m.bar_time) AS observed_rows
        FROM _carry_minute_candidates c
        LEFT JOIN public.futures_minute m
          ON m.symbol = c.minute_symbol
         AND m.bar_time >= c.window_start
         AND m.bar_time < c.window_end
         AND m.bar_time >= '{lower_literal}'::TIMESTAMPTZ
         AND m.bar_time < '{upper_literal}'::TIMESTAMPTZ
        GROUP BY c.trade_date, c.product, c.daily_contract,
                 c.minute_symbol, c.exchange, c.window_start, c.window_end
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
        if len(actual) != len(set(actual)) or set(actual) != set(_CANDIDATE_COLUMNS):
            raise MinuteDataError(
                check="minute_candidates",
                reason="candidate frame must contain exactly the MinuteCandidate fields",
                context={"columns": actual, "required": _CANDIDATE_COLUMNS},
            )
        raw_candidates: Sequence[Any] = tuple(
            candidate_frame.loc[:, _CANDIDATE_COLUMNS].to_dict("records")
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


def _validate_plan(payload: Any) -> None:
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


def _validate_boundary_frame(
    rows: Sequence[Sequence[Any]],
    candidates: Sequence[MinuteCandidate],
) -> pd.DataFrame:
    expected = sorted(
        (
            candidate.trade_date,
            candidate.product,
            candidate.daily_contract,
            candidate.minute_symbol,
            candidate.exchange,
            candidate.window_start,
            candidate.window_end,
        )
        for candidate in candidates
    )
    if len(rows) != len(expected):
        raise MinuteDataError(
            check="session_boundaries",
            reason="boundary query must return exactly one row per candidate",
            context={"expected_rows": len(expected), "actual_rows": len(rows)},
        )

    identities: list[tuple[Any, ...]] = []
    normalized_rows: list[Sequence[Any]] = []
    boundary_indexes = range(7, 15)
    for row_number, row in enumerate(rows):
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
        observed_rows = row[15]
        if (
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
        normalized_rows.append((*row[:15], int(observed_rows)))

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
                _validate_plan(control_cursor.fetchone())
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
    ) -> pd.DataFrame:
        """Return one validated grouped boundary row per candidate."""
        candidates = _canonical_candidates(
            candidate_frame,
            lower=lower,
            upper=upper,
        )
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
                frame = _validate_boundary_frame(cursor.fetchall(), candidates)
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
