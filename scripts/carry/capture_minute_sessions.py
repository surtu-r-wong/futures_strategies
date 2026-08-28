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
from common.minute.pg_source import (
    MinuteCandidate,
    PublicMinuteSource,
    minute_contract_identity,
)
from common.minute.sessions import (
    SESSION_RULES_CAPTURE_START,
    SESSION_RULES_VERSION,
    SessionRule,
    build_trading_slots,
    load_session_rules,
    night_label_to_offset,
    night_offset_to_label,
    parse_night_interval,
    resolve_session_rule,
    validate_capture_coverage,
)
from cta_carry.pg_source import (
    load_public_carry_data,
    load_public_product_history_starts,
)
from cta_carry.session_authority import (
    AbsentProductDay,
    EffectiveAuthorityRange,
    matching_absent_product_day,
    SessionAuthority,
    SessionAuthorityError,
    SessionException,
    authorize_night_observation,
    load_session_authority,
    matching_ranges,
    validate_session_exception_calendar,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CSV_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_start",
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
SESSION_EXCEPTIONS_PATH = (
    REPOSITORY_ROOT / "config" / "carry_minute_session_exceptions.csv"
)
DAY_ONLY_PATH = REPOSITORY_ROOT / "config" / "carry_minute_day_only_regimes.csv"
ABSENT_PRODUCT_DAYS_PATH = (
    REPOSITORY_ROOT / "config" / "carry_minute_absent_product_days.csv"
)
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


@dataclass(frozen=True)
class NightObservation:
    """One classified night interval plus the audit note it earned, if any."""

    night_start: str
    night_end: str
    note: str | None = None


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
        raise SessionCaptureError("raw_exclusion_identity: object_id must be a string")
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
    # A contract halted for the day keeps its open interest, so ranking on that
    # alone can hand the audit a contract that never traded and therefore
    # cannot say whether the session opened. Prefer one that did trade; when a
    # whole product is halted none did, and the boundary gate fires as before.
    ranking["traded"] = ranking["volume"].fillna(0) > 0
    representatives = (
        ranking.sort_values(
            ["exchange", "product", "trade_date", "traded", "oi", "volume", "contract"],
            ascending=[True, True, True, False, False, False, True],
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


def build_audit(
    prices: pd.DataFrame,
    *,
    resolve_pool,
    start: date,
    end: date,
    config: CarryConfig | None = None,
) -> DefaultLiquidityAudit:
    """Build the capture envelope from an externally decided pool.

    The pool is the only thing that differs between consumers. Carry ranks on
    its own liquidity rule; the continuous strategy uses the report's turnover
    universe. Everything else here -- representative contracts, the global
    calendar, the audit key sets, the candidates -- is the same work either way.

    ``resolve_pool`` is a callable, not a plain set, so the representative index
    is built exactly once: deriving it walks every daily row through
    ``minute_contract_identity``, and Carry's pool rule needs to read it.

    It is called as ``resolve_pool(representative_index=..., normalized_keys=...,
    global_calendar=...)`` and returns ``(in_pool_source_keys, history_status_by_key)``.
    """
    _require_capture_dates(start, end)
    representative_index = _build_representative_index(prices)
    global_calendar = _ordered_calendar(prices["trade_date"].drop_duplicates().tolist())
    normalized_keys = frozenset(
        key for key in representative_index if start <= key[2] <= end
    )
    if not normalized_keys:
        raise SessionCaptureError("requested range contains no normalized product-days")

    in_pool_source_keys, history_status_by_key = resolve_pool(
        representative_index=representative_index,
        normalized_keys=normalized_keys,
        global_calendar=global_calendar,
    )
    in_pool_source_keys = frozenset(in_pool_source_keys)

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
        config=CarryConfig() if config is None else config,
        global_calendar=global_calendar,
        key_sets=key_sets,
        in_pool_source_keys=in_pool_source_keys,
        history_status_by_key=history_status_by_key,
        candidates=candidates,
    )


def _carry_liquidity_pool(
    prices: pd.DataFrame,
    *,
    history_starts: pd.DataFrame,
    history_exceptions: Iterable[EffectiveAuthorityRange],
    start: date,
    config: CarryConfig,
):
    """Carry's own pool rule and history gate, unchanged, as a ``resolve_pool``."""

    def resolve(*, representative_index, normalized_keys, global_calendar):
        liquidity = aggregate_product_liquidity(prices, config)
        liquidity_by_product_date = {
            (row.product, row.trade_date): row
            for row in liquidity.itertuples(index=False)
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

        return in_pool_source_keys, history_status_by_key

    return resolve


def _build_default_liquidity_audit(
    prices: pd.DataFrame,
    *,
    history_starts: pd.DataFrame,
    history_exceptions: Iterable[EffectiveAuthorityRange],
    start: date,
    end: date,
    config: CarryConfig,
) -> DefaultLiquidityAudit:
    return build_audit(
        prices,
        resolve_pool=_carry_liquidity_pool(
            prices,
            history_starts=history_starts,
            history_exceptions=history_exceptions,
            start=start,
            config=config,
        ),
        start=start,
        end=end,
        config=config,
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
    *,
    absent_identities: frozenset[tuple[date, str, str]] = frozenset(),
    tolerate_empty: bool = False,
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
        frame = source.iter_session_boundaries(
            candidates,
            lower=lower,
            upper=upper,
            absent_identities=absent_identities,
            tolerate_empty=tolerate_empty,
        )
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


def _boundary_error_reason(row: Any, check: str, reason: str) -> None:
    raise SessionCaptureError(
        f"{row['trade_date']} {row['exchange']} {row['product']} "
        f"{row['daily_contract']} {check}: {reason}"
    )


def _night_boundary_error(row: Any, field: str, reason: str) -> None:
    raise SessionCaptureError(
        f"{row['trade_date']} {row['exchange']} {row['product']} "
        f"{row['daily_contract']} {field}: {reason}"
    )


def classify_session_boundary(
    row: Any, *, day_session_absent: bool = False
) -> NightObservation:
    """Classify one exact empirical row into its authoritative night interval."""
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
    if day_session_absent:
        # An authorised absence says the archive holds no day session at all for
        # this product-day. A day that is partly there is a different thing --
        # a malformed or non-canonical clock -- and stays fatal.
        present = sorted(
            field for field in expected_day if not _missing_boundary(row[field])
        )
        if present:
            _boundary_error_reason(
                row,
                "day_session_partially_present",
                "authorised day-session absence requires every day boundary to be "
                f"missing; present={present}",
            )
    else:
        for field, expected in expected_day.items():
            _require_boundary(row, field, expected)

    night_first = row["night_first"]
    night_last = row["night_last"]
    traded_first = row["night_traded_first"]
    traded_second = row["night_traded_second"]
    traded_flat = row["night_traded_first_flat"]
    if _missing_boundary(night_first) and _missing_boundary(night_last):
        return NightObservation("none", "none")
    if _missing_boundary(night_first) or _missing_boundary(night_last):
        _boundary_error(
            row,
            "night_first" if _missing_boundary(night_first) else "night_last",
            None,
        )
    if _missing_boundary(traded_first):
        # Bars exist but nothing traded. A padded day-only product and a night
        # nobody traded are indistinguishable here, so hand the call to the
        # authority layer and leave a counted trace either way.
        return NightObservation("none", "none", "night_untraded_padding")
    for field, value in (
        ("night_traded_first", traded_first),
        ("night_last", night_last),
    ):
        if not isinstance(value, datetime) or value.tzinfo is None:
            _night_boundary_error(row, field, "requires an aware datetime")
    try:
        start = traded_first.astimezone(SHANGHAI)
        end = night_last.astimezone(SHANGHAI) + timedelta(minutes=1)
    except (TypeError, ValueError, OverflowError):
        _night_boundary_error(
            row, "night_traded_first", "could not be converted to the exchange clock"
        )
    note = None
    if (
        start.minute % 15
        and traded_flat is True
        and not _missing_boundary(traded_second)
    ):
        if not isinstance(traded_second, datetime) or traded_second.tzinfo is None:
            _night_boundary_error(
                row, "night_traded_second", "requires an aware datetime"
            )
        shifted = traded_second.astimezone(SHANGHAI)
        if shifted == start + timedelta(minutes=1) and shifted.minute % 15 == 0:
            # The auction match printed one minute before the session it opened.
            start = shifted
            note = "night_auction_attributed"
    after_midnight = previous_trade_date.fromordinal(
        previous_trade_date.toordinal() + 1
    )
    if start.date() != previous_trade_date:
        _night_boundary_error(
            row,
            "night_traded_first",
            f"expected the previous trade date {previous_trade_date}; "
            f"got {start.date()}",
        )
    if end > _at(after_midnight, 2, 30):
        _night_boundary_error(
            row,
            "night_last",
            f"ends after the 02:30 commodity clock bound; got {end}",
        )
    labels = (f"{start:%H:%M}", f"{end:%H:%M}")
    for field, label in (("night_traded_first", labels[0]), ("night_last", labels[1])):
        try:
            night_label_to_offset(label)
        except ValueError as exc:
            _night_boundary_error(row, field, str(exc))
    try:
        parse_night_interval(*labels)
    except ValueError as exc:
        _night_boundary_error(row, "night_traded_first/night_last", str(exc))
    return NightObservation(labels[0], labels[1], note)


def classify_authorized_boundaries(
    boundaries: pd.DataFrame,
    authority: SessionAuthority,
    *,
    global_calendar: Sequence[date],
) -> tuple[pd.DataFrame, tuple[AmbiguityRecord, ...], tuple[str, ...]]:
    """Classify every boundary and reconcile each result with authority."""
    identity_columns = {"exchange", "product", "trade_date"}
    missing = sorted(identity_columns.difference(boundaries.columns))
    if missing:
        raise SessionCaptureError(f"captured boundaries missing columns: {missing}")
    loaded_dates = frozenset(_ordered_calendar(global_calendar))

    classified: list[dict[str, Any]] = []
    ambiguous: list[AmbiguityRecord] = []
    notes: list[str] = []
    # Keyed by the row's own identity: an exchange-wide row is consumed by any
    # product of that exchange, a product-scoped row only by its own product.
    consumed_exception_keys: set[tuple[str, str, date]] = set()
    ordered = boundaries.sort_values(
        ["trade_date", "exchange", "product", "daily_contract"],
        kind="mergesort",
    )
    for row in ordered.to_dict("records"):
        absent = matching_absent_product_day(
            authority.absent_product_days,
            row["exchange"],
            row["product"],
            row["trade_date"],
        )
        try:
            observation = classify_session_boundary(
                row, day_session_absent=absent is not None
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
        # The note records an observed fact, so it must survive an
        # authorization failure that turns the row into an ambiguity.
        if observation.note is not None:
            notes.append(
                f"{observation.note}={row['trade_date'].isoformat()} "
                f"{row['exchange']} {row['product']}"
            )
        night_start = observation.night_start
        night_end = observation.night_end
        try:
            consumed = authorize_night_observation(
                authority,
                exchange=row["exchange"],
                product=row["product"],
                trade_date=row["trade_date"],
                observed_night_start=night_start,
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
        if consumed is not None:
            consumed_exception_keys.add(
                (consumed.exchange, consumed.product, consumed.trade_date)
            )
        classified.append(
            {
                "exchange": row["exchange"],
                "product": row["product"],
                "trade_date": row["trade_date"],
                "night_start": night_start,
                "night_end": night_end,
            }
        )
    relevant_keys = sorted(
        {
            (row.exchange, row.product, row.trade_date)
            for row in authority.session_exceptions
            if row.trade_date in loaded_dates
        }
    )
    for exchange, product, trade_date in relevant_keys:
        if (exchange, product, trade_date) in consumed_exception_keys:
            continue
        ambiguous.append(
            AmbiguityRecord(
                trade_date=trade_date,
                exchange=exchange,
                product=product or "*",
                check="session_exception_unconsumed",
                reason=(
                    "loaded session exception was never consumed by an audited "
                    "product-day observation"
                ),
            )
        )
    return (
        pd.DataFrame(
            classified,
            columns=[
                "exchange",
                "product",
                "trade_date",
                "night_start",
                "night_end",
            ],
        ),
        tuple(sorted(ambiguous)),
        tuple(notes),
    )


def validate_capture_session_exception_calendar(
    rows: Sequence[SessionException],
    global_calendar: Sequence[date],
) -> None:
    """Validate only authority rows whose target date is in the loaded calendar."""
    calendar = _ordered_calendar(global_calendar)
    loaded_dates = frozenset(calendar)
    relevant = tuple(row for row in rows if row.trade_date in loaded_dates)
    validate_session_exception_calendar(relevant, calendar)


def _as_builtin_datetime(value: Any) -> datetime:
    """Coerce a captured boundary value to a concrete built-in datetime.

    `to_dict("records")` hands back pandas Timestamps whenever the column
    happens to be datetime64, and MinuteCandidate checks the exact type, so the
    conversion has to be explicit rather than left to whichever dtype pandas
    chose for that frame.
    """
    if type(value) is datetime:
        return value
    converted = getattr(value, "to_pydatetime", None)
    if converted is not None:
        result = converted()
        if type(result) is datetime:
            return result
    raise SessionCaptureError(
        f"sibling_window_datetime: expected a datetime; got {type(value).__name__}"
    )


def sibling_night_candidates(
    boundaries: pd.DataFrame,
    ambiguities: Sequence[AmbiguityRecord],
    prices: pd.DataFrame,
) -> tuple[CapturedCandidate, ...]:
    """Build a supplementary look at the other contracts of an unsettled product-day.

    Only product-days the first classification pass could not settle are widened,
    so a run that already agreed with authority cannot change. Everything needed
    comes off the captured frame, which already carries each candidate's window.
    """
    unsettled = {
        (item.trade_date, item.exchange, item.product)
        for item in ambiguities
        if item.product != "*"
    }
    if not unsettled:
        return ()
    anchor_by_key: dict[tuple[date, str, str], Mapping[str, Any]] = {}
    for row in boundaries.to_dict("records"):
        key = (row["trade_date"], row["exchange"], row["product"])
        if key in unsettled:
            anchor_by_key.setdefault(key, row)
    selected: list[CapturedCandidate] = []
    seen: set[tuple[date, str]] = set()
    for row in prices.loc[:, ["trade_date", "contract"]].itertuples(index=False):
        trade_date, contract = row.trade_date, row.contract
        try:
            product, minute_symbol, exchange = minute_contract_identity(
                contract, trade_date
            )
        except (SessionCaptureError, ValueError, TypeError):
            continue
        key = (trade_date, exchange, product)
        anchor = anchor_by_key.get(key)
        if anchor is None or contract == anchor["daily_contract"]:
            continue
        if (trade_date, contract) in seen:
            continue
        seen.add((trade_date, contract))
        selected.append(
            CapturedCandidate(
                candidate=MinuteCandidate(
                    trade_date=trade_date,
                    product=product,
                    daily_contract=contract,
                    minute_symbol=minute_symbol,
                    exchange=exchange,
                    window_start=_as_builtin_datetime(anchor["window_start"]),
                    window_end=_as_builtin_datetime(anchor["window_end"]),
                    candidate_role="session_representative",
                    causal_in_pool_date=trade_date,
                    selection_source="night_start_widening",
                ),
                previous_trade_date=anchor["previous_trade_date"],
                causal_in_pool_date=trade_date,
                selection_source="night_start_widening",
            )
        )
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.candidate.trade_date,
                item.candidate.product,
                item.candidate.daily_contract,
            ),
        )
    )


def earliest_sibling_night_starts(
    sibling_boundaries: pd.DataFrame,
) -> dict[tuple[date, str, str], datetime]:
    """Reduce the supplementary rows to one earliest night trade per product-day."""
    earliest: dict[tuple[date, str, str], datetime] = {}
    for row in sibling_boundaries.to_dict("records"):
        value = row["night_traded_first"]
        if _missing_boundary(value):
            continue
        key = (row["trade_date"], row["exchange"], row["product"])
        current = earliest.get(key)
        if current is None or value < current:
            earliest[key] = value
    return earliest


def widened_night_traded_first(
    representative: datetime | None,
    sibling_earliest: datetime | None,
) -> datetime | None:
    """Take the earliest night trade across one product's contracts.

    A session is a fact about the product, not about whichever contract was
    picked to represent it. The representative can miss the opening minute, or
    sit out the night entirely, while its siblings trade from the open; the
    session still started when the earliest of them traded.

    Returning None when nobody traded keeps the fail-closed property: that night
    still reaches the padding gate instead of being handed a start it never had.
    """
    if _missing_boundary(representative):
        return None if _missing_boundary(sibling_earliest) else sibling_earliest
    if _missing_boundary(sibling_earliest):
        return representative
    return min(representative, sibling_earliest)


_DAY_BOUNDARY_COLUMNS = (
    "day_1_first",
    "day_1_last",
    "day_2_first",
    "day_2_last",
    "day_3_first",
    "day_3_last",
)


def consumed_absent_day_sessions(
    boundaries: pd.DataFrame,
) -> frozenset[tuple[date, str, str, str]]:
    """Name the product-days whose captured row carries no day session at all.

    Read from what the query returned rather than from the registry, so the run
    reports the absences it actually leaned on, not the ones it was permitted.
    """
    missing = sorted(set(_DAY_BOUNDARY_COLUMNS).difference(boundaries.columns))
    if missing:
        raise SessionCaptureError(f"captured boundaries missing columns: {missing}")
    absent: set[tuple[date, str, str, str]] = set()
    for row in boundaries.to_dict("records"):
        if all(_missing_boundary(row[column]) for column in _DAY_BOUNDARY_COLUMNS):
            absent.add(
                (
                    row["trade_date"],
                    row["exchange"],
                    row["product"],
                    row["daily_contract"],
                )
            )
    return frozenset(absent)


def authorized_absent_day_session_lines(
    registered: Iterable[AbsentProductDay],
    consumed_identities: frozenset[tuple[date, str, str, str]],
) -> tuple[str, ...]:
    """Name every absence the run actually leaned on, and count it.

    An authorised absence must never be silent: it is the one place the capture
    stops requiring evidence, so the run says out loud which product-days it
    excused and which contract stood in for each.
    """
    rows = tuple(registered)
    by_key = {(row.trade_date, row.exchange, row.product): row for row in rows}
    lines: list[str] = []
    for trade_date, exchange, product, contract in sorted(consumed_identities):
        row = by_key.get((trade_date, exchange, product))
        if row is None:
            raise SessionCaptureError(
                "authorized_absent_day_session_unregistered: "
                f"key={exchange}/{product}/{trade_date.isoformat()} "
                f"contract={contract}"
            )
        lines.append(
            f"authorized_absent_day_session key={row.exchange}/{row.product}/"
            f"{row.trade_date.isoformat()} contract={contract} "
            f"source_url={row.source_url}"
        )
    lines.append(
        f"authorized_absent_day_session_count={len(consumed_identities)} "
        f"registered={len(rows)}"
    )
    return tuple(lines)


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
            key = raw_exclusion_product_day_identity(row["object_id"], trade_date)
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
        in_pool = frozenset(key for key in key_sets.in_pool_keys if key[2].year == year)
        universe = frozenset(
            key for key in key_sets.audit_universe_keys if key[2].year == year
        )
        audited = frozenset(key for key in key_sets.audit_keys if key[2].year == year)
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
                    f"{len(audited) / len(universe):.6f}" if universe else "0.000000"
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


def _classified_night_interval(
    exchange: str, product: str, record: Any
) -> tuple[str, str]:
    night_start = record.night_start
    night_end = record.night_end
    try:
        parse_night_interval(night_start, night_end)
    except ValueError as exc:
        raise SessionCaptureError(
            f"{record.trade_date} {exchange} {product}: unsupported night "
            f"interval {night_start!r}/{night_end!r}: {exc}"
        ) from exc
    return (night_start, night_end)


def _collapsed_rule(
    exchange: str,
    product: str,
    effective_start: date,
    effective_end: date,
    night_interval: tuple[str, str],
) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "product": product,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "night_start": night_interval[0],
        "night_end": night_interval[1],
        "version": SESSION_RULES_VERSION,
    }


def collapse_session_rules(
    classified: pd.DataFrame,
    *,
    global_calendar: Sequence[date],
    audit_keys: Iterable[AuditKey],
) -> list[dict[str, Any]]:
    """Collapse adjacent audited product dates with identical observed clocks."""
    required = {"exchange", "product", "trade_date", "night_start", "night_end"}
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
        night_interval = _classified_night_interval(exchange, product, records[0])
        previous_date = start
        for record in records[1:]:
            observed = _classified_night_interval(exchange, product, record)
            adjacent = (
                position_by_date[record.trade_date]
                == position_by_date[previous_date] + 1
            )
            if observed != night_interval or not adjacent:
                rules.append(
                    _collapsed_rule(
                        exchange, product, start, previous_date, night_interval
                    )
                )
                start = record.trade_date
                night_interval = observed
            previous_date = record.trade_date
        rules.append(
            _collapsed_rule(exchange, product, start, previous_date, night_interval)
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
        raise SessionCaptureError(f"boundary_schema: missing columns {missing_columns}")
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
        raise SessionCaptureError("boundary_missing_keys: " + repr(sorted(missing)))
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
    coverage_check=None,
) -> date:
    """Enforce the consumer's coverage rule and the repository asset's fixed start.

    Two separate concerns live here. The repository guard protects
    ``carry_minute_sessions.csv`` from a partial capture and always applies.

    The coverage rule belongs to whoever will read the asset, and they do not
    agree. Carry warms a minute state machine, so its asset must begin 730 days
    before it starts trading -- that is ``validate_capture_coverage``, the
    default. The continuous panel has no such warmup: it builds bars month by
    month from the asset's first day, and so can never satisfy Carry's rule.
    It passes its own instead.
    """
    _require_capture_dates(start, start)
    check = validate_capture_coverage if coverage_check is None else coverage_check
    required_start = check(
        capture_start=start,
        backtest_start=backtest_start,
        prewarm_calendar_days=prewarm_calendar_days,
    )
    if (
        Path(output).resolve(strict=False) == SESSION_RULES_PATH.resolve(strict=False)
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
    expected_names = {
        "session_exception",
        "day_only",
        "history_exception",
        "absent_product_day",
    }
    if set(authority_paths) != expected_names:
        raise SessionCaptureError(
            "authority_hash_paths: expected every fixed authority asset"
        )
    if set(authority.sha256_by_asset) != expected_names:
        raise SessionCaptureError(
            "authority_hash_manifest: expected every authority hash"
        )
    for name in sorted(expected_names):
        try:
            actual = hashlib.sha256(
                Path(authority_paths[name]).read_bytes()
            ).hexdigest()
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
                        "night_start": rule["night_start"],
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


def _expected_night_interval(rule: SessionRule) -> tuple[str, str]:
    night_segments = tuple(
        segment
        for segment in rule.segments
        if segment.start_minute < 0 or segment.end_minute <= 150
    )
    if not night_segments:
        return "none", "none"
    if len(night_segments) != 1:
        raise SessionCaptureError(
            "session_rule_replay: expected at most one night segment"
        )
    segment = night_segments[0]
    return (
        night_offset_to_label(segment.start_minute),
        night_offset_to_label(segment.end_minute),
    )


def validate_audited_boundaries(
    boundaries: pd.DataFrame,
    rules: Sequence[SessionRule],
    *,
    absent_product_days: Sequence[AbsentProductDay] = (),
) -> None:
    """Require every empirical boundary to occupy exactly one authoritative slot."""
    registered = tuple(absent_product_days)
    for row in boundaries.to_dict("records"):
        rule = resolve_session_rule(
            rules,
            row["exchange"],
            row["product"],
            row["trade_date"],
        )
        expected_interval = _expected_night_interval(rule)
        absent = matching_absent_product_day(
            registered, row["exchange"], row["product"], row["trade_date"]
        )
        observation = classify_session_boundary(
            row, day_session_absent=absent is not None
        )
        actual_interval = (observation.night_start, observation.night_end)
        if expected_interval != actual_interval:
            raise SessionCaptureError(
                f"session_rule_replay: {row['trade_date']} {row['product']}: "
                "captured rule changed after reload; "
                f"expected={expected_interval}; actual={actual_interval}"
            )
        slots = build_trading_slots(
            row["trade_date"],
            row["previous_trade_date"],
            rule,
        )
        # night_first is the first bar present, not a session boundary. The
        # archive writes bars on the exchange's normal schedule, so on a night
        # that was delayed or did not run at all it holds flat bars from the
        # usual open. The classifier reads the session start off
        # night_traded_first for exactly that reason, and the replay checks the
        # boundaries the classifier actually used.
        columns = tuple(c for c in BOUNDARY_COLUMNS if c != "night_first")
        if actual_interval == ("none", "none"):
            columns = tuple(c for c in columns if not c.startswith("night_"))
        if absent is not None:
            # The day session was never observed, so there is no day timestamp
            # for the published rule to account for.
            columns = tuple(c for c in columns if not c.startswith("day_"))
        for column in columns:
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
        validate_audited_boundaries(
            boundaries,
            loaded_rules,
            absent_product_days=authority.absent_product_days,
        )
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
        f"session_exception_sha256={hashes['session_exception']} "
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
    audit_builder=None,
    coverage_check=None,
    boundaries=None,
) -> _CaptureOutcome:
    """Capture, authority-check, and atomically publish session rules.

    ``audit_builder`` decides which product-days the capture audits. It is
    called exactly as ``_build_default_liquidity_audit`` is, and defaults to it,
    so the Carry path is unchanged. The continuous strategy injects its own so
    the same publish path can serve a different universe.
    """
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
        coverage_check=coverage_check,
    )
    authority_paths = {
        "session_exception": SESSION_EXCEPTIONS_PATH,
        "day_only": DAY_ONLY_PATH,
        "history_exception": HISTORY_EXCEPTIONS_PATH,
        "absent_product_day": ABSENT_PRODUCT_DAYS_PATH,
    }
    authority = load_session_authority(
        session_exception_path=SESSION_EXCEPTIONS_PATH,
        day_only_path=DAY_ONLY_PATH,
        history_exception_path=HISTORY_EXCEPTIONS_PATH,
        absent_product_day_path=ABSENT_PRODUCT_DAYS_PATH,
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
    build = (
        _build_default_liquidity_audit if audit_builder is None else audit_builder
    )
    audit = build(
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
    validate_capture_session_exception_calendar(
        authority.session_exceptions, audit.global_calendar
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
        summary = f"products={len(products)} rules=0 checked_days=0 ambiguous=0"
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
    absent_identities = frozenset(
        (row.trade_date, row.exchange, row.product)
        for row in authority.absent_product_days
    )
    # Observing the minute table is the expensive step and the survey has
    # already done it. Reuse that frame when it is handed over; capture fresh
    # otherwise, which is what every existing caller does.
    if boundaries is None:
        boundaries = capture_session_boundaries(
            source, audit.candidates, absent_identities=absent_identities
        )
    absent_lines = authorized_absent_day_session_lines(
        authority.absent_product_days, consumed_absent_day_sessions(boundaries)
    )
    for line in absent_lines:
        print(line)
    log_lines = (*log_lines, *absent_lines)
    boundary_keys = validate_boundary_keys(boundaries, audit.key_sets.audit_keys)
    checked_days = len(boundary_keys)
    if checked_days != sum(row["audited_days"] for row in report.rows):
        raise SessionCaptureError(
            "checked_days_mismatch: boundary and yearly audited totals differ"
        )
    classified, ambiguities, attribution_notes = classify_authorized_boundaries(
        boundaries, authority, global_calendar=audit.global_calendar
    )
    # Second look: a session is a fact about the product, so a product-day the
    # first pass could not settle gets to hear from the product's other
    # contracts before it is called ambiguous. Only unsettled rows are touched,
    # so a run that already agreed with authority cannot change.
    siblings = sibling_night_candidates(boundaries, ambiguities, data.prices)
    if siblings:
        sibling_frame = capture_session_boundaries(
            source, siblings, tolerate_empty=True
        )
        earliest = earliest_sibling_night_starts(sibling_frame)
        widened_lines: list[str] = []
        if earliest:
            unsettled = {
                (item.trade_date, item.exchange, item.product)
                for item in ambiguities
                if item.product != "*"
            }
            starts = list(boundaries["night_traded_first"])
            for position, row in enumerate(boundaries.to_dict("records")):
                key = (row["trade_date"], row["exchange"], row["product"])
                if key not in unsettled or key not in earliest:
                    continue
                widened = widened_night_traded_first(
                    row["night_traded_first"], earliest[key]
                )
                if widened is not row["night_traded_first"]:
                    starts[position] = widened
                    widened_lines.append(
                        f"night_start_widened={key[0].isoformat()} {key[1]} {key[2]} "
                        f"representative={row['daily_contract']} "
                        f"observed={row['night_traded_first']} widened={widened}"
                    )
            if widened_lines:
                boundaries = boundaries.assign(night_traded_first=starts)
                for line in widened_lines:
                    print(line)
                log_lines = (*log_lines, *widened_lines)
                classified, ambiguities, attribution_notes = (
                    classify_authorized_boundaries(
                        boundaries,
                        authority,
                        global_calendar=audit.global_calendar,
                    )
                )
    log_lines = (*log_lines, *attribution_notes)
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
    audit_builder=None,
    coverage_check=None,
    boundaries=None,
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
        audit_builder=audit_builder,
        coverage_check=coverage_check,
        boundaries=boundaries,
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
