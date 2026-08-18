"""Strict versioned authority for commodity night-session observations."""

from collections.abc import Iterable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import io
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from .minute_sessions import SESSION_RULES_VERSION, parse_night_interval


AUTHORITY_VERSION = SESSION_RULES_VERSION
NOTICE_EVENING = re.compile(r"\bnotice_evening=(\d{4}-\d{2}-\d{2})\b")

_SESSION_EXCEPTION_COLUMNS = (
    "exchange",
    "version",
    "trade_date",
    "night_start",
    "night_end",
    "reason",
    "source_url",
)
_RANGE_COLUMNS = (
    "version",
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "reason",
    "source_url",
)
_NORMAL_NIGHT_ENDS = frozenset({"23:00", "23:30", "01:00", "02:30"})


def _stable_value(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{key!r}: {_stable_value(item)}"
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
            + "}"
        )
    return repr(value)


class SessionAuthorityError(ValueError):
    """Raised when repository authority cannot safely explain an observation."""

    def __init__(
        self,
        *,
        check: str,
        reason: str,
        row: object | None = None,
        row_identity: Mapping[str, object] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.check = check
        self.row = row
        self.row_identity = (
            dict(row_identity)
            if row_identity is not None
            else _authority_row_identity(row)
        )
        self.reason = reason
        self.context = dict(context) if context is not None else None
        details = [f"check={check}", f"reason={reason}"]
        if self.row_identity is not None:
            details.append(f"row_identity={_stable_value(self.row_identity)}")
        if self.context is not None:
            details.append(f"context={_stable_value(self.context)}")
        super().__init__("session_authority_error: " + "; ".join(details))


def _authority_row_identity(row: object | None) -> dict[str, object] | None:
    if row is None:
        return None
    if isinstance(row, SessionException):
        return {
            "version": row.version,
            "exchange": row.exchange,
            "trade_date": row.trade_date.isoformat()
            if type(row.trade_date) is date
            else row.trade_date,
        }
    if isinstance(row, EffectiveAuthorityRange):
        return {
            "version": row.version,
            "exchange": row.exchange,
            "product": row.product,
            "effective_start": row.effective_start.isoformat()
            if type(row.effective_start) is date
            else row.effective_start,
        }
    return {"row": repr(row)}


def _validate_required_record_text(
    *, record_kind: str, field: str, value: object, identity: Mapping[str, object]
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SessionAuthorityError(
            check="authority_record_required",
            reason=f"{record_kind} {field} must be a nonempty string",
            row_identity=identity,
            context={"field": field, "value": value},
        )


@dataclass(frozen=True, order=True)
class SessionException:
    exchange: str
    version: str
    trade_date: date
    night_start: str
    night_end: str
    reason: str
    source_url: str

    def __post_init__(self) -> None:
        identity = {
            "version": self.version,
            "exchange": self.exchange,
            "trade_date": self.trade_date.isoformat()
            if type(self.trade_date) is date
            else self.trade_date,
        }
        if self.version != AUTHORITY_VERSION:
            raise SessionAuthorityError(
                check="authority_record_version",
                reason=f"expected {AUTHORITY_VERSION!r}",
                row_identity=identity,
                context={"actual": self.version},
            )
        for field in ("exchange", "reason", "source_url"):
            _validate_required_record_text(
                record_kind="session exception",
                field=field,
                value=getattr(self, field),
                identity=identity,
            )
        if type(self.trade_date) is not date:
            raise SessionAuthorityError(
                check="authority_record_date",
                reason="session exception trade_date must be an actual date",
                row_identity=identity,
                context={"field": "trade_date", "type": type(self.trade_date).__name__},
            )
        try:
            parse_night_interval(self.night_start, self.night_end)
        except ValueError as exc:
            raise SessionAuthorityError(
                check="authority_csv_time",
                reason=str(exc),
                row_identity=identity,
                context={
                    "night_start": self.night_start,
                    "night_end": self.night_end,
                },
            ) from exc


@dataclass(frozen=True)
class EffectiveAuthorityRange:
    version: str
    exchange: str
    product: str
    effective_start: date
    effective_end: date | None
    reason: str
    source_url: str

    def __post_init__(self) -> None:
        identity = {
            "version": self.version,
            "exchange": self.exchange,
            "product": self.product,
            "effective_start": self.effective_start.isoformat()
            if type(self.effective_start) is date
            else self.effective_start,
        }
        if self.version != AUTHORITY_VERSION:
            raise SessionAuthorityError(
                check="authority_record_version",
                reason=f"expected {AUTHORITY_VERSION!r}",
                row_identity=identity,
                context={"actual": self.version},
            )
        for field in ("exchange", "product", "reason", "source_url"):
            _validate_required_record_text(
                record_kind="effective authority range",
                field=field,
                value=getattr(self, field),
                identity=identity,
            )
        if type(self.effective_start) is not date or (
            self.effective_end is not None and type(self.effective_end) is not date
        ):
            raise SessionAuthorityError(
                check="authority_record_date",
                reason="effective bounds must be actual date values",
                row_identity=identity,
                context={
                    "effective_start_type": type(self.effective_start).__name__,
                    "effective_end_type": type(self.effective_end).__name__,
                },
            )
        if self.effective_end is not None and self.effective_start > self.effective_end:
            raise SessionAuthorityError(
                check="authority_range_order",
                reason="effective_start must not exceed effective_end",
                row_identity=identity,
                context={"effective_end": self.effective_end},
            )


@dataclass(frozen=True)
class SessionAuthority:
    session_exceptions: tuple[SessionException, ...]
    day_only_regimes: tuple[EffectiveAuthorityRange, ...]
    liquidity_history_exceptions: tuple[EffectiveAuthorityRange, ...]
    sha256_by_asset: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_exceptions", tuple(self.session_exceptions))
        object.__setattr__(self, "day_only_regimes", tuple(self.day_only_regimes))
        object.__setattr__(
            self,
            "liquidity_history_exceptions",
            tuple(self.liquidity_history_exceptions),
        )
        object.__setattr__(
            self, "sha256_by_asset", MappingProxyType(dict(self.sha256_by_asset))
        )


def _row_identity(row: Mapping[str, str], *, row_number: int) -> dict[str, object]:
    return {"row_number": row_number, **row}


def _read_asset_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SessionAuthorityError(
            check="authority_asset_read",
            reason="authority asset could not be read",
            row_identity={"path": str(path)},
            context={"error": str(exc)},
        ) from exc


def _read_csv_rows(
    path: Path,
    payload: bytes,
    *,
    columns: tuple[str, ...],
    optional_fields: frozenset[str] = frozenset(),
) -> tuple[tuple[int, dict[str, str]], ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionAuthorityError(
            check="authority_asset_decode",
            reason="authority asset is not valid UTF-8",
            row_identity={"path": str(path)},
            context={"start": exc.start, "end": exc.end},
        ) from exc

    with io.StringIO(text, newline="") as handle:
        reader = csv.DictReader(handle)
        actual = tuple(reader.fieldnames or ())
        if actual != columns:
            raise SessionAuthorityError(
                check="authority_csv_header",
                reason="CSV header does not match the authority schema",
                row_identity={"path": str(path)},
                context={"path": str(path), "expected": columns, "actual": actual},
            )

        rows: list[tuple[int, dict[str, str]]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw or any(value is None for value in raw.values()):
                raise SessionAuthorityError(
                    check="authority_csv_row_width",
                    reason="CSV row does not match the authority schema width",
                    row_identity={"path": str(path), "row_number": row_number},
                    context={"expected_columns": len(columns)},
                )
            row = dict(raw)
            for field in columns:
                if field in optional_fields:
                    continue
                if not row[field].strip():
                    raise SessionAuthorityError(
                        check="authority_csv_required",
                        reason="authority field must be nonempty",
                        row_identity=_row_identity(row, row_number=row_number),
                        context={"path": str(path), "field": field},
                    )
            if row["version"] != AUTHORITY_VERSION:
                raise SessionAuthorityError(
                    check="authority_csv_version",
                    reason=f"expected authority version {AUTHORITY_VERSION!r}",
                    row_identity=_row_identity(row, row_number=row_number),
                    context={"path": str(path), "actual": row["version"]},
                )
            rows.append((row_number, row))
    return tuple(rows)


def _parse_csv_date(
    *, path: Path, row_number: int, row: Mapping[str, str], field: str
) -> date:
    value = row[field]
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SessionAuthorityError(
            check="authority_csv_date",
            reason="authority date must use a valid ISO calendar date",
            row_identity=_row_identity(row, row_number=row_number),
            context={"path": str(path), "field": field, "value": value},
        ) from exc


def _session_exception_sort_key(row: SessionException) -> tuple[object, ...]:
    return (
        row.version,
        row.exchange,
        row.trade_date,
        row.night_start,
        row.night_end,
        row.reason,
        row.source_url,
    )


def _range_sort_key(row: EffectiveAuthorityRange) -> tuple[object, ...]:
    return (
        row.version,
        row.exchange,
        row.product,
        row.effective_start,
        row.effective_end or date.max,
        row.reason,
        row.source_url,
    )


def _load_session_exceptions_payload(
    path: Path, payload: bytes
) -> tuple[SessionException, ...]:
    parsed: list[SessionException] = []
    seen: set[tuple[str, str, date]] = set()
    for row_number, row in _read_csv_rows(
        path, payload, columns=_SESSION_EXCEPTION_COLUMNS
    ):
        trade_date = _parse_csv_date(
            path=path, row_number=row_number, row=row, field="trade_date"
        )
        key = (row["version"], row["exchange"], trade_date)
        if key in seen:
            raise SessionAuthorityError(
                check="authority_duplicate_key",
                reason="duplicate session exception authority key",
                row_identity={
                    "version": key[0],
                    "exchange": key[1],
                    "trade_date": key[2].isoformat(),
                },
                context={"path": str(path), "row_number": row_number},
            )
        seen.add(key)
        try:
            parsed.append(
                SessionException(
                    exchange=row["exchange"],
                    version=row["version"],
                    trade_date=trade_date,
                    night_start=row["night_start"],
                    night_end=row["night_end"],
                    reason=row["reason"],
                    source_url=row["source_url"],
                )
            )
        except SessionAuthorityError as exc:
            if exc.check != "authority_csv_time":
                raise
            raise SessionAuthorityError(
                check=exc.check,
                reason=exc.reason,
                row_identity=_row_identity(row, row_number=row_number),
                context={
                    "path": str(path),
                    "night_start": row["night_start"],
                    "night_end": row["night_end"],
                },
            ) from exc
    return tuple(sorted(parsed, key=_session_exception_sort_key))


def load_session_exceptions(path: Path) -> tuple[SessionException, ...]:
    """Load exchange-date session exceptions with exact schema and unique keys."""
    return _load_session_exceptions_payload(path, _read_asset_bytes(path))


def _load_authority_ranges_payload(
    path: Path, payload: bytes
) -> tuple[EffectiveAuthorityRange, ...]:
    parsed: list[EffectiveAuthorityRange] = []
    seen: set[tuple[str, str, str, date]] = set()
    for row_number, row in _read_csv_rows(
        path,
        payload,
        columns=_RANGE_COLUMNS,
        optional_fields=frozenset({"effective_end"}),
    ):
        effective_start = _parse_csv_date(
            path=path, row_number=row_number, row=row, field="effective_start"
        )
        effective_end = (
            _parse_csv_date(
                path=path, row_number=row_number, row=row, field="effective_end"
            )
            if row["effective_end"]
            else None
        )
        key = (
            row["version"],
            row["exchange"],
            row["product"],
            effective_start,
        )
        if key in seen:
            raise SessionAuthorityError(
                check="authority_duplicate_key",
                reason="duplicate effective authority range key",
                row_identity={
                    "version": key[0],
                    "exchange": key[1],
                    "product": key[2],
                    "effective_start": key[3].isoformat(),
                },
                context={"path": str(path), "row_number": row_number},
            )
        seen.add(key)
        try:
            parsed.append(
                EffectiveAuthorityRange(
                    version=row["version"],
                    exchange=row["exchange"],
                    product=row["product"],
                    effective_start=effective_start,
                    effective_end=effective_end,
                    reason=row["reason"],
                    source_url=row["source_url"],
                )
            )
        except SessionAuthorityError as exc:
            if exc.check != "authority_range_order":
                raise
            raise SessionAuthorityError(
                check=exc.check,
                reason=exc.reason,
                row_identity=_row_identity(row, row_number=row_number),
                context={"path": str(path), "effective_end": effective_end},
            ) from exc

    ordered = tuple(sorted(parsed, key=_range_sort_key))
    prior_by_identity: dict[tuple[str, str, str], EffectiveAuthorityRange] = {}
    for row in ordered:
        identity = (row.version, row.exchange, row.product)
        prior = prior_by_identity.get(identity)
        if prior is not None and (
            prior.effective_end is None or row.effective_start <= prior.effective_end
        ):
            raise SessionAuthorityError(
                check="authority_range_overlap",
                reason="authority ranges overlap on inclusive effective dates",
                row=row,
                context={
                    "previous_effective_start": prior.effective_start,
                    "previous_effective_end": prior.effective_end,
                },
            )
        prior_by_identity[identity] = row
    return ordered


def load_authority_ranges(path: Path) -> tuple[EffectiveAuthorityRange, ...]:
    """Load product-effective authority ranges and reject inclusive overlap."""
    return _load_authority_ranges_payload(path, _read_asset_bytes(path))


def load_session_authority(
    *,
    session_exception_path: Path,
    day_only_path: Path,
    history_exception_path: Path,
) -> SessionAuthority:
    """Load all authority assets and bind their exact bytes to content hashes."""
    paths = {
        "session_exception": session_exception_path,
        "day_only": day_only_path,
        "history_exception": history_exception_path,
    }
    payloads = {name: _read_asset_bytes(path) for name, path in paths.items()}
    return SessionAuthority(
        session_exceptions=_load_session_exceptions_payload(
            session_exception_path, payloads["session_exception"]
        ),
        day_only_regimes=_load_authority_ranges_payload(
            day_only_path, payloads["day_only"]
        ),
        liquidity_history_exceptions=_load_authority_ranges_payload(
            history_exception_path, payloads["history_exception"]
        ),
        sha256_by_asset={
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        },
    )


def validate_session_exception_calendar(
    rows: Sequence[SessionException], global_calendar: Sequence[date]
) -> None:
    """Verify that notice evenings map to the next global target trade date."""
    for value in global_calendar:
        if type(value) is not date:
            raise SessionAuthorityError(
                check="notice_calendar_date",
                reason="global calendar must contain actual date values",
                row_identity={"value": repr(value)},
                context={"type": type(value).__name__},
            )
    ordered = tuple(sorted(set(global_calendar)))
    for row in rows:
        tokens = NOTICE_EVENING.findall(row.reason)
        if len(tokens) != 1:
            raise SessionAuthorityError(
                check="notice_evening_token",
                reason="reason must contain exactly one notice_evening ISO date token",
                row=row,
                context={"token_count": len(tokens)},
            )
        try:
            evening = date.fromisoformat(tokens[0])
        except ValueError as exc:
            raise SessionAuthorityError(
                check="notice_evening_date",
                reason="notice_evening token is not a valid ISO calendar date",
                row=row,
                context={"value": tokens[0]},
            ) from exc
        target = next((day for day in ordered if day > evening), None)
        if target != row.trade_date:
            raise SessionAuthorityError(
                check="notice_target_trade_date",
                reason="notice evening does not map to the declared target trade date",
                row=row,
                context={"expected_trade_date": target},
            )


def _validate_match_query(
    *, exchange: object, product: object | None, trade_date: object
) -> None:
    if (
        not isinstance(exchange, str)
        or not exchange.strip()
        or (
            product is not None
            and (not isinstance(product, str) or not product.strip())
        )
    ):
        raise SessionAuthorityError(
            check="authority_match_identity",
            reason="matching identity values must be nonempty strings",
            row_identity={"exchange": exchange, "product": product},
        )
    if type(trade_date) is not date:
        raise SessionAuthorityError(
            check="authority_match_date",
            reason="matching trade_date must be an actual date",
            row_identity={
                "exchange": exchange,
                "product": product,
                "trade_date": trade_date,
            },
            context={"type": type(trade_date).__name__},
        )


def matching_ranges(
    rows: Iterable[EffectiveAuthorityRange],
    exchange: str,
    product: str,
    trade_date: date,
) -> tuple[EffectiveAuthorityRange, ...]:
    """Return the one deterministic inclusive range match, or fail on multiplicity."""
    _validate_match_query(exchange=exchange, product=product, trade_date=trade_date)
    matches = tuple(
        sorted(
            (
                row
                for row in rows
                if row.exchange == exchange
                and row.product == product
                and row.effective_start <= trade_date
                and (row.effective_end is None or trade_date <= row.effective_end)
            ),
            key=_range_sort_key,
        )
    )
    if len(matches) > 1:
        raise SessionAuthorityError(
            check="authority_match_cardinality",
            reason="expected at most one effective authority range match",
            row_identity={
                "exchange": exchange,
                "product": product,
                "trade_date": trade_date.isoformat(),
            },
            context={"match_count": len(matches)},
        )
    return matches


def matching_session_exceptions(
    rows: Iterable[SessionException], exchange: str, trade_date: date
) -> tuple[SessionException, ...]:
    """Return the one deterministic exchange-date match, or fail on multiplicity."""
    _validate_match_query(exchange=exchange, product=None, trade_date=trade_date)
    matches = tuple(
        sorted(
            (
                row
                for row in rows
                if row.exchange == exchange and row.trade_date == trade_date
            ),
            key=_session_exception_sort_key,
        )
    )
    if len(matches) > 1:
        raise SessionAuthorityError(
            check="authority_match_cardinality",
            reason="expected at most one session exception match",
            row_identity={
                "exchange": exchange,
                "trade_date": trade_date.isoformat(),
            },
            context={"match_count": len(matches)},
        )
    return matches


def authorize_night_observation(
    authority: SessionAuthority,
    *,
    exchange: str,
    product: str,
    trade_date: date,
    observed_night_start: str,
    observed_night_end: str,
) -> SessionException | None:
    """Bidirectionally reconcile an observation against repository authority."""
    _validate_match_query(exchange=exchange, product=product, trade_date=trade_date)
    identity = {
        "exchange": exchange,
        "product": product,
        "trade_date": trade_date.isoformat(),
    }
    try:
        parse_night_interval(observed_night_start, observed_night_end)
    except ValueError as exc:
        raise SessionAuthorityError(
            check="night_observation_value",
            reason=str(exc),
            row_identity=identity,
            context={
                "observed_night_start": observed_night_start,
                "observed_night_end": observed_night_end,
            },
        ) from exc

    regimes = matching_ranges(authority.day_only_regimes, exchange, product, trade_date)
    exceptions = matching_session_exceptions(
        authority.session_exceptions, exchange, trade_date
    )
    observed = (observed_night_start, observed_night_end)
    if regimes:
        if observed == ("none", "none") and not exceptions:
            return None
    elif exceptions:
        expected = (exceptions[0].night_start, exceptions[0].night_end)
        if observed == expected:
            return exceptions[0]
    elif observed_night_start == "21:00" and observed_night_end in _NORMAL_NIGHT_ENDS:
        return None
    raise SessionAuthorityError(
        check="night_authority_conflict",
        reason="observed night interval conflicts with repository authority",
        row_identity=identity,
        context={
            "observed_night_start": observed_night_start,
            "observed_night_end": observed_night_end,
            "day_only_matches": len(regimes),
            "session_exception_matches": len(exceptions),
        },
    )
