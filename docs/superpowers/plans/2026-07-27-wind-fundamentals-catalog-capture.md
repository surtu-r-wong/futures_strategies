# Wind Fundamentals Catalog and Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-07-31 trimmed per workspace anti-redundancy ruling** (vertical slice
> first; deferred items listed at bottom of Plans 2 and 3); original scope in
> git history at `e31e1e5`. This plan is kept essentially as-is: data capture
> is the irreplaceable part (Wind quota principle — unused quota is wasted).
> Only the process-tier note below and the cross-references to the trimmed
> Plans 2/3 changed.

**Goal:** Build a Windows-compatible, catalog-driven Wind WSD/EDB capture package that produces a reviewed versioned series catalog and resumable raw recovery artifacts for `M, RB, CU, AL, TA, PP, MA, BU, RU`.

**Architecture:** A pure-Python core validates immutable YAML catalog versions and normalizes Wind responses behind an injected gateway. The Windows-only adapter imports `WindPy` lazily, while Linux tests use a fake gateway. Every successful source/year chunk is written atomically to a gzip JSONL recovery package before any database upload is attempted.

**Review tiers:** Across the three plans: PIT vintage selection + basis calculation = Tier 1 (full review); other code = Tier 2 (single combined review); docs/config = Tier 3 (self-verify). For this plan specifically — it contains neither vintage selection nor basis calculation — that means: capture/normalization code (models, catalog, adapter, availability, preflight, recovery, capture) = **Tier 2** (single review); catalog YAML validation = **Tier 2**; pure docs (runbook) = **Tier 3** (self-verify).

**Tech Stack:** Python 3.11+/3.13, dataclasses, `decimal`, PyYAML, WindPy on the Windows data machine, gzip JSONL, SHA-256, pytest

---

**Design reference:**
`/home/elfbob/claude-code/futures_strategies/docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`

**Repository:** `/home/elfbob/market-monitor`

**This plan stops before database DDL or writes.** Its output is an approved
`catalog.v1.yaml`, a preflight report, and a small recovery-package smoke
artifact. The next plan (the trimmed
`2026-07-27-commodity-fundamentals-store-materialize.md`: append-only
observation store keyed by capture `run_id`, single conservative PIT mode,
one materialized daily build) owns PostgreSQL ingestion and standardization.

## File map

| File | Responsibility |
|---|---|
| `commodity_fundamentals/__init__.py` | Public package exports |
| `commodity_fundamentals/models.py` | Immutable catalog and observation types |
| `commodity_fundamentals/catalog.py` | YAML parsing, validation, canonical hashing |
| `commodity_fundamentals/wind_adapter.py` | Wind gateway protocol and lazy WindPy adapter |
| `commodity_fundamentals/availability.py` | Shared release-lag and trading-calendar availability rule |
| `commodity_fundamentals/preflight.py` | Source coverage profiling and report CLI |
| `commodity_fundamentals/recovery.py` | Atomic manifest and gzip JSONL recovery artifacts |
| `commodity_fundamentals/capture.py` | Chunking, retry-safe capture orchestration and CLI |
| `commodity_fundamentals/catalog.v1.yaml` | Reviewed Wind series catalog produced by this plan |
| `tests/commodity_fundamentals/conftest.py` | Shared valid catalog and fake Wind fixtures |
| `tests/commodity_fundamentals/test_models.py` | Domain-model tests |
| `tests/commodity_fundamentals/test_catalog.py` | Catalog validation and hash tests |
| `tests/commodity_fundamentals/test_wind_adapter.py` | Wind response normalization tests |
| `tests/commodity_fundamentals/test_preflight.py` | Coverage profile tests |
| `tests/commodity_fundamentals/test_recovery.py` | Atomicity and resume tests |
| `tests/commodity_fundamentals/test_capture.py` | Chunk orchestration tests |
| `docs/operations/commodity-fundamentals-wind.md` | Windows deployment and preflight runbook |

### Task 1: Preserve and compare the live Windows collector

**Files:**
- Read: `D:\marketmonitor\realtime_upgrade\`
- Read: `/home/elfbob/market-monitor/data-collecter/realtime_upgrade/`
- Create outside Git: `/tmp/wind-data-collecter-live-20260727.zip`

- [ ] **Step 1: Create a read-only snapshot on the Wind machine**

Run in Windows PowerShell:

```powershell
$source = "D:\marketmonitor\realtime_upgrade"
$snapshotDir = "D:\marketmonitor\snapshots"
$archive = Join-Path $snapshotDir "wind-data-collecter-live-20260727.zip"
New-Item -ItemType Directory -Force -Path $snapshotDir | Out-Null
Compress-Archive -Path "$source\*" -DestinationPath $archive -Force
Get-FileHash -Algorithm SHA256 $archive
```

Expected: one SHA-256 line for
`D:\marketmonitor\snapshots\wind-data-collecter-live-20260727.zip`.

- [ ] **Step 2: Transfer the archive without overwriting repository files**

Copy the archive to:

```text
/tmp/wind-data-collecter-live-20260727.zip
```

Use the existing manual Windows-to-ThinkPad transfer method. Do not copy the
archive over `data-collecter/realtime_upgrade`.

- [ ] **Step 3: Compare the live snapshot with the repository backup**

Run:

```bash
snapshot_dir="$(mktemp -d /tmp/wind-collector-compare.XXXXXX)"
unzip -q /tmp/wind-data-collecter-live-20260727.zip -d "$snapshot_dir"
diff -ru \
  --exclude='__pycache__' \
  --exclude='*.log' \
  --exclude='watchlist.xlsx' \
  /home/elfbob/market-monitor/data-collecter/realtime_upgrade \
  "$snapshot_dir"
