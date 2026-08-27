"""Build the shared, versioned commodity panel bundle.

The signal bars are back-adjusted. Every cached execution price, including the
two legs of a dominant roll, remains a raw concrete-contract price.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from numbers import Integral, Real
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Mapping, Sequence

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.commodity.bundle import (  # noqa: E402
    BUNDLE_VERSION,
    TABLE_SCHEMAS,
    normalise_bundle_table,
    write_bundle,
)
from common.commodity.continuous import adjustment_factors  # noqa: E402
from common.commodity.dominant import choose_dominant_commodity  # noqa: E402
from common.commodity.panel import (  # noqa: E402
    FILL_MINUTES,
    build_contexts,
    build_panel,
    context_choices_for_month,
)
from common.commodity.universe import (  # noqa: E402
    FINANCIAL_FUTURES,
    product_daily_turnover,
    universe_for_month,
)
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from common.minute.bars import MinuteDataError, five_minute_vwap  # noqa: E402
from common.minute.pg_source import (  # noqa: E402
    MinuteCandidate,
    PublicMinuteSource,
    minute_contract_identity,
)
from common.minute.sessions import load_session_rules  # noqa: E402
from cta_carry.session_authority import (  # noqa: E402
    load_pricing_bases,
    pricing_basis_for,
)

SESSION_RULES = _REPO_ROOT / "config" / "carry_minute_sessions.csv"
PRICING_BASES = _REPO_ROOT / "config" / "carry_minute_pricing_basis.csv"
DAILY_HISTORY_START = date(2010, 1, 1)
_DAILY_COLUMNS = ["symbol", "trade_date", "oi", "volume", "turnover", "close"]


def _start_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m").date().replace(day=1)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"expected YYYY-MM-DD (or legacy YYYY-MM), got {value!r}"
            ) from exc


def _end_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            month = datetime.strptime(value, "%Y-%m").date().replace(day=1)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"expected YYYY-MM-DD (or legacy YYYY-MM), got {value!r}"
            ) from exc
        return _next_month(month) - timedelta(days=1)


class _EndDateAction(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        setattr(namespace, self.dest, _end_date(value))
        setattr(namespace, "_legacy_end_month", len(value) == 7)


def _resolve_end(requested: date, *, reliable_end: date, legacy_month: bool) -> date:
    if requested <= reliable_end:
        return requested
    if (
        legacy_month
        and requested.year == reliable_end.year
        and requested.month == reliable_end.month
    ):
        return reliable_end
    raise ValueError(
        "panel_session_authority: "
        f"requested end {requested} exceeds reliable end {reliable_end}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a shared versioned commodity panel bundle."
    )
    parser.add_argument("--start", required=True, type=_start_date)
    parser.set_defaults(_legacy_end_month=False)
    parser.add_argument("--end", required=True, action=_EndDateAction)
    parser.add_argument(
        "--output-dir",
        "--out",
        dest="output_dir",
        required=True,
        type=Path,
        help="bundle directory (legacy alias: --out)",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        help="settings YAML; defaults to the repository settings convention",
    )
    parser.add_argument("--use-test", action="store_true")
    return parser


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(anchor: date) -> date:
    return date(anchor.year + anchor.month // 12, anchor.month % 12 + 1, 1)


def _months(start: date, end: date):
    current = _month_start(start)
    last = _month_start(end)
    while current <= last:
        yield current
        current = _next_month(current)


def _copy_daily(cursor, *, end: date) -> pd.DataFrame:
    buffer = io.StringIO()
    upper = end + timedelta(days=1)
    sql = (
        "SELECT symbol, trade_date, oi, volume, turnover, close "
        "FROM public.futures_daily "
        f"WHERE trade_date >= DATE '{DAILY_HISTORY_START.isoformat()}' "
        f"AND trade_date < DATE '{upper.isoformat()}' "
        "AND oi IS NOT NULL AND volume IS NOT NULL"
    )
    cursor.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buffer)
    frame = pd.read_csv(io.StringIO(buffer.getvalue()))
    missing = set(_DAILY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"panel_daily_columns: missing={sorted(missing)!r}")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise").dt.date
    return frame.loc[:, _DAILY_COLUMNS]


def _frame_sha256(frame: pd.DataFrame) -> str:
    """Hash a frame's schema and rows independent of source row order."""
    ordered = frame.sort_values(
        list(frame.columns), kind="mergesort", na_position="first"
    ).reset_index(drop=True)
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            [(column, str(ordered[column].dtype)) for column in ordered.columns],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(
        pd.util.hash_pandas_object(ordered, index=False, categorize=False)
        .to_numpy(dtype="uint64")
        .tobytes()
    )
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SOURCE_EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "generated",
    "output",
    "tests",
    "venv",
}


