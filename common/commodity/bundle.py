"""Versioned, content-addressed commodity panel bundles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Mapping

import pandas as pd

from common.commodity.panel import PANEL_COLUMNS

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
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
    if (
        column not in _NULLABLE_COLUMNS.get(table, set())
        and converted.isna().any()
    ):
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
        raise _schema_error(table, column, f"boolean dtype required, got {values.dtype}")
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
        for column in (
            "open", "high", "low", "close", "open_interest", "fill_price"
        ):
            present = frame[column].dropna()
            if not present.map(math.isfinite).all():
                raise _value_error(
                    table, f"column={column!r} must be finite when present"
                )
        if (
            not frame["volume"].map(math.isfinite).all()
            or (frame["volume"] < 0).any()
        ):
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
    actual = list(frame.columns)
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
            out[column] = _normalise_aware_datetime(
                values, table=table, column=column
            )
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
    if table == "bars" and tuple(out.columns) != PANEL_COLUMNS:
        raise RuntimeError("bundle bars schema drifted from PANEL_COLUMNS")
    out = out.sort_values(list(_SORT_COLUMNS[table]), kind="mergesort")
    return out.reset_index(drop=True)


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
                raise ValueError(f"bundle_manifest_sensitive: forbidden key {path}.{key}")
            result[key] = _manifest_object(child, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _manifest_object(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(
        f"bundle_manifest_value: unsupported or non-finite value at {path}: {value!r}"
    )


def _safe_directory(directory: str | Path, *, create: bool) -> Path:
    path = Path(directory)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"bundle_directory_invalid: {path}")
    elif create:
        path.mkdir(parents=True, exist_ok=False)
    else:
        raise ValueError(f"bundle_directory_missing: {path}")
    return path


def _write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    """Normalize and atomically publish a deterministic version-1 bundle."""
    path = _safe_directory(directory, create=True)
    frames = {
        "bars": bars,
        "universes": universes,
        "dominants": dominants,
        "roll_fills": roll_fills,
    }
    normalised = {
        table: normalise_bundle_table(table, frame)
        for table, frame in frames.items()
    }
    clean_inputs = _manifest_object(inputs or {}, path="inputs")
    clean_provenance = _manifest_object(provenance or {}, path="provenance")

    manifest_path = path / "manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        existing = _read_manifest(path)
        if (
            existing["inputs"] != clean_inputs
            or existing["provenance"] != clean_provenance
        ):
            raise ValueError(
                "bundle_manifest_mismatch: existing inputs/provenance differ"
            )

    table_manifest: dict[str, dict[str, str]] = {}
    for table, filename in TABLE_FILES.items():
        table_path = path / filename
        if table_path.is_symlink():
            raise ValueError(f"bundle_table_path_invalid: {table_path}")
        _write_parquet_atomic(normalised[table], table_path)
        table_manifest[table] = {
            "filename": filename,
            "sha256": _sha256(table_path),
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

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path,
            prefix=".manifest.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(path / "manifest.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return PanelBundle(manifest=manifest, **normalised)


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
    return PanelBundle(manifest=manifest, **frames)