```

Expected: either no output, or a bounded diff that is reviewed before any
collector deployment. Preserve the diff in the execution transcript; do not
merge unrelated live changes into this feature.

- [ ] **Step 4: Confirm both repositories are clean enough to begin**

Run:

```bash
git -C /home/elfbob/market-monitor status --short --branch
git -C /home/elfbob/claude-code/futures_strategies status --short --branch
```

Expected: only already-understood changes. Stop if either command shows an
unrelated change that overlaps a file listed in this plan.

### Task 2: Add immutable domain models

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/__init__.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/models.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/commodity_fundamentals/test_models.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from commodity_fundamentals.models import Observation, SeriesSpec


def test_series_spec_normalizes_product_and_method():
    spec = SeriesSpec.from_mapping(
        {
            "series_id": "m.spot.primary",
            "source_code": "S1234567",
            "source_name": "豆粕现货",
            "product_code": "m",
            "metric_role": "spot",
            "frequency": "daily",
            "api_method": "edb",
            "field_name": None,
            "wind_options": "",
            "source_unit": "元/吨",
            "target_unit": "元/吨",
            "currency": "CNY",
            "scale_multiplier": "1",
            "aggregation_rule": "primary",
            "date_semantics": "observation_date",
            "release_lag_rule": {"calendar_days": 0, "time": "15:00"},
            "unknown_time_policy": "next_trading_close",
            "max_staleness_trading_days": 5,
            "valid_from": "2016-01-01",
            "valid_to": None,
            "active": True,
        }
    )

    assert spec.product_code == "M"
    assert spec.api_method == "edb"
    assert spec.scale_multiplier == Decimal("1")


def test_wsd_series_requires_field_name():
    row = {
        "series_id": "rb.spot.primary",
        "source_code": "RB.SHF",
        "source_name": "螺纹钢现货",
        "product_code": "RB",
        "metric_role": "spot",
        "frequency": "daily",
        "api_method": "wsd",
        "field_name": None,
        "wind_options": "",
        "source_unit": "元/吨",
        "target_unit": "元/吨",
        "currency": "CNY",
        "scale_multiplier": "1",
        "aggregation_rule": "primary",
        "date_semantics": "observation_date",
        "release_lag_rule": {"calendar_days": 0, "time": "15:00"},
        "unknown_time_policy": "next_trading_close",
        "max_staleness_trading_days": 5,
        "valid_from": "2016-01-01",
        "valid_to": None,
        "active": True,
    }

    with pytest.raises(ValueError, match="field_name"):
        SeriesSpec.from_mapping(row)


def test_observation_rejects_nonfinite_values():
    with pytest.raises(ValueError, match="finite"):
        Observation(
            series_id="m.spot.primary",
            observation_date=date(2020, 1, 2),
            raw_value=Decimal("NaN"),
            recorded_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )


def test_enriched_observation_requires_aware_available_at():
    with pytest.raises(ValueError, match="available_at"):
        Observation(
            series_id="m.spot.primary",
            observation_date=date(2020, 1, 2),
            raw_value=Decimal("100"),
            recorded_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            available_at=datetime(2020, 1, 3, 15),
            source_unit="元/吨",
            currency="CNY",
            vintage_quality="backfill_final",
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_models.py -q
```

Expected: collection fails because `commodity_fundamentals.models` does not
exist.

- [ ] **Step 3: Implement the model types**

Create `commodity_fundamentals/models.py` with these public types and exact
validation rules:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

PRODUCTS = frozenset({"M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU"})
METRIC_ROLES = frozenset(
    {"spot", "inventory", "profit_direct", "profit_component", "conversion_component"}
)
FREQUENCIES = frozenset({"daily", "weekly", "monthly"})
API_METHODS = frozenset({"edb", "wsd"})
UNKNOWN_TIME_POLICIES = frozenset({"next_trading_close"})


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    source_code: str
    source_name: str
    product_code: str
    metric_role: str
    frequency: str
    api_method: str
    field_name: str | None
    wind_options: str
    source_unit: str
    target_unit: str
    currency: str
    scale_multiplier: Decimal
    aggregation_rule: str
    date_semantics: str
    release_lag_rule: dict[str, Any]
    unknown_time_policy: str
    max_staleness_trading_days: int
    valid_from: date
    valid_to: date | None
    active: bool

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "SeriesSpec":
        product = str(row["product_code"]).strip().upper()
        method = str(row["api_method"]).strip().lower()
        field = str(row["field_name"]).strip() if row.get("field_name") else None
        role = str(row["metric_role"]).strip().lower()
        frequency = str(row["frequency"]).strip().lower()
        unknown_policy = str(row["unknown_time_policy"]).strip().lower()
        if product not in PRODUCTS:
            raise ValueError(f"unsupported product_code: {product}")
        if role not in METRIC_ROLES:
            raise ValueError(f"unsupported metric_role: {role}")
        if frequency not in FREQUENCIES:
            raise ValueError(f"unsupported frequency: {frequency}")
        if method not in API_METHODS:
            raise ValueError(f"unsupported api_method: {method}")
        if method == "wsd" and not field:
            raise ValueError("field_name is required for wsd")
        if method == "edb" and field:
            raise ValueError("field_name must be empty for edb")
        if unknown_policy not in UNKNOWN_TIME_POLICIES:
            raise ValueError(f"unsupported unknown_time_policy: {unknown_policy}")
        stale = int(row["max_staleness_trading_days"])
        if stale <= 0:
            raise ValueError("max_staleness_trading_days must be positive")
        lag = dict(row["release_lag_rule"])
        if "calendar_days" not in lag or "time" not in lag:
            raise ValueError("release_lag_rule needs calendar_days and time")
        source_unit = str(row["source_unit"]).strip()
        target_unit = str(row["target_unit"]).strip()
        currency = str(row["currency"]).strip().upper()
        if not source_unit or not target_unit or not currency:
            raise ValueError("source_unit, target_unit, and currency are required")
        return cls(
            series_id=str(row["series_id"]).strip(),
            source_code=str(row["source_code"]).strip(),
            source_name=str(row["source_name"]).strip(),
            product_code=product,
            metric_role=role,
            frequency=frequency,
            api_method=method,
            field_name=field,
            wind_options=str(row.get("wind_options") or ""),
            source_unit=source_unit,
            target_unit=target_unit,
            currency=currency,
            scale_multiplier=Decimal(str(row["scale_multiplier"])),
            aggregation_rule=str(row["aggregation_rule"]).strip(),
            date_semantics=str(row["date_semantics"]).strip(),
            release_lag_rule=lag,
            unknown_time_policy=unknown_policy,
            max_staleness_trading_days=stale,
            valid_from=date.fromisoformat(str(row["valid_from"])),
            valid_to=date.fromisoformat(str(row["valid_to"])) if row.get("valid_to") else None,
            active=bool(row["active"]),
        )


