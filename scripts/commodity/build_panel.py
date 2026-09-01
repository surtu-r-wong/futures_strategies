"""Build the shared, versioned commodity panel bundle.

Cached OHLC and every execution price are raw concrete-contract values.
Signal consumers apply the cached adjustment factor exactly once.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
import io
import json
import fcntl
import math
from numbers import Integral, Real
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
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
from common.commodity.panel import (
    require_session_coverage,  # noqa: E402
    FILL_MINUTES,
    UncoveredProductDay,
    build_contexts,
    context_choices_for_month,
    iter_panel_months,
    normalise_panel,
)
from common.commodity.universe import (  # noqa: E402
    FINANCIAL_FUTURES,
    product_daily_turnover,
    shadow_scope,
    universe_for_month,
)
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from common.minute.bars import (  # noqa: E402
    UNRESOLVED_MULTIPLIER_CHECKS,
    infer_contract_multiplier,
    MinuteDataError,
    _range_diagnostics,
    five_minute_vwap,
    validate_metadata_multiplier,
)
from common.minute.pg_source import (  # noqa: E402
    MinuteCandidate,
    PublicMinuteSource,
    minute_contract_identity,
)
from common.minute.sessions import load_session_rules  # noqa: E402
from cta_carry.session_authority import (  # noqa: E402
    load_absent_product_days,
    load_pricing_bases,
    pricing_basis_for,
)

#: 默认的时段规则资产 = 连续信号那一份。**这个 main 有两个消费者**：连续信号
#: （`scripts/continuous/build_panel.py` 直接委派过来）和商品复刻，后者的宇宙更宽、
#: 有自己的第三份资产。两份不可互换 —— 连续信号要 2011 年的规则，商品复刻的采集
#: 从 2012-01-04 起 —— 所以资产走参数，落进 manifest 的 provenance 里可查。
SESSION_RULES = _REPO_ROOT / "config" / "continuous_minute_sessions.csv"
PRICING_BASES = _REPO_ROOT / "config" / "carry_minute_pricing_basis.csv"
ABSENT_PRODUCT_DAYS = _REPO_ROOT / "config" / "carry_minute_absent_product_days.csv"
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
        help=(
            "bundle directory (legacy alias: --out); incomplete monthly source "
            "work resumes from a digest-bound .panel-checkpoint"
        ),
    )
    parser.add_argument(
        "--settings",
        type=Path,
        help="settings YAML; defaults to the repository settings convention",
    )
    parser.add_argument(
        "--session-rules",
        dest="session_rules",
        type=Path,
        default=SESSION_RULES,
        help=(
            "versioned session-rule asset; defaults to the continuous-signal "
            "asset, the commodity replication passes its own"
        ),
    )
    parser.add_argument("--use-test", action="store_true")
    return parser


def _asset_label(path: Path) -> str:
    """Name an asset for provenance: repo-relative when it lives here."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


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
_CREDENTIAL_VALUE = (
    re.compile(r"[a-z][a-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@", re.IGNORECASE),
    re.compile(
        r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\bBearer\s+\S{12,}|\bsk-[A-Za-z0-9_-]{16,})",
        re.IGNORECASE,
    ),
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
    if type(value) is str:
        if any(pattern.search(value) for pattern in _CREDENTIAL_VALUE):
            return None
        return value
    if value is None or type(value) in (int, bool):
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


def _minute_row_payloads(frame: pd.DataFrame):
    """Yield exact row encodings without retaining bytes proportional to rows."""
    missing = set(_MINUTE_DIGEST_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"panel_minute_digest_columns: missing={sorted(missing)!r}")
    schema = b"commodity-minute-row-v2\0" + b"".join(
        _encoded_field(b"C", column.encode("utf-8"))
        for column in _MINUTE_DIGEST_COLUMNS
    )
    rows = frame.loc[:, _MINUTE_DIGEST_COLUMNS].itertuples(index=False, name=None)
    for row in rows:
        yield (
            schema
            + b"R"
            + b"".join(
                _minute_scalar_bytes(column, value)
                for column, value in zip(_MINUTE_DIGEST_COLUMNS, row, strict=True)
            )
        )


def _minute_frame_evidence(frame: pd.DataFrame) -> dict[str, object]:
    """Return a bounded-memory, row-order-neutral exact typed frame digest."""
    row_count = 0
    row_digest_sum = 0
    for payload in _minute_row_payloads(frame):
        row_count += 1
        row_digest_sum = (
            row_digest_sum + int.from_bytes(hashlib.sha256(payload).digest(), "big")
        ) % _DIGEST_MODULUS
    return {
        "rows": row_count,
        "row_digest_sum": f"{row_digest_sum:064x}",
    }


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
                for line in _minute_row_payloads(frame):
                    row_count += 1
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

    def checkpoint_state(self) -> object:
        underlying = (
            self._source.checkpoint_audit_state()
            if hasattr(self._source, "checkpoint_audit_state")
            else None
        )
        return {"requests": self.minute_request_digests, "underlying_audit": underlying}

    def restore_checkpoint_state(self, state: object) -> None:
        if type(state) is not dict or set(state) != {
            "requests",
            "underlying_audit",
        } or type(
            state["requests"]
        ) is not list:
            raise ValueError("panel_checkpoint_state: invalid minute state")
        restored = [dict(item) for item in state["requests"]]
        if self._requests and restored[: len(self._requests)] != self._requests:
            raise ValueError("panel_checkpoint_state: minute prefix mismatch")
        if state["underlying_audit"] is not None:
            if not hasattr(self._source, "restore_checkpoint_audit_state"):
                raise ValueError("panel_checkpoint_state: minute audit unavailable")
            self._source.restore_checkpoint_audit_state(state["underlying_audit"])
        self._requests = restored

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
    """Fingerprint complete multiplier resolutions and their exact evidence."""

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
        if hasattr(self._resolver, "set_phase"):
            self._resolver.set_phase(phase)

    def __call__(self, candidate, frame, *, inference_frame=None) -> int:
        if self._phase is None:
            raise ValueError("panel_multiplier_provenance_phase: phase must be set")
        resolved = self._resolver(candidate, frame, inference_frame=inference_frame)
        if isinstance(resolved, bool):
            raise ValueError(
                "panel_multiplier_provenance_value: multiplier must be an integer"
            )
        if isinstance(resolved, Integral):
            multiplier = int(resolved)
            resolution_evidence: object = {
                "kind": "direct_integer",
                "multiplier": {"integer": str(multiplier)},
            }
        else:
            try:
                multiplier = int(resolved.multiplier)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(
                    "panel_multiplier_provenance_value: resolver must return an "
                    "integer or an audited resolution"
                ) from exc
            if isinstance(resolved.multiplier, bool) or (
                not isinstance(resolved.multiplier, Integral)
            ):
                raise ValueError(
                    "panel_multiplier_provenance_value: multiplier must be an integer"
                )
            resolution_evidence = _canonical_resolution_evidence(resolved)
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
            "resolution_evidence": resolution_evidence,
            # The production metadata resolver receives this same exact frame
            # for both validation and inference; record it once explicitly.
            "validation_and_inference_frame": _minute_frame_evidence(frame),
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

    def checkpoint_state(self) -> object:
        return {"entries": list(self._entries.values())}

    def restore_checkpoint_state(self, state: object) -> None:
        if type(state) is not dict or set(state) != {"entries"} or type(
            state["entries"]
        ) is not list:
            raise ValueError("panel_checkpoint_state: invalid multiplier state")
        restored: dict[str, dict[str, object]] = {}
        for raw in state["entries"]:
            if type(raw) is not dict:
                raise ValueError("panel_checkpoint_state: invalid multiplier entry")
            entry = dict(raw)
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
            if identity in restored and restored[identity] != entry:
                raise ValueError("panel_checkpoint_state: multiplier conflict")
            restored[identity] = entry
        for identity, entry in self._entries.items():
            if restored.get(identity) != entry:
                raise ValueError("panel_checkpoint_state: multiplier prefix mismatch")
        self._entries = restored

    def assert_complete(self, *, bars: pd.DataFrame, roll_fills: pd.DataFrame) -> None:
        recorded_bars = {
            (
                str(entry["trade_date"]),
                str(entry["daily_contract"]),
                int(entry["resolved_multiplier"]),
            )
            for entry in self._entries.values()
            if entry["purpose"] == "bar"
        }
        missing: list[str] = []
        if not bars.empty:
            required = {"trade_date", "contract", "multiplier"}
            if not required.issubset(bars.columns):
                missing.append("bars_schema")
            else:
                used_bars = {
                    (
                        pd.Timestamp(trade_date).date().isoformat(),
                        str(contract),
                        int(multiplier),
                    )
                    for trade_date, contract, multiplier in bars.loc[
                        :, ["trade_date", "contract", "multiplier"]
                    ].itertuples(index=False, name=None)
                }
                missing.extend(
                    f"bar:{trade_date}:{contract}:{multiplier}"
                    for trade_date, contract, multiplier in sorted(
                        used_bars - recorded_bars
                    )
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


def _canonical_evidence_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"boolean": value}
    if isinstance(value, Integral):
        return {"integer": str(int(value))}
    if isinstance(value, Real):
        return {"float64": struct.pack(">d", float(value)).hex()}
    if isinstance(value, datetime):
        return {
            "datetime": value.isoformat(),
            "timezone_aware": value.tzinfo is not None,
        }
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, str):
        return {"string": value}
    if isinstance(value, (list, tuple)):
        return [_canonical_evidence_value(item) for item in value]
    raise ValueError(
        "panel_multiplier_provenance_evidence: unsupported returned field type "
        f"{type(value).__name__!r}"
    )


