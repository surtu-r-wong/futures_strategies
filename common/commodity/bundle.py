"""Versioned, content-addressed commodity panel bundles."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Mapping
import uuid

import pandas as pd

from common.commodity.panel import PANEL_COLUMNS
from common.minute.pg_source import minute_contract_identity

__all__ = [
    "BUNDLE_VERSION",
    "TABLE_FILES",
    "TABLE_SCHEMAS",
    "PanelBundle",
    "normalise_bundle_table",
    "read_bundle",
    "write_bundle",
]


BUNDLE_VERSION = 1
TABLE_FILES = {
    "bars": "bars.parquet",
    "universes": "universes.parquet",
    "dominants": "dominants.parquet",
    "roll_fills": "roll_fills.parquet",
}

TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    "bars": {
        "product": "string",
        "contract": "string",
        "trade_date": "date",
        "slot_end": "aware_datetime",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
        "open_interest": "float64",
        "no_trade": "bool",
        "adj_factor": "float64",
        "continuity_segment": "int64",
        "fill_time": "aware_datetime",
        "fill_price": "float64",
        "fill_pending": "bool",
        "fill_unpriceable": "bool",
        "pricing_basis": "string",
        "multiplier": "int64",
    },
    "universes": {
        "month_start": "date",
        "product": "string",
    },
    "dominants": {
        "trade_date": "date",
        "product": "string",
        "contract": "string",
        "oi": "int64",
        "volume": "int64",
        "selected_from": "date",
        "adj_factor": "float64",
    },
    "roll_fills": {
        "trade_date": "date",
        "product": "string",
        "old_contract": "string",
        "new_contract": "string",
        "fill_time": "aware_datetime",
        "old_price": "float64",
        "new_price": "float64",
        "old_pricing_basis": "string",
        "new_pricing_basis": "string",
    },
}

_SORT_COLUMNS = {
    "bars": ("trade_date", "product", "slot_end", "contract"),
    "universes": ("month_start", "product"),
    "dominants": ("trade_date", "product", "contract"),
    "roll_fills": ("trade_date", "product", "old_contract", "new_contract"),
}
_PRIMARY_KEYS = {
    "bars": ("trade_date", "product", "slot_end"),
    "universes": ("month_start", "product"),
    "dominants": ("trade_date", "product"),
    "roll_fills": ("trade_date", "product"),
}
_NULLABLE_COLUMNS = {
    "bars": {
        "open",
        "high",
        "low",
        "close",
        "open_interest",
        "fill_time",
        "fill_price",
    },
}
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|api[_-]?key|private[_-]?key|dsn)",
    re.IGNORECASE,
)
_CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@", re.IGNORECASE)
_DSN_SECRET = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_TOKEN_VALUE = re.compile(
    r"(?:\bBearer\s+\S{12,}|\bsk-[A-Za-z0-9_-]{16,})", re.IGNORECASE
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_INCOMPLETE_FILE = ".incomplete-generation.json"
_INCOMPLETE_VERSION = 1


@dataclass(frozen=True, slots=True)
class PanelBundle:
    bars: pd.DataFrame
    universes: pd.DataFrame
    dominants: pd.DataFrame
    roll_fills: pd.DataFrame
    manifest: dict[str, object]


def _schema_error(table: str, column: str, detail: object) -> ValueError:
    return ValueError(
        f"bundle_schema_dtype: table={table!r} column={column!r} detail={detail!r}"
    )


def _normalise_date(values: pd.Series, *, table: str, column: str) -> pd.Series:
    try:
        converted = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc
    if isinstance(converted.dtype, pd.DatetimeTZDtype):
        raise _schema_error(table, column, "timezone-aware date is not allowed")
    converted = converted.astype("datetime64[ns]")
    if converted.isna().any():
        raise _schema_error(table, column, "null date")
    if converted.ne(converted.dt.normalize()).any():
        raise _schema_error(
            table, column, "date value must be a naive midnight timestamp"
        )
    return converted


def _normalise_aware_datetime(
    values: pd.Series, *, table: str, column: str
) -> pd.Series:
    for value in values:
        if pd.isna(value):
            continue
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _schema_error(table, column, str(exc)) from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise _schema_error(table, column, "timezone-aware value required")
    try:
        converted = (
            pd.to_datetime(values, utc=True, errors="raise")
            .dt.tz_convert("Asia/Shanghai")
            .astype("datetime64[ns, Asia/Shanghai]")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc
    if column not in _NULLABLE_COLUMNS.get(table, set()) and converted.isna().any():
        raise _schema_error(table, column, "null datetime")
    return converted


def _normalise_float(values: pd.Series, *, table: str, column: str) -> pd.Series:
    try:
        converted = pd.to_numeric(values, errors="raise").astype("float64")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc
    if column not in _NULLABLE_COLUMNS.get(table, set()):
        if converted.isna().any() or not converted.map(math.isfinite).all():
            raise _schema_error(table, column, "finite value required")
    return converted


def _normalise_int(values: pd.Series, *, table: str, column: str) -> pd.Series:
    try:
        numeric = pd.to_numeric(values, errors="raise")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc
    if numeric.isna().any():
        raise _schema_error(table, column, "null integer")
    try:
        finite = numeric.astype("float64")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc
    if not finite.map(math.isfinite).all() or not (finite % 1 == 0).all():
        raise _schema_error(table, column, "finite integral value required")
    try:
        return numeric.astype("int64")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _schema_error(table, column, str(exc)) from exc


def _normalise_bool(values: pd.Series, *, table: str, column: str) -> pd.Series:
    if not pd.api.types.is_bool_dtype(values.dtype):
        raise _schema_error(
            table, column, f"boolean dtype required, got {values.dtype}"
        )
    if values.isna().any():
        raise _schema_error(table, column, "null boolean")
    return values.astype("bool")


def _normalise_text(values: pd.Series, *, table: str, column: str) -> pd.Series:
    converted = values.astype("string")
    if converted.isna().any() or converted.str.len().eq(0).any():
        raise _schema_error(table, column, "non-empty text required")
    return converted


def _value_error(table: str, detail: str) -> ValueError:
    return ValueError(f"bundle_schema_value: table={table!r} {detail}")


def _validate_table_values(table: str, frame: pd.DataFrame) -> None:
    if table == "bars":
        for column in ("open", "high", "low", "close", "open_interest", "fill_price"):
            present = frame[column].dropna()
            if not present.map(math.isfinite).all():
                raise _value_error(
                    table, f"column={column!r} must be finite when present"
                )
        if not frame["volume"].map(math.isfinite).all() or (frame["volume"] < 0).any():
            raise _value_error(table, "volume must be finite and nonnegative")
        if (
            not frame["adj_factor"].map(math.isfinite).all()
            or (frame["adj_factor"] <= 0).any()
        ):
            raise _value_error(table, "adj_factor must be finite and positive")
        if (frame["multiplier"] <= 0).any():
            raise _value_error(table, "multiplier must be positive")
        traded = ~frame["no_trade"]
        required = ["open", "high", "low", "close", "open_interest"]
        if frame.loc[traded, required].isna().any().any():
            raise _value_error(table, "traded bars require finite OHLC and OI")
        priceable = ~frame["fill_pending"] & ~frame["fill_unpriceable"]
        if frame.loc[priceable, ["fill_time", "fill_price"]].isna().any().any():
            raise _value_error(table, "priceable fills require time and raw price")
    elif table == "dominants":
        if (frame[["oi", "volume"]] < 0).any().any():
            raise _value_error(table, "oi and volume must be nonnegative")
        if (frame["adj_factor"] <= 0).any():
            raise _value_error(table, "adj_factor must be positive")
    elif table == "roll_fills":
        if (frame[["old_price", "new_price"]] <= 0).any().any():
            raise _value_error(table, "raw roll prices must be positive")
    elif table == "universes":
        if frame["month_start"].dt.day.ne(1).any():
            raise _value_error(table, "month_start must be the first day of a month")


def normalise_bundle_table(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Validate one production schema and return a deterministically ordered copy."""
    if table not in TABLE_SCHEMAS:
        raise ValueError(f"bundle_table_unknown: {table!r}")
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"bundle_schema_frame: table={table!r} must be a DataFrame")

    schema = TABLE_SCHEMAS[table]
    missing = [column for column in schema if column not in frame.columns]
    extra = [column for column in frame.columns if column not in schema]
    if missing or extra:
        raise ValueError(
            f"bundle_schema_columns: table={table!r} missing={missing!r} extra={extra!r}"
        )

    out = frame.loc[:, list(schema)].copy()
    for column, dtype in schema.items():
        values = out[column]
        if dtype == "date":
            out[column] = _normalise_date(values, table=table, column=column)
        elif dtype == "aware_datetime":
            out[column] = _normalise_aware_datetime(values, table=table, column=column)
        elif dtype == "float64":
            out[column] = _normalise_float(values, table=table, column=column)
        elif dtype == "int64":
            out[column] = _normalise_int(values, table=table, column=column)
        elif dtype == "bool":
            out[column] = _normalise_bool(values, table=table, column=column)
        elif dtype == "string":
            out[column] = _normalise_text(values, table=table, column=column)
        else:  # pragma: no cover - declaration invariant
            raise RuntimeError(f"unsupported bundle dtype declaration: {dtype!r}")

    _validate_table_values(table, out)
    primary_key = list(_PRIMARY_KEYS[table])
    if out.duplicated(primary_key, keep=False).any():
        raise ValueError(
            "bundle_primary_key: "
            f"table={table!r} columns={primary_key!r} values must be unique"
        )
    if table == "bars" and tuple(out.columns) != PANEL_COLUMNS:
        raise RuntimeError("bundle bars schema drifted from PANEL_COLUMNS")
    out = out.sort_values(list(_SORT_COLUMNS[table]), kind="mergesort")
    return out.reset_index(drop=True)


