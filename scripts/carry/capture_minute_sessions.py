"""Capture versioned commodity session rules from bounded minute observations."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.curve import aggregate_product_liquidity
from cta_carry.minute_pg_source import (
    MinuteCandidate,
    PublicMinuteSource,
    minute_contract_identity,
)
from cta_carry.minute_sessions import (
    SESSION_RULES_CAPTURE_START,
    SESSION_RULES_VERSION,
    SessionRule,
    build_trading_slots,
    load_session_rules,
    resolve_session_rule,
    validate_capture_coverage,
)
from cta_carry.pg_source import (
    load_public_carry_data,
    load_public_product_history_starts,
)
from cta_carry.session_authority import (
    EffectiveAuthorityRange,
    NoNightDate,
    SessionAuthority,
    SessionAuthorityError,
    authorize_night_observation,
    load_session_authority,
    matching_ranges,
    validate_no_night_calendar,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CSV_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_end",
    "version",
)
BOUNDARY_COLUMNS = (
    "night_first",
    "night_last",
    "day_1_first",
    "day_1_last",
    "day_2_first",
    "day_2_last",
    "day_3_first",
    "day_3_last",
)
ALLOWED_NIGHT_ENDS = ("none", "23:00", "23:30", "01:00", "02:30")
AuditKey = tuple[str, str, date]
_RAW_EXCLUSION_IDENTITY = re.compile(
    r"^(?P<product>[A-Za-z]+)(?P<delivery>[0-9]{3,4})"
    r"(?P<marker>F|TAS)?\.(?P<suffix>[A-Za-z]+)$",
    flags=re.IGNORECASE,
)
_DAILY_SUFFIX_TO_EXCHANGE = {
    "SHF": "SHFE",
    "DCE": "DCE",
    "CZC": "CZCE",
    "INE": "INE",
    "GFE": "GFEX",
}
HISTORY_EXCEPTIONS_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "carry_liquidity_history_exceptions.csv"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NO_NIGHT_PATH = REPOSITORY_ROOT / "config" / "carry_minute_no_night_dates.csv"
DAY_ONLY_PATH = REPOSITORY_ROOT / "config" / "carry_minute_day_only_regimes.csv"
SESSION_RULES_PATH = REPOSITORY_ROOT / "config" / "carry_minute_sessions.csv"


@dataclass(frozen=True)
class AuditKeySets:
    normalized_keys: frozenset[AuditKey]
    in_pool_keys: frozenset[AuditKey]
    audit_universe_keys: frozenset[AuditKey]
    audit_keys: frozenset[AuditKey]


@dataclass(frozen=True)
class DefaultLiquidityAudit:
    config: CarryConfig
    global_calendar: tuple[date, ...]
    key_sets: AuditKeySets
    in_pool_source_keys: frozenset[AuditKey]
    history_status_by_key: Mapping[AuditKey, str]
    candidates: tuple["CapturedCandidate", ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "global_calendar", tuple(self.global_calendar))
        object.__setattr__(
            self, "in_pool_source_keys", frozenset(self.in_pool_source_keys)
        )
        object.__setattr__(
            self,
            "history_status_by_key",
            MappingProxyType(dict(self.history_status_by_key)),
        )
        object.__setattr__(self, "candidates", tuple(self.candidates))


class SessionCaptureError(ValueError):
    """Raised when empirical boundaries cannot define one exact session rule."""


@dataclass(frozen=True, order=True)
class AmbiguityRecord:
    trade_date: date
    exchange: str
    product: str
    check: str
    reason: str


@dataclass(frozen=True)
class CoverageReport:
    rows: tuple[Mapping[str, Any], ...]
    unknown_date_unkeyable_rows: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rows",
            tuple(MappingProxyType(dict(row)) for row in self.rows),
        )

    @property
    def has_unkeyable(self) -> bool:
        return self.unknown_date_unkeyable_rows != 0 or any(
            row["normalization_unkeyable_rows"] != 0 for row in self.rows
        )


@dataclass(frozen=True)
class _CaptureOutcome:
    counts: tuple[int, int, int, int]
    blocked: bool


@dataclass(frozen=True)
class CapturedCandidate:
    candidate: MinuteCandidate
    previous_trade_date: date
    causal_in_pool_date: date | None = None
    selection_source: str = "target_day_main"

    def __post_init__(self) -> None:
        role = self.candidate.candidate_role
        if type(role) is not str or role != "session_representative":
            return
        if type(self.causal_in_pool_date) is not date:
            raise SessionCaptureError(
                "captured_candidate_metadata: session representative outer "
                "causal_in_pool_date must be a concrete date"
            )
        if (
            type(self.selection_source) is not str
            or not self.selection_source.strip()
            or self.selection_source.casefold() == "unspecified"
        ):
            raise SessionCaptureError(
                "captured_candidate_metadata: session representative outer "
                "selection_source must be explicit"
            )
        if self.causal_in_pool_date != self.candidate.causal_in_pool_date:
            raise SessionCaptureError(
                "captured_candidate_metadata: outer causal_in_pool_date differs "
                "from the MinuteCandidate provenance"
            )
        if self.selection_source != self.candidate.selection_source:
            raise SessionCaptureError(
                "captured_candidate_metadata: outer selection_source differs "
                "from the MinuteCandidate provenance"
            )


@dataclass(frozen=True)
class RepresentativeContract:
    daily_contract: str
    minute_symbol: str


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)


def _require_capture_dates(start: date, end: date) -> None:
    if type(start) is not date or type(end) is not date or start > end:
        raise SessionCaptureError("start and end must be ordered concrete dates")


def _ordered_calendar(values: Sequence[date]) -> tuple[date, ...]:
    if any(type(value) is not date for value in values):
        raise SessionCaptureError("global calendar must contain concrete dates")
    ordered = tuple(sorted(set(values)))
    if not ordered:
        raise SessionCaptureError("global calendar must not be empty")
    return ordered


def _validate_audit_key(key: object) -> AuditKey:
    if (
        not isinstance(key, tuple)
        or len(key) != 3
        or not isinstance(key[0], str)
        or not key[0].strip()
        or not isinstance(key[1], str)
        or not key[1].strip()
        or type(key[2]) is not date
    ):
        raise SessionCaptureError(f"invalid audit key: {key!r}")
    return key[0].strip().upper(), key[1].strip().upper(), key[2]


def raw_exclusion_product_day_identity(
    object_id: object,
    trade_date: object,
) -> AuditKey:
    """Recover only the product-day identity needed to audit a raw exclusion."""
    if type(trade_date) is not date:
        raise SessionCaptureError(
            "raw_exclusion_identity: trade_date must be an exact date"
        )
    if type(object_id) is not str:
        raise SessionCaptureError(
            "raw_exclusion_identity: object_id must be a string"
        )
    match = _RAW_EXCLUSION_IDENTITY.fullmatch(object_id.strip())
    if match is None:
        raise SessionCaptureError(
            "raw_exclusion_identity: object_id has no strict product/suffix identity"
        )
    suffix = match.group("suffix").upper()
    try:
        exchange = _DAILY_SUFFIX_TO_EXCHANGE[suffix]
    except KeyError as exc:
        raise SessionCaptureError(
            f"raw_exclusion_identity: unsupported daily suffix {suffix!r}"
        ) from exc
    return exchange, match.group("product").upper(), trade_date


def _assert_audit_key_subsets(key_sets: AuditKeySets) -> None:
    if not key_sets.in_pool_keys <= key_sets.normalized_keys:
        raise SessionCaptureError("audit_key_subset: in_pool not normalized")
    if not key_sets.normalized_keys <= key_sets.audit_universe_keys:
        raise SessionCaptureError("audit_key_subset: normalized outside universe")
    if not key_sets.in_pool_keys <= key_sets.audit_keys:
        raise SessionCaptureError("audit_key_subset: in_pool not audited")
    if not key_sets.audit_keys <= key_sets.audit_universe_keys:
        raise SessionCaptureError("audit_key_subset: audit outside universe")


def build_audit_key_sets(
    *,
    normalized_keys: Iterable[AuditKey],
    in_pool_keys: Iterable[AuditKey],
    global_calendar: Sequence[date],
    start: date,
    end: date,
) -> AuditKeySets:
    """Emit every in-pool key through two global trading-date successors."""
    _require_capture_dates(start, end)
    calendar = _ordered_calendar(global_calendar)
    position_by_date = {
        trade_date: position for position, trade_date in enumerate(calendar)
    }
    normalized = frozenset(
        key
        for raw in normalized_keys
        if start <= (key := _validate_audit_key(raw))[2] <= end
    )
    pool_sources = frozenset(_validate_audit_key(raw) for raw in in_pool_keys)

    emitted: set[AuditKey] = set()
    for exchange, product, source_date in pool_sources:
        try:
            source_position = position_by_date[source_date]
        except KeyError as exc:
            raise SessionCaptureError(
                f"audit pool source date is outside global calendar: {source_date}"
            ) from exc
        for target_date in calendar[source_position : source_position + 3]:
            if start <= target_date <= end:
                emitted.add((exchange, product, target_date))

    requested_pool = frozenset(key for key in pool_sources if start <= key[2] <= end)
    audit_keys = frozenset(emitted)
    key_sets = AuditKeySets(
        normalized_keys=normalized,
        in_pool_keys=requested_pool,
        audit_universe_keys=frozenset(normalized.union(audit_keys)),
        audit_keys=audit_keys,
    )
    _assert_audit_key_subsets(key_sets)
    return key_sets


def _build_representative_index(
    prices: pd.DataFrame,
) -> dict[AuditKey, RepresentativeContract]:
    """Validate all identities, then retain one ranked row per product-day."""
    required = {"trade_date", "product", "contract", "oi", "volume"}
    missing = sorted(required.difference(prices.columns))
    if missing:
        raise SessionCaptureError(f"daily prices missing required columns: {missing}")

    ranking = prices.loc[
        :, ["trade_date", "product", "contract", "oi", "volume"]
    ].copy()
    exchanges: list[str] = []
    normalized_products: list[str] = []
    for trade_date, declared_product, contract in ranking.loc[
        :, ["trade_date", "product", "contract"]
    ].itertuples(index=False, name=None):
        if type(trade_date) is not date:
            raise SessionCaptureError("daily trade_date values must be concrete dates")
        product, _, exchange = minute_contract_identity(contract, trade_date)
        if (
            not isinstance(declared_product, str)
            or declared_product.strip().upper() != product
        ):
            raise SessionCaptureError(
                f"{trade_date} {contract}: normalized product identity conflicts"
            )
        exchanges.append(exchange)
        normalized_products.append(product)
    ranking["exchange"] = exchanges
    ranking["product"] = normalized_products
    representatives = (
        ranking.sort_values(
            ["exchange", "product", "trade_date", "oi", "volume", "contract"],
            ascending=[True, True, True, False, False, True],
            kind="mergesort",
        )
        .drop_duplicates(["exchange", "product", "trade_date"], keep="first")
        .reset_index(drop=True)
    )

    result: dict[AuditKey, RepresentativeContract] = {}
    for row in representatives.itertuples(index=False):
        product, minute_symbol, exchange = minute_contract_identity(
            row.contract, row.trade_date
        )
        result[(exchange, product, row.trade_date)] = RepresentativeContract(
            daily_contract=row.contract,
            minute_symbol=minute_symbol,
        )
    return result


def _select_audit_candidates_from_index(
    representative_index: Mapping[AuditKey, RepresentativeContract],
    *,
    audit_keys: Iterable[AuditKey],
    in_pool_source_keys: Iterable[AuditKey],
    global_calendar: Sequence[date],
) -> tuple[CapturedCandidate, ...]:
    calendar = _ordered_calendar(global_calendar)
    position_by_date = {
        trade_date: position for position, trade_date in enumerate(calendar)
    }
    previous_by_date = {
        current: previous
        for previous, current in zip(calendar, calendar[1:], strict=False)
    }
    pool_sources = frozenset(_validate_audit_key(raw) for raw in in_pool_source_keys)
    pool_sources_by_identity: dict[tuple[str, str], set[date]] = {}
    for exchange, product, source_date in pool_sources:
        pool_sources_by_identity.setdefault((exchange, product), set()).add(source_date)

    selected: list[CapturedCandidate] = []
    for key in sorted(
        (_validate_audit_key(raw) for raw in audit_keys),
        key=lambda item: (item[2], item[0], item[1]),
    ):
        exchange, product, trade_date = key
        if trade_date not in previous_by_date:
            raise SessionCaptureError(
                f"audit target lacks previous global trading date: {trade_date}"
            )
        target_position = position_by_date[trade_date]
        identity_sources = pool_sources_by_identity.get((exchange, product), set())
        producer_dates = tuple(
            calendar[position]
            for position in range(max(0, target_position - 2), target_position + 1)
            if calendar[position] in identity_sources
        )
        if not producer_dates:
            raise SessionCaptureError(
                f"audit key has no causal in-pool source: {key!r}"
            )
        causal_date = producer_dates[-1]
        representative = representative_index.get(key)
        if representative is not None:
            selection_source = "target_day_main"
        else:
            representative = representative_index[(exchange, product, causal_date)]
            selection_source = "causal_in_pool_main"

        previous_trade_date = previous_by_date[trade_date]
        selected.append(
            CapturedCandidate(
                candidate=MinuteCandidate(
                    trade_date=trade_date,
                    product=product,
                    daily_contract=representative.daily_contract,
                    minute_symbol=representative.minute_symbol,
                    exchange=exchange,
                    window_start=_at(previous_trade_date, 21, 0),
                    window_end=_at(trade_date, 15, 1),
                    candidate_role="session_representative",
                    causal_in_pool_date=causal_date,
                    selection_source=selection_source,
                ),
                previous_trade_date=previous_trade_date,
                causal_in_pool_date=causal_date,
                selection_source=selection_source,
            )
        )
    return tuple(selected)


def select_audit_candidates(
    prices: pd.DataFrame,
    *,
    audit_keys: Iterable[AuditKey],
    in_pool_source_keys: Iterable[AuditKey],
    global_calendar: Sequence[date],
) -> tuple[CapturedCandidate, ...]:
    """Select target-day representatives without dropping synthetic exit keys."""
    return _select_audit_candidates_from_index(
        _build_representative_index(prices),
        audit_keys=audit_keys,
        in_pool_source_keys=in_pool_source_keys,
        global_calendar=global_calendar,
    )


def _finite_liquidity(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _history_start_by_product(history_starts: pd.DataFrame) -> dict[str, date]:
    required = {"product", "first_trade_date"}
    missing = sorted(required.difference(history_starts.columns))
    if missing:
        raise SessionCaptureError(f"product history starts missing columns: {missing}")
    result: dict[str, date] = {}
    for row in history_starts.to_dict("records"):
        product = row["product"]
        first_trade_date = row["first_trade_date"]
        if not isinstance(product, str) or not product.strip():
            raise SessionCaptureError("product history starts contain an empty product")
        if type(first_trade_date) is not date:
            raise SessionCaptureError(
                "product history starts contain a non-date first_trade_date"
            )
        normalized_product = product.strip().upper()
        if normalized_product in result:
            raise SessionCaptureError(
                f"product history starts contain duplicate product {normalized_product}"
            )
        result[normalized_product] = first_trade_date
    return result


def _validate_loaded_history_starts(
    representative_index: Mapping[AuditKey, RepresentativeContract],
    first_by_product: Mapping[str, date],
) -> None:
    loaded_first_by_product: dict[str, date] = {}
    for _, product, trade_date in representative_index:
        current = loaded_first_by_product.get(product)
        if current is None or trade_date < current:
            loaded_first_by_product[product] = trade_date

    for product, loaded_first in sorted(loaded_first_by_product.items()):
        first_history = first_by_product.get(product)
        if first_history is None:
            raise SessionCaptureError(
                "liquidity_history_incomplete: "
                f"product={product} reason=missing_history_start "
                f"loaded_first_trade_date={loaded_first}"
            )
        if first_history > loaded_first:
            raise SessionCaptureError(
                "liquidity_history_incomplete: "
                f"product={product} reason=history_start_after_loaded_data "
                f"first_trade_date={first_history} "
                f"loaded_first_trade_date={loaded_first}"
            )


def _build_default_liquidity_audit(
    prices: pd.DataFrame,
    *,
    history_starts: pd.DataFrame,
    history_exceptions: Iterable[EffectiveAuthorityRange],
    start: date,
    end: date,
    config: CarryConfig,
) -> DefaultLiquidityAudit:
    _require_capture_dates(start, end)
    representative_index = _build_representative_index(prices)
    global_calendar = _ordered_calendar(prices["trade_date"].drop_duplicates().tolist())
    normalized_keys = frozenset(
        key for key in representative_index if start <= key[2] <= end
    )
    if not normalized_keys:
        raise SessionCaptureError("requested range contains no normalized product-days")

    liquidity = aggregate_product_liquidity(prices, config)
    liquidity_by_product_date = {
        (row.product, row.trade_date): row for row in liquidity.itertuples(index=False)
    }
    in_pool_source_keys = frozenset(
        key
        for key in representative_index
        if bool(liquidity_by_product_date[(key[1], key[2])].in_pool)
    )

    daily_load_start = start - timedelta(days=config.prewarm_calendar_days)
    first_by_product = _history_start_by_product(history_starts)
    _validate_loaded_history_starts(
        representative_index,
        first_by_product,
    )
    exception_rows = tuple(history_exceptions)
    first_target_by_identity: dict[tuple[str, str], date] = {}
    for exchange, product, trade_date in sorted(
        normalized_keys, key=lambda item: (item[0], item[1], item[2])
    ):
        first_target_by_identity.setdefault((exchange, product), trade_date)

    history_status_by_key: dict[AuditKey, str] = {}
    for (exchange, product), first_target in first_target_by_identity.items():
        key = (exchange, product, first_target)
        first_history = first_by_product[product]
        liquidity_row = liquidity_by_product_date[(product, first_target)]
        if _finite_liquidity(liquidity_row.liquidity_mean):
            status = "finite"
        elif first_history >= daily_load_start:
            status = "insufficient_since_inception"
        else:
            if matching_ranges(exception_rows, exchange, product, first_target):
                status = "authorized_history_gap"
            else:
                raise SessionCaptureError(
                    "liquidity_history_incomplete: "
                    f"exchange={exchange} product={product} "
                    f"trade_date={first_target} "
                    f"daily_load_start={daily_load_start} "
                    f"first_trade_date={first_history}"
                )
        history_status_by_key[key] = status
        if status != "finite" and key in in_pool_source_keys:
            raise SessionCaptureError(
                f"{status} product-day must remain out of pool: {key!r}"
            )

    key_sets = build_audit_key_sets(
        normalized_keys=normalized_keys,
        in_pool_keys=in_pool_source_keys,
        global_calendar=global_calendar,
        start=start,
        end=end,
    )
    candidates = _select_audit_candidates_from_index(
        representative_index,
        audit_keys=key_sets.audit_keys,
        in_pool_source_keys=in_pool_source_keys,
        global_calendar=global_calendar,
    )
    return DefaultLiquidityAudit(
        config=config,
        global_calendar=global_calendar,
        key_sets=key_sets,
        in_pool_source_keys=in_pool_source_keys,
        history_status_by_key=history_status_by_key,
        candidates=candidates,
    )


def build_default_liquidity_audit(
    prices: pd.DataFrame,
    *,
    history_starts: pd.DataFrame,
    history_exceptions: Iterable[EffectiveAuthorityRange],
    start: date,
    end: date,
) -> DefaultLiquidityAudit:
    """Build the minute audit envelope from the exact baseline Carry config."""
    return _build_default_liquidity_audit(
        prices,
        history_starts=history_starts,
        history_exceptions=history_exceptions,
        start=start,
        end=end,
        config=CarryConfig(),
    )


def select_session_candidates(
    prices: pd.DataFrame,
    *,
    start: date,
    end: date,
) -> tuple[CapturedCandidate, ...]:
    """Select one deterministic highest-OI concrete contract per product-day."""
    _require_capture_dates(start, end)
    required = {"trade_date", "product", "contract", "oi", "volume"}
    missing = sorted(required.difference(prices.columns))
    if missing:
        raise SessionCaptureError(f"daily prices missing required columns: {missing}")
    if prices.empty:
        raise SessionCaptureError("daily prices contain no candidate rows")

    frame = prices.loc[:, sorted(required)].copy()
    if any(type(value) is not date for value in frame["trade_date"]):
        raise SessionCaptureError("daily trade_date values must be concrete dates")
    calendar = sorted(set(frame["trade_date"]))
    previous_by_date = {
        current: previous
        for previous, current in zip(calendar, calendar[1:], strict=False)
    }
    frame = frame.loc[frame["trade_date"].between(start, end)].copy()
    if frame.empty:
        raise SessionCaptureError("requested range contains no daily candidates")
    missing_lag = sorted(set(frame["trade_date"]).difference(previous_by_date))
    if missing_lag:
        raise SessionCaptureError(
            "daily history does not contain the previous trading date for "
            + ", ".join(day.isoformat() for day in missing_lag)
        )

    frame = frame.sort_values(
        ["trade_date", "product", "oi", "volume", "contract"],
        ascending=[True, True, False, False, True],
        kind="mergesort",
    ).drop_duplicates(["trade_date", "product"], keep="first")

    selected: list[CapturedCandidate] = []
    for row in frame.itertuples(index=False):
        trade_date = row.trade_date
        product, minute_symbol, exchange = minute_contract_identity(
            row.contract,
            trade_date,
        )
        if type(row.product) is not str or row.product.strip().upper() != product:
            raise SessionCaptureError(
                f"{trade_date} {row.contract}: normalized product identity conflicts"
            )
        previous_trade_date = previous_by_date[trade_date]
        selected.append(
            CapturedCandidate(
                candidate=MinuteCandidate(
                    trade_date=trade_date,
                    product=product,
                    daily_contract=row.contract,
                    minute_symbol=minute_symbol,
                    exchange=exchange,
                    window_start=_at(previous_trade_date, 21, 0),
                    window_end=_at(trade_date, 15, 1),
                    candidate_role="session_representative",
                    causal_in_pool_date=trade_date,
                    selection_source="target_day_main",
                ),
                previous_trade_date=previous_trade_date,
                causal_in_pool_date=trade_date,
                selection_source="target_day_main",
            )
        )
    return tuple(selected)


def capture_session_boundaries(
    source: PublicMinuteSource,
    selected: Sequence[CapturedCandidate],
) -> pd.DataFrame:
    """Query grouped boundary observations in target-trade-date calendar months."""
    if not selected:
        raise SessionCaptureError("at least one session candidate is required")
    groups: dict[tuple[int, int], list[CapturedCandidate]] = {}
    for item in selected:
        key = (item.candidate.trade_date.year, item.candidate.trade_date.month)
        groups.setdefault(key, []).append(item)

    frames: list[pd.DataFrame] = []
    for month in sorted(groups):
        batch = groups[month]
        candidates = [item.candidate for item in batch]
        lower = min(item.window_start for item in candidates)
        upper = max(item.window_end for item in candidates)
        frame = source.iter_session_boundaries(candidates, lower=lower, upper=upper)
        lag_by_identity = {
            (
                item.candidate.trade_date,
                item.candidate.product,
                item.candidate.daily_contract,
            ): item.previous_trade_date
            for item in batch
        }
        frame = frame.copy()
        frame["previous_trade_date"] = [
            lag_by_identity[(row.trade_date, row.product, row.daily_contract)]
            for row in frame.itertuples(index=False)
        ]
        frames.append(frame)
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["trade_date", "product", "daily_contract"], kind="mergesort")
        .reset_index(drop=True)
    )


def _missing_boundary(value: Any) -> bool:
    return value is None or value is pd.NaT


def _boundary_error(row: Any, field: str, expected: datetime | None) -> None:
    actual = row[field]
    raise SessionCaptureError(
        f"{row['trade_date']} {row['exchange']} {row['product']} "
        f"{row['daily_contract']} {field}: expected {expected!r}; got {actual!r}"
    )


def _require_boundary(row: Any, field: str, expected: datetime) -> None:
    actual = row[field]
    if not isinstance(actual, datetime) or actual.tzinfo is None:
        _boundary_error(row, field, expected)
    try:
        matches = actual.astimezone(SHANGHAI) == expected
    except (TypeError, ValueError, OverflowError):
        matches = False
    if not matches:
        _boundary_error(row, field, expected)


def classify_session_boundary(row: Any) -> str:
    """Classify one exact empirical row into the four supported night clocks."""
    trade_date = row["trade_date"]
    previous_trade_date = row["previous_trade_date"]
    if type(trade_date) is not date or type(previous_trade_date) is not date:
        raise SessionCaptureError("boundary trade dates must be concrete dates")
    expected_day = {
        "day_1_first": _at(trade_date, 9, 0),
        "day_1_last": _at(trade_date, 10, 14),
        "day_2_first": _at(trade_date, 10, 30),
        "day_2_last": _at(trade_date, 11, 29),
        "day_3_first": _at(trade_date, 13, 30),
        "day_3_last": _at(trade_date, 14, 59),
    }
    for field, expected in expected_day.items():
        _require_boundary(row, field, expected)

    night_first = row["night_first"]
    night_last = row["night_last"]
    if _missing_boundary(night_first) and _missing_boundary(night_last):
        return "none"
    if _missing_boundary(night_first) or _missing_boundary(night_last):
        _boundary_error(
            row,
            "night_first" if _missing_boundary(night_first) else "night_last",
            None,
        )
    _require_boundary(row, "night_first", _at(previous_trade_date, 21, 0))
    after_midnight = previous_trade_date.fromordinal(
        previous_trade_date.toordinal() + 1
    )
    endings = {
        "23:00": _at(previous_trade_date, 22, 59),
        "23:30": _at(previous_trade_date, 23, 29),
        "01:00": _at(after_midnight, 0, 59),
        "02:30": _at(after_midnight, 2, 29),
    }
    for label, expected in endings.items():
        try:
            matches = night_last.astimezone(SHANGHAI) == expected
        except (AttributeError, TypeError, ValueError, OverflowError):
            matches = False
        if matches:
            return label
    _boundary_error(row, "night_last", None)
    raise AssertionError("unreachable")


def classify_authorized_boundaries(
    boundaries: pd.DataFrame,
    authority: SessionAuthority,
) -> tuple[pd.DataFrame, tuple[AmbiguityRecord, ...]]:
    """Classify every boundary and reconcile each result with authority."""
    identity_columns = {"exchange", "product", "trade_date"}
    missing = sorted(identity_columns.difference(boundaries.columns))
    if missing:
        raise SessionCaptureError(f"captured boundaries missing columns: {missing}")

    classified: list[dict[str, Any]] = []
    ambiguous: list[AmbiguityRecord] = []
    ordered = boundaries.sort_values(
        ["trade_date", "exchange", "product", "daily_contract"],
        kind="mergesort",
    )
    for row in ordered.to_dict("records"):
        try:
            night_end = classify_session_boundary(row)
            authorize_night_observation(
                authority,
                exchange=row["exchange"],
                product=row["product"],
                trade_date=row["trade_date"],
                observed_night_end=night_end,
            )
        except (SessionCaptureError, SessionAuthorityError) as exc:
            ambiguous.append(
                AmbiguityRecord(
                    trade_date=row["trade_date"],
                    exchange=row["exchange"],
                    product=row["product"],
                    check=getattr(exc, "check", "empirical_boundary"),
                    reason=str(exc),
                )
            )
            continue
        classified.append(
            {
                "exchange": row["exchange"],
                "product": row["product"],
                "trade_date": row["trade_date"],
                "night_end": night_end,
            }
        )
    return (
        pd.DataFrame(
            classified,
            columns=["exchange", "product", "trade_date", "night_end"],
        ),
        tuple(sorted(ambiguous)),
    )


def validate_capture_no_night_calendar(
    rows: Sequence[NoNightDate],
    global_calendar: Sequence[date],
) -> None:
    """Validate only authority rows whose target date is in the loaded calendar."""
    calendar = _ordered_calendar(global_calendar)
    loaded_dates = frozenset(calendar)
    relevant = tuple(row for row in rows if row.trade_date in loaded_dates)
    validate_no_night_calendar(relevant, calendar)


def authorized_history_gap_lines(
    history_status_by_key: Mapping[AuditKey, str],
    history_exceptions: Iterable[EffectiveAuthorityRange],
) -> tuple[str, ...]:
    """Render exact authority provenance only for history gaps actually used."""
    exception_rows = tuple(history_exceptions)
    authorized_keys = sorted(
        (
            _validate_audit_key(key)
            for key, status in history_status_by_key.items()
            if status == "authorized_history_gap"
        ),
        key=lambda item: (item[2], item[0], item[1]),
    )
    lines: list[str] = []
    for exchange, product, trade_date in authorized_keys:
        matches = matching_ranges(
            exception_rows,
            exchange,
            product,
            trade_date,
        )
        if len(matches) != 1:
            raise SessionCaptureError(
                "authorized_history_gap_cardinality: "
                f"key={exchange}/{product}/{trade_date.isoformat()} "
                f"matches={len(matches)}"
            )
        authority_range = matches[0]
        effective_end = (
            authority_range.effective_end.isoformat()
            if authority_range.effective_end is not None
            else "open"
        )
        lines.append(
            "authorized_history_gap "
            f"key={exchange}/{product}/{trade_date.isoformat()} "
            "authority_effective_start="
            f"{authority_range.effective_start.isoformat()} "
            f"authority_effective_end={effective_end} "
            f"reason={authority_range.reason!r} "
            f"source_url={authority_range.source_url!r}"
        )
    return tuple(lines)


def coverage_report(
    *,
    data_quality: pd.DataFrame,
    key_sets: AuditKeySets,
    start: date,
    end: date,
) -> CoverageReport:
    """Derive yearly coverage and normalization exclusions independently."""
    _require_capture_dates(start, end)
    _assert_audit_key_subsets(key_sets)
    required = {"object_type", "object_id", "trade_date", "status", "action"}
    missing = sorted(required.difference(data_quality.columns))
    if missing:
        raise SessionCaptureError(
            f"data quality audit missing required columns: {missing}"
        )

    excluded_keys: set[AuditKey] = set()
    unkeyable_by_year: dict[int, int] = {}
    unknown_date_rows = 0
    for row in data_quality.to_dict("records"):
        if row["status"] != "excluded" or row["action"] != "exclude_candidate":
            continue
        trade_date = row["trade_date"]
        if type(trade_date) is not date:
            unknown_date_rows += 1
            continue
        if not start <= trade_date <= end:
            continue
        try:
            key = raw_exclusion_product_day_identity(
                row["object_id"], trade_date
            )
        except SessionCaptureError:
            unkeyable_by_year[trade_date.year] = (
                unkeyable_by_year.get(trade_date.year, 0) + 1
            )
            continue
        if key not in key_sets.normalized_keys:
            excluded_keys.add(key)

    rows: list[dict[str, Any]] = []
    total_audited_days = 0
    for year in range(start.year, end.year + 1):
        normalized = frozenset(
            key for key in key_sets.normalized_keys if key[2].year == year
        )
        in_pool = frozenset(
            key for key in key_sets.in_pool_keys if key[2].year == year
        )
        universe = frozenset(
            key for key in key_sets.audit_universe_keys if key[2].year == year
        )
        audited = frozenset(
            key for key in key_sets.audit_keys if key[2].year == year
        )
        if not in_pool <= normalized <= universe:
            raise SessionCaptureError(
                f"coverage_key_subset: invalid normalized chain for year={year}"
            )
        if not in_pool <= audited <= universe:
            raise SessionCaptureError(
                f"coverage_key_subset: invalid audit chain for year={year}"
            )
        total_audited_days += len(audited)
        rows.append(
            {
                "coverage_year": year,
                "all_product_days": len(normalized),
                "in_pool_days": len(in_pool),
                "in_pool_ratio": (
                    f"{len(in_pool) / len(normalized):.6f}"
                    if normalized
                    else "0.000000"
                ),
                "audit_universe_days": len(universe),
                "audited_days": len(audited),
                "audited_ratio": (
                    f"{len(audited) / len(universe):.6f}"
                    if universe
                    else "0.000000"
                ),
                "normalization_excluded_product_days": sum(
                    key[2].year == year for key in excluded_keys
                ),
                "normalization_unkeyable_rows": unkeyable_by_year.get(year, 0),
            }
        )
    if total_audited_days != len(key_sets.audit_keys):
        raise SessionCaptureError(
            "coverage_audited_total: yearly audited days differ from audit keys"
        )
    return CoverageReport(
        rows=tuple(rows),
        unknown_date_unkeyable_rows=unknown_date_rows,
    )


def collapse_session_rules(
    classified: pd.DataFrame,
    *,
    global_calendar: Sequence[date],
    audit_keys: Iterable[AuditKey],
) -> list[dict[str, Any]]:
    """Collapse adjacent audited product dates with identical observed clocks."""
    required = {"exchange", "product", "trade_date", "night_end"}
    missing = sorted(required.difference(classified.columns))
    if missing:
        raise SessionCaptureError(f"classified boundaries missing columns: {missing}")
    ordered = classified.loc[:, sorted(required)].sort_values(
        ["exchange", "product", "trade_date"], kind="mergesort"
    )
    if ordered.duplicated(["exchange", "product", "trade_date"]).any():
        raise SessionCaptureError(
            "classified boundaries contain duplicate product-days"
        )
    if ordered.empty:
        raise SessionCaptureError("no classified boundaries are available")

    calendar = _ordered_calendar(global_calendar)
    position_by_date = {day: position for position, day in enumerate(calendar)}
    audited = frozenset(_validate_audit_key(key) for key in audit_keys)
    classified_keys = frozenset(
        (row.exchange, row.product, row.trade_date)
        for row in ordered.itertuples(index=False)
    )
    if classified_keys != audited:
        raise SessionCaptureError(
            "boundary_keys_mismatch: classified boundary keys must equal audit keys"
        )
    missing_calendar = sorted({key[2] for key in audited}.difference(calendar))
    if missing_calendar:
        raise SessionCaptureError(
            "audit keys fall outside global calendar: "
            + ", ".join(day.isoformat() for day in missing_calendar)
        )

    rules: list[dict[str, Any]] = []
    for (exchange, product), group in ordered.groupby(
        ["exchange", "product"], sort=True
    ):
        records = list(group.itertuples(index=False))
        start = records[0].trade_date
        night_end = records[0].night_end
        if night_end not in ALLOWED_NIGHT_ENDS:
            raise SessionCaptureError(f"unsupported night_end {night_end!r}")
        previous_date = start
        for record in records[1:]:
            if record.night_end not in ALLOWED_NIGHT_ENDS:
                raise SessionCaptureError(f"unsupported night_end {record.night_end!r}")
            adjacent = (
                position_by_date[record.trade_date]
                == position_by_date[previous_date] + 1
            )
            if record.night_end != night_end or not adjacent:
                rules.append(
                    {
                        "exchange": exchange,
                        "product": product,
                        "effective_start": start,
                        "effective_end": previous_date,
                        "night_end": night_end,
                        "version": SESSION_RULES_VERSION,
                    }
                )
                start = record.trade_date
                night_end = record.night_end
            previous_date = record.trade_date
        rules.append(
            {
                "exchange": exchange,
                "product": product,
                "effective_start": start,
                "effective_end": previous_date,
                "night_end": night_end,
                "version": SESSION_RULES_VERSION,
            }
        )
    return sorted(
        rules,
        key=lambda row: (row["exchange"], row["product"], row["effective_start"]),
    )


def validate_boundary_keys(
    boundaries: pd.DataFrame,
    audit_keys: Iterable[AuditKey],
) -> frozenset[AuditKey]:
    """Require exactly one empirical boundary row for every audit key."""
    required = {"exchange", "product", "trade_date"}
    missing_columns = sorted(required.difference(boundaries.columns))
    if missing_columns:
        raise SessionCaptureError(
            f"boundary_schema: missing columns {missing_columns}"
        )
    if boundaries.empty:
        raise SessionCaptureError("boundary_zero_rows: no boundaries returned")
    identity = boundaries.loc[:, ["exchange", "product", "trade_date"]]
    if identity.duplicated().any():
        raise SessionCaptureError(
            "boundary_duplicate_rows: duplicate product-day boundaries"
        )
    boundary_keys = frozenset(
        _validate_audit_key(tuple(row))
        for row in identity.itertuples(index=False, name=None)
    )
    audited = frozenset(_validate_audit_key(key) for key in audit_keys)
    missing = audited.difference(boundary_keys)
    unexpected = boundary_keys.difference(audited)
    if missing:
        raise SessionCaptureError(
            "boundary_missing_keys: " + repr(sorted(missing))
        )
    if unexpected:
        raise SessionCaptureError(
            "boundary_unexpected_keys: " + repr(sorted(unexpected))
        )
    return boundary_keys


def require_publishable_coverage(report: CoverageReport) -> None:
    if report.has_unkeyable:
        raise SessionCaptureError(
            "normalization_unkeyable: publication requires all raw exclusions "
            "to have an auditable product-day identity"
        )


def validate_capture_request(
    *,
    start: date,
    backtest_start: date,
    output: Path,
    prewarm_calendar_days: int,
) -> date:
    """Enforce prewarm coverage and the repository asset's fixed start."""
    _require_capture_dates(start, start)
    required_start = validate_capture_coverage(
        capture_start=start,
        backtest_start=backtest_start,
        prewarm_calendar_days=prewarm_calendar_days,
    )
    if (
        Path(output).resolve(strict=False)
        == SESSION_RULES_PATH.resolve(strict=False)
        and start != SESSION_RULES_CAPTURE_START
    ):
        raise SessionCaptureError(
            "repository_capture_start: repository session rules must begin "
            f"{SESSION_RULES_CAPTURE_START.isoformat()}"
        )
    return required_start