def _canonical_resolution_evidence(resolution: object) -> dict[str, object]:
    if is_dataclass(resolution) and not isinstance(resolution, type):
        names = [field.name for field in fields(resolution)]
    else:
        names = [
            "multiplier",
            "source",
            "sample_rows",
            "pass_rate",
            "sample_dates",
            "sample_start",
            "sample_end",
            "max_range_error",
        ]
        names = [name for name in names if hasattr(resolution, name)]
    if "multiplier" not in names:
        raise ValueError(
            "panel_multiplier_provenance_evidence: multiplier field is required"
        )
    return {
        "type": type(resolution).__name__,
        "fields": {
            name: _canonical_evidence_value(getattr(resolution, name)) for name in names
        },
    }


def _amount_disagrees_with_daily(
    frame: pd.DataFrame,
    *,
    minute_symbol: str,
    declared_turnover: float | None,
) -> float | None:
    """这一天的分钟成交额与日线记录对得上吗？对不上就是 `amount` 那一列坏了。

    焦煤 JM1309 在 2013-04-08 与 04-11 两天，分钟 `amount` **恰好是日线 turnover 的
    1/6**（乘数 60、按 10 合成），价格反推出 202 而当天价带是 1211–1216；同一张合约
    前后几天 amount 与日线**逐日精确相等**、成交量全程精确相等。所以矛盾的不是乘数，
    是这一天的成交额列。

    日线是独立于分钟归档的另一份记录，拿它当裁判就不需要任何阈值判断"谁更可信"：
    对得上 ⇒ 分钟与日线一致，那矛盾只能出在乘数上，照旧硬失败；对不上 ⇒ 这天的
    成交额不可用，`five_minute_vwap` 自己的区间校验会把成交价拒掉（bar 照常出，
    `fill_unpriceable=True`），不需要合成任何价格。
    """
    if declared_turnover is None or not math.isfinite(declared_turnover):
        return None
    traded = frame.loc[
        (frame["symbol"] == minute_symbol)
        & (pd.to_numeric(frame["volume"], errors="coerce") > 0)
    ]
    if traded.empty:
        return None
    observed = float(pd.to_numeric(traded["amount"], errors="coerce").fillna(0.0).sum())
    if not math.isfinite(observed) or observed <= 0.0:
        return None
    if math.isclose(observed, declared_turnover, rel_tol=1e-3):
        return None
    return observed


def _sibling_multiplier(sample, candidate):
    """同品种**兄弟合约**推出的乘数 —— 乘数是品种级常量，交易所公告改动才变。

    本合约自己推不出来时（元数据缺档 + 取样跨不到足够多的交易日，白银 AG1209
    2012-05 就是），拿同一份样本里同品种的其他合约各自推一遍：**两张以上推出同一个
    值才采用**，不一致或不足两张仍然维持原来的硬失败。2026-08-31 用户裁决。
    """
    if sample is None or getattr(sample, "empty", True):
        return None
    if "symbol" not in sample.columns:
        return None
    siblings = sorted(
        {str(symbol) for symbol in sample["symbol"]} - {candidate.minute_symbol}
    )
    resolutions = []
    for symbol in siblings:
        rows = sample.loc[sample["symbol"].astype(str) == symbol]
        if rows.empty:
            continue
        try:
            resolutions.append(infer_contract_multiplier(rows, contract=symbol))
        except MinuteDataError:
            continue
    values = {int(item.multiplier) for item in resolutions}
    if len(resolutions) < 2 or len(values) != 1:
        return None
    best = max(resolutions, key=lambda item: (item.sample_dates, item.sample_rows))
    return replace(
        best,
        source="sibling_inference",
        resolution_path="sibling_inference",
    )


def _metadata_multiplier_resolution(
    source,
    pricing_basis_by_exchange: Mapping[str, str],
    candidate,
    frame: pd.DataFrame,
    *,
    inference_frame: pd.DataFrame | None = None,
):
    """Resolve against this call's validation frame and its wider inference sample.

    校验用当天那一帧；**推断**（元数据缺档时才走）用调用方给的更宽样本 —— 取样要
    跨多个交易日，一个品种日给不出来。
    """
    if candidate.exchange not in pricing_basis_by_exchange:
        raise ValueError(
            f"panel_multiplier_pricing_basis_missing: exchange={candidate.exchange!r}"
        )
    sample = frame if inference_frame is None else inference_frame
    # 推断只认**这一张合约**的行（`_validated_multiplier_rows` 见到别的合约会直接报
    # `minute_contract`）。宽样本是给兄弟合约兜底用的，先按合约切出自己的那一份。
    own = sample
    if not sample.empty and "symbol" in sample.columns:
        symbols = {str(symbol) for symbol in sample["symbol"]}
        if symbols - {candidate.minute_symbol}:
            own = sample.loc[sample["symbol"].astype(str) == candidate.minute_symbol]
            if own.empty:
                own = frame
    try:
        return source.resolve_metadata_multiplier(
            daily_contract=candidate.daily_contract,
            trade_date=candidate.trade_date,
            frame=frame,
            inference_frame=own,
            pricing_basis=pricing_basis_by_exchange[candidate.exchange],
        )
    except MinuteDataError as exc:
        if getattr(exc, "check", None) not in UNRESOLVED_MULTIPLIER_CHECKS:
            raise
        resolved = _sibling_multiplier(sample, candidate)
        if resolved is None:
            raise
        return resolved


def _checkpoint_scalar(value: object) -> object:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, Integral):
        return {"type": "int", "value": str(int(value))}
    if isinstance(value, Real):
        return {"type": "float64", "value": struct.pack(">d", float(value)).hex()}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, (list, tuple)):
        return {"type": "tuple", "value": [_checkpoint_scalar(item) for item in value]}
    raise ValueError(
        f"panel_checkpoint_state: unsupported value {type(value).__name__!r}"
    )


def _restore_checkpoint_scalar(payload: object) -> object:
    if type(payload) is not dict or "type" not in payload:
        raise ValueError("panel_checkpoint_state: invalid encoded value")
    kind = payload["type"]
    if kind == "null" and set(payload) == {"type"}:
        return None
    if set(payload) != {"type", "value"}:
        raise ValueError("panel_checkpoint_state: invalid encoded value shape")
    value = payload["value"]
    if kind == "bool" and type(value) is bool:
        return value
    if kind == "int" and type(value) is str:
        return int(value)
    if kind == "float64" and type(value) is str:
        return struct.unpack(">d", bytes.fromhex(value))[0]
    if kind == "datetime" and type(value) is str:
        return datetime.fromisoformat(value)
    if kind == "date" and type(value) is str:
        return date.fromisoformat(value)
    if kind == "string" and type(value) is str:
        return value
    if kind == "tuple" and type(value) is list:
        return tuple(_restore_checkpoint_scalar(item) for item in value)
    raise ValueError("panel_checkpoint_state: invalid encoded value type")