def _validate_bundle_relationships(frames: Mapping[str, pd.DataFrame]) -> None:
    bars = frames["bars"]
    dominants = frames["dominants"]
    roll_fills = frames["roll_fills"]

    for table, contract_columns in (
        ("bars", ("contract",)),
        ("dominants", ("contract",)),
        ("roll_fills", ("old_contract", "new_contract")),
    ):
        frame = frames[table]
        for row in frame.itertuples(index=False):
            trade_date = pd.Timestamp(row.trade_date).date()
            expected_product = str(row.product)
            for column in contract_columns:
                try:
                    product, _, _ = minute_contract_identity(
                        str(getattr(row, column)), trade_date
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "bundle_relationship: canonical_contract required; "
                        f"table={table!r} column={column!r}"
                    ) from exc
                if product != expected_product:
                    raise ValueError(
                        "bundle_relationship: canonical_contract product mismatch; "
                        f"table={table!r} column={column!r}"
                    )

    if roll_fills["old_contract"].eq(roll_fills["new_contract"]).any():
        raise ValueError(
            "bundle_relationship: roll_dominants require distinct old/new contracts"
        )

    bar_contracts = bars.loc[
        :, ["trade_date", "product", "contract", "adj_factor"]
    ].drop_duplicates()
    dominant_contracts = dominants.loc[
        :, ["trade_date", "product", "contract", "adj_factor"]
    ]
    joined = bar_contracts.merge(
        dominant_contracts,
        on=["trade_date", "product"],
        how="outer",
        suffixes=("_bar", "_dominant"),
        indicator=True,
    )
    disagreement = joined["_merge"].ne("both")
    disagreement |= joined["contract_bar"].ne(joined["contract_dominant"])
    disagreement |= joined["adj_factor_bar"].ne(joined["adj_factor_dominant"])
    if disagreement.any():
        raise ValueError(
            "bundle_relationship: bars_dominants require exactly one matching "
            "contract and adjustment factor for every product-date"
        )

    dominant_rows = dominants.sort_values(
        ["product", "trade_date"], kind="mergesort"
    ).reset_index(drop=True)
    expected_transitions: dict[tuple[pd.Timestamp, str], tuple[str, str]] = {}
    for _, group in dominant_rows.groupby("product", sort=False):
        previous_contract: str | None = None
        for row in group.itertuples(index=False):
            contract = str(row.contract)
            if previous_contract is not None and contract != previous_contract:
                expected_transitions[(row.trade_date, str(row.product))] = (
                    previous_contract,
                    contract,
                )
            previous_contract = contract

    actual_transitions = {
        (row.trade_date, str(row.product)): (
            str(row.old_contract),
            str(row.new_contract),
        )
        for row in roll_fills.itertuples(index=False)
    }
    dominant_by_key = {
        (row.trade_date, str(row.product)): str(row.contract)
        for row in dominant_rows.itertuples(index=False)
    }
    invalid_roll = any(
        dominant_by_key.get(key) != transition[1]
        for key, transition in actual_transitions.items()
    )
    invalid_roll |= any(
        actual_transitions.get(key) != transition
        for key, transition in expected_transitions.items()
    )
    # A fill on the first retained date can refer to a dominant just outside the
    # bundle's date window, so only its new leg can be checked locally.
    first_keys = {
        (group.iloc[0]["trade_date"], str(product))
        for product, group in dominant_rows.groupby("product", sort=False)
        if not group.empty
    }
    invalid_roll |= any(
        key not in expected_transitions and key not in first_keys
        for key in actual_transitions
    )
    if invalid_roll:
        raise ValueError(
            "bundle_relationship: roll_dominants require every in-range dominant "
            "transition to have the matching old/new raw roll fill"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_object(value: object, *, path: str) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError(f"bundle_manifest_value: non-string key at {path}")
            if _SENSITIVE_KEY.search(key):
                raise ValueError(
                    f"bundle_manifest_sensitive: forbidden key {path}.{key}"
                )
            result[key] = _manifest_object(child, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _manifest_object(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    if type(value) is str:
        if any(
            pattern.search(value)
            for pattern in (_CREDENTIAL_URL, _DSN_SECRET, _TOKEN_VALUE)
        ):
            raise ValueError(f"bundle_manifest_sensitive: forbidden value at {path}")
        return value
    if value is None or type(value) in (int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(
        f"bundle_manifest_value: unsupported or non-finite value at {path}: {value!r}"
    )


def _safe_directory(directory: str | Path, *, create: bool) -> Path:
    path = Path(directory)
    if create and not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # A cooperating writer can win this race before either process has
            # acquired the lock inside the newly created directory.
            pass
    if not path.exists():
        raise ValueError(f"bundle_directory_missing: {path}")
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"bundle_directory_invalid: {path}")
    return path


@contextmanager
def _bundle_lock(directory: Path):
    """Serialize publication in this process and across cooperating processes."""
    key = str(directory.resolve())
    with _LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock:
        lock_path = directory / ".bundle.lock"
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise ValueError(f"bundle_lock_invalid: {lock_path}")
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_parquet(frame: pd.DataFrame, path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError(f"bundle_stage_exists: {path.name}")
    frame.to_parquet(path, index=False)
    _fsync_file(path)
    return path


def _stage_manifest(payload: bytes, directory: Path) -> Path:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=".manifest.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        result = temporary
        temporary = None
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _incomplete_payload(
    *,
    generation_id: str,
    table_manifest: Mapping[str, Mapping[str, str]],
    staged: Mapping[str, Path],
    manifest_temporary: Path,
    manifest_sha256: str | None,
    phase: str,
) -> dict[str, object]:
    return {
        "incomplete_version": _INCOMPLETE_VERSION,
        "generation_id": generation_id,
        "phase": phase,
        "tables": {
            table: {
                "filename": table_manifest[table]["filename"],
                "sha256": table_manifest[table]["sha256"],
                "staged_filename": staged[table].name,
            }
            for table in TABLE_FILES
        },
        "manifest": {
            "staged_filename": manifest_temporary.name,
            "sha256": manifest_sha256,
        },
    }


def _publish_incomplete(payload: Mapping[str, object], directory: Path) -> Path:
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
    generation_id = str(payload["generation_id"])
    temporary = directory / f".incomplete-generation.{generation_id}.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    marker = directory / _INCOMPLETE_FILE
    renamed = False
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(marker)
        renamed = True
        _fsync_directory(directory)
        return marker
    finally:
        if not renamed:
            temporary.unlink(missing_ok=True)


def _owned_recovery_path(directory: Path, name: object, *, label: str) -> Path:
    if type(name) is not str or not name or Path(name).name != name:
        raise ValueError(f"bundle_recovery_invalid: invalid {label} filename")
    path = directory / name
    if path.is_symlink():
        raise ValueError(f"bundle_recovery_invalid: symlinked {label}")
    return path


def _read_incomplete_path(marker: Path, directory: Path) -> dict[str, object]:
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("bundle_recovery_invalid: incomplete marker is not a file")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("bundle_recovery_invalid: unreadable incomplete marker") from exc
    if type(payload) is not dict or set(payload) != {
        "incomplete_version",
        "generation_id",
        "phase",
        "tables",
        "manifest",
    }:
        raise ValueError("bundle_recovery_invalid: invalid marker shape")
    if payload["incomplete_version"] != _INCOMPLETE_VERSION or not re.fullmatch(
        r"[0-9a-f]{32}", str(payload["generation_id"])
    ):
        raise ValueError("bundle_recovery_invalid: invalid generation identity")
    if payload["phase"] not in {"staging", "publishing", "recovering"}:
        raise ValueError("bundle_recovery_invalid: invalid generation phase")
    tables = payload["tables"]
    if type(tables) is not dict or set(tables) != set(TABLE_FILES):
        raise ValueError("bundle_recovery_invalid: invalid table inventory")
    for table, filename in TABLE_FILES.items():
        entry = tables[table]
        if type(entry) is not dict or set(entry) != {
            "filename",
            "sha256",
            "staged_filename",
        }:
            raise ValueError("bundle_recovery_invalid: invalid table entry")
        digest = entry["sha256"]
        if entry["filename"] != filename or not (
            digest is None and payload["phase"] in {"staging", "recovering"}
        ) and not _SHA256.fullmatch(str(digest)):
            raise ValueError("bundle_recovery_invalid: invalid table declaration")
        staged_path = _owned_recovery_path(
            directory, entry["staged_filename"], label=f"{table} staged"
        )
        if not staged_path.name.startswith(f".{filename}.") or not staged_path.name.endswith(
            ".tmp"
        ):
            raise ValueError("bundle_recovery_invalid: invalid staged table name")
    manifest = payload["manifest"]
    if type(manifest) is not dict or set(manifest) != {
        "staged_filename",
        "sha256",
    }:
        raise ValueError("bundle_recovery_invalid: invalid manifest declaration")
    manifest_stage = _owned_recovery_path(
        directory, manifest["staged_filename"], label="staged manifest"
    )
    if not manifest_stage.name.startswith(".manifest.") or not manifest_stage.name.endswith(
        ".tmp"
    ):
        raise ValueError("bundle_recovery_invalid: invalid staged manifest name")
    if not (
        manifest["sha256"] is None
        and payload["phase"] in {"staging", "recovering"}
    ) and not _SHA256.fullmatch(str(manifest["sha256"])):
        raise ValueError("bundle_recovery_invalid: invalid manifest digest")
    return payload


def _read_incomplete(directory: Path) -> dict[str, object]:
    return _read_incomplete_path(directory / _INCOMPLETE_FILE, directory)


def _adopt_incomplete_bootstrap(directory: Path) -> None:
    bootstraps = list(directory.glob(".incomplete-generation.*.tmp"))
    if len(bootstraps) > 1:
        raise ValueError("bundle_recovery_invalid: multiple bootstrap journals")
    if not bootstraps:
        return
    bootstrap = bootstraps[0]
    payload = _read_incomplete_path(bootstrap, directory)
    marker = directory / _INCOMPLETE_FILE
    if marker.exists() or marker.is_symlink():
        current = _read_incomplete(directory)
        if current["generation_id"] != payload["generation_id"]:
            raise ValueError("bundle_recovery_invalid: bootstrap generation mismatch")
        bootstrap.unlink()
    else:
        bootstrap.replace(marker)
    _fsync_directory(directory)


def _recover_incomplete(directory: Path) -> None:
    payload = _read_incomplete(directory)
    declared: set[Path] = set()
    strict = payload["phase"] == "publishing"
    for table, filename in TABLE_FILES.items():
        entry = payload["tables"][table]
        live = directory / filename
        staged = _owned_recovery_path(
            directory, entry["staged_filename"], label=f"{table} staged"
        )
        declared.add(staged)
        existing = [path for path in (live, staged) if path.is_file()]
        if (strict and len(existing) != 1) or len(existing) > 1:
            raise ValueError(
                f"bundle_recovery_invalid: incomplete table mismatch {table!r}"
            )
        if existing and entry["sha256"] is not None and (
            _sha256(existing[0]) != entry["sha256"]
        ):
            raise ValueError(
                f"bundle_recovery_invalid: incomplete table mismatch {table!r}"
            )
        declared.add(live)

    manifest = payload["manifest"]
    manifest_stage = _owned_recovery_path(
        directory, manifest["staged_filename"], label="staged manifest"
    )
    if strict and not manifest_stage.is_file():
        raise ValueError("bundle_recovery_invalid: staged manifest mismatch")
    if manifest_stage.is_file() and manifest["sha256"] is not None and (
        _sha256(manifest_stage) != manifest["sha256"]
    ):
        raise ValueError("bundle_recovery_invalid: staged manifest mismatch")
    declared.add(manifest_stage)

    relevant = {
        path
        for path in directory.iterdir()
        if path.name in TABLE_FILES.values()
        or (
            path.name.startswith(".")
            and path.name.endswith(".tmp")
            and (
                any(
                    path.name.startswith(f".{filename}.")
                    for filename in TABLE_FILES.values()
                )
                or path.name.startswith(".manifest.")
            )
        )
    }
    if not relevant.issubset(declared):
        raise ValueError("bundle_recovery_invalid: undeclared generation files")

    if payload["phase"] != "recovering":
        _publish_incomplete({**payload, "phase": "recovering"}, directory)

    for path in declared:
        path.unlink(missing_ok=True)
    (directory / _INCOMPLETE_FILE).unlink(missing_ok=True)
    _fsync_directory(directory)


def write_bundle(
    directory: str | Path,
    *,
    bars: pd.DataFrame,
    universes: pd.DataFrame,
    dominants: pd.DataFrame,
    roll_fills: pd.DataFrame,
    inputs: Mapping[str, object] | None = None,
    provenance: Mapping[str, object] | None = None,
) -> PanelBundle:
    """Publish a deterministic bundle, or return an identical valid generation."""
    path = _safe_directory(directory, create=True)
    clean_inputs = _manifest_object(inputs or {}, path="inputs")
    clean_provenance = _manifest_object(provenance or {}, path="provenance")
    with _bundle_lock(path):
        return _write_bundle_locked(
            path,
            bars=bars,
            universes=universes,
            dominants=dominants,
            roll_fills=roll_fills,
            clean_inputs=clean_inputs,
            clean_provenance=clean_provenance,
        )


def _write_bundle_locked(
    path: Path,
    *,
    bars: pd.DataFrame,
    universes: pd.DataFrame,
    dominants: pd.DataFrame,
    roll_fills: pd.DataFrame,
    clean_inputs: object,
    clean_provenance: object,
) -> PanelBundle:

    _adopt_incomplete_bootstrap(path)
    manifest_path = path / "manifest.json"
    incomplete_marker = path / _INCOMPLETE_FILE
    if incomplete_marker.exists() or incomplete_marker.is_symlink():
        if manifest_path.exists() or manifest_path.is_symlink():
            # A crash can happen after the manifest commit but before journal
            # removal. The committed bundle remains authoritative.
            existing = read_bundle(path)
            marker = _read_incomplete(path)
            for table in TABLE_FILES:
                if (
                    marker["tables"][table]["sha256"]
                    != existing.manifest["tables"][table]["sha256"]
                ):
                    raise ValueError(
                        "bundle_recovery_invalid: committed marker digest mismatch"
                    )
            for table in TABLE_FILES:
                staged_path = _owned_recovery_path(
                    path,
                    marker["tables"][table]["staged_filename"],
                    label=f"{table} staged",
                )
                staged_path.unlink(missing_ok=True)
            staged_manifest = _owned_recovery_path(
                path,
                marker["manifest"]["staged_filename"],
                label="staged manifest",
            )
            staged_manifest.unlink(missing_ok=True)
            incomplete_marker.unlink()
            _fsync_directory(path)
        else:
            _recover_incomplete(path)

    if manifest_path.exists() or manifest_path.is_symlink():
        existing = read_bundle(path)
        if (
            existing.manifest["inputs"] != clean_inputs
            or existing.manifest["provenance"] != clean_provenance
        ):
            raise ValueError("bundle_input_mismatch: existing inputs/provenance differ")
        return existing

    existing_tables = [
        filename
        for filename in TABLE_FILES.values()
        if (path / filename).exists() or (path / filename).is_symlink()
    ]
    if existing_tables:
        raise ValueError(
            "bundle_manifest_missing: refusing to replace unclaimed tables "
            f"{existing_tables!r}"
        )
    orphan_stages = [
        item.name
        for item in path.iterdir()
        if item.name.endswith(".tmp")
        and (
            any(
                item.name.startswith(f".{filename}.")
                for filename in TABLE_FILES.values()
            )
            or item.name.startswith(".manifest.")
        )
    ]
    if orphan_stages:
        raise ValueError(
            "bundle_manifest_missing: refusing unclaimed staged files "
            f"{sorted(orphan_stages)!r}"
        )

    frames = {
        "bars": bars,
        "universes": universes,
        "dominants": dominants,
        "roll_fills": roll_fills,
    }
    normalised = {
        table: normalise_bundle_table(table, frame) for table, frame in frames.items()
    }
    _validate_bundle_relationships(normalised)

    generation_id = uuid.uuid4().hex
    staged = {
        table: path / f".{filename}.{generation_id}.tmp"
        for table, filename in TABLE_FILES.items()
    }
    manifest_temporary = path / f".manifest.{generation_id}.tmp"
    published: list[Path] = []
    marker_published = True
    try:
        _publish_incomplete(
            _incomplete_payload(
                generation_id=generation_id,
                table_manifest={
                    table: {"filename": filename, "sha256": None}
                    for table, filename in TABLE_FILES.items()
                },
                staged=staged,
                manifest_temporary=manifest_temporary,
                manifest_sha256=None,
                phase="staging",
            ),
            path,
        )

        table_manifest: dict[str, dict[str, str]] = {}
        for table, filename in TABLE_FILES.items():
            temporary = _stage_parquet(normalised[table], staged[table])
            table_manifest[table] = {
                "filename": filename,
                "sha256": _sha256(temporary),
            }

        manifest: dict[str, object] = {
            "bundle_version": BUNDLE_VERSION,
            "inputs": clean_inputs,
            "provenance": clean_provenance,
            "tables": table_manifest,
        }
        payload = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with manifest_temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_incomplete(
            _incomplete_payload(
                generation_id=generation_id,
                table_manifest=table_manifest,
                staged=staged,
                manifest_temporary=manifest_temporary,
                manifest_sha256=_sha256(manifest_temporary),
                phase="publishing",
            ),
            path,
        )

        for table, filename in TABLE_FILES.items():
            live_path = path / filename
            published.append(live_path)
            staged[table].replace(live_path)
        manifest_temporary.replace(manifest_path)
        _fsync_directory(path)
        incomplete_marker.unlink()
        marker_published = False
        _fsync_directory(path)
        published.clear()
        return PanelBundle(manifest=manifest, **normalised)
    finally:
        if manifest_path.is_file() and not manifest_path.is_symlink():
            published.clear()
        elif marker_published and incomplete_marker.exists():
            _recover_incomplete(path)
            published.clear()
        else:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
            manifest_temporary.unlink(missing_ok=True)
            for live_path in published:
                live_path.unlink(missing_ok=True)


def _read_manifest(path: Path) -> dict[str, object]:
    manifest_path = path / "manifest.json"

    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"bundle_manifest_missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"bundle_manifest_invalid: {exc}") from exc
    if type(manifest) is not dict:
        raise ValueError("bundle_manifest_shape: manifest must be an object")
    expected_keys = {"bundle_version", "inputs", "provenance", "tables"}
    if set(manifest) != expected_keys:
        raise ValueError(
            "bundle_manifest_shape: "
            f"missing={sorted(expected_keys - set(manifest))!r} "
            f"extra={sorted(set(manifest) - expected_keys)!r}"
        )
    if type(manifest.get("bundle_version")) is not int or (
        manifest["bundle_version"] != BUNDLE_VERSION
    ):
        raise ValueError(
            "bundle_version_unsupported: "
            f"expected={BUNDLE_VERSION} got={manifest.get('bundle_version')!r}"
        )
    if type(manifest["inputs"]) is not dict or type(manifest["provenance"]) is not dict:
        raise ValueError("bundle_manifest_shape: inputs and provenance must be objects")
    _manifest_object(manifest["inputs"], path="inputs")
    _manifest_object(manifest["provenance"], path="provenance")

    tables = manifest["tables"]
    if type(tables) is not dict or set(tables) != set(TABLE_FILES):
        actual = set(tables) if type(tables) is dict else set()
        raise ValueError(
            "bundle_manifest_shape: "
            f"table_missing={sorted(set(TABLE_FILES) - actual)!r} "
            f"table_extra={sorted(actual - set(TABLE_FILES))!r}"
        )
    for table, filename in TABLE_FILES.items():
        entry = tables[table]
        if type(entry) is not dict or set(entry) != {"filename", "sha256"}:
            raise ValueError(f"bundle_manifest_shape: invalid table entry {table!r}")
        if entry["filename"] != filename:
            raise ValueError(f"bundle_manifest_shape: invalid filename for {table!r}")
        if type(entry["sha256"]) is not str or not _SHA256.fullmatch(entry["sha256"]):
            raise ValueError(f"bundle_manifest_shape: invalid digest for {table!r}")
    return manifest


def read_bundle(directory: str | Path) -> PanelBundle:
    """Fail closed: validate manifest and every file digest before Parquet decoding."""
    path = _safe_directory(directory, create=False)
    manifest = _read_manifest(path)
    tables = manifest["tables"]
    table_paths: dict[str, Path] = {}

    for table, filename in TABLE_FILES.items():
        table_path = path / filename
        if table_path.is_symlink() or not table_path.is_file():
            raise ValueError(f"bundle_table_missing: table={table!r} path={table_path}")
        actual = _sha256(table_path)
        expected = tables[table]["sha256"]
        if actual != expected:
            raise ValueError(
                "bundle_digest_mismatch: "
                f"table={table!r} expected={expected} actual={actual}"
            )
        table_paths[table] = table_path

    frames = {
        table: normalise_bundle_table(table, pd.read_parquet(table_path))
        for table, table_path in table_paths.items()
    }
    _validate_bundle_relationships(frames)
    return PanelBundle(manifest=manifest, **frames)