def validate_capture_output_paths(
    *,
    output: Path,
    inventory_output: Path,
    audit_report: Path,
) -> None:
    resolved = {
        Path(output).resolve(strict=False),
        Path(inventory_output).resolve(strict=False),
        Path(audit_report).resolve(strict=False),
    }
    if len(resolved) != 3:
        raise SessionCaptureError(
            "capture_output_path_collision: output, inventory, and audit "
            "report paths must be pairwise distinct"
        )


def _validate_authority_hashes(
    authority: SessionAuthority,
    authority_paths: Mapping[str, Path],
) -> None:
    expected_names = {"no_night", "day_only", "history_exception"}
    if set(authority_paths) != expected_names:
        raise SessionCaptureError(
            "authority_hash_paths: expected all three fixed authority assets"
        )
    if set(authority.sha256_by_asset) != expected_names:
        raise SessionCaptureError(
            "authority_hash_manifest: expected all three authority hashes"
        )
    for name in sorted(expected_names):
        try:
            actual = hashlib.sha256(Path(authority_paths[name]).read_bytes()).hexdigest()
        except OSError as exc:
            raise SessionCaptureError(
                f"authority_hash_read: {name} could not be read"
            ) from exc
        if actual != authority.sha256_by_asset[name]:
            raise SessionCaptureError(
                f"authority_hash_mismatch: asset={name} "
                f"expected={authority.sha256_by_asset[name]} actual={actual}"
            )