class CachingMetadataMultiplierResolver:
    """Resolve remotely once per concrete contract/basis and audit every use."""

    def __init__(
        self,
        source,
        *,
        pricing_basis_by_exchange: Mapping[str, str],
        daily_turnover_by_key: Mapping[tuple[str, date], float] | None = None,
    ) -> None:
        self._source = source
        self._pricing_basis_by_exchange = dict(pricing_basis_by_exchange)
        self._daily_turnover = dict(daily_turnover_by_key or {})
        self._phase = "default"
        self._cache: dict[tuple[str, str, str], tuple[date, object]] = {}
        self._amount_disagreements: dict[tuple[str, date], dict[str, object]] = {}

    def set_phase(self, phase: str) -> None:
        if phase not in {"roll_fills", "bars"}:
            raise ValueError(f"panel_multiplier_cache_phase: {phase!r}")
        self._phase = phase

    def __call__(self, candidate, frame: pd.DataFrame, *, inference_frame=None):
        if candidate.exchange not in self._pricing_basis_by_exchange:
            raise ValueError(
                "panel_multiplier_pricing_basis_missing: "
                f"exchange={candidate.exchange!r}"
            )
        pricing_basis = self._pricing_basis_by_exchange[candidate.exchange]
        # CZCE's three-digit daily identifiers recur by decade. The resolved
        # minute symbol is the concrete identity; phase separation prevents a
        # later roll-exit lookup from leaking into earlier historical bars.
        key = (self._phase, candidate.minute_symbol, pricing_basis)
        if key not in self._cache:
            self._cache[key] = (
                candidate.trade_date,
                _metadata_multiplier_resolution(
                    self._source,
                    self._pricing_basis_by_exchange,
                    candidate,
                    frame,
                    inference_frame=inference_frame,
                ),
            )
            return self._cache[key][1]

        resolved_as_of, resolution = self._cache[key]
        if candidate.trade_date < resolved_as_of:
            raise ValueError(
                "panel_multiplier_cache_causality: cached resolution is from a "
                f"later date; minute_symbol={candidate.minute_symbol!r}"
            )
        multiplier = (
            int(resolution)
            if isinstance(resolution, Integral)
            else int(resolution.multiplier)
        )
        if not frame.empty and pricing_basis == "amount_vwap":
            try:
                validate_metadata_multiplier(
                    frame,
                    contract=candidate.minute_symbol,
                    multiplier=multiplier,
                    source="cached_contract_resolution",
                )
            except MinuteDataError as exc:
                if exc.check != "contract_multiplier_sample":
                    raise ValueError(
                        "panel_multiplier_cached_conflict: "
                        f"contract={candidate.daily_contract!r} "
                        f"trade_date={candidate.trade_date.isoformat()}"
                    ) from exc
                # The production panel passes one day at a time, while the
                # shared validator intentionally requires multiple dates.
                # Apply its same price-range diagnostic locally so cached
                # values are still checked on every amount-based caller day.
                traded = frame.loc[
                    (frame["symbol"] == candidate.minute_symbol)
                    & (pd.to_numeric(frame["volume"], errors="coerce") > 0)
                ].copy()
                if not traded.empty:
                    for column in ("amount", "volume", "low", "high"):
                        traded[column] = pd.to_numeric(
                            traded[column], errors="coerce"
                        )
                    valid = traded[["amount", "volume", "low", "high"]].notna().all(
                        axis=1
                    )
                    pass_rate = (
                        _range_diagnostics(traded.loc[valid], multiplier)[0]
                        if valid.any()
                        else 1.0
                    )
                    if pass_rate < 0.60:
                        observed = _amount_disagrees_with_daily(
                            frame,
                            minute_symbol=candidate.minute_symbol,
                            declared_turnover=self._daily_turnover.get(
                                (candidate.daily_contract, candidate.trade_date)
                            ),
                        )
                        if observed is None:
                            raise ValueError(
                                "panel_multiplier_cached_conflict: local day evidence "
                                f"contradicts contract={candidate.daily_contract!r} "
                                f"trade_date={candidate.trade_date.isoformat()}"
                            ) from exc
                        # 日线否掉的是这天的成交额，不是乘数。乘数照用，这一天的
                        # 成交价由 `five_minute_vwap` 自己的区间校验拒掉。
                        self._amount_disagreements[
                            (candidate.daily_contract, candidate.trade_date)
                        ] = {
                            "trade_date": candidate.trade_date,
                            "product": candidate.product,
                            "contract": candidate.daily_contract,
                            "minute_amount": observed,
                            "daily_turnover": self._daily_turnover[
                                (candidate.daily_contract, candidate.trade_date)
                            ],
                        }
        return resolution

    @property
    def amount_disagreements(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self._amount_disagreements[key]
            for key in sorted(self._amount_disagreements)
        )

    def checkpoint_state(self) -> object:
        entries = []
        for (phase, minute_symbol, basis), (resolved_as_of, resolution) in sorted(
            self._cache.items()
        ):
            if isinstance(resolution, Integral):
                payload = {"kind": "integer", "value": str(int(resolution))}
            elif is_dataclass(resolution) and not isinstance(resolution, type):
                payload = {
                    "kind": "MultiplierResolution",
                    "fields": {
                        field.name: _checkpoint_scalar(getattr(resolution, field.name))
                        for field in fields(resolution)
                    },
                }
            else:
                raise ValueError("panel_checkpoint_state: unaudited cached multiplier")
            entries.append(
                {
                    "phase": phase,
                    "minute_symbol": minute_symbol,
                    "pricing_basis": basis,
                    "resolved_as_of": resolved_as_of.isoformat(),
                    "resolution": payload,
                }
            )
        # 成交额与日线对不上的那些天是这一跑的产出之一（验收要写它的数），
        # 与缓存一起过 checkpoint，续跑之后才不会只剩后半段。
        return {
            "entries": entries,
            "amount_disagreements": [
                {
                    "trade_date": row["trade_date"].isoformat(),
                    "product": str(row["product"]),
                    "contract": str(row["contract"]),
                    "minute_amount": repr(float(row["minute_amount"])),
                    "daily_turnover": repr(float(row["daily_turnover"])),
                }
                for row in self.amount_disagreements
            ],
        }

    def restore_checkpoint_state(self, state: object) -> None:
        from common.minute.bars import MultiplierResolution

        expected = {"entries", "amount_disagreements"}
        if type(state) is not dict or set(state) != expected:
            raise ValueError("panel_checkpoint_state: invalid cached multiplier state")
        if any(type(state[name]) is not list for name in expected):
            raise ValueError("panel_checkpoint_state: invalid cached multiplier state")
        disagreements: dict[tuple[str, date], dict[str, object]] = {}
        for row in state["amount_disagreements"]:
            if type(row) is not dict or set(row) != {
                "trade_date",
                "product",
                "contract",
                "minute_amount",
                "daily_turnover",
            }:
                raise ValueError("panel_checkpoint_state: invalid amount disagreement")
            trade_date = date.fromisoformat(str(row["trade_date"]))
            disagreements[(str(row["contract"]), trade_date)] = {
                "trade_date": trade_date,
                "product": str(row["product"]),
                "contract": str(row["contract"]),
                "minute_amount": float(row["minute_amount"]),
                "daily_turnover": float(row["daily_turnover"]),
            }
        restored: dict[tuple[str, str, str], tuple[date, object]] = {}
        for entry in state["entries"]:
            if type(entry) is not dict or set(entry) != {
                "phase",
                "minute_symbol",
                "pricing_basis",
                "resolved_as_of",
                "resolution",
            }:
                raise ValueError("panel_checkpoint_state: invalid cached multiplier entry")
            payload = entry["resolution"]
            if type(payload) is not dict:
                raise ValueError("panel_checkpoint_state: invalid cached resolution")
            if payload.get("kind") == "integer" and set(payload) == {"kind", "value"}:
                resolution: object = int(payload["value"])
            elif payload.get("kind") == "MultiplierResolution" and set(payload) == {
                "kind",
                "fields",
            } and type(payload["fields"]) is dict:
                resolution = MultiplierResolution(
                    **{
                        name: _restore_checkpoint_scalar(value)
                        for name, value in payload["fields"].items()
                    }
                )
            else:
                raise ValueError("panel_checkpoint_state: invalid cached resolution")
            key = (
                str(entry["phase"]),
                str(entry["minute_symbol"]),
                str(entry["pricing_basis"]),
            )
            if key in restored:
                raise ValueError("panel_checkpoint_state: duplicate cached multiplier")
            restored[key] = (date.fromisoformat(entry["resolved_as_of"]), resolution)
        for key, (resolved_as_of, resolution) in self._cache.items():
            if key not in restored or restored[key][0] != resolved_as_of or (
                _canonical_resolution_evidence(restored[key][1])
                != _canonical_resolution_evidence(resolution)
            ):
                raise ValueError("panel_checkpoint_state: cached multiplier prefix mismatch")
        for key in self._amount_disagreements:
            if key not in disagreements:
                raise ValueError(
                    "panel_checkpoint_state: amount disagreement prefix mismatch"
                )
        self._cache = restored
        self._amount_disagreements = disagreements


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
    choices: Sequence[object],
    contexts: Mapping[tuple[date, str], object],
    *,
    uncovered: frozenset[tuple[date, str]] = frozenset(),
):
    """面板看得见的那条链上的换月 —— 不覆盖的品种日既不发事件，也不当前态。

    bundle 的换月期望是从 `dominants` 反推的，而那张表只有被覆盖的品种日。前态若仍
    取自一个剔掉的日子，链在**这里**看是"合约没变"，在 bundle 看却是换了 —— 那次
    换月于是一张成交单都发不出，整跑在写 bundle 的最后一步作废。转移顺延到下一个
    看得见的交易日按常规定价；旧腿那时若已退市，照旧走「窗口零成交 ⇒ 不发单」。
    """
    previous_by_product: dict[str, object] = {}
    events = []
    for current in sorted(choices, key=lambda item: (item.trade_date, item.product)):
        key = (current.trade_date, current.product)
        if key in uncovered:
            continue
        previous = previous_by_product.get(current.product)
        previous_by_product[current.product] = current
        if previous is None or key not in contexts:
            continue
        if _contract_changed(previous, current):
            events.append((previous, current, contexts[key]))
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
    # 成交价只取头五分钟，但**请求整段**：合约乘数在元数据缺档时要靠推断，而推断
    # 需要至少十根有成交的分钟（`infer_contract_multiplier`）。五分钟最多给五根 ——
    # 聚丙烯上市第二周的换月就是这样把整跑打断的（PP1405 2014-03-05，
    # eligible_rows=5 required_rows=10，日线兜底也没兜住）。
    return MinuteCandidate(
        trade_date=choice.trade_date,
        product=product,
        daily_contract=choice.contract,
        minute_symbol=minute_symbol,
        exchange=exchange,
        window_start=context.slots[0],
        window_end=context.slots[-1] + timedelta(minutes=1),
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
    uncovered: frozenset[tuple[date, str]] = frozenset(),
    daily_turnover_by_key: Mapping[tuple[str, date], float] | None = None,
) -> tuple[pd.DataFrame, tuple[dict[str, object], ...]]:
    """Price both concrete legs of every in-scope roll in one bounded batch/month.

    返回 ``(fills, skipped)``。**成交窗口里零成交的那条腿定不出价，这样的换月不发
    成交单** —— 没有人能在建模的时点上完成这次转移。两种形态都真实存在：链断（旧
    合约在换月日之前就到期，AU1912 到期十天后主力才换到 AU2006）与薄成交（那一腿
    当天零成交，或成交都不落在开盘五分钟里，PP1405 2014-03-05）。连续价仍由日线
    收盘算出的复权因子缝合，与断代处「不要求换月成交单」同一条口径。

    其余任何定价失败仍然硬失败（乘数、定价基准、分钟行结构）—— 那是缺数据或口径
    错误，不是「没有市场」。
    """
    events = _roll_events(choices, contexts, uncovered=uncovered)
    if not events:
        return (
            normalise_bundle_table(
                "roll_fills",
                pd.DataFrame(columns=list(TABLE_SCHEMAS["roll_fills"])),
            ),
            (),
        )

    by_month: dict[tuple[int, int], list[tuple[object, object, object]]] = {}
    for event in events:
        current = event[1]
        by_month.setdefault(
            (current.trade_date.year, current.trade_date.month), []
        ).append(event)

    records: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
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
            unpriceable: dict[str, object] | None = None
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
                # 成交窗口零成交 ⇒ 这次转移没人能执行，不发单。**先看证据再定价**：
                # 一张当天没有任何分钟行的合约，连乘数都解析不出来（乘数校验要读价），
                # 那时抛出的错与「缺数据」长得一模一样。
                # 定价只用头五分钟；乘数解析用整段（见 `_roll_candidate`）。
                window = (
                    frame
                    if frame.empty
                    else frame.loc[
                        frame["bar_time"].isin(list(context.slots[:FILL_MINUTES]))
                    ].copy()
                )
                traded_volume = (
                    0.0
                    if window.empty
                    else float(
                        pd.to_numeric(window["volume"], errors="coerce")
                        .fillna(0.0)
                        .sum()
                    )
                )
                if traded_volume <= 0.0:
                    unpriceable = {
                        "trade_date": current.trade_date,
                        "product": current.product,
                        "old_contract": previous.contract,
                        "new_contract": current.contract,
                        "unpriceable_leg": candidate.daily_contract,
                    }
                    break
                try:
                    multiplier = multiplier_resolver(candidate, frame)
                except MinuteDataError as exc:
                    if getattr(exc, "check", None) not in UNRESOLVED_MULTIPLIER_CHECKS:
                        raise ValueError(
                            "roll_fill_unpriceable: both raw dominant legs are "
                            f"required; {current.trade_date} {current.product} "
                            f"{previous.contract!r} -> {current.contract!r}; "
                            f"failed={candidate.daily_contract!r}"
                        ) from exc
                    # 乘数定不出来 ⇒ 这条腿定不出价，与「窗口零成交」同一个结果：
                    # 不发单。元数据缺档时乘数只能从分钟推断，而推断要跨**多个交易日**
                    # 取样（`_select_multiplier_sample`），换月却只请求当天一段 ——
                    # 聚丙烯上市第二周的换月就卡在这里（PP1405 在 bars 阶段用整月
                    # 的行解析得出来，所以只是这一次转移发不出单）。bars 阶段用同样
                    # 的证据再解析一次：那里仍解不出来的，整个品种日不进面板。
                    unpriceable = {
                        "trade_date": current.trade_date,
                        "product": current.product,
                        "old_contract": previous.contract,
                        "new_contract": current.contract,
                        "unpriceable_leg": candidate.daily_contract,
                    }
                    break
                try:
                    fill = five_minute_vwap(
                        window,
                        slots=context.slots[:FILL_MINUTES],
                        contract=candidate.minute_symbol,
                        multiplier=multiplier,
                        pricing_basis=basis,
                    )
                except (MinuteDataError, KeyError, TypeError, ValueError) as exc:
                    # 这一天的分钟成交额与日线对不上 ⇒ 坏的是 `amount` 那一列，
                    # 没有人能在这条腿上按成交额定出价。与「窗口零成交」同一个结果：
                    # 不发单。日线对得上时仍然硬失败 —— 那才是口径或结构错了。
                    disagreeing = _amount_disagrees_with_daily(
                        frame,
                        minute_symbol=candidate.minute_symbol,
                        declared_turnover=(daily_turnover_by_key or {}).get(
                            (candidate.daily_contract, candidate.trade_date)
                        ),
                    )
                    if disagreeing is None:
                        raise ValueError(
                            "roll_fill_unpriceable: both raw dominant legs are "
                            f"required; {current.trade_date} {current.product} "
                            f"{previous.contract!r} -> {current.contract!r}; "
                            f"failed={candidate.daily_contract!r}"
                        ) from exc
                    unpriceable = {
                        "trade_date": current.trade_date,
                        "product": current.product,
                        "old_contract": previous.contract,
                        "new_contract": current.contract,
                        "unpriceable_leg": candidate.daily_contract,
                    }
                    break
                prices.append(fill.price)
                bases.append(basis)

            if unpriceable is not None:
                skipped.append(unpriceable)
                continue
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
    return (
        normalise_bundle_table(
            "roll_fills",
            pd.DataFrame.from_records(
                records, columns=list(TABLE_SCHEMAS["roll_fills"])
            ),
        ),
        tuple(sorted(skipped, key=lambda row: (row["trade_date"], row["product"]))),
    )