@dataclass(frozen=True)
class Observation:
    series_id: str
    observation_date: date
    raw_value: Decimal
    recorded_at: datetime
    published_at: datetime | None = None
    available_at: datetime | None = None
    source_unit: str | None = None
    currency: str | None = None
    vintage_quality: str | None = None

    def __post_init__(self) -> None:
        if not self.raw_value.is_finite():
            raise ValueError("raw_value must be finite")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.published_at is not None and (
            self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("published_at must be timezone-aware")
        if self.available_at is not None and (
            self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
        ):
            raise ValueError("available_at must be timezone-aware")
        enrichment = (
            self.available_at,
            self.source_unit,
            self.currency,
            self.vintage_quality,
        )
        if any(value is not None for value in enrichment) and not all(
            value is not None for value in enrichment
        ):
            raise ValueError(
                "available_at, source_unit, currency and vintage_quality "
                "must be populated together"
            )
        if self.vintage_quality is not None and self.vintage_quality not in {
            "backfill_final",
            "captured_live",
            "legacy_unverified",
        }:
            raise ValueError(
                f"unsupported vintage_quality: {self.vintage_quality}"
            )
```

Create `commodity_fundamentals/__init__.py`:

```python
"""Auditable Wind-backed commodity fundamental data pipeline."""

from commodity_fundamentals.models import Observation, SeriesSpec

__all__ = ["Observation", "SeriesSpec"]
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run the command from Step 2.

Expected: `3 passed`.

- [ ] **Step 5: Commit the domain model**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/__init__.py \
  commodity_fundamentals/models.py \
  tests/commodity_fundamentals/test_models.py
git commit -m "feat: define commodity fundamental source models"
```

### Task 3: Parse, validate, and hash immutable catalogs

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/catalog.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/conftest.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_catalog.py`

- [ ] **Step 1: Add shared fixtures and failing catalog tests**

Create `tests/commodity_fundamentals/conftest.py`:

```python
from copy import deepcopy

import pytest


@pytest.fixture
def valid_catalog_dict():
    return {
        "catalog_version": "v1",
        "series": [
            {
                "series_id": "m.spot.primary",
                "source_code": "S1234567",
                "source_name": "豆粕现货",
                "product_code": "M",
                "metric_role": "spot",
                "frequency": "daily",
                "api_method": "edb",
                "field_name": None,
                "wind_options": "",
                "source_unit": "元/吨",
                "target_unit": "元/吨",
                "currency": "CNY",
                "scale_multiplier": "1",
                "aggregation_rule": "primary",
                "date_semantics": "observation_date",
                "release_lag_rule": {"calendar_days": 0, "time": "15:00"},
                "unknown_time_policy": "next_trading_close",
                "max_staleness_trading_days": 5,
                "valid_from": "2016-01-01",
                "valid_to": None,
                "active": True,
            }
        ],
    }


@pytest.fixture
def copy_catalog(valid_catalog_dict):
    return lambda: deepcopy(valid_catalog_dict)
```

Create `tests/commodity_fundamentals/test_catalog.py`:

```python
import yaml
import pytest

from commodity_fundamentals.catalog import load_catalog


def _write(path, payload):
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def test_catalog_hash_is_stable_across_yaml_key_order(tmp_path, copy_catalog):
    first = copy_catalog()
    second = {
        "series": first["series"],
        "catalog_version": first["catalog_version"],
    }
    p1 = tmp_path / "one.yaml"
    p2 = tmp_path / "two.yaml"
    _write(p1, first)
    _write(p2, second)

    assert load_catalog(p1).config_hash == load_catalog(p2).config_hash


def test_catalog_rejects_duplicate_series_id(tmp_path, copy_catalog):
    payload = copy_catalog()
    payload["series"].append(dict(payload["series"][0]))
    path = tmp_path / "duplicate.yaml"
    _write(path, payload)

    with pytest.raises(ValueError, match="duplicate series_id"):
        load_catalog(path)


def test_catalog_rejects_au_and_ag(tmp_path, copy_catalog):
    payload = copy_catalog()
    payload["series"][0]["product_code"] = "AU"
    path = tmp_path / "excluded.yaml"
    _write(path, payload)

    with pytest.raises(ValueError, match="unsupported product_code"):
        load_catalog(path)
```

- [ ] **Step 2: Verify the catalog tests fail**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_catalog.py -q
```

Expected: import failure for `commodity_fundamentals.catalog`.

- [ ] **Step 3: Implement canonical catalog loading**

Create `commodity_fundamentals/catalog.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

import yaml

from commodity_fundamentals.models import SeriesSpec


@dataclass(frozen=True)
class Catalog:
    catalog_version: str
    series: tuple[SeriesSpec, ...]
    config_hash: str

    def active_series(self) -> tuple[SeriesSpec, ...]:
        return tuple(spec for spec in self.series if spec.active)


def load_catalog(path: str | Path) -> Catalog:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    version = str(payload.get("catalog_version") or "").strip()
    if not version:
        raise ValueError("catalog_version is required")
    rows = payload.get("series")
    if not isinstance(rows, list) or not rows:
        raise ValueError("series must be a non-empty list")
    specs = tuple(SeriesSpec.from_mapping(row) for row in rows)
    ids = [spec.series_id for spec in specs]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate series_id: {duplicates}")
    canonical = {
        "catalog_version": version,
        "series": [
            _jsonable(asdict(spec))
            for spec in sorted(specs, key=lambda item: item.series_id)
        ],
    }
    digest = sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return Catalog(version, specs, digest)


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value.__class__.__module__ == "decimal" else value
```

- [ ] **Step 4: Verify catalog tests pass**

Run the command from Step 2.

Expected: `3 passed`.

- [ ] **Step 5: Commit catalog validation**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/catalog.py \
  tests/commodity_fundamentals/conftest.py \
  tests/commodity_fundamentals/test_catalog.py
git commit -m "feat: validate versioned Wind series catalogs"
```

### Task 4: Isolate WindPy behind a testable gateway

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/wind_adapter.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_wind_adapter.py`

- [ ] **Step 1: Write failing adapter tests**

Create `tests/commodity_fundamentals/test_wind_adapter.py`:

```python
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from commodity_fundamentals.models import SeriesSpec
from commodity_fundamentals.wind_adapter import (
    WindQueryError,
    fetch_series,
    fetch_trading_dates,
)


def _spec(method, field=None):
    return SeriesSpec.from_mapping(
        {
            "series_id": "m.spot.primary",
            "source_code": "S1234567",
            "source_name": "豆粕现货",
            "product_code": "M",
            "metric_role": "spot",
            "frequency": "daily",
            "api_method": method,
            "field_name": field,
            "wind_options": "",
            "source_unit": "元/吨",
            "target_unit": "元/吨",
            "currency": "CNY",
            "scale_multiplier": "1",
            "aggregation_rule": "primary",
            "date_semantics": "observation_date",
            "release_lag_rule": {"calendar_days": 0, "time": "15:00"},
            "unknown_time_policy": "next_trading_close",
            "max_staleness_trading_days": 5,
            "valid_from": "2016-01-01",
            "valid_to": None,
            "active": True,
        }
    )


class FakeWind:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def edb(self, code, start, end, options):
        self.calls.append(("edb", code, start, end, options))
        return self.result

    def wsd(self, code, field, start, end, options):
        self.calls.append(("wsd", code, field, start, end, options))
        return self.result

    def tdays(self, start, end, options):
        self.calls.append(("tdays", start, end, options))
        return self.result


def test_edb_response_becomes_sorted_observations():
    result = SimpleNamespace(
        ErrorCode=0,
        Times=[datetime(2020, 1, 3), datetime(2020, 1, 2)],
        Data=[[101.5, 100.0]],
    )
    gateway = FakeWind(result)

    points = fetch_series(gateway, _spec("edb"), date(2020, 1, 1), date(2020, 1, 5))

    assert [(p.observation_date, str(p.raw_value)) for p in points] == [
        (date(2020, 1, 2), "100.0"),
        (date(2020, 1, 3), "101.5"),
    ]
    assert gateway.calls[0][0] == "edb"


def test_wsd_uses_configured_field():
    result = SimpleNamespace(
        ErrorCode=0,
        Times=[datetime(2020, 1, 2)],
        Data=[[88.0]],
    )
    gateway = FakeWind(result)

    fetch_series(gateway, _spec("wsd", "close"), date(2020, 1, 1), date(2020, 1, 5))

    assert gateway.calls[0][0:3] == ("wsd", "S1234567", "close")


def test_wind_error_code_is_not_treated_as_empty_data():
    gateway = FakeWind(SimpleNamespace(ErrorCode=-40520007, Times=[], Data=[]))

    with pytest.raises(WindQueryError, match="-40520007"):
        fetch_series(gateway, _spec("edb"), date(2020, 1, 1), date(2020, 1, 5))


def test_tdays_response_becomes_unique_sorted_dates():
    gateway = FakeWind(
        SimpleNamespace(
            ErrorCode=0,
            Times=[
                datetime(2020, 1, 3),
                datetime(2020, 1, 2),
                datetime(2020, 1, 3),
            ],
            Data=[],
        )
    )

    dates = fetch_trading_dates(
        gateway,
        date(2020, 1, 1),
        date(2020, 1, 5),
    )

    assert dates == [date(2020, 1, 2), date(2020, 1, 3)]
    assert gateway.calls == [("tdays", "2020-01-01", "2020-01-05", "")]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_wind_adapter.py -q
```

Expected: import failure for `wind_adapter`.

- [ ] **Step 3: Implement the gateway**

Create `commodity_fundamentals/wind_adapter.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from commodity_fundamentals.models import Observation, SeriesSpec


class WindQueryError(RuntimeError):
    """Wind startup, response-code, or response-shape failure."""


class WindGateway(Protocol):
    def edb(self, code, start, end, options):
        raise NotImplementedError

    def wsd(self, code, field, start, end, options):
        raise NotImplementedError

    def tdays(self, start, end, options):
        raise NotImplementedError


class WindPyGateway:
    def __init__(self):
        from WindPy import w

        status = w.start()
        if getattr(status, "ErrorCode", -1) != 0:
            raise WindQueryError(f"Wind start failed: {status}")
        self._wind = w

    def edb(self, code, start, end, options):
        return self._wind.edb(code, start, end, options)

    def wsd(self, code, field, start, end, options):
        return self._wind.wsd(code, field, start, end, options)

    def tdays(self, start, end, options):
        return self._wind.tdays(start, end, options)


def fetch_trading_dates(
    gateway: WindGateway,
    start: date,
    end: date,
) -> list[date]:
    result = gateway.tdays(start.isoformat(), end.isoformat(), "")
    error_code = int(getattr(result, "ErrorCode", -1))
    if error_code != 0:
        raise WindQueryError(f"Wind tdays failed error={error_code}")
    times = list(getattr(result, "Times", []) or [])
    dates = sorted({item.date() for item in times})
    if not dates:
        raise WindQueryError(
            f"Wind tdays returned no dates start={start} end={end}"
        )
    return dates


def fetch_series(
    gateway: WindGateway,
    spec: SeriesSpec,
    start: date,
    end: date,
    *,
    recorded_at: datetime | None = None,
) -> list[Observation]:
    recorded_at = recorded_at or datetime.now(timezone.utc)
    if spec.api_method == "edb":
        result = gateway.edb(
            spec.source_code,
            start.isoformat(),
            end.isoformat(),
            spec.wind_options,
        )
    else:
        result = gateway.wsd(
            spec.source_code,
            spec.field_name,
            start.isoformat(),
            end.isoformat(),
            spec.wind_options,
        )
    error_code = int(getattr(result, "ErrorCode", -1))
    if error_code != 0:
        raise WindQueryError(
            f"Wind query failed series={spec.series_id} error={error_code}"
        )
    times = list(getattr(result, "Times", []) or [])
    data = list(getattr(result, "Data", []) or [])
    values = data[0] if len(data) == 1 else []
    if len(times) != len(values):
        raise WindQueryError(
            f"Wind shape mismatch series={spec.series_id} "
            f"times={len(times)} values={len(values)}"
        )
    observations = [
        Observation(
            series_id=spec.series_id,
            observation_date=item_time.date(),
            raw_value=Decimal(str(value)),
            recorded_at=recorded_at,
        )
        for item_time, value in zip(times, values)
        if value is not None
    ]
    return sorted(observations, key=lambda item: item.observation_date)
```

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: `3 passed`.

- [ ] **Step 5: Commit the adapter**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/wind_adapter.py \
  tests/commodity_fundamentals/test_wind_adapter.py
git commit -m "feat: add testable Wind fundamentals gateway"
```

### Task 5: Build deterministic preflight profiles

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/preflight.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_preflight.py`

- [ ] **Step 1: Write failing profile tests**

Create `tests/commodity_fundamentals/test_preflight.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

from commodity_fundamentals.models import Observation
from commodity_fundamentals.preflight import profile_series


def test_profile_reports_range_missingness_and_nonpositive_count():
    points = [
        Observation("m.spot.primary", date(2020, 1, 2), Decimal("100"), datetime.now(timezone.utc)),
        Observation("m.spot.primary", date(2020, 1, 3), Decimal("0"), datetime.now(timezone.utc)),
        Observation("m.spot.primary", date(2020, 1, 6), Decimal("102"), datetime.now(timezone.utc)),
    ]

    row = profile_series(
        series_id="m.spot.primary",
        points=points,
        frequency="daily",
        start=date(2020, 1, 2),
        end=date(2020, 1, 7),
    )

    assert row["first_date"] == "2020-01-02"
    assert row["last_date"] == "2020-01-06"
    assert row["point_count"] == 3
    assert row["missing_count"] == 1
    assert row["nonpositive_count"] == 1


def test_weekly_profile_counts_missing_iso_weeks():
    points = [
        Observation("m.inventory", date(2020, 1, 2), Decimal("10"), datetime.now(timezone.utc)),
        Observation("m.inventory", date(2020, 1, 16), Decimal("11"), datetime.now(timezone.utc)),
    ]

    row = profile_series(
        series_id="m.inventory",
        points=points,
        frequency="weekly",
        start=date(2020, 1, 1),
        end=date(2020, 1, 21),
    )

    assert row["missing_count"] == 2


def test_monthly_profile_counts_missing_calendar_months():
    points = [
        Observation("m.profit", date(2020, 1, 15), Decimal("10"), datetime.now(timezone.utc)),
        Observation("m.profit", date(2020, 3, 15), Decimal("11"), datetime.now(timezone.utc)),
    ]

    row = profile_series(
        series_id="m.profit",
        points=points,
        frequency="monthly",
        start=date(2020, 1, 1),
        end=date(2020, 3, 31),
    )

    assert row["missing_count"] == 1
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_preflight.py -q
```

Expected: import failure for `preflight`.

- [ ] **Step 3: Implement profiling and CLI output**

Create `commodity_fundamentals/preflight.py` with:

```python
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path

from commodity_fundamentals.catalog import load_catalog
from commodity_fundamentals.wind_adapter import WindPyGateway, fetch_series


def cadence_key(value: date, frequency: str):
    if frequency == "daily":
        return value
    if frequency == "weekly":
        iso = value.isocalendar()
        return iso.year, iso.week
    if frequency == "monthly":
        return value.year, value.month
    raise ValueError(f"unsupported frequency: {frequency}")


def expected_cadence_keys(
    *,
    frequency: str,
    start: date,
    end: date,
):
    if end < start:
        raise ValueError("end precedes start")
    keys = set()
    cursor = start
    while cursor <= end:
        if frequency != "daily" or cursor.weekday() < 5:
            keys.add(cadence_key(cursor, frequency))
        cursor += timedelta(days=1)
    return keys


def profile_series(*, series_id, points, frequency, start, end):
    dates = {point.observation_date for point in points}
    observed = {cadence_key(value, frequency) for value in dates}
    expected = expected_cadence_keys(
        frequency=frequency,
        start=start,
        end=end,
    )
    return {
        "series_id": series_id,
        "first_date": min(dates).isoformat() if dates else "",
        "last_date": max(dates).isoformat() if dates else "",
        "point_count": len(points),
        "missing_count": len(expected - observed),
        "nonpositive_count": sum(point.raw_value <= 0 for point in points),
    }


def write_report(rows, path: Path) -> None:
    fields = [
        "series_id",
        "product_code",
        "metric_role",
        "source_code",
        "source_name",
        "frequency",
        "source_unit",
        "target_unit",
        "currency",
        "first_date",
        "last_date",
        "point_count",
        "missing_count",
        "nonpositive_count",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    gateway = WindPyGateway()
    rows = []
    for spec in catalog.active_series():
        points = fetch_series(gateway, spec, args.start, args.end)
        row = profile_series(
            series_id=spec.series_id,
            points=points,
            frequency=spec.frequency,
            start=args.start,
            end=args.end,
        )
        row.update(
            {
                "product_code": spec.product_code,
                "metric_role": spec.metric_role,
                "source_code": spec.source_code,
                "source_name": spec.source_name,
                "frequency": spec.frequency,
                "source_unit": spec.source_unit,
                "target_unit": spec.target_unit,
                "currency": spec.currency,
            }
        )
        rows.append(row)
    write_report(rows, args.output)


if __name__ == "__main__":
    main()
```

The cadence keys deliberately avoid guessing a weekly release weekday or a
monthly release day. Daily series are checked on weekdays, weekly series by
ISO week, and monthly series by calendar month. Exchange holidays can
therefore overstate daily missingness in this source-discovery report; every
daily exception is reviewed against the trading calendar before catalog
approval.

- [ ] **Step 4: Verify GREEN and add cadence cases**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_preflight.py -q
```

Expected: all preflight tests pass, including daily, weekly, and monthly
cadence cases.

- [ ] **Step 5: Commit preflight profiling**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/preflight.py \
  tests/commodity_fundamentals/test_preflight.py
git commit -m "feat: profile Wind fundamental series coverage"
```

### Task 6: Write atomic recovery packages

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/recovery.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_recovery.py`

- [ ] **Step 1: Write failing recovery tests**

Create `tests/commodity_fundamentals/test_recovery.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import json

from commodity_fundamentals.models import Observation
from commodity_fundamentals.recovery import RecoveryPackage


def test_chunk_write_is_atomic_and_manifest_marks_completion(tmp_path):
    package = RecoveryPackage.create(
        tmp_path,
        run_id="11111111-1111-1111-1111-111111111111",
        catalog_version="v1",
        config_hash="abc",
        mode="backfill",
        requested_start=date(2020, 1, 1),
        requested_end=date(2020, 12, 31),
    )
    point = Observation(
        "m.spot.primary",
        date(2020, 1, 2),
        Decimal("100.5"),
        datetime(2026, 7, 27, tzinfo=timezone.utc),
    )

    package.write_chunk("m.spot.primary", 2020, [point])

    assert not list(package.root.glob("*.partial"))
    manifest = json.loads((package.root / "manifest.json").read_text())
    assert manifest["chunks"]["m.spot.primary:2020"]["status"] == "complete"
    with gzip.open(package.root / "m.spot.primary__2020.jsonl.gz", "rt") as handle:
        row = json.loads(handle.readline())
    assert row["raw_value"] == "100.5"


def test_completed_chunk_is_skipped_on_resume(tmp_path):
    package = RecoveryPackage.create(
        tmp_path,
        run_id="11111111-1111-1111-1111-111111111111",
        catalog_version="v1",
        config_hash="abc",
        mode="backfill",
        requested_start=date(2020, 1, 1),
        requested_end=date(2020, 12, 31),
    )
    package.write_chunk("m.spot.primary", 2020, [])

    reopened = RecoveryPackage.open(package.root)

    assert reopened.is_complete("m.spot.primary", 2020)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_recovery.py -q
```

Expected: import failure for `recovery`.

- [ ] **Step 3: Implement atomic chunk and manifest writes**

Implement `RecoveryPackage` in `commodity_fundamentals/recovery.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import UUID

from commodity_fundamentals.models import Observation


class RecoveryPackage:
    REQUIRED_MANIFEST_FIELDS = {
        "run_id",
        "catalog_version",
        "config_hash",
        "mode",
        "requested_start",
        "requested_end",
        "created_at",
        "trading_dates",
        "chunks",
    }

    def __init__(self, root: Path, manifest: dict):
        self.root = root
        self.manifest = manifest

    @classmethod
    def create(
        cls,
        base_dir: str | Path,
        *,
        run_id: str,
        catalog_version: str,
        config_hash: str,
        mode: str,
        requested_start: date,
        requested_end: date,
    ) -> "RecoveryPackage":
        UUID(run_id)
        if requested_end < requested_start:
            raise ValueError("requested_end precedes requested_start")
        if mode not in {"backfill", "incremental", "audit"}:
            raise ValueError(f"unsupported mode: {mode}")
        if not catalog_version or not config_hash:
            raise ValueError("catalog_version and config_hash are required")
        root = Path(base_dir) / run_id
        root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "run_id": run_id,
            "catalog_version": catalog_version,
            "config_hash": config_hash,
            "mode": mode,
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trading_dates": [],
            "chunks": {},
        }
        package = cls(root, manifest)
        package._write_manifest()
        return package

    @classmethod
    def open(cls, root: str | Path) -> "RecoveryPackage":
        root = Path(root)
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid recovery manifest: {manifest_path}") from exc
        missing = cls.REQUIRED_MANIFEST_FIELDS - set(manifest)
        if missing:
            raise ValueError(f"recovery manifest missing fields: {sorted(missing)}")
        UUID(str(manifest["run_id"]))
        if root.name != manifest["run_id"]:
            raise ValueError("recovery directory does not match manifest run_id")
        date.fromisoformat(manifest["requested_start"])
        date.fromisoformat(manifest["requested_end"])
        if not isinstance(manifest["chunks"], dict):
            raise ValueError("recovery manifest chunks must be an object")
        if not isinstance(manifest["trading_dates"], list):
            raise ValueError("recovery manifest trading_dates must be a list")
        for value in manifest["trading_dates"]:
            date.fromisoformat(value)
        return cls(root, manifest)

    @property
    def requested_start(self) -> date:
        return date.fromisoformat(self.manifest["requested_start"])

    @property
    def requested_end(self) -> date:
        return date.fromisoformat(self.manifest["requested_end"])

    @property
    def catalog_version(self) -> str:
        return str(self.manifest["catalog_version"])

    @property
    def config_hash(self) -> str:
        return str(self.manifest["config_hash"])

    @property
    def mode(self) -> str:
        return str(self.manifest["mode"])

    @property
    def trading_dates(self) -> list[date]:
        return [
            date.fromisoformat(value)
            for value in self.manifest["trading_dates"]
        ]

    def set_trading_dates(self, values: list[date]) -> None:
        normalized = sorted(set(values))
        if not normalized:
            raise ValueError("trading_dates must not be empty")
        encoded = [value.isoformat() for value in normalized]
        existing = self.manifest["trading_dates"]
        if existing and existing != encoded:
            raise ValueError("refusing to replace recovery trading calendar")
        self.manifest["trading_dates"] = encoded
        self._write_manifest()

    def is_complete(self, series_id: str, year: int) -> bool:
        key = self._chunk_key(series_id, year)
        entry = self.manifest["chunks"].get(key)
        if not entry or entry.get("status") != "complete":
            return False
        final_path = self.root / self._chunk_name(series_id, year)
        if not final_path.is_file():
            raise ValueError(f"complete chunk file missing: {final_path.name}")
        actual = self._file_hash(final_path)
        if actual != entry.get("sha256"):
            raise ValueError(f"complete chunk checksum mismatch: {key}")
        return True

    def write_chunk(
        self,
        series_id: str,
        year: int,
        observations: list[Observation],
    ) -> None:
        if "/" in series_id or "\\" in series_id:
            raise ValueError("series_id cannot contain a path separator")
        rows = sorted(observations, key=lambda item: item.observation_date)
        for point in rows:
            if point.series_id != series_id:
                raise ValueError("chunk contains a different series_id")
            if point.observation_date.year != year:
                raise ValueError("chunk contains an observation from another year")

        key = self._chunk_key(series_id, year)
        final_path = self.root / self._chunk_name(series_id, year)
        partial_path = final_path.with_name(final_path.name + ".partial")
        partial_path.unlink(missing_ok=True)
        with partial_path.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_handle,
                mtime=0,
            ) as compressed:
                for point in rows:
                    payload = {
                        "series_id": point.series_id,
                        "observation_date": point.observation_date.isoformat(),
                        "raw_value": str(point.raw_value),
                        "recorded_at": point.recorded_at.isoformat(),
                        "published_at": (
                            point.published_at.isoformat()
                            if point.published_at is not None
                            else None
                        ),
                        "available_at": (
                            point.available_at.isoformat()
                            if point.available_at is not None
                            else None
                        ),
                        "source_unit": point.source_unit,
                        "currency": point.currency,
                        "vintage_quality": point.vintage_quality,
                    }
                    compressed.write(
                        (
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
            raw_handle.flush()
            os.fsync(raw_handle.fileno())

        checksum = self._file_hash(partial_path)
        existing = self.manifest["chunks"].get(key)
        if existing and existing.get("status") == "complete":
            if existing.get("sha256") != checksum or not self.is_complete(series_id, year):
                partial_path.unlink(missing_ok=True)
                raise ValueError(f"refusing to overwrite complete chunk: {key}")
            partial_path.unlink()
            return

        partial_path.replace(final_path)
        self.manifest["chunks"][key] = {
            "status": "complete",
            "path": final_path.name,
            "row_count": len(rows),
            "sha256": checksum,
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        partial = self.root / "manifest.json.partial"
        with partial.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                self.manifest,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(self.root / "manifest.json")

    @staticmethod
    def _chunk_key(series_id: str, year: int) -> str:
        return f"{series_id}:{year}"

    @staticmethod
    def _chunk_name(series_id: str, year: int) -> str:
        return f"{series_id}__{year}.jsonl.gz"

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
```

The deterministic gzip header makes a same-payload retry produce the same
checksum. A complete chunk is read-only; corruption or payload drift raises
before either the chunk or manifest can be replaced.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: recovery tests pass.

- [ ] **Step 5: Commit recovery support**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/recovery.py \
  tests/commodity_fundamentals/test_recovery.py
git commit -m "feat: persist resumable Wind recovery packages"
```

### Task 7: Orchestrate chunked capture

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/availability.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/capture.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_availability.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_capture.py`

- [ ] **Step 1: Write failing availability and orchestration tests**

Create `tests/commodity_fundamentals/test_availability.py`:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

from commodity_fundamentals.availability import effective_available_at

CN = ZoneInfo("Asia/Shanghai")


def test_unknown_release_time_uses_next_real_trading_close():
    available = effective_available_at(
        observation_date=date(2026, 9, 30),
        published_at=None,
        recorded_at=datetime(2026, 10, 20, 10, tzinfo=CN),
        lag_rule={"calendar_days": 0, "time": None},
        unknown_time_policy="next_trading_close",
        vintage_quality="backfill_final",
        trading_dates=[
            date(2026, 9, 30),
            date(2026, 10, 9),
            date(2026, 10, 12),
        ],
    )

    assert available == datetime(2026, 10, 9, 15, tzinfo=CN)
```

Create `tests/commodity_fundamentals/test_capture.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import json

from commodity_fundamentals.capture import capture_catalog
from commodity_fundamentals.catalog import load_catalog
from commodity_fundamentals.models import Observation
from commodity_fundamentals.recovery import RecoveryPackage


def test_capture_splits_years_and_skips_completed_chunks(
    tmp_path, valid_catalog_dict, monkeypatch
):
    import yaml

    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(valid_catalog_dict, allow_unicode=True, sort_keys=False)
    )
    catalog = load_catalog(catalog_path)
    package = RecoveryPackage.create(
        tmp_path,
        run_id="11111111-1111-1111-1111-111111111111",
        catalog_version="v1",
        config_hash=catalog.config_hash,
        mode="backfill",
        requested_start=date(2019, 12, 1),
        requested_end=date(2020, 1, 31),
    )
    package.set_trading_dates(
        [
            date(2019, 12, 2),
            date(2020, 1, 1),
            date(2020, 1, 2),
        ]
    )
    package.write_chunk("m.spot.primary", 2019, [])
    calls = []

    def fake_fetch(gateway, spec, start, end, recorded_at=None):
        calls.append((start, end))
        return [
            Observation(
                spec.series_id,
                start,
                Decimal("100"),
                datetime(2026, 7, 27, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr("commodity_fundamentals.capture.fetch_series", fake_fetch)

    capture_catalog(object(), catalog, package)

    assert calls == [(date(2020, 1, 1), date(2020, 1, 31))]
    assert package.is_complete("m.spot.primary", 2020)
    with gzip.open(
        package.root / "m.spot.primary__2020.jsonl.gz",
        "rt",
        encoding="utf-8",
    ) as handle:
        row = json.loads(handle.readline())
    assert row["available_at"].startswith("2020-01-01T15:00:00")
    assert row["source_unit"] == "元/吨"
    assert row["vintage_quality"] == "backfill_final"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_capture.py -q
```

Expected: import failures for `availability` and `capture`.

- [ ] **Step 3: Implement capture orchestration and CLI**

Create `commodity_fundamentals/availability.py`:

```python
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CN = ZoneInfo("Asia/Shanghai")


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(CN)


def _first_trading_date(
    trading_dates: list[date],
    boundary: date,
    *,
    strictly_after: bool,
) -> date:
    for candidate in sorted(set(trading_dates)):
        if candidate > boundary or (
            candidate == boundary and not strictly_after
        ):
            return candidate
    relation = "after" if strictly_after else "on or after"
    raise ValueError(f"no trading date {relation} {boundary}")


def effective_available_at(
    *,
    observation_date,
    published_at,
    recorded_at,
    lag_rule,
    unknown_time_policy,
    vintage_quality,
    trading_dates,
):
    recorded_at = _require_aware(recorded_at, "recorded_at")
    if published_at is not None:
        source_available = _require_aware(published_at, "published_at")
    else:
        lagged_date = observation_date + timedelta(
            days=int(lag_rule["calendar_days"])
        )
        configured_time = lag_rule.get("time")
        if configured_time is None:
            if unknown_time_policy != "next_trading_close":
                raise ValueError(
                    f"unsupported unknown_time_policy: {unknown_time_policy}"
                )
            release_date = _first_trading_date(
                trading_dates,
                lagged_date,
                strictly_after=True,
            )
            release_time = time(15, 0)
        else:
            release_date = _first_trading_date(
                trading_dates,
                lagged_date,
                strictly_after=False,
            )
            release_time = time.fromisoformat(str(configured_time))
        source_available = datetime.combine(
            release_date,
            release_time,
            tzinfo=CN,
        )

    if vintage_quality == "captured_live":
        return max(source_available, recorded_at)
    if vintage_quality not in {"backfill_final", "legacy_unverified"}:
        raise ValueError(f"unsupported vintage_quality: {vintage_quality}")
    return source_available
```

Create `commodity_fundamentals/capture.py` with:

```python
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from commodity_fundamentals.availability import effective_available_at
from commodity_fundamentals.catalog import load_catalog
from commodity_fundamentals.recovery import RecoveryPackage
from commodity_fundamentals.wind_adapter import (
    WindPyGateway,
    fetch_series,
    fetch_trading_dates,
)


def year_chunks(start: date, end: date):
    for year in range(start.year, end.year + 1):
        yield max(start, date(year, 1, 1)), min(end, date(year, 12, 31))


def capture_catalog(gateway, catalog, package):
    vintage_quality = {
        "backfill": "backfill_final",
        "incremental": "captured_live",
    }.get(package.mode)
    if vintage_quality is None:
        raise ValueError(f"capture does not support mode={package.mode}")
    if not package.trading_dates:
        raise ValueError("recovery package has no trading calendar")
    for spec in catalog.active_series():
        for start, end in year_chunks(
            package.requested_start,
            package.requested_end,
        ):
            if end < spec.valid_from or (
                spec.valid_to is not None and start > spec.valid_to
            ):
                continue
            chunk_start = max(start, spec.valid_from)
            chunk_end = min(end, spec.valid_to) if spec.valid_to else end
            if package.is_complete(spec.series_id, chunk_start.year):
                continue
            observations = fetch_series(
                gateway,
                spec,
                chunk_start,
                chunk_end,
            )
            observations = [
                replace(
                    point,
                    available_at=effective_available_at(
                        observation_date=point.observation_date,
                        published_at=point.published_at,
                        recorded_at=point.recorded_at,
                        lag_rule=spec.release_lag_rule,
                        unknown_time_policy=spec.unknown_time_policy,
                        vintage_quality=vintage_quality,
                        trading_dates=package.trading_dates,
                    ),
                    source_unit=spec.source_unit,
                    currency=spec.currency,
                    vintage_quality=vintage_quality,
                )
                for point in observations
            ]
            package.write_chunk(
                spec.series_id,
                chunk_start.year,
                observations,
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m commodity_fundamentals.capture"
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--mode",
        choices=["backfill", "incremental"],
        required=True,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--artifact-dir", type=Path)
    target.add_argument("--resume", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    if args.resume is not None:
        if args.run_id is not None:
            parser.error("--run-id cannot be combined with --resume")
        package = RecoveryPackage.open(args.resume)
        drift = {
            "catalog_version": (
                package.catalog_version,
                catalog.catalog_version,
            ),
            "config_hash": (package.config_hash, catalog.config_hash),
            "requested_start": (package.requested_start, args.start),
            "requested_end": (package.requested_end, args.end),
            "mode": (package.mode, args.mode),
        }
        changed = {
            key: values
            for key, values in drift.items()
            if values[0] != values[1]
        }
        if changed:
            raise ValueError(f"resume request drift: {changed}")
    else:
        package = RecoveryPackage.create(
            args.artifact_dir,
            run_id=args.run_id or str(uuid4()),
            catalog_version=catalog.catalog_version,
            config_hash=catalog.config_hash,
            mode=args.mode,
            requested_start=args.start,
            requested_end=args.end,
        )

    gateway = WindPyGateway()
    trading_dates = fetch_trading_dates(
        gateway,
        args.start,
        args.end + timedelta(days=62),
    )
    package.set_trading_dates(trading_dates)
    capture_catalog(gateway, catalog, package)
    print(package.root.resolve())


if __name__ == "__main__":
    main()
```

Creating `RecoveryPackage` precedes `WindPyGateway`, so a startup failure still
leaves a durable manifest. The calendar extends 62 calendar days beyond the
request so monthly/holiday release lags can resolve to a real next trading
close. Resume compares the exact catalog version/hash, calendar and date range
before observations are rewritten.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_capture.py \
  tests/commodity_fundamentals/test_recovery.py -q
```

Expected: all capture and recovery tests pass.

- [ ] **Step 5: Commit capture orchestration**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/availability.py \
  commodity_fundamentals/capture.py \
  tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_capture.py
git commit -m "feat: capture Wind fundamentals in yearly chunks"
```

### Task 8: Add the Windows runbook and execute catalog preflight

**Files:**
- Create: `/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-wind.md`
- Create after Wind review: `/home/elfbob/market-monitor/commodity_fundamentals/catalog.v1.yaml`

- [ ] **Step 1: Write the runbook**

Document these exact Windows locations and commands:

```text
Package:   D:\marketmonitor\fundamentals\commodity_fundamentals
Catalog:   D:\marketmonitor\fundamentals\catalog.v1.yaml
Reports:   D:\marketmonitor\fundamentals\reports
Artifacts: D:\marketmonitor\fundamentals\artifacts
```

Preflight command:

```powershell
Set-Location D:\marketmonitor\fundamentals
python -m commodity_fundamentals.preflight `
  --catalog .\catalog.v1.yaml `
  --start 2016-01-01 `
  --end 2026-07-27 `
  --output .\reports\catalog-v1-preflight.csv
```

Small capture smoke:

```powershell
python -m commodity_fundamentals.capture `
  --catalog .\catalog.v1.yaml `
  --start 2025-01-01 `
  --end 2025-01-31 `
  --mode backfill `
  --artifact-dir .\artifacts
```

The runbook must state that Wind raw data and recovery artifacts are licensed
runtime data and are not committed to Git.

- [ ] **Step 2: Deploy only the new package to the Wind machine**

Copy:

```text
/home/elfbob/market-monitor/commodity_fundamentals/
```

to:

```text
D:\marketmonitor\fundamentals\commodity_fundamentals\
```

Do not overwrite `D:\marketmonitor\realtime_upgrade` in this plan.

- [ ] **Step 3: Create and validate the reviewed catalog**

Use the Wind terminal's indicator browser to enter actual WSD/EDB codes and
metadata into `D:\marketmonitor\fundamentals\catalog.v1.yaml`. Every active
row must satisfy the schema enforced by `load_catalog`; Wind code, source
name, units, release lag, and staleness must come from the terminal or source
documentation rather than inference from the old database.

Run the preflight command from Step 1.

Expected:

- basis candidates cover 9/9 products;
- inventory candidates cover at least 8/9;
- direct profit or formula-component candidates cover at least 7/9;
- every row reports a non-empty date range and point count;
- every non-positive value is reviewed against the metric's economic domain.

If a coverage target fails, stop here and return the uncovered
product/metric list for a scope decision. Do not proceed to database work.

- [ ] **Step 4: Run the small capture smoke and inspect recovery integrity**

Run the small capture command from Step 1, then:

```powershell
Get-ChildItem .\artifacts -Recurse
Get-Content .\artifacts\*\manifest.json
```

Expected: one manifest, one complete chunk per active series for January 2025,
no `.partial` files, and no failed chunk status.

- [ ] **Step 5: Copy reviewed non-raw artifacts back to the repository**

Copy only:

```text
catalog.v1.yaml
reports\catalog-v1-preflight.csv
```

The catalog goes to
`commodity_fundamentals/catalog.v1.yaml`. Store the preflight report under
`docs/data/commodity-fundamentals/catalog-v1-preflight.csv`. Confirm it
contains source metadata and aggregate coverage only, not licensed raw time
series.

- [ ] **Step 6: Run the full local test suite for this subsystem**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals -q
```

Expected: all catalog/capture tests pass.

- [ ] **Step 7: Commit the approved catalog and runbook**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/catalog.v1.yaml \
  docs/data/commodity-fundamentals/catalog-v1-preflight.csv \
  docs/operations/commodity-fundamentals-wind.md
git commit -m "data: approve Wind commodity fundamentals catalog v1"
```

### Task 9: Final verification for Plan 1

**Files:**
- Verify all files listed in this plan

- [ ] **Step 1: Run focused and repository-safe checks**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals -q
git diff --check HEAD~1 HEAD
git status --short --branch
```

Expected:

- all fundamental capture tests pass;
- `git diff --check` prints nothing;
- the worktree contains no uncommitted implementation files;
- the branch is ahead only by the intended plan commits.

- [ ] **Step 2: Record the handoff contract for Plan 2**

Record these immutable values in the execution handoff:

```text
catalog_version
catalog config_hash
approved product/metric coverage
Windows recovery package run_id
Windows recovery package manifest SHA-256
```

The trimmed Plan 2 consumes these values as immutable inputs: its
`load-catalog` step loads exactly this catalog version and hash, its
uploader uploads exactly the recorded recovery package, and its Linux-side
count verification checks the stored rows against this manifest.