def _production_python_path(root: Path, relative: Path) -> bool:
    return (
        relative.suffix == ".py"
        and not any(part in _SOURCE_EXCLUDED_PARTS for part in relative.parts)
        and (root / relative).is_file()
        and not (root / relative).is_symlink()
    )


def _git_command(root: Path, *arguments: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _source_revision(project_root: str | Path = _REPO_ROOT) -> dict[str, str]:
    """Identify executable source by revision and exact working-tree bytes."""
    root = Path(project_root).resolve()
    tracked = _git_command(root, "ls-files", "-z", "--", "*.py")
    if tracked is None:
        candidates = (path.relative_to(root) for path in root.rglob("*.py"))
    else:
        candidates = (Path(raw.decode("utf-8")) for raw in tracked.split(b"\0") if raw)
    relative_paths = sorted(
        {
            relative
            for relative in candidates
            if _production_python_path(root, relative)
        },
        key=lambda path: path.as_posix(),
    )
    digest = hashlib.sha256()
    digest.update(b"commodity-production-python-v1\0")
    for relative in relative_paths:
        encoded_path = relative.as_posix().encode("utf-8")
        content = (root / relative).read_bytes()
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    raw_head = _git_command(root, "rev-parse", "--verify", "HEAD")
    head = raw_head.decode("ascii", errors="ignore").strip() if raw_head else ""
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        head = "unavailable"
    return {
        "git_head": head.lower(),
        "production_python_sha256": digest.hexdigest(),
    }


_CREDENTIAL_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|api[_-]?key|private[_-]?key|"
    r"dsn|user(?:name)?)",
    re.IGNORECASE,
)
_MINUTE_DIGEST_COLUMNS = (
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
_DIGEST_MODULUS = 1 << 256


def _safe_config_value(value: object) -> object:
    """Return canonical settings with credential-bearing entries removed."""
    if isinstance(value, Mapping):
        return {
            str(key): _safe_config_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if not _CREDENTIAL_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_config_value(child) for child in value]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not pd.notna(value) or value in (float("inf"), float("-inf")):
            raise ValueError("panel_config_digest: non-finite setting")
        return value
    raise ValueError(
        f"panel_config_digest: unsupported setting type {type(value).__name__}"
    )


def _effective_config_sha256(
    cfg: Mapping[str, object],
    session_rules_path: Path,
    pricing_bases_path: Path,
    *,
    source_revision: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, object]]:
    """Hash only non-secret effective settings and immutable build authorities."""
    safe_settings = _safe_config_value(cfg)
    payload = {
        "settings": safe_settings,
        "session_rules_sha256": _file_sha256(session_rules_path),
        "pricing_basis_sha256": _file_sha256(pricing_bases_path),
        "bundle_version": BUNDLE_VERSION,
        "source_revision": dict(source_revision or _source_revision()),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), safe_settings