def _write_roll_manifest(output_dir: Path, skipped: Sequence[Mapping[str, object]]):
    """跳过的换月一次报全 —— 条数与键都要能与 bundle 的申报对上。

    清单描述的是**这一次**换月的结果：覆盖定稿后换月会重算，上一遍留下的文件必须
    跟着消失，否则落盘的清单与 manifest 申报的条数各说各话。
    """
    manifest = output_dir / "roll-fill-unpriceable.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if not skipped:
        manifest.unlink(missing_ok=True)
        print("unpriceable rolls: 0", flush=True)
        return None
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "trade_date",
                "product",
                "old_contract",
                "new_contract",
                "unpriceable_leg",
            ]
        )
        for row in skipped:
            writer.writerow(
                [
                    row["trade_date"].isoformat(),
                    row["product"],
                    row["old_contract"],
                    row["new_contract"],
                    row["unpriceable_leg"],
                ]
            )
    products = sorted({row["product"] for row in skipped})
    print(
        f"unpriceable rolls: {len(skipped)} across {len(products)} products "
        f"({' '.join(products)}); manifest={manifest}",
        flush=True,
    )
    return manifest


def _bundle_bars(
    frame: pd.DataFrame,
    *,
    contexts: Mapping[tuple[date, str], object],
    dominants: pd.DataFrame,
) -> pd.DataFrame:
    """Map legacy minute IDs to exact dominant IDs at the bundle boundary.

    Both spellings come from the already-resolved session context.  This boundary
    deliberately does not add exchange suffixes or otherwise guess contract IDs.
    """
    bar_required = {"trade_date", "product", "contract"}
    dominant_required = {"trade_date", "product", "contract"}
    if not bar_required.issubset(frame.columns):
        missing = sorted(bar_required - set(frame.columns))
        raise ValueError(
            f"panel_bundle_contract_mapping: missing bar columns={missing!r}"
        )
    if not dominant_required.issubset(dominants.columns):
        missing = sorted(dominant_required - set(dominants.columns))
        raise ValueError(
            f"panel_bundle_contract_mapping: missing dominant columns={missing!r}"
        )

    dominant_map = dominants.loc[:, ["trade_date", "product", "contract"]].copy()
    dominant_map["trade_date"] = pd.to_datetime(
        dominant_map["trade_date"], errors="coerce"
    ).dt.date
    dominant_map["product"] = dominant_map["product"].astype("string")
    if dominant_map[["trade_date", "product"]].isna().any(axis=None):
        raise ValueError("panel_bundle_contract_mapping: missing dominant key")
    duplicate = dominant_map.duplicated(["trade_date", "product"], keep=False)
    if duplicate.any():
        first = dominant_map.loc[duplicate, ["trade_date", "product"]].iloc[0]
        raise ValueError(
            "panel_bundle_contract_mapping: ambiguous daily dominant mapping; "
            f"first={(first['trade_date'], first['product'])!r}"
        )

    identity_records = []
    for key, context in contexts.items():
        candidate = context.candidate
        candidate_key = (candidate.trade_date, str(candidate.product))
        declared_key = (key[0], str(key[1]))
        if declared_key != candidate_key:
            raise ValueError(
                "panel_bundle_contract_mapping: context key mismatch; "
                f"declared={declared_key!r} candidate={candidate_key!r}"
            )
        identity_records.append(
            {
                "trade_date": candidate.trade_date,
                "product": str(candidate.product),
                "minute_contract": candidate.minute_symbol,
                "daily_contract": candidate.daily_contract,
            }
        )
    identity_map = pd.DataFrame.from_records(
        identity_records,
        columns=["trade_date", "product", "minute_contract", "daily_contract"],
    )
    identity_map["product"] = identity_map["product"].astype("string")

    mapping = dominant_map.merge(
        identity_map,
        on=["trade_date", "product"],
        how="outer",
        indicator=True,
        validate="one_to_one",
    )
    missing_mapping = mapping["_merge"].ne("both")
    if missing_mapping.any():
        first = mapping.loc[missing_mapping, ["trade_date", "product", "_merge"]].iloc[
            0
        ]
        raise ValueError(
            "panel_bundle_contract_mapping: missing identity mapping; "
            f"first={(first['trade_date'], first['product'], first['_merge'])!r}"
        )

    daily_mismatch = (
        mapping["contract"]
        .astype("string")
        .ne(mapping["daily_contract"].astype("string"))
        .fillna(True)
    )
    if daily_mismatch.any():
        first = mapping.loc[
            daily_mismatch,
            ["trade_date", "product", "contract", "daily_contract"],
        ].iloc[0]
        raise ValueError(
            "panel_bundle_contract_mapping: context daily contract disagrees with dominant; "
            f"first={tuple(first)!r}"
        )
    key_columns = ["trade_date", "product"]
    mapping_index = pd.MultiIndex.from_frame(mapping.loc[:, key_columns])
    minute_by_key = pd.Series(mapping["minute_contract"].array, index=mapping_index)
    daily_by_key = pd.Series(mapping["contract"].array, index=mapping_index)
    out = frame.copy().reset_index(drop=True)
    bar_dates = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
    bar_products = out["product"].astype("string")
    bar_index = pd.MultiIndex.from_arrays([bar_dates, bar_products], names=key_columns)
    expected_minute = minute_by_key.reindex(bar_index).reset_index(drop=True)
    daily_contract = daily_by_key.reindex(bar_index).reset_index(drop=True)
    if expected_minute.isna().any() or daily_contract.isna().any():
        first = next(
            index
            for index, value in enumerate(expected_minute)
            if pd.isna(value) or pd.isna(daily_contract.iloc[index])
        )
        raise ValueError(
            "panel_bundle_contract_mapping: missing bar identity mapping; "
            f"first={(bar_dates.iloc[first], bar_products.iloc[first])!r}"
        )
    raw_contract = out["contract"].astype("string")
    mismatch = raw_contract.ne(expected_minute.astype("string")).fillna(True)
    if mismatch.any():
        first = mismatch[mismatch].index[0]
        raise ValueError(
            "panel_bundle_contract_mapping: minute_contract mismatch; "
            f"first={(bar_dates.iloc[first], bar_products.iloc[first], raw_contract.iloc[first], expected_minute.iloc[first])!r}"
        )
    out["contract"] = daily_contract.astype("string")
    return normalise_bundle_table("bars", out)


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
    absent_product_days: frozenset[tuple[str, str, date]] = frozenset(),
    traded_contract_days: frozenset[tuple[date, str]] | None = None,
):
    """面板覆盖哪些品种日 —— 看不见的那些在这里剔除。

    剔在**上下文**这一层而不是产出 bar 那一层：bundle 的跨表关系要求每个主力品种日
    都有 bar，所以"有主力行、没有 bar"会当场违约。剔掉之后这个品种日在 bundle 里
    整个不存在，换月则顺延到下一个看得见的交易日按常规定价。

    两类剔除：

    1. `absent_product_days` —— 授权资产登记过的「归档本来就没有这一天」。
    2. `traded_contract_days` —— **主力合约当天自己没有成交**的品种日。既没有分钟
       可观测，也没有仓位可动；菜籽油 OI1307 2012 年逐日零成交、价格冻在 10230/9810
       却当着主力，面板于是去要一张从没交易过的合约的乘数与分钟。全历史 160,890 条
       主力选择里这样的有 2,120 条（1.3%），集中在 WR/FU/B/SM/SF 这些薄或已死的品种。
       这是 D11「发出去的那一天自己必须有成交」在**合约**这一层的落实 —— 主力链本身
       不动（它与连续信号那条线共用，那份面板已验收）。

    第三个返回值是**不覆盖的品种日键集**。换月阶段要拿它认前态：bundle 的换月期望是
    从 `dominants`（只有被覆盖的品种日）反推的，前态若取自一个剔掉的日子，换月就会
    在链上凭空消失。
    """
    contexts = {}
    untraded: list[tuple[date, str, str]] = []
    uncovered: set[tuple[date, str]] = set()
    for month in _months(start, end):
        selected = context_choices_for_month(choices, month_start=month)
        monthly = build_contexts(selected, rules=rules, month=month)
        for key, context in monthly.items():
            candidate = context.candidate
            # 窗口之外的那些不是「不覆盖」，是不在范围 —— 换月的前态照旧认它们
            # （bundle 允许保留区间首日的成交单指向窗口外的主力）。
            in_scope = start <= key[0] <= end and _month_start(key[0]) == month
            if (
                candidate.exchange,
                candidate.product,
                candidate.trade_date,
            ) in absent_product_days:
                if in_scope:
                    uncovered.add((candidate.trade_date, candidate.product))
                continue
            if (
                traded_contract_days is not None
                and (candidate.trade_date, candidate.daily_contract)
                not in traded_contract_days
            ):
                untraded.append(
                    (candidate.trade_date, candidate.product, candidate.daily_contract)
                )
                if in_scope:
                    uncovered.add((candidate.trade_date, candidate.product))
                continue
            if in_scope:
                contexts[key] = context
    return contexts, tuple(sorted(untraded)), frozenset(uncovered)


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