def expand_rule_keys(
    rules: Sequence[SessionRule],
    *,
    global_calendar: Sequence[date],
) -> frozenset[AuditKey]:
    """Expand loaded inclusive rules only over concrete global trading days."""
    calendar = _ordered_calendar(global_calendar)
    expanded: set[AuditKey] = set()
    for rule in rules:
        for trade_date in calendar:
            if trade_date < rule.effective_start:
                continue
            if rule.effective_end is not None and trade_date > rule.effective_end:
                continue
            expanded.add((rule.exchange, rule.product, trade_date))
    return frozenset(expanded)


def _stage_session_rules(output: Path, rules: Sequence[dict[str, Any]]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for rule in rules:
                writer.writerow(
                    {
                        "exchange": rule["exchange"],
                        "product": rule["product"],
                        "effective_start": rule["effective_start"].isoformat(),
                        "effective_end": rule["effective_end"].isoformat(),
                        "night_end": rule["night_end"],
                        "version": rule["version"],
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _expected_night_end(rule: SessionRule) -> str:
    night_segments = [
        segment
        for segment in rule.segments
        if segment.start_minute < 0 or segment.end_minute <= 150
    ]
    if not night_segments:
        return "none"
    offsets = {
        (-180, -60): "23:00",
        (-180, -30): "23:30",
        (-180, 60): "01:00",
        (-180, 150): "02:30",
    }
    pair = (night_segments[0].start_minute, night_segments[0].end_minute)
    try:
        return offsets[pair]
    except KeyError as exc:
        raise SessionCaptureError(f"unsupported loaded night segment {pair!r}") from exc


def validate_audited_boundaries(
    boundaries: pd.DataFrame,
    rules: Sequence[SessionRule],
) -> None:
    """Require every empirical boundary to occupy exactly one authoritative slot."""
    for row in boundaries.to_dict("records"):
        rule = resolve_session_rule(
            rules,
            row["exchange"],
            row["product"],
            row["trade_date"],
        )
        actual_night_end = classify_session_boundary(row)
        if _expected_night_end(rule) != actual_night_end:
            raise SessionCaptureError(
                f"{row['trade_date']} {row['product']}: captured rule changed after reload"
            )
        slots = build_trading_slots(
            row["trade_date"],
            row["previous_trade_date"],
            rule,
        )
        for column in BOUNDARY_COLUMNS:
            timestamp = row[column]
            if _missing_boundary(timestamp):
                continue
            if sum(slot == timestamp for slot in slots) != 1:
                raise SessionCaptureError(
                    f"{row['trade_date']} {row['product']} {column}: "
                    "timestamp does not map to exactly one authoritative slot"
                )


def publish_session_rules(
    *,
    output: Path,
    rule_rows: Sequence[dict[str, Any]],
    boundaries: pd.DataFrame,
    global_calendar: Sequence[date],
    audit_keys: Iterable[AuditKey],
    authority: SessionAuthority,
    authority_paths: Mapping[str, Path],
    validated_callback: Callable[[tuple[SessionRule, ...]], None] | None = None,
) -> tuple[SessionRule, ...]:
    """Replay, reverse-expand, then atomically replace the formal asset."""
    audited = frozenset(_validate_audit_key(key) for key in audit_keys)
    validate_boundary_keys(boundaries, audited)
    _validate_authority_hashes(authority, authority_paths)
    temporary = _stage_session_rules(Path(output), rule_rows)
    try:
        loaded_rules = tuple(load_session_rules(temporary))
        validate_audited_boundaries(boundaries, loaded_rules)
        reverse_keys = expand_rule_keys(
            loaded_rules,
            global_calendar=global_calendar,
        )
        if reverse_keys != audited:
            raise SessionCaptureError(
                "reverse_key_mismatch: loaded rules expand beyond or omit audit keys"
            )
        if validated_callback is not None:
            validated_callback(loaded_rules)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return loaded_rules


def _coverage_line(row: Mapping[str, Any]) -> str:
    fields = (
        "coverage_year",
        "all_product_days",
        "in_pool_days",
        "in_pool_ratio",
        "audit_universe_days",
        "audited_days",
        "audited_ratio",
        "normalization_excluded_product_days",
        "normalization_unkeyable_rows",
    )
    return " ".join(f"{field}={row[field]}" for field in fields)


def _authority_line(authority: SessionAuthority) -> str:
    hashes = authority.sha256_by_asset
    return (
        f"session_authority version={SESSION_RULES_VERSION} "
        f"no_night_sha256={hashes['no_night']} "
        f"day_only_sha256={hashes['day_only']} "
        f"history_exception_sha256={hashes['history_exception']}"
    )


def write_capture_diagnostics(
    *,
    inventory_output: Path,
    audit_report: Path,
    ambiguities: Sequence[AmbiguityRecord],
    report: CoverageReport,
    status: str,
    log_lines: Sequence[str],
    summary: str,
) -> None:
    """Write deterministic non-authoritative inventory and audit outputs."""
    if status not in {"blocked", "validated"}:
        raise SessionCaptureError(f"diagnostic_status: unsupported {status!r}")
    inventory_output.parent.mkdir(parents=True, exist_ok=True)
    with inventory_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("exchange", "product", "trade_date", "check", "reason"),
            lineterminator="\n",
        )
        writer.writeheader()
        for item in sorted(ambiguities):
            writer.writerow(
                {
                    "exchange": item.exchange,
                    "product": item.product,
                    "trade_date": item.trade_date.isoformat(),
                    "check": item.check,
                    "reason": item.reason,
                }
            )

    report_lines = [
        f"publication_status={status}",
        *log_lines,
        *(_coverage_line(row) for row in report.rows),
        (
            "normalization_unkeyable_unknown_date_rows="
            f"{report.unknown_date_unkeyable_rows}"
        ),
        *(
            "ambiguous_session="
            f"{item.trade_date.isoformat()} {item.exchange} {item.product} "
            f"check={item.check} reason={item.reason}"
            for item in sorted(ambiguities)
        ),
        summary,
    ]
    audit_report.parent.mkdir(parents=True, exist_ok=True)
    audit_report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def _capture_and_publish_outcome(
    *,
    start: date,
    end: date,
    backtest_start: date,
    output: Path,
    inventory_output: Path,
    audit_report: Path,
    settings: Path | None,
    use_test: bool,
) -> _CaptureOutcome:
    """Capture, authority-check, and atomically publish session rules."""
    validate_capture_output_paths(
        output=output,
        inventory_output=inventory_output,
        audit_report=audit_report,
    )
    _require_capture_dates(start, end)
    config = CarryConfig()
    validate_capture_request(
        start=start,
        backtest_start=backtest_start,
        output=output,
        prewarm_calendar_days=config.prewarm_calendar_days,
    )
    authority_paths = {
        "no_night": NO_NIGHT_PATH,
        "day_only": DAY_ONLY_PATH,
        "history_exception": HISTORY_EXCEPTIONS_PATH,
    }
    authority = load_session_authority(
        no_night_path=NO_NIGHT_PATH,
        day_only_path=DAY_ONLY_PATH,
        history_exception_path=HISTORY_EXCEPTIONS_PATH,
    )
    eligibility_line = (
        "eligibility_config "
        f"liquidity_window={config.liquidity_window} "
        f"liquidity_threshold={config.liquidity_threshold} "
        f"prewarm_calendar_days={config.prewarm_calendar_days}"
    )
    range_line = (
        f"requested_range start={start.isoformat()} "
        f"end={end.isoformat()} "
        f"daily_load_start="
        f"{(start - timedelta(days=config.prewarm_calendar_days)).isoformat()} "
        f"backtest_start={backtest_start.isoformat()}"
    )
    authority_line = _authority_line(authority)
    base_log_lines = (eligibility_line, range_line, authority_line)
    for line in base_log_lines:
        print(line)

    data = load_public_carry_data(
        start=start,
        end=end,
        config=config,
        config_path=settings,
        use_test=use_test,
    )
    history_starts = load_public_product_history_starts(
        config_path=settings,
        use_test=use_test,
    )
    audit = _build_default_liquidity_audit(
        data.prices,
        history_starts=history_starts,
        history_exceptions=authority.liquidity_history_exceptions,
        start=start,
        end=end,
        config=config,
    )
    _assert_audit_key_subsets(audit.key_sets)
    history_gap_lines = authorized_history_gap_lines(
        audit.history_status_by_key,
        authority.liquidity_history_exceptions,
    )
    for line in history_gap_lines:
        print(line)
    log_lines = (*base_log_lines, *history_gap_lines)
    validate_capture_no_night_calendar(
        authority.no_night_dates, audit.global_calendar
    )
    report = coverage_report(
        data_quality=data.data_quality,
        key_sets=audit.key_sets,
        start=start,
        end=end,
    )
    for row in report.rows:
        print(_coverage_line(row))
    print(
        "normalization_unkeyable_unknown_date_rows="
        f"{report.unknown_date_unkeyable_rows}"
    )

    products = sorted({key[1] for key in audit.key_sets.audit_keys})
    try:
        require_publishable_coverage(report)
    except SessionCaptureError:
        blocked = report.unknown_date_unkeyable_rows + sum(
            row["normalization_unkeyable_rows"] for row in report.rows
        )
        summary = (
            f"products={len(products)} rules=0 checked_days=0 "
            "ambiguous=0"
        )
        print(
            f"publication_blocked=normalization_unkeyable count={blocked}",
            file=os.sys.stderr,
        )
        print(summary)
        write_capture_diagnostics(
            inventory_output=inventory_output,
            audit_report=audit_report,
            ambiguities=(),
            report=report,
            status="blocked",
            log_lines=log_lines,
            summary=summary,
        )
        return _CaptureOutcome(
            counts=(len(products), 0, 0, 0),
            blocked=True,
        )

    source = PublicMinuteSource(config_path=settings, use_test=use_test)
    boundaries = capture_session_boundaries(source, audit.candidates)
    boundary_keys = validate_boundary_keys(boundaries, audit.key_sets.audit_keys)
    checked_days = len(boundary_keys)
    if checked_days != sum(row["audited_days"] for row in report.rows):
        raise SessionCaptureError(
            "checked_days_mismatch: boundary and yearly audited totals differ"
        )
    classified, ambiguities = classify_authorized_boundaries(boundaries, authority)
    if ambiguities:
        for item in ambiguities:
            print(
                "ambiguous_session="
                f"{item.trade_date.isoformat()} {item.exchange} {item.product} "
                f"check={item.check} reason={item.reason}",
                file=os.sys.stderr,
            )
        summary = (
            f"products={len(products)} rules=0 checked_days={checked_days} "
            f"ambiguous={len(ambiguities)}"
        )
        print(summary)
        write_capture_diagnostics(
            inventory_output=inventory_output,
            audit_report=audit_report,
            ambiguities=ambiguities,
            report=report,
            status="blocked",
            log_lines=log_lines,
            summary=summary,
        )
        return _CaptureOutcome(
            counts=(len(products), 0, checked_days, len(ambiguities)),
            blocked=True,
        )

    rule_rows = collapse_session_rules(
        classified,
        global_calendar=audit.global_calendar,
        audit_keys=audit.key_sets.audit_keys,
    )
    def write_validated_diagnostics(
        validated_rules: tuple[SessionRule, ...],
    ) -> None:
        validated_summary = (
            f"products={len(products)} rules={len(validated_rules)} "
            f"checked_days={checked_days} ambiguous=0"
        )
        write_capture_diagnostics(
            inventory_output=inventory_output,
            audit_report=audit_report,
            ambiguities=(),
            report=report,
            status="validated",
            log_lines=log_lines,
            summary=validated_summary,
        )

    loaded_rules = publish_session_rules(
        output=output,
        rule_rows=rule_rows,
        boundaries=boundaries,
        global_calendar=audit.global_calendar,
        audit_keys=audit.key_sets.audit_keys,
        authority=authority,
        authority_paths=authority_paths,
        validated_callback=write_validated_diagnostics,
    )
    summary = (
        f"products={len(products)} rules={len(loaded_rules)} "
        f"checked_days={checked_days} ambiguous=0"
    )
    print(summary)
    return _CaptureOutcome(
        counts=(len(products), len(loaded_rules), checked_days, 0),
        blocked=False,
    )


def capture_and_publish(
    *,
    start: date,
    end: date,
    backtest_start: date,
    output: Path,
    inventory_output: Path,
    audit_report: Path,
    settings: Path | None,
    use_test: bool,
) -> tuple[int, int, int, int]:
    """Capture and publish while retaining the established four-count API."""
    return _capture_and_publish_outcome(
        start=start,
        end=end,
        backtest_start=backtest_start,
        output=output,
        inventory_output=inventory_output,
        audit_report=audit_report,
        settings=settings,
        use_test=use_test,
    ).counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture versioned commodity minute-session rules"
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--backtest-start", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory-output", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--use-test", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    outcome = _capture_and_publish_outcome(
        start=args.start,
        end=args.end,
        backtest_start=args.backtest_start,
        output=args.output,
        inventory_output=args.inventory_output,
        audit_report=args.audit_report,
        settings=args.settings,
        use_test=args.use_test,
    )
    return int(outcome.blocked)


if __name__ == "__main__":
    raise SystemExit(main())