def _candidate_digest_record(candidate) -> dict[str, object]:
    return {
        "trade_date": candidate.trade_date.isoformat(),
        "product": candidate.product,
        "daily_contract": candidate.daily_contract,
        "minute_symbol": candidate.minute_symbol,
        "exchange": candidate.exchange,
        "window_start": candidate.window_start.isoformat(),
        "window_end": candidate.window_end.isoformat(),
        "candidate_role": candidate.candidate_role,
        "causal_in_pool_date": (
            candidate.causal_in_pool_date.isoformat()
            if candidate.causal_in_pool_date is not None
            else None
        ),
        "selection_source": candidate.selection_source,
    }


def _encoded_field(tag: bytes, payload: bytes = b"") -> bytes:
    return tag + len(payload).to_bytes(8, "big") + payload


def _minute_scalar_bytes(column: str, value: object) -> bytes:
    """Encode one scalar without text or floating-point precision loss."""
    try:
        missing = bool(pd.isna(value))
    except (TypeError, ValueError):
        missing = False
    if value is None or missing:
        return _encoded_field(b"N")

    if column == "trade_date":
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None or timestamp != timestamp.normalize():
            raise ValueError(f"panel_minute_digest_date: invalid trade_date={value!r}")
        return _encoded_field(b"D", timestamp.date().isoformat().encode("ascii"))

    if column == "bar_time":
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            payload = b"N" + struct.pack(">q", timestamp.value)
        else:
            payload = b"A" + struct.pack(">q", timestamp.tz_convert("UTC").value)
        return _encoded_field(b"T", payload)

    if isinstance(value, bool):
        return _encoded_field(b"B", b"1" if bool(value) else b"0")
    if isinstance(value, Integral):
        return _encoded_field(b"I", str(int(value)).encode("ascii"))
    if isinstance(value, Real):
        return _encoded_field(b"F", struct.pack(">d", float(value)))
    if isinstance(value, str):
        return _encoded_field(b"S", value.encode("utf-8"))
    raise ValueError(
        f"panel_minute_digest_type: column={column!r} type={type(value).__name__!r}"
    )


def _minute_row_payloads(frame: pd.DataFrame) -> list[bytes]:
    missing = set(_MINUTE_DIGEST_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"panel_minute_digest_columns: missing={sorted(missing)!r}")
    schema = b"commodity-minute-row-v2\0" + b"".join(
        _encoded_field(b"C", column.encode("utf-8"))
        for column in _MINUTE_DIGEST_COLUMNS
    )
    rows = frame.loc[:, _MINUTE_DIGEST_COLUMNS].itertuples(index=False, name=None)
    return [
        schema
        + b"R"
        + b"".join(
            _minute_scalar_bytes(column, value)
            for column, value in zip(_MINUTE_DIGEST_COLUMNS, row, strict=True)
        )
        for row in rows
    ]