_CHECKPOINT_VERSION = 2
_CHECKPOINT_MANIFEST = "checkpoint.json"
_CHECKPOINT_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHECKPOINT_LOCKS_GUARD = threading.Lock()
_CHECKPOINT_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _panel_checkpoint_lock(directory: Path):
    lock_path = directory.with_name(directory.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _CHECKPOINT_LOCKS_GUARD:
        process_lock = _CHECKPOINT_LOCKS.setdefault(key, threading.Lock())
    with process_lock, lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _checkpoint_frame(frame: pd.DataFrame, path: Path) -> dict[str, str]:
    if path.exists() or path.is_symlink():
        raise ValueError(f"panel_checkpoint_invalid: stage exists {path.name!r}")
    normalise_panel(frame).to_parquet(path, index=False)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    return {"filename": path.name, "sha256": _file_sha256(path)}


def _checkpoint_uncovered(rows) -> list[dict[str, str]]:
    return [
        {
            "trade_date": row.trade_date.isoformat(),
            "product": str(row.product),
            "contract": str(row.contract),
            "reason": str(row.reason),
        }
        for row in rows
    ]


def _restore_checkpoint_uncovered(payload: object) -> tuple[UncoveredProductDay, ...]:
    if type(payload) is not list:
        raise ValueError("panel_checkpoint_invalid: uncovered inventory")
    restored = []
    for row in payload:
        if type(row) is not dict or set(row) != {
            "trade_date",
            "product",
            "contract",
            "reason",
        }:
            raise ValueError("panel_checkpoint_invalid: uncovered entry shape")
        if any(type(value) is not str for value in row.values()):
            raise ValueError("panel_checkpoint_invalid: uncovered entry value")
        try:
            trade_date = date.fromisoformat(row["trade_date"])
        except ValueError as exc:
            raise ValueError("panel_checkpoint_invalid: uncovered entry date") from exc
        restored.append(
            UncoveredProductDay(
                trade_date=trade_date,
                product=row["product"],
                contract=row["contract"],
                reason=row["reason"],
            )
        )
    return tuple(restored)


def _checkpoint_manifest_write(directory: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    temporary = directory / ".checkpoint.next.tmp"
    renamed = False
    try:
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("panel_checkpoint_invalid: pending marker already exists")
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / _CHECKPOINT_MANIFEST)
        renamed = True
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if not renamed:
            temporary.unlink(missing_ok=True)


def _checkpoint_file(
    directory: Path,
    declaration: object,
    *,
    label: str,
    required: bool = True,
) -> Path:
    if type(declaration) is not dict or set(declaration) != {"filename", "sha256"}:
        raise ValueError(f"panel_checkpoint_invalid: invalid {label} declaration")
    filename = declaration["filename"]
    digest = declaration["sha256"]
    if (
        type(filename) is not str
        or Path(filename).name != filename
        or type(digest) is not str
        or not _CHECKPOINT_SHA256.fullmatch(digest)
    ):
        raise ValueError(f"panel_checkpoint_invalid: invalid {label} identity")
    path = directory / filename
    if path.is_symlink() or (required and not path.is_file()):
        raise ValueError(f"panel_checkpoint_invalid: {label} digest mismatch")
    if path.is_file() and _file_sha256(path) != digest:
        raise ValueError(f"panel_checkpoint_invalid: {label} digest mismatch")
    return path


def _checkpoint_open(
    directory: Path,
    *,
    key: str,
    state_objects: Mapping[str, object],
) -> dict[str, object]:
    if not _CHECKPOINT_SHA256.fullmatch(key):
        raise ValueError("panel_checkpoint_key: expected a sha256 digest")
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / _CHECKPOINT_MANIFEST
    marker_stage = directory / ".checkpoint.next.tmp"
    if marker_stage.exists() or marker_stage.is_symlink():
        try:
            staged_payload = json.loads(marker_stage.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("panel_checkpoint_invalid: unreadable staged marker") from exc
        if type(staged_payload) is not dict:
            raise ValueError("panel_checkpoint_invalid: staged marker shape")
        if marker.exists() or marker.is_symlink():
            marker_stage.unlink()
        else:
            marker_stage.replace(marker)
    if not marker.exists():
        if any(directory.iterdir()):
            raise ValueError("panel_checkpoint_invalid: unclaimed checkpoint files")
        payload: dict[str, object] = {
            "checkpoint_version": _CHECKPOINT_VERSION,
            "key_sha256": key,
            "completed": [],
            "pending": None,
            "incomplete": None,
            "cleanup_started": False,
            "states": {
                name: state.checkpoint_state()
                for name, state in sorted(state_objects.items())
            },
        }
        _checkpoint_manifest_write(directory, payload)
        return payload
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("panel_checkpoint_invalid: marker must be a regular file")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("panel_checkpoint_invalid: unreadable marker") from exc
    if type(payload) is not dict or set(payload) != {
        "checkpoint_version",
        "key_sha256",
        "completed",
        "pending",
        "incomplete",
        "cleanup_started",
        "states",
    }:
        raise ValueError("panel_checkpoint_invalid: marker shape")
    if payload["checkpoint_version"] != _CHECKPOINT_VERSION:
        raise ValueError("panel_checkpoint_invalid: unsupported version")
    if type(payload["completed"]) is not list or type(payload["states"]) is not dict:
        raise ValueError("panel_checkpoint_invalid: inventory shape")
    if type(payload["cleanup_started"]) is not bool:
        raise ValueError("panel_checkpoint_invalid: cleanup flag")
    if payload["cleanup_started"]:
        owned: list[Path] = []
        for entry in payload["completed"]:
            if type(entry) is not dict or not {"bars", "pending"}.issubset(entry):
                raise ValueError("panel_checkpoint_invalid: cleanup inventory")
            owned.extend(
                (
                    _checkpoint_file(
                        directory, entry["bars"], label="bars", required=False
                    ),
                    _checkpoint_file(
                        directory, entry["pending"], label="pending", required=False
                    ),
                )
            )
        for item in owned:
            item.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        return _checkpoint_open(directory, key=key, state_objects=state_objects)

    incomplete = payload["incomplete"]
    if incomplete is not None:
        if type(incomplete) is not dict or set(incomplete) != {
            "phase",
            "month_start",
            "bars_filename",
            "pending_filename",
            "bars_sha256",
            "pending_sha256",
            "commit",
        }:
            raise ValueError("panel_checkpoint_invalid: incomplete shape")
        if incomplete["phase"] not in {"staging", "publishing"}:
            raise ValueError("panel_checkpoint_invalid: incomplete phase")
        try:
            month_start = date.fromisoformat(incomplete["month_start"])
        except (TypeError, ValueError) as exc:
            raise ValueError("panel_checkpoint_invalid: incomplete month") from exc
        if month_start.day != 1:
            raise ValueError("panel_checkpoint_invalid: incomplete month")
        owned = []
        for label in ("bars", "pending"):
            filename = incomplete[f"{label}_filename"]
            if type(filename) is not str or Path(filename).name != filename:
                raise ValueError("panel_checkpoint_invalid: incomplete filename")
            item = directory / filename
            if item.is_symlink():
                raise ValueError("panel_checkpoint_invalid: incomplete symlink")
            digest = incomplete[f"{label}_sha256"]
            if incomplete["phase"] == "publishing":
                if not isinstance(digest, str) or not _CHECKPOINT_SHA256.fullmatch(
                    digest
                ):
                    raise ValueError("panel_checkpoint_invalid: incomplete digest")
                if not item.is_file() or _file_sha256(item) != digest:
                    raise ValueError("panel_checkpoint_invalid: incomplete file")
            elif digest is not None:
                raise ValueError("panel_checkpoint_invalid: staging digest")
            owned.append(item)
        if incomplete["phase"] == "staging":
            for item in owned:
                item.unlink(missing_ok=True)
            payload["incomplete"] = None
        else:
            commit = incomplete["commit"]
            if type(commit) is not dict or set(commit) != {"entry", "states"}:
                raise ValueError("panel_checkpoint_invalid: incomplete commit")
            payload["completed"].append(commit["entry"])
            payload["pending"] = commit["entry"]["pending"]
            payload["states"] = commit["states"]
            payload["incomplete"] = None
        _checkpoint_manifest_write(directory, payload)

    if payload["key_sha256"] != key:
        raise ValueError("panel_checkpoint_mismatch: inputs/config/source changed")
    if set(payload["states"]) != set(state_objects):
        raise ValueError("panel_checkpoint_mismatch: checkpoint state set changed")
    declared = {marker.name}
    previous_month: date | None = None
    for entry in payload["completed"]:
        if type(entry) is not dict or set(entry) != {
            "month_start",
            "bars",
            "pending",
            "uncovered",
        }:
            raise ValueError("panel_checkpoint_invalid: completed entry shape")
        _restore_checkpoint_uncovered(entry["uncovered"])
        try:
            month_start = date.fromisoformat(entry["month_start"])
        except (TypeError, ValueError) as exc:
            raise ValueError("panel_checkpoint_invalid: completed month") from exc
        if month_start.day != 1 or (previous_month and month_start <= previous_month):
            raise ValueError("panel_checkpoint_invalid: completed month order")
        previous_month = month_start
        declared.add(_checkpoint_file(directory, entry["bars"], label="bars").name)
        declared.add(
            _checkpoint_file(directory, entry["pending"], label="pending").name
        )
    if payload["pending"] is not None:
        declared.add(
            _checkpoint_file(directory, payload["pending"], label="pending").name
        )
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != declared:
        raise ValueError("panel_checkpoint_invalid: unclaimed checkpoint files")
    for name, state in state_objects.items():
        state.restore_checkpoint_state(payload["states"][name])
    return payload


def _build_panel_checkpointed_locked(
    *,
    contexts,
    source,
    pricing_basis_by_exchange,
    multiplier_resolver,
    adjustment_factor_by_key,
    continuity_segment_by_key,
    checkpoint_directory: str | Path,
    checkpoint_key: str,
    checkpoint_state_objects: Mapping[str, object] | None = None,
    drop_unformable_days: bool = False,
) -> tuple[pd.DataFrame, tuple[UncoveredProductDay, ...]]:
    """Stage finalized months and resume after the last atomic checkpoint.

    Only the current month's rows and one pending row per product live in the
    panel iterator. Final concatenation reads staged Parquet after all source
    work succeeds, preserving the legacy DataFrame result for bundle validation.

    不覆盖的品种日与 bar 一起过 checkpoint：它决定面板的覆盖口径，续跑之后少一条就会
    有一个「有主力行、没有 bar」的品种日活到写 bundle 那一刻。
    """
    directory = Path(checkpoint_directory)
    states = dict(checkpoint_state_objects or {})
    payload = _checkpoint_open(directory, key=checkpoint_key, state_objects=states)
    completed = payload["completed"]
    resume_after = (
        date.fromisoformat(completed[-1]["month_start"]) if completed else None
    )
    initial_pending = None
    if payload["pending"] is not None:
        initial_pending = normalise_panel(
            pd.read_parquet(_checkpoint_file(directory, payload["pending"], label="pending"))
        )

    for chunk in iter_panel_months(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange=pricing_basis_by_exchange,
        multiplier_resolver=multiplier_resolver,
        adjustment_factor_by_key=adjustment_factor_by_key,
        continuity_segment_by_key=continuity_segment_by_key,
        resume_after=resume_after,
        initial_pending=initial_pending,
        drop_unformable_days=drop_unformable_days,
    ):
        month_label = chunk.month_start.strftime("%Y-%m")
        generation = hashlib.sha256(
            f"{checkpoint_key}:{month_label}".encode("ascii")
        ).hexdigest()[:16]
        bars_path = directory / f"bars-{month_label}-{generation}.parquet"
        pending_path = directory / f"pending-{month_label}-{generation}.parquet"
        payload["incomplete"] = {
            "phase": "staging",
            "month_start": chunk.month_start.isoformat(),
            "bars_filename": bars_path.name,
            "pending_filename": pending_path.name,
            "bars_sha256": None,
            "pending_sha256": None,
            "commit": None,
        }
        _checkpoint_manifest_write(directory, payload)
        bars_declaration = _checkpoint_frame(chunk.bars, bars_path)
        pending_declaration = _checkpoint_frame(chunk.pending, pending_path)
        entry = {
            "month_start": chunk.month_start.isoformat(),
            "bars": bars_declaration,
            "pending": pending_declaration,
            "uncovered": _checkpoint_uncovered(chunk.uncovered),
        }
        next_states = {
            name: state.checkpoint_state() for name, state in sorted(states.items())
        }
        payload["incomplete"] = {
            "phase": "publishing",
            "month_start": chunk.month_start.isoformat(),
            "bars_filename": bars_path.name,
            "pending_filename": pending_path.name,
            "bars_sha256": bars_declaration["sha256"],
            "pending_sha256": pending_declaration["sha256"],
            "commit": {"entry": entry, "states": next_states},
        }
        _checkpoint_manifest_write(directory, payload)
        payload["completed"].append(entry)
        payload["pending"] = pending_declaration
        payload["states"] = next_states
        payload["incomplete"] = None
        _checkpoint_manifest_write(directory, payload)

    frames = [
        normalise_panel(
            pd.read_parquet(_checkpoint_file(directory, entry["bars"], label="bars"))
        )
        for entry in payload["completed"]
    ]
    uncovered = tuple(
        row
        for entry in payload["completed"]
        for row in _restore_checkpoint_uncovered(entry["uncovered"])
    )
    if payload["pending"] is not None:
        frames.append(
            normalise_panel(
                pd.read_parquet(
                    _checkpoint_file(directory, payload["pending"], label="pending")
                )
            )
        )
    if not frames:
        return (
            normalise_panel(pd.DataFrame(columns=list(TABLE_SCHEMAS["bars"]))),
            uncovered,
        )
    return normalise_panel(pd.concat(frames, ignore_index=True)), uncovered


def _build_panel_checkpointed(
    **kwargs,
) -> tuple[pd.DataFrame, tuple[UncoveredProductDay, ...]]:
    directory = Path(kwargs["checkpoint_directory"])
    with _panel_checkpoint_lock(directory):
        return _build_panel_checkpointed_locked(**kwargs)


def _clear_panel_checkpoint_locked(directory: str | Path) -> None:
    path = Path(directory)
    if not path.exists():
        return
    marker = path / _CHECKPOINT_MANIFEST
    marker_stage = path / ".checkpoint.next.tmp"
    if marker_stage.exists() or marker_stage.is_symlink():
        try:
            staged = json.loads(marker_stage.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("panel_checkpoint_invalid: staged cleanup marker") from exc
        if type(staged) is not dict:
            raise ValueError("panel_checkpoint_invalid: staged cleanup marker")
        if marker.exists():
            marker_stage.unlink()
        else:
            marker_stage.replace(marker)
    if not marker.is_file() or marker.is_symlink():
        raise ValueError("panel_checkpoint_invalid: cannot clear unclaimed directory")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    if type(payload) is not dict or "cleanup_started" not in payload:
        raise ValueError("panel_checkpoint_invalid: cleanup marker shape")
    declared = []
    for entry in payload.get("completed", []):
        declared.append(
            _checkpoint_file(
                path,
                entry["bars"],
                label="bars",
                required=not payload["cleanup_started"],
            )
        )
        declared.append(
            _checkpoint_file(
                path,
                entry["pending"],
                label="pending",
                required=not payload["cleanup_started"],
            )
        )
    expected = {marker.name, *(item.name for item in declared)}
    actual = {item.name for item in path.iterdir()}
    if not payload["cleanup_started"] and actual != expected:
        raise ValueError("panel_checkpoint_invalid: refusing unsafe cleanup")
    if payload["cleanup_started"] and not actual.issubset(expected):
        raise ValueError("panel_checkpoint_invalid: refusing unsafe cleanup")
    if not payload["cleanup_started"]:
        payload["cleanup_started"] = True
        _checkpoint_manifest_write(path, payload)
    for item in declared:
        item.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    path.rmdir()


def _clear_panel_checkpoint(directory: str | Path) -> None:
    path = Path(directory)
    with _panel_checkpoint_lock(path):
        _clear_panel_checkpoint_locked(path)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end must not precede --start")

    session_rules_path = args.session_rules
    rules = load_session_rules(session_rules_path)
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
    # 授权登记过的「归档本来就没有这一天」（供应商 2026-08-21 明确无法补的五个品种日）。
    # 面板对它们不产出 bar，也不中止 —— 没登记的空帧仍然硬失败。
    absent_days = frozenset(
        (row.exchange, row.product, row.trade_date)
        for row in load_absent_product_days(ABSENT_PRODUCT_DAYS)
    )

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
    # 影子策略只在**可能被选中**的品种上有意义：`selected = pool ∩ eligible`，
    # 而 `pool` 就是逐月宇宙。一个在整个区间里从没跨过 50 亿门槛的品种永远进不了
    # pool，它的影子再怎么跑也不会进组合 —— 只会白扫几万个品种日的分钟表，并要求
    # 一批永远用不上的时段规则。窗口内曾入池过的品种全部保留（含入池前的历史），
    # 因为月度筛选要读它入池前 252 天的影子表现。
    tradeable = {
        product
        for products in products_by_month.values()
        for product in products
    }
    history_products = tuple(
        sorted((set(turnover["product"]) - FINANCIAL_FUTURES) & tradeable)
    )
    if not history_products:
        raise ValueError("panel_products_empty: no commodity products in daily history")
    dropped = len(set(turnover["product"]) - FINANCIAL_FUTURES) - len(history_products)
    print(
        f"products: {len(history_products)} tradeable "
        f"({dropped} never reach the liquidity gate, excluded)",
        flush=True,
    )

    choices = choose_dominant_commodity(daily, products=history_products)
    # 砍掉每个品种「首次入池前 252 个交易日」之前的历史 —— 月度筛选最多回看 252 个
    # 观测，更早的影子历史永远读不到，留着只会索取采不到的时段规则。
    scope = shadow_scope(
        universe_by_month=products_by_month,
        market_days=sorted(set(daily["trade_date"])),
    )
    before = len(choices)
    choices = tuple(
        choice
        for choice in choices
        if choice.trade_date >= scope[choice.product]
    )
    print(
        f"dominant choices: {len(choices):,} "
        f"({before - len(choices):,} dropped as unreadable pre-warmup history)",
        flush=True,
    )
    closes = {
        (trade_date, str(symbol)): float(close)
        for trade_date, symbol, close in daily.loc[
            daily["close"].notna(), ["trade_date", "symbol", "close"]
        ].itertuples(index=False, name=None)
    }
    factors = adjustment_factors(choices, closes=closes)
    segment_by_key = {
        (row.trade_date, row.product): int(row.continuity_segment)
        for row in factors.itertuples(index=False)
    }
    breaks = int(factors["continuity_segment"].max()) if len(factors) else 0
    if breaks:
        summary = (
            factors.loc[factors["continuity_segment"] > 0]
            .groupby("product")["continuity_segment"]
            .max()
            .to_dict()
        )
        print(f"continuity breaks: {breaks} across {summary}", flush=True)
    factor_by_key = {
        (row.trade_date, row.product): float(row.adj_factor)
        for row in factors.itertuples(index=False)
    }
    # 覆盖闸必须在任何一次分钟查询之前：缺规则要一次报全，而不是跑到第 N 个月
    # 才崩在某一天上。这个失效模式已经露头过三次。
    require_session_coverage(
        choices=choices,
        months=list(_months(args.start, args.end)),
        rules=rules,
        manifest_path=Path(args.output_dir) / "session-coverage-gap.csv",
    )
    traded_contract_days = frozenset(
        (trade_date, str(symbol))
        for trade_date, symbol, volume in daily.loc[
            :, ["trade_date", "symbol", "volume"]
        ].itertuples(index=False, name=None)
        if float(volume or 0.0) > 0.0
    )
    contexts, untraded_dominants, uncovered_keys = _target_contexts(
        choices=choices,
        rules=rules,
        start=args.start,
        end=args.end,
        absent_product_days=absent_days,
        traded_contract_days=traded_contract_days,
    )
    if untraded_dominants:
        untraded_manifest = Path(args.output_dir) / "dominant-untraded.csv"
        untraded_manifest.parent.mkdir(parents=True, exist_ok=True)
        with untraded_manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["trade_date", "product", "contract"])
            for trade_date, product, contract in untraded_dominants:
                writer.writerow([trade_date.isoformat(), product, contract])
        untraded_products = sorted({row[1] for row in untraded_dominants})
        print(
            f"untraded dominants dropped: {len(untraded_dominants)} across "
            f"{len(untraded_products)} products ({' '.join(untraded_products)}); "
            f"manifest={untraded_manifest}",
            flush=True,
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

    # 日线 turnover 是独立于分钟归档的另一份记录：分钟 `amount` 与它对不上的那些天，
    # 坏的是成交额列而不是乘数（焦煤 JM1309 2013-04-08/11 恰好是日线的 1/6）。
    daily_turnover_by_key = {
        (str(symbol), trade_date): float(turnover)
        for symbol, trade_date, turnover in daily.loc[
            :, ["symbol", "trade_date", "turnover"]
        ].itertuples(index=False, name=None)
        if turnover is not None and float(turnover) > 0.0
    }
    metadata_multiplier_resolver = CachingMetadataMultiplierResolver(
        source,
        pricing_basis_by_exchange=basis_by_exchange,
        daily_turnover_by_key=daily_turnover_by_key,
    )
    multiplier_resolver = DigestingMultiplierResolver(
        metadata_multiplier_resolver,
        pricing_basis_by_exchange=basis_by_exchange,
    )

    source_revision = _source_revision()
    effective_config_sha256, _ = _effective_config_sha256(
        cfg,
        session_rules_path,
        PRICING_BASES,
        source_revision=source_revision,
    )
    daily_sha256 = _frame_sha256(daily)
    contexts_sha256 = _contexts_sha256(contexts)
    checkpoint_payload = {
        "version": _CHECKPOINT_VERSION,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "database_profile": "test" if args.use_test else "production",
        "daily_sha256": daily_sha256,
        "contexts_sha256": contexts_sha256,
        "factors_sha256": _frame_sha256(factors),
        "effective_config_sha256": effective_config_sha256,
        "source_revision": source_revision,
    }
    checkpoint_key = hashlib.sha256(
        json.dumps(
            checkpoint_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    checkpoint_directory = args.output_dir / ".panel-checkpoint"

    def _price_rolls():
        source.set_phase("roll_fills")
        multiplier_resolver.set_phase("roll_fills")
        fills, skipped = build_roll_fills(
            choices=choices,
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange=basis_by_exchange,
            multiplier_resolver=multiplier_resolver,
            uncovered=uncovered_keys,
            daily_turnover_by_key=daily_turnover_by_key,
        )
        # 换月阶段一次跑完全部换月，所以跳过的那些在长跑的分钟阶段之前就报全了 ——
        # 不再一次炸一条。清单落 CSV，计数进 manifest。
        _write_roll_manifest(Path(args.output_dir), skipped)
        return fills, skipped

    roll_fills, unpriced_rolls = _price_rolls()
    source.set_phase("bars")
    multiplier_resolver.set_phase("bars")
    raw_bars, unformable = _build_panel_checkpointed(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange=basis_by_exchange,
        multiplier_resolver=multiplier_resolver,
        adjustment_factor_by_key={key: factor_by_key[key] for key in contexts},
        continuity_segment_by_key={key: segment_by_key[key] for key in contexts},
        checkpoint_directory=checkpoint_directory,
        checkpoint_key=checkpoint_key,
        checkpoint_state_objects={
            "metadata_multiplier": metadata_multiplier_resolver,
            "minute": source,
            "multiplier": multiplier_resolver,
        },
        drop_unformable_days=True,
    )
    # 乘数解不出来的品种日形不成 bar，面板不覆盖（用户 2026-09-01 裁决 C）。覆盖是
    # 这一刻才定稿的，所以换月要按定稿后的链重算一遍：bundle 的换月期望是从
    # `dominants` 反推的，剔掉的那天若正好是换月日，转移会顺延到下一个覆盖日。
    unformable_manifest = Path(args.output_dir) / "bar-unformable.csv"
    unformable_manifest.parent.mkdir(parents=True, exist_ok=True)
    if not unformable:
        # 清单描述这一次的产出：上一次尝试留下的文件不能还躺在输出目录里。
        unformable_manifest.unlink(missing_ok=True)
    else:
        with unformable_manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["trade_date", "product", "contract", "reason"])
            for row in unformable:
                writer.writerow(
                    [
                        row.trade_date.isoformat(),
                        row.product,
                        row.contract,
                        row.reason,
                    ]
                )
        unformable_products = sorted({row.product for row in unformable})
        print(
            f"unformable product-days dropped: {len(unformable)} across "
            f"{len(unformable_products)} products "
            f"({' '.join(unformable_products)}); manifest={unformable_manifest}",
            flush=True,
        )
        unformable_keys = frozenset((row.trade_date, row.product) for row in unformable)
        contexts = {
            key: context
            for key, context in contexts.items()
            if key not in unformable_keys
        }
        if not contexts:
            raise ValueError(
                "panel_contexts_empty: every dominant session is unformable"
            )
        uncovered_keys = uncovered_keys | unformable_keys
        roll_fills, unpriced_rolls = _price_rolls()
        print(
            f"rolls repriced on the covered chain: {len(roll_fills)} filled, "
            f"{len(unpriced_rolls)} skipped",
            flush=True,
        )
    # 分钟成交额与日线对不上的品种日：bar 照常，成交价由区间校验拒掉。清单落盘，
    # 因为「安静时段没人成交」与「这天的成交额列坏了」是两回事。
    amount_disagreements = metadata_multiplier_resolver.amount_disagreements
    amount_manifest = Path(args.output_dir) / "minute-amount-disagrees-with-daily.csv"
    amount_manifest.parent.mkdir(parents=True, exist_ok=True)
    if not amount_disagreements:
        amount_manifest.unlink(missing_ok=True)
    else:
        with amount_manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["trade_date", "product", "contract", "minute_amount", "daily_turnover"]
            )
            for row in amount_disagreements:
                writer.writerow(
                    [
                        row["trade_date"].isoformat(),
                        row["product"],
                        row["contract"],
                        repr(float(row["minute_amount"])),
                        repr(float(row["daily_turnover"])),
                    ]
                )
        disagreeing_products = sorted({row["product"] for row in amount_disagreements})
        print(
            f"minute amount disagrees with daily: {len(amount_disagreements)} "
            f"product-days across {len(disagreeing_products)} products "
            f"({' '.join(disagreeing_products)}); manifest={amount_manifest}",
            flush=True,
        )
    dominants = _dominant_frame(choices, contexts=contexts, factor_by_key=factor_by_key)
    bars = _bundle_bars(raw_bars, contexts=contexts, dominants=dominants)
    universes = _universe_frame(products_by_month)
    multiplier_resolver.assert_complete(bars=bars, roll_fills=roll_fills)

    audit = source.audit
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
            "daily_sha256": daily_sha256,
            "minute_candidates_sha256": contexts_sha256,
            "minute_content_sha256": source.minute_content_sha256,
            "minute_request_digests": source.minute_request_digests,
            "multiplier_resolutions_sha256": (
                multiplier_resolver.multiplier_resolutions_sha256
            ),
            "effective_config_sha256": effective_config_sha256,
            "unpriceable_rolls": len(unpriced_rolls),
            "unpriceable_roll_keys": [
                f"{row['trade_date']:%Y-%m-%d}/{row['product']}"
                for row in unpriced_rolls
            ],
            # 面板不覆盖的品种日：形不成 bar（乘数解不出来）的那一类。归档缺日与
            # 未成交主力两类由各自的资产/清单申报，都不在 bundle 的换月校验里。
            "amount_disagrees_with_daily": len(amount_disagreements),
            "amount_disagrees_with_daily_keys": [
                f"{row['trade_date']:%Y-%m-%d}/{row['product']}"
                for row in amount_disagreements
            ],
            "unformable_product_days": len(unformable),
            "unformable_product_day_keys": [
                f"{row.trade_date:%Y-%m-%d}/{row.product}" for row in unformable
            ],
        },
        provenance={
            **source_revision,
            "session_rules_file": _asset_label(session_rules_path),
            "session_rules_sha256": _file_sha256(session_rules_path),
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
    _clear_panel_checkpoint(checkpoint_directory)
    print(
        f"bundle: {args.output_dir} bars={len(bundle.bars):,} "
        f"universes={len(bundle.universes):,} "
        f"dominants={len(bundle.dominants):,} rolls={len(bundle.roll_fills):,}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