class DigestingMinuteSource:
    """Audit actual minute call streams with labeled, row-order-neutral digests.

    Each row is hashed canonically and the row hashes are accumulated as a
    multiset, so database row/chunk ordering does not alter a request digest.
    Request labels preserve the meaningful roll-fill versus bar call stream.
    """

    def __init__(
        self,
        source,
        *,
        pricing_basis_by_exchange: Mapping[str, str],
    ) -> None:
        self._source = source
        self._pricing_basis_by_exchange = dict(pricing_basis_by_exchange)
        self._phase: str | None = None
        self._requests: list[dict[str, object]] = []

    def __getattr__(self, name: str):
        return getattr(self._source, name)

    def set_phase(self, phase: str) -> None:
        if phase not in {"roll_fills", "bars"}:
            raise ValueError(f"panel_minute_digest_phase: {phase!r}")
        self._phase = phase

    def iter_month(self, candidates, lower, upper):
        if self._phase is None:
            raise ValueError("panel_minute_digest_phase: phase must be set")
        candidate_stream = tuple(candidates)
        ordered_candidates = sorted(
            candidate_stream,
            key=lambda candidate: (
                candidate.trade_date,
                candidate.daily_contract,
                candidate.candidate_role,
            ),
        )
        header = {
            "phase": self._phase,
            "sequence": len(self._requests) + 1,
            "lower": lower.isoformat(),
            "upper": upper.isoformat(),
            "candidates": [
                _candidate_digest_record(candidate) for candidate in ordered_candidates
            ],
            "pricing_bases": {
                exchange: self._pricing_basis_by_exchange[exchange]
                for exchange in sorted(
                    {candidate.exchange for candidate in ordered_candidates}
                )
            },
        }
        row_count = 0
        row_digest_sum = 0
        try:
            for frame in self._source.iter_month(candidate_stream, lower, upper):
                lines = _minute_row_payloads(frame)
                row_count += len(lines)
                for line in lines:
                    row_digest_sum = (
                        row_digest_sum
                        + int.from_bytes(hashlib.sha256(line).digest(), "big")
                    ) % _DIGEST_MODULUS
                yield frame
        finally:
            request_payload = {
                **header,
                "rows": row_count,
                "row_digest_sum": f"{row_digest_sum:064x}",
            }
            encoded = json.dumps(
                request_payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._requests.append(
                {
                    "phase": self._phase,
                    "sequence": header["sequence"],
                    "rows": row_count,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )

    @property
    def minute_request_digests(self) -> list[dict[str, object]]:
        return [dict(request) for request in self._requests]

    @property
    def minute_content_sha256(self) -> str:
        encoded = json.dumps(
            self._requests,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class DigestingMultiplierResolver:
    """Fingerprint effective multipliers by contract and consuming purpose."""

    def __init__(
        self,
        resolver,
        *,
        pricing_basis_by_exchange: Mapping[str, str],
    ) -> None:
        self._resolver = resolver
        self._pricing_basis_by_exchange = dict(pricing_basis_by_exchange)
        self._phase: str | None = None
        self._entries: dict[str, dict[str, object]] = {}

    def set_phase(self, phase: str) -> None:
        if phase not in {"roll_fills", "bars"}:
            raise ValueError(f"panel_multiplier_provenance_phase: {phase!r}")
        self._phase = phase

    def __call__(self, candidate, frame) -> int:
        if self._phase is None:
            raise ValueError("panel_multiplier_provenance_phase: phase must be set")
        resolved = self._resolver(candidate, frame)
        if isinstance(resolved, bool) or not isinstance(resolved, Integral):
            raise ValueError(
                "panel_multiplier_provenance_value: multiplier must be an integer"
            )
        multiplier = int(resolved)
        if multiplier <= 0:
            raise ValueError(
                "panel_multiplier_provenance_value: multiplier must be positive"
            )
        purpose = "bar" if self._phase == "bars" else candidate.candidate_role
        entry: dict[str, object] = {
            "purpose": purpose,
            "product": candidate.product,
            "daily_contract": candidate.daily_contract,
            "minute_symbol": candidate.minute_symbol,
            "exchange": candidate.exchange,
            "trade_date": candidate.trade_date.isoformat(),
            "window_start": candidate.window_start.isoformat(),
            "window_end": candidate.window_end.isoformat(),
            "pricing_basis": self._pricing_basis_by_exchange.get(
                candidate.exchange, "amount_vwap"
            ),
            "resolved_multiplier": multiplier,
        }
        identity = json.dumps(
            {
                key: value
                for key, value in entry.items()
                if key != "resolved_multiplier"
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = self._entries.get(identity)
        if previous is not None and previous != entry:
            raise ValueError(
                "panel_multiplier_provenance_conflict: "
                f"purpose={purpose!r} contract={candidate.daily_contract!r}"
            )
        self._entries[identity] = entry
        return multiplier

    @property
    def multiplier_resolutions_sha256(self) -> str:
        resolutions = sorted(
            self._entries.values(),
            key=lambda entry: json.dumps(
                entry,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        payload = json.dumps(
            {
                "version": 1,
                "resolutions": resolutions,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def assert_complete(self, *, bars: pd.DataFrame, roll_fills: pd.DataFrame) -> None:
        recorded_bars = {
            (str(entry["minute_symbol"]), int(entry["resolved_multiplier"]))
            for entry in self._entries.values()
            if entry["purpose"] == "bar"
        }
        missing: list[str] = []
        if not bars.empty:
            required = {"contract", "multiplier"}
            if not required.issubset(bars.columns):
                missing.append("bars_schema")
            else:
                used_bars = {
                    (str(contract), int(multiplier))
                    for contract, multiplier in bars.loc[
                        :, ["contract", "multiplier"]
                    ].itertuples(index=False, name=None)
                }
                missing.extend(
                    f"bar:{contract}:{multiplier}"
                    for contract, multiplier in sorted(used_bars - recorded_bars)
                )

        recorded_rolls = {
            (
                str(entry["trade_date"]),
                str(entry["daily_contract"]),
                str(entry["purpose"]),
            )
            for entry in self._entries.values()
            if entry["purpose"] in {"roll_old", "roll_new"}
        }
        if not roll_fills.empty:
            required = {"trade_date", "old_contract", "new_contract"}
            if not required.issubset(roll_fills.columns):
                missing.append("roll_fills_schema")
            else:
                for trade_date, old_contract, new_contract in roll_fills.loc[
                    :, ["trade_date", "old_contract", "new_contract"]
                ].itertuples(index=False, name=None):
                    date_key = pd.Timestamp(trade_date).date().isoformat()
                    for contract, purpose in (
                        (old_contract, "roll_old"),
                        (new_contract, "roll_new"),
                    ):
                        key = (date_key, str(contract), purpose)
                        if key not in recorded_rolls:
                            missing.append(":".join(key))
        if missing:
            raise ValueError(
                "panel_multiplier_provenance_missing: "
                f"used_without_resolution={sorted(missing)!r}"
            )


def _contract_changed(previous, current) -> bool:
    old_product, old_symbol, old_exchange = minute_contract_identity(
        previous.contract, current.trade_date
    )
    new_product, new_symbol, new_exchange = minute_contract_identity(
        current.contract, current.trade_date
    )
    if old_product != new_product or old_exchange != new_exchange:
        raise ValueError(
            "roll_contract_identity: dominant roll legs disagree on product/exchange; "
            f"{previous.contract!r} -> {current.contract!r}"
        )
    return old_symbol != new_symbol


def _roll_events(
    choices: Sequence[object], contexts: Mapping[tuple[date, str], object]
):
    previous_by_product: dict[str, object] = {}
    events = []
    for current in sorted(choices, key=lambda item: (item.trade_date, item.product)):
        previous = previous_by_product.get(current.product)
        previous_by_product[current.product] = current
        if previous is None or (current.trade_date, current.product) not in contexts:
            continue
        if _contract_changed(previous, current):
            events.append(
                (previous, current, contexts[(current.trade_date, current.product)])
            )
    return events


def _roll_candidate(choice, context, *, role: str) -> MinuteCandidate:
    product, minute_symbol, exchange = minute_contract_identity(
        choice.contract, choice.trade_date
    )
    slots = context.slots[:FILL_MINUTES]
    if len(slots) != FILL_MINUTES:
        raise ValueError(
            "roll_fill_slots: next session does not contain five authoritative slots; "
            f"{choice.trade_date} {choice.contract!r}"
        )
    return MinuteCandidate(
        trade_date=choice.trade_date,
        product=product,
        daily_contract=choice.contract,
        minute_symbol=minute_symbol,
        exchange=exchange,
        window_start=slots[0],
        window_end=slots[-1] + timedelta(minutes=1),
        candidate_role=role,
        causal_in_pool_date=choice.selected_from,
        selection_source="daily_both_max_irreversible_roll",
    )


def build_roll_fills(
    *,
    choices: Sequence[object],
    contexts: Mapping[tuple[date, str], object],
    source,
    pricing_basis_by_exchange: Mapping[str, str],
    multiplier_resolver,
) -> pd.DataFrame:
    """Price both concrete legs of every in-scope roll in one bounded batch/month."""
    events = _roll_events(choices, contexts)
    if not events:
        return normalise_bundle_table(
            "roll_fills",
            pd.DataFrame(columns=list(TABLE_SCHEMAS["roll_fills"])),
        )

    by_month: dict[tuple[int, int], list[tuple[object, object, object]]] = {}
    for event in events:
        current = event[1]
        by_month.setdefault(
            (current.trade_date.year, current.trade_date.month), []
        ).append(event)

    records: list[dict[str, object]] = []
    for month_events in by_month.values():
        candidates: list[MinuteCandidate] = []
        legs_by_event = []
        for previous, current, context in month_events:
            old_candidate = _roll_candidate(
                replace(previous, trade_date=current.trade_date),
                context,
                role="roll_old",
            )
            new_candidate = _roll_candidate(current, context, role="roll_new")
            candidates.extend((old_candidate, new_candidate))
            legs_by_event.append(
                (previous, current, context, old_candidate, new_candidate)
            )

        lower = min(candidate.window_start for candidate in candidates)
        upper = max(candidate.window_end for candidate in candidates)
        chunks = list(source.iter_month(candidates, lower, upper))
        minute = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()

        for previous, current, context, old_candidate, new_candidate in legs_by_event:
            prices = []
            bases = []
            for candidate in (old_candidate, new_candidate):
                if candidate.exchange not in pricing_basis_by_exchange:
                    raise ValueError(
                        "roll_fill_pricing_basis_missing: "
                        f"exchange={candidate.exchange!r}"
                    )
                basis = pricing_basis_by_exchange[candidate.exchange]
                if minute.empty:
                    frame = minute
                else:
                    frame = minute.loc[
                        (minute["trade_date"] == candidate.trade_date)
                        & (minute["daily_contract"] == candidate.daily_contract)
                    ].copy()
                try:
                    multiplier = multiplier_resolver(candidate, frame)
                    fill = five_minute_vwap(
                        frame,
                        slots=context.slots[:FILL_MINUTES],
                        contract=candidate.minute_symbol,
                        multiplier=multiplier,
                        pricing_basis=basis,
                    )
                except (MinuteDataError, KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "roll_fill_unpriceable: both raw dominant legs are required; "
                        f"{current.trade_date} {current.product} "
                        f"{previous.contract!r} -> {current.contract!r}; "
                        f"failed={candidate.daily_contract!r}"
                    ) from exc
                prices.append(fill.price)
                bases.append(basis)

            records.append(
                {
                    "trade_date": current.trade_date,
                    "product": current.product,
                    "old_contract": previous.contract,
                    "new_contract": current.contract,
                    "fill_time": context.slots[FILL_MINUTES - 1],
                    "old_price": prices[0],
                    "new_price": prices[1],
                    "old_pricing_basis": bases[0],
                    "new_pricing_basis": bases[1],
                }
            )
    return normalise_bundle_table(
        "roll_fills",
        pd.DataFrame.from_records(records, columns=list(TABLE_SCHEMAS["roll_fills"])),
    )


def adjust_signal_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply back-adjustment only to signal OHLC; concrete fill prices stay raw."""
    required = {"adj_factor", "open", "high", "low", "close", "fill_price"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"panel_adjust_columns: missing={sorted(missing)!r}")
    out = frame.copy().reset_index(drop=True)
    factors = pd.to_numeric(out["adj_factor"], errors="coerce")
    if factors.isna().any() or (factors <= 0).any():
        raise ValueError("panel_adjust_factor: factors must be finite and positive")
    for column in ("open", "high", "low", "close"):
        out[column] = pd.to_numeric(out[column], errors="coerce") * factors
    return out


def _universe_frame(products_by_month: Mapping[date, Sequence[str]]) -> pd.DataFrame:
    records = [
        {"month_start": month, "product": product}
        for month in sorted(products_by_month)
        for product in sorted(products_by_month[month])
    ]
    return normalise_bundle_table(
        "universes",
        pd.DataFrame.from_records(records, columns=list(TABLE_SCHEMAS["universes"])),
    )


def _dominant_frame(
    choices: Sequence[object],
    *,
    contexts: Mapping[tuple[date, str], object],
    factor_by_key: Mapping[tuple[date, str], float],
) -> pd.DataFrame:
    records = []
    for choice in choices:
        key = (choice.trade_date, choice.product)
        if key not in contexts:
            continue
        records.append(
            {
                "trade_date": choice.trade_date,
                "product": choice.product,
                "contract": choice.contract,
                "oi": choice.oi,
                "volume": choice.volume,
                "selected_from": choice.selected_from,
                "adj_factor": factor_by_key[key],
            }
        )
    return normalise_bundle_table(
        "dominants",
        pd.DataFrame.from_records(records, columns=list(TABLE_SCHEMAS["dominants"])),
    )


def _target_contexts(
    *,
    choices: Sequence[object],
    rules,
    start: date,
    end: date,
):
    contexts = {}
    for month in _months(start, end):
        selected = context_choices_for_month(choices, month_start=month)
        monthly = build_contexts(selected, rules=rules)
        for key, context in monthly.items():
            if start <= key[0] <= end and _month_start(key[0]) == month:
                contexts[key] = context
    return contexts


def _contexts_sha256(contexts: Mapping[tuple[date, str], object]) -> str:
    records = []
    for key in sorted(contexts):
        context = contexts[key]
        candidate = context.candidate
        records.append(
            {
                "trade_date": candidate.trade_date.isoformat(),
                "product": candidate.product,
                "daily_contract": candidate.daily_contract,
                "minute_symbol": candidate.minute_symbol,
                "exchange": candidate.exchange,
                "window_start": candidate.window_start.isoformat(),
                "window_end": candidate.window_end.isoformat(),
                "session_rule_version": context.rule.version,
                "slot_count": len(context.slots),
            }
        )
    return _frame_sha256(pd.DataFrame.from_records(records))


def _reliable_end(rules) -> date:
    ends = [rule.effective_end for rule in rules if rule.effective_end is not None]
    if not ends:
        raise ValueError("panel_session_authority: session rules have no finite bound")
    return max(ends)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end must not precede --start")

    rules = load_session_rules(SESSION_RULES)
    reliable_end = _reliable_end(rules)
    try:
        args.end = _resolve_end(
            args.end,
            reliable_end=reliable_end,
            legacy_month=args._legacy_end_month,
        )
    except ValueError as exc:
        parser.error(str(exc))
    bases = load_pricing_bases(PRICING_BASES)

    settings_path = args.settings or resolve_settings_path()
    cfg = load_config(settings_path)
    pg = pg_config_from(cfg, use_test=args.use_test)
    with get_connection(pg) as connection, connection.cursor() as cursor:
        cursor.execute("SET statement_timeout='900s'")
        daily = _copy_daily(cursor, end=args.end)
    print(f"daily rows: {len(daily):,}", flush=True)

    turnover = product_daily_turnover(
        daily.loc[:, ["symbol", "trade_date", "turnover"]]
    )
    products_by_month = {
        month: universe_for_month(turnover, month_start=month)
        for month in _months(args.start, args.end)
    }
    history_products = tuple(sorted(set(turnover["product"]) - FINANCIAL_FUTURES))
    if not history_products:
        raise ValueError("panel_products_empty: no commodity products in daily history")

    choices = choose_dominant_commodity(daily, products=history_products)
    closes = {
        (trade_date, str(symbol)): float(close)
        for trade_date, symbol, close in daily.loc[
            daily["close"].notna(), ["trade_date", "symbol", "close"]
        ].itertuples(index=False, name=None)
    }
    factors = adjustment_factors(choices, closes=closes)
    factor_by_key = {
        (row.trade_date, row.product): float(row.adj_factor)
        for row in factors.itertuples(index=False)
    }
    contexts = _target_contexts(
        choices=choices,
        rules=rules,
        start=args.start,
        end=args.end,
    )
    if not contexts:
        raise ValueError("panel_contexts_empty: no reliable dominant sessions in range")

    basis_by_exchange = {
        context.candidate.exchange: pricing_basis_for(bases, context.candidate.exchange)
        for context in contexts.values()
    }
    source = DigestingMinuteSource(
        PublicMinuteSource(pg=pg),
        pricing_basis_by_exchange=basis_by_exchange,
    )
    multiplier_cache: dict[str, int] = {}

    def resolve_multiplier(candidate, frame):
        key = candidate.minute_symbol
        if key not in multiplier_cache:
            resolution = source.resolve_metadata_multiplier(
                daily_contract=candidate.daily_contract,
                trade_date=candidate.trade_date,
                frame=frame,
                inference_frame=frame,
                pricing_basis=pricing_basis_for(bases, candidate.exchange),
            )
            multiplier_cache[key] = resolution.multiplier
        return multiplier_cache[key]

    multiplier_resolver = DigestingMultiplierResolver(
        resolve_multiplier,
        pricing_basis_by_exchange=basis_by_exchange,
    )

    source.set_phase("roll_fills")
    multiplier_resolver.set_phase("roll_fills")
    roll_fills = build_roll_fills(
        choices=choices,
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange=basis_by_exchange,
        multiplier_resolver=multiplier_resolver,
    )
    source.set_phase("bars")
    multiplier_resolver.set_phase("bars")
    raw_bars = build_panel(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange=basis_by_exchange,
        multiplier_resolver=multiplier_resolver,
        adjustment_factor_by_key={key: factor_by_key[key] for key in contexts},
    )
    bars = adjust_signal_bars(raw_bars)
    universes = _universe_frame(products_by_month)
    dominants = _dominant_frame(choices, contexts=contexts, factor_by_key=factor_by_key)
    multiplier_resolver.assert_complete(bars=bars, roll_fills=roll_fills)

    audit = source.audit
    source_revision = _source_revision()
    effective_config_sha256, _ = _effective_config_sha256(
        cfg,
        SESSION_RULES,
        PRICING_BASES,
        source_revision=source_revision,
    )
    bundle = write_bundle(
        args.output_dir,
        bars=bars,
        universes=universes,
        dominants=dominants,
        roll_fills=roll_fills,
        inputs={
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "daily_history_start": DAILY_HISTORY_START.isoformat(),
            "daily_relation": "public.futures_daily",
            "minute_relation": "public.futures_minute",
            "daily_rows": len(daily),
            "daily_sha256": _frame_sha256(daily),
            "minute_candidates_sha256": _contexts_sha256(contexts),
            "minute_content_sha256": source.minute_content_sha256,
            "minute_request_digests": source.minute_request_digests,
            "multiplier_resolutions_sha256": (
                multiplier_resolver.multiplier_resolutions_sha256
            ),
            "effective_config_sha256": effective_config_sha256,
        },
        provenance={
            **source_revision,
            "session_rules_file": SESSION_RULES.relative_to(_REPO_ROOT).as_posix(),
            "session_rules_sha256": _file_sha256(SESSION_RULES),
            "session_rules_reliable_end": reliable_end.isoformat(),
            "pricing_basis_file": PRICING_BASES.relative_to(_REPO_ROOT).as_posix(),
            "pricing_basis_sha256": _file_sha256(PRICING_BASES),
            "dominant_selection": "daily_both_max_irreversible_lag_1",
            "database_profile": "test" if args.use_test else "production",
            "roll_fill_minutes": FILL_MINUTES,
            "minute_query_months": audit.minute_query_months,
            "minute_rows": audit.minute_rows,
            "minute_candidate_contract_days": audit.minute_candidate_contract_days,
        },
    )
    print(
        f"bundle: {args.output_dir} bars={len(bundle.bars):,} "
        f"universes={len(bundle.universes):,} "
        f"dominants={len(bundle.dominants):,} rolls={len(bundle.roll_fills):,}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
