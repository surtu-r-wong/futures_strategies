# Commodity Fundamentals Store and Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist approved Wind observations in an auditable research schema, build availability-aware conservative and strict daily fundamentals, and complete a resumable 2016-onward backfill without publishing partial data.

**Architecture:** A dedicated transactional FastAPI endpoint stores immutable catalog snapshots, ingest runs, and append-only observation vintages. A Linux builder selects permissible vintages by PIT mode, standardizes spot/inventory/profit inputs, calculates basis from the unadjusted dominant-contract close, validates coverage, and atomically promotes one current build per PIT mode.

**Tech Stack:** PostgreSQL 15, psycopg2, FastAPI/Pydantic v2, pandas, NumPy, PyYAML, Python 3.13, pytest

---

**Design reference:**
`/home/elfbob/claude-code/futures_strategies/docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`

**Prerequisite:** Complete
`docs/superpowers/plans/2026-07-27-wind-fundamentals-catalog-capture.md`
and record its catalog version, config hash, recovery `run_id`, and manifest
checksum.

**Repository:** `/home/elfbob/market-monitor`

## File map

| File | Responsibility |
|---|---|
| `migration/add_commodity_research_20260727.sql` | Parameterized research-schema DDL |
| `migration/drop_commodity_research_20260727.sql` | Explicit rollback DDL |
| `commodity_fundamentals/db.py` | Safe identifiers, connections, catalog/formula loading |
| `commodity_fundamentals/ingest.py` | Ingest lifecycle and idempotent observation service |
| `commodity_fundamentals/uploader.py` | Recovery-package upload through Phase 5 client |
| `writer/backend/commodity_fundamentals_api.py` | Pydantic API models and handlers |
| `writer/backend/main.py` | Register the dedicated fundamentals endpoints |
| `commodity_fundamentals/availability.py` | Trading-calendar and PIT eligibility rules |
| `commodity_fundamentals/standardize.py` | Unit conversion, primary selection, spot/basis/inventory |
| `commodity_fundamentals/profit.py` | Versioned profit-formula validation and evaluation |
| `commodity_fundamentals/quality.py` | Data and coverage gates |
| `commodity_fundamentals/builder.py` | Reproducible build and atomic per-mode promotion |
| `commodity_fundamentals/__main__.py` | Load, build, validate, and publish CLI |
| `commodity_fundamentals/profit_formulas.v1.yaml` | Reviewed profit definitions |
| `tests/commodity_fundamentals/test_schema_sql.py` | Migration contract tests |
| `tests/commodity_fundamentals/test_ingest.py` | Run and observation idempotency tests |
| `tests/commodity_fundamentals/test_uploader.py` | Phase 5 upload/resume tests |
| `tests/commodity_fundamentals/test_availability.py` | PIT boundary tests |
| `tests/commodity_fundamentals/test_standardize.py` | Basis and inventory tests |
| `tests/commodity_fundamentals/test_profit.py` | Formula and component-availability tests |
| `tests/commodity_fundamentals/test_quality.py` | Coverage and anomaly gate tests |
| `tests/commodity_fundamentals/test_builder.py` | Atomic publication tests |
| `docs/operations/commodity-fundamentals-pipeline.md` | DDL, backfill, recovery, and daily operations |

### Task 1: Lock the live writer source and DDL safety state

**Files:**
- Compare: `/home/elfbob/market-monitor/writer/backend/main.py`
- Compare remotely: `/home/elfbob/market-monitor/local-server/backend/main.py`
- Read: `/home/elfbob/market-monitor/migration/SCHEMA_CHANGES.md`

- [ ] **Step 1: Re-read the DDL safety card**

Run:

```bash
sed -n '1,130p' /home/elfbob/market-monitor/migration/SCHEMA_CHANGES.md
```

Expected: the backup, sync-health, rollback, non-synchronized-object, and Pi5
rules are visible in the execution transcript.

- [ ] **Step 2: Compare the deployed Debian writer with Git**

Run:

```bash
scp elfbob@100.65.111.79:/home/elfbob/market-monitor/local-server/backend/main.py \
  /tmp/market-monitor-writer-main.live.py
diff -u \
  /home/elfbob/market-monitor/writer/backend/main.py \
  /tmp/market-monitor-writer-main.live.py
```

Expected: no output, or a reviewed bounded diff. Stop if the deployed file has
unrelated changes that are absent from Git.

- [ ] **Step 3: Check sync health and object classification read-only**

Run:

```bash
ssh elfbob@100.65.111.79 \
  "psql -d market_monitor -Atc \
  \"SELECT schema_name, table_name, last_run_status
    FROM sync_state
    WHERE schema_name='commodity_research'
    ORDER BY table_name\""
ssh elfbob@100.65.111.79 \
  "journalctl -u market-monitor-sync --since '1 hour ago' \
  | grep -iE 'error|err=[1-9]'"
```

Expected: the first command returns no rows and the second returns no current
sync errors. The pilot remains Debian-only.

### Task 2: Define the complete research schema

**Files:**
- Create: `/home/elfbob/market-monitor/migration/add_commodity_research_20260727.sql`
- Create: `/home/elfbob/market-monitor/migration/drop_commodity_research_20260727.sql`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_schema_sql.py`

- [ ] **Step 1: Write failing migration-contract tests**

Create `tests/commodity_fundamentals/test_schema_sql.py`:

```python
from pathlib import Path

DDL = Path("migration/add_commodity_research_20260727.sql")


def test_migration_contains_all_versioned_objects():
    sql = DDL.read_text()
    for name in (
        "product_mapping",
        "series_catalog",
        "ingest_run",
        "observation_vintage",
        "profit_formula",
        "fundamental_build",
        "fundamental_daily",
        "current_fundamental_daily",
    ):
        assert name in sql


def test_migration_keeps_catalog_and_formula_version_keys():
    sql = " ".join(DDL.read_text().split())
    assert "PRIMARY KEY (catalog_version, series_id)" in sql
    assert "PRIMARY KEY (formula_id, formula_version)" in sql
    assert "UNIQUE (run_id, catalog_version, series_id, observation_date)" in sql


def test_migration_has_one_current_build_per_pit_mode():
    sql = " ".join(DDL.read_text().split())
    assert "UNIQUE INDEX" in sql
    assert "fundamental_build (pit_mode)" in sql
    assert "WHERE is_current" in sql


def test_migration_does_not_alter_public_tables():
    sql = DDL.read_text().lower()
    forbidden = (
        "alter table public.spot_prices",
        "alter table public.inventory",
        "alter table public.commodities_spot_prices",
    )
    assert not any(item in sql for item in forbidden)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_schema_sql.py -q
```

Expected: failures because the migration file is absent.

- [ ] **Step 3: Write the parameterized migration**

Create `migration/add_commodity_research_20260727.sql`. It must accept a psql
identifier variable so the same DDL can be transaction-tested as
`commodity_research_test`:

```sql
\if :{?fundamental_schema}
\else
\set fundamental_schema commodity_research
\endif

BEGIN;

CREATE SCHEMA IF NOT EXISTS :"fundamental_schema";

CREATE TABLE IF NOT EXISTS :"fundamental_schema".product_mapping (
    product_code TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'CNY'),
    futures_quote_unit TEXT NOT NULL,
    active_from DATE NOT NULL,
    active_to DATE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".series_catalog (
    catalog_version TEXT NOT NULL,
    series_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source = 'wind'),
    source_code TEXT NOT NULL,
    source_name TEXT NOT NULL,
    product_code TEXT NOT NULL
        REFERENCES :"fundamental_schema".product_mapping(product_code),
    metric_role TEXT NOT NULL CHECK (
        metric_role IN (
            'spot', 'inventory', 'profit_direct',
            'profit_component', 'conversion_component'
        )
    ),
    frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    api_method TEXT NOT NULL CHECK (api_method IN ('edb', 'wsd')),
    field_name TEXT,
    wind_options TEXT NOT NULL DEFAULT '',
    source_unit TEXT NOT NULL,
    target_unit TEXT NOT NULL,
    currency TEXT NOT NULL,
    scale_multiplier NUMERIC NOT NULL,
    aggregation_rule TEXT NOT NULL,
    date_semantics TEXT NOT NULL,
    release_lag_rule JSONB NOT NULL,
    unknown_time_policy TEXT NOT NULL
        CHECK (unknown_time_policy = 'next_trading_close'),
    max_staleness_trading_days INTEGER NOT NULL
        CHECK (max_staleness_trading_days > 0),
    valid_from DATE NOT NULL,
    valid_to DATE,
    active BOOLEAN NOT NULL,
    config_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_version, series_id),
    CHECK (
        (api_method = 'edb' AND field_name IS NULL)
        OR (api_method = 'wsd' AND field_name IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".ingest_run (
    run_id UUID PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('backfill', 'incremental', 'audit')),
    catalog_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    requested_start DATE NOT NULL,
    requested_end DATE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'raw_complete', 'validated', 'failed')
    ),
    requested_rows BIGINT NOT NULL DEFAULT 0,
    received_rows BIGINT NOT NULL DEFAULT 0,
    rejected_rows BIGINT NOT NULL DEFAULT 0,
    stored_rows BIGINT NOT NULL DEFAULT 0,
    recovery_artifact_checksum TEXT,
    error_summary TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".observation_vintage (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL
        REFERENCES :"fundamental_schema".ingest_run(run_id),
    catalog_version TEXT NOT NULL,
    series_id TEXT NOT NULL,
    observation_date DATE NOT NULL,
    published_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    raw_value NUMERIC NOT NULL,
    source_unit TEXT NOT NULL,
    currency TEXT NOT NULL,
    vintage_quality TEXT NOT NULL CHECK (
        vintage_quality IN ('backfill_final', 'captured_live', 'legacy_unverified')
    ),
    payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (catalog_version, series_id)
        REFERENCES :"fundamental_schema".series_catalog(catalog_version, series_id),
    UNIQUE (run_id, catalog_version, series_id, observation_date)
);

CREATE INDEX IF NOT EXISTS observation_vintage_lookup
    ON :"fundamental_schema".observation_vintage
       (catalog_version, series_id, observation_date, available_at, recorded_at);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".profit_formula (
    formula_id TEXT NOT NULL,
    product_code TEXT NOT NULL
        REFERENCES :"fundamental_schema".product_mapping(product_code),
    formula_version TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    components JSONB NOT NULL,
    output_unit TEXT NOT NULL,
    fixed_cost NUMERIC NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (formula_id, formula_version)
);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".fundamental_build (
    build_version TEXT PRIMARY KEY,
    catalog_version TEXT NOT NULL,
    pit_mode TEXT NOT NULL CHECK (pit_mode IN ('conservative', 'strict')),
    source_recorded_cutoff TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'validated', 'published', 'failed')
    ),
    input_rows BIGINT NOT NULL DEFAULT 0,
    output_rows BIGINT NOT NULL DEFAULT 0,
    rejected_rows BIGINT NOT NULL DEFAULT 0,
    coverage_rows BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL,
    error_summary TEXT,
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS fundamental_build_current_mode
    ON :"fundamental_schema".fundamental_build (pit_mode)
    WHERE is_current;

CREATE TABLE IF NOT EXISTS :"fundamental_schema".fundamental_daily (
    build_version TEXT NOT NULL
        REFERENCES :"fundamental_schema".fundamental_build(build_version),
    trade_date DATE NOT NULL,
    product_code TEXT NOT NULL
        REFERENCES :"fundamental_schema".product_mapping(product_code),
    metric TEXT NOT NULL CHECK (
        metric IN ('spot', 'basis_rate', 'inventory', 'profit')
    ),
    value NUMERIC NOT NULL,
    source_observation_date DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    series_id TEXT,
    formula_id TEXT,
    catalog_version TEXT NOT NULL,
    vintage_quality TEXT NOT NULL,
    staleness_trading_days INTEGER NOT NULL CHECK (staleness_trading_days >= 0),
    lineage JSONB NOT NULL,
    lineage_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (build_version, trade_date, product_code, metric),
    CHECK ((series_id IS NULL) <> (formula_id IS NULL))
);

CREATE OR REPLACE VIEW :"fundamental_schema".current_fundamental_daily AS
SELECT d.*, b.pit_mode, b.source_recorded_cutoff
FROM :"fundamental_schema".fundamental_daily AS d
JOIN :"fundamental_schema".fundamental_build AS b
  ON b.build_version = d.build_version
WHERE b.status = 'published' AND b.is_current;

INSERT INTO :"fundamental_schema".product_mapping (
    product_code, product_name, exchange, currency,
    futures_quote_unit, active_from
) VALUES
    ('M',  '豆粕',     'DCE',  'CNY', '元/吨', '2016-01-01'),
    ('RB', '螺纹钢',   'SHFE', 'CNY', '元/吨', '2016-01-01'),
    ('CU', '铜',       'SHFE', 'CNY', '元/吨', '2016-01-01'),
    ('AL', '铝',       'SHFE', 'CNY', '元/吨', '2016-01-01'),
    ('TA', 'PTA',      'CZCE', 'CNY', '元/吨', '2016-01-01'),
    ('PP', '聚丙烯',   'DCE',  'CNY', '元/吨', '2016-01-01'),
    ('MA', '甲醇',     'CZCE', 'CNY', '元/吨', '2016-01-01'),
    ('BU', '石油沥青', 'SHFE', 'CNY', '元/吨', '2016-01-01'),
    ('RU', '天然橡胶', 'SHFE', 'CNY', '元/吨', '2016-01-01')
ON CONFLICT (product_code) DO NOTHING;

COMMIT;
```

- [ ] **Step 4: Add explicit rollback SQL**

Create `migration/drop_commodity_research_20260727.sql`:

```sql
\if :{?fundamental_schema}
\else
\set fundamental_schema commodity_research
\endif

BEGIN;
DROP SCHEMA IF EXISTS :"fundamental_schema" CASCADE;
COMMIT;
```

The runbook must mark this file destructive and require a fresh dump plus
explicit approval before use on `commodity_research`.

- [ ] **Step 5: Verify migration tests pass**

Run the command from Step 2.

Expected: `4 passed`.

- [ ] **Step 6: Transaction-test DDL under a disposable schema**

Run on Debian:

```bash
scp migration/add_commodity_research_20260727.sql \
  elfbob@100.65.111.79:/tmp/
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 \
   -v fundamental_schema=commodity_research_test \
   -d market_monitor \
   -f /tmp/add_commodity_research_20260727.sql"
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 \
   -v fundamental_schema=commodity_research_test \
   -d market_monitor \
   -c \"SELECT count(*) FROM commodity_research_test.product_mapping\""
```

Expected: count `9`.

Remove the disposable schema only after resolving it exactly:

```bash
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 -d market_monitor \
   -c 'DROP SCHEMA commodity_research_test CASCADE'"
```

Expected: `DROP SCHEMA`. This deletion targets only the disposable test
schema.

- [ ] **Step 7: Commit schema migration**

```bash
cd /home/elfbob/market-monitor
git add migration/add_commodity_research_20260727.sql \
  migration/drop_commodity_research_20260727.sql \
  tests/commodity_fundamentals/test_schema_sql.py
git commit -m "feat: define commodity fundamentals research schema"
```

### Task 3: Add idempotent ingest-domain services

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/ingest.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_ingest.py`

- [ ] **Step 1: Write failing service tests against a fake repository**

Create `tests/commodity_fundamentals/test_ingest.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from commodity_fundamentals.ingest import (
    FundamentalIngestService,
    ObservationInput,
    RunInput,
)


class FakeRepo:
    def __init__(self):
        self.runs = {}
        self.rows = {}

    def create_run(self, run):
        existing = self.runs.get(run.run_id)
        if existing and existing != run:
            raise ValueError("run_id metadata mismatch")
        self.runs[run.run_id] = run

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def put_observations(self, run_id, rows):
        for row in rows:
            key = (run_id, row.catalog_version, row.series_id, row.observation_date)
            existing = self.rows.get(key)
            if existing and existing.payload_hash != row.payload_hash:
                raise ValueError("idempotency payload mismatch")
            self.rows[key] = row
        return len(rows)

    def complete_run(self, run_id, expected_rows, checksum):
        actual = sum(key[0] == run_id for key in self.rows)
        if actual != expected_rows:
            raise ValueError(f"row count mismatch expected={expected_rows} actual={actual}")
        return actual


def _run():
    return RunInput(
        run_id="11111111-1111-1111-1111-111111111111",
        mode="backfill",
        catalog_version="v1",
        config_hash="abc",
        requested_start=date(2020, 1, 1),
        requested_end=date(2020, 12, 31),
        started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )


def _row(value="100"):
    return ObservationInput(
        catalog_version="v1",
        series_id="m.spot.primary",
        observation_date=date(2020, 1, 2),
        published_at=None,
        available_at=datetime(2020, 1, 3, 15, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        raw_value=Decimal(value),
        source_unit="元/吨",
        currency="CNY",
        vintage_quality="backfill_final",
        payload_hash=f"hash-{value}",
    )


def test_same_run_and_payload_are_idempotent():
    repo = FakeRepo()
    service = FundamentalIngestService(repo)
    service.create_run(_run())
    service.put_observations(_run().run_id, [_row()])
    service.put_observations(_run().run_id, [_row()])

    assert len(repo.rows) == 1


def test_same_key_with_different_payload_is_rejected():
    repo = FakeRepo()
    service = FundamentalIngestService(repo)
    service.create_run(_run())
    service.put_observations(_run().run_id, [_row()])

    with pytest.raises(ValueError, match="payload mismatch"):
        service.put_observations(_run().run_id, [_row("101")])


def test_run_completion_requires_exact_row_count():
    repo = FakeRepo()
    service = FundamentalIngestService(repo)
    service.create_run(_run())
    service.put_observations(_run().run_id, [_row()])

    with pytest.raises(ValueError, match="row count mismatch"):
        service.complete_run(_run().run_id, expected_rows=2, checksum="manifest")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_ingest.py -q
```

Expected: import failure for `ingest`.

- [ ] **Step 3: Implement typed inputs, protocol, and service**

Create `commodity_fundamentals/ingest.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID


RUN_MODES = frozenset({"backfill", "incremental", "audit"})
VINTAGE_QUALITIES = frozenset(
    {"backfill_final", "captured_live", "legacy_unverified"}
)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class RunInput:
    run_id: str
    mode: str
    catalog_version: str
    config_hash: str
    requested_start: date
    requested_end: date
    started_at: datetime

    def __post_init__(self) -> None:
        UUID(self.run_id)
        if self.mode not in RUN_MODES:
            raise ValueError(f"unsupported mode: {self.mode}")
        if not self.catalog_version or not self.config_hash:
            raise ValueError("catalog_version and config_hash are required")
        if self.requested_end < self.requested_start:
            raise ValueError("requested_end precedes requested_start")
        _require_aware(self.started_at, "started_at")


@dataclass(frozen=True)
class ObservationInput:
    catalog_version: str
    series_id: str
    observation_date: date
    published_at: datetime | None
    available_at: datetime
    recorded_at: datetime
    raw_value: Decimal
    source_unit: str
    currency: str
    vintage_quality: str
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.catalog_version or not self.series_id:
            raise ValueError("catalog_version and series_id are required")
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
        _require_aware(self.available_at, "available_at")
        _require_aware(self.recorded_at, "recorded_at")
        if not isinstance(self.raw_value, Decimal) or not self.raw_value.is_finite():
            raise ValueError("raw_value must be a finite Decimal")
        if not self.source_unit or not self.currency or not self.payload_hash:
            raise ValueError("source_unit, currency and payload_hash are required")
        if self.vintage_quality not in VINTAGE_QUALITIES:
            raise ValueError(
                f"unsupported vintage_quality: {self.vintage_quality}"
            )
        if (
            self.vintage_quality == "captured_live"
            and self.available_at < self.recorded_at
        ):
            raise ValueError(
                "captured_live available_at cannot precede recorded_at"
            )


class FundamentalRepository(Protocol):
    def create_run(self, run: RunInput) -> None:
        raise NotImplementedError

    def get_run(self, run_id: str) -> RunInput | None:
        raise NotImplementedError

    def put_observations(
        self,
        run_id: str,
        rows: list[ObservationInput],
    ) -> int:
        raise NotImplementedError

    def complete_run(
        self,
        run_id: str,
        expected_rows: int,
        checksum: str,
    ) -> int:
        raise NotImplementedError


class FundamentalIngestService:
    def __init__(self, repository: FundamentalRepository):
        self.repository = repository

    def create_run(self, run: RunInput) -> None:
        self.repository.create_run(run)

    def put_observations(self, run_id: str, rows: list[ObservationInput]) -> int:
        if not rows:
            raise ValueError("observation batch must not be empty")
        if len({row.catalog_version for row in rows}) != 1:
            raise ValueError("batch must contain one catalog_version")
        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id: {run_id}")
        expected_vintage = {
            "backfill": "backfill_final",
            "incremental": "captured_live",
            "audit": "legacy_unverified",
        }[run.mode]
        for row in rows:
            if row.catalog_version != run.catalog_version:
                raise ValueError("observation catalog_version differs from run")
            if not run.requested_start <= row.observation_date <= run.requested_end:
                raise ValueError(
                    "observation_date outside requested run bounds: "
                    f"{row.observation_date}"
                )
            if row.vintage_quality != expected_vintage:
                raise ValueError(
                    f"mode={run.mode} requires vintage_quality={expected_vintage}"
                )
        return self.repository.put_observations(run_id, rows)

    def complete_run(self, run_id: str, *, expected_rows: int, checksum: str) -> int:
        if expected_rows < 0 or not checksum:
            raise ValueError("expected_rows and checksum are required")
        return self.repository.complete_run(run_id, expected_rows, checksum)
```

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all ingest-service tests pass.

- [ ] **Step 5: Commit ingest services**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/ingest.py \
  tests/commodity_fundamentals/test_ingest.py
git commit -m "feat: add idempotent fundamental ingest service"
```

### Task 4: Implement the PostgreSQL repository and dedicated API

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/db.py`
- Create: `/home/elfbob/market-monitor/writer/backend/commodity_fundamentals_api.py`
- Modify: `/home/elfbob/market-monitor/writer/backend/main.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_api.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/commodity_fundamentals/test_api.py` using FastAPI `TestClient`
and an injected fake service:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commodity_fundamentals_api import create_router


class FakeService:
    def __init__(self):
        self.created = []
        self.batches = []
        self.completed = []

    def create_run(self, run):
        self.created.append(run)

    def put_observations(self, run_id, rows):
        self.batches.append((run_id, rows))
        return len(rows)

    def complete_run(self, run_id, *, expected_rows, checksum):
        self.completed.append((run_id, expected_rows, checksum))
        return expected_rows


def _client(service):
    app = FastAPI()
    app.include_router(
        create_router(
            service_dependency=lambda: service,
            auth_dependency=lambda: "ok",
        )
    )
    return TestClient(app)


def test_create_run_endpoint_is_typed():
    service = FakeService()
    response = _client(service).post(
        "/api/fundamentals/runs",
        json={
            "run_id": "11111111-1111-1111-1111-111111111111",
            "mode": "backfill",
            "catalog_version": "v1",
            "config_hash": "abc",
            "requested_start": "2020-01-01",
            "requested_end": "2020-12-31",
            "started_at": "2026-07-27T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert len(service.created) == 1


def test_empty_observation_batch_returns_422():
    response = _client(FakeService()).post(
        "/api/fundamentals/observations",
        json={
            "run_id": "11111111-1111-1111-1111-111111111111",
            "observations": [],
        },
    )

    assert response.status_code == 422
```

Insert `writer/backend` into `sys.path` in the test module before importing
`commodity_fundamentals_api`.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_api.py -q
```

Expected: import failure for `commodity_fundamentals_api`.

- [ ] **Step 3: Implement the PostgreSQL repository**

In `commodity_fundamentals/db.py`, implement:

```python
from __future__ import annotations

from psycopg2 import sql
from psycopg2.extras import execute_values

from commodity_fundamentals.ingest import RunInput


class PostgresFundamentalRepository:
    def __init__(self, connection, schema="commodity_research"):
        self.connection = connection
        self.schema = schema

    def _table(self, name: str):
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema),
            sql.Identifier(name),
        )

    def create_run(self, run):
        fields = (
            str(run.run_id),
            run.mode,
            run.catalog_version,
            run.config_hash,
            run.requested_start,
            run.requested_end,
            run.started_at,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        run_id, mode, catalog_version, config_hash,
                        requested_start, requested_end, started_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'running')
                    ON CONFLICT (run_id) DO NOTHING
                    """
                ).format(self._table("ingest_run")),
                fields,
            )
            cursor.execute(
                sql.SQL(
                    """
                    SELECT
                        run_id::text, mode, catalog_version, config_hash,
                        requested_start, requested_end, started_at
                    FROM {}
                    WHERE run_id = %s
                    """
                ).format(self._table("ingest_run")),
                (run.run_id,),
            )
            stored = cursor.fetchone()
        if stored != fields:
            raise ValueError("run_id metadata mismatch")

    def get_run(self, run_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    SELECT
                        run_id::text, mode, catalog_version, config_hash,
                        requested_start, requested_end, started_at
                    FROM {}
                    WHERE run_id = %s
                    """
                ).format(self._table("ingest_run")),
                (run_id,),
            )
            row = cursor.fetchone()
        return RunInput(*row) if row is not None else None

    def put_observations(self, run_id, rows):
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    SELECT status, catalog_version, requested_start, requested_end
                    FROM {}
                    WHERE run_id = %s
                    FOR UPDATE
                    """
                ).format(self._table("ingest_run")),
                (run_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise ValueError(f"unknown run_id: {run_id}")
            status, catalog_version, requested_start, requested_end = run
            if status not in {"running", "raw_complete"}:
                raise ValueError(f"run does not accept observations: {status}")
            if any(row.catalog_version != catalog_version for row in rows):
                raise ValueError("observation catalog_version differs from run")
            if any(
                not requested_start <= row.observation_date <= requested_end
                for row in rows
            ):
                raise ValueError("observation_date outside requested run bounds")

            series_ids = sorted({row.series_id for row in rows})
            cursor.execute(
                sql.SQL(
                    """
                    SELECT series_id, source_unit, currency
                    FROM {}
                    WHERE catalog_version = %s
                      AND series_id = ANY(%s)
                    """
                ).format(self._table("series_catalog")),
                (catalog_version, series_ids),
            )
            catalog_rows = {
                item[0]: (item[1], item[2])
                for item in cursor.fetchall()
            }
            missing = sorted(set(series_ids) - set(catalog_rows))
            if missing:
                raise ValueError(f"series absent from catalog: {missing}")
            for row in rows:
                expected_unit, expected_currency = catalog_rows[row.series_id]
                if (row.source_unit, row.currency) != (
                    expected_unit,
                    expected_currency,
                ):
                    raise ValueError(
                        f"catalog unit/currency mismatch: {row.series_id}"
                    )

            if status == "running":
                values = [
                    (
                        run_id,
                        row.catalog_version,
                        row.series_id,
                        row.observation_date,
                        row.published_at,
                        row.available_at,
                        row.recorded_at,
                        row.raw_value,
                        row.source_unit,
                        row.currency,
                        row.vintage_quality,
                        row.payload_hash,
                    )
                    for row in rows
                ]
                query = sql.SQL(
                    """
                    INSERT INTO {} (
                        run_id, catalog_version, series_id, observation_date,
                        published_at, available_at, recorded_at, raw_value,
                        source_unit, currency, vintage_quality, payload_hash
                    ) VALUES %s
                    ON CONFLICT (
                        run_id, catalog_version, series_id, observation_date
                    ) DO NOTHING
                    """
                ).format(self._table("observation_vintage"))
                execute_values(
                    cursor,
                    query.as_string(self.connection),
                    values,
                    page_size=1000,
                )

            for row in rows:
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT payload_hash
                        FROM {}
                        WHERE run_id = %s
                          AND catalog_version = %s
                          AND series_id = %s
                          AND observation_date = %s
                        """
                    ).format(self._table("observation_vintage")),
                    (
                        run_id,
                        row.catalog_version,
                        row.series_id,
                        row.observation_date,
                    ),
                )
                stored = cursor.fetchone()
                if stored is None or stored[0] != row.payload_hash:
                    raise ValueError(
                        "idempotency payload mismatch for "
                        f"{row.series_id}:{row.observation_date}"
                    )

            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE run_id = %s"
                ).format(self._table("observation_vintage")),
                (run_id,),
            )
            stored_rows = int(cursor.fetchone()[0])
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET stored_rows = %s,
                        received_rows = GREATEST(received_rows, %s),
                        updated_at = now()
                    WHERE run_id = %s
                    """
                ).format(self._table("ingest_run")),
                (stored_rows, stored_rows, run_id),
            )
        return len(rows)

    def complete_run(self, run_id, expected_rows, checksum):
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    SELECT status, recovery_artifact_checksum
                    FROM {}
                    WHERE run_id = %s
                    FOR UPDATE
                    """
                ).format(self._table("ingest_run")),
                (run_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise ValueError(f"unknown run_id: {run_id}")
            status, prior_checksum = run
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE run_id = %s"
                ).format(self._table("observation_vintage")),
                (run_id,),
            )
            actual = int(cursor.fetchone()[0])
            if actual != expected_rows:
                raise ValueError(
                    f"row count mismatch expected={expected_rows} actual={actual}"
                )
            if status == "raw_complete":
                if prior_checksum != checksum:
                    raise ValueError("completion checksum mismatch")
                return actual
            if status != "running":
                raise ValueError(f"run cannot complete from status={status}")
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET status = 'raw_complete',
                        requested_rows = %s,
                        received_rows = %s,
                        stored_rows = %s,
                        recovery_artifact_checksum = %s,
                        finished_at = now(),
                        updated_at = now()
                    WHERE run_id = %s
                    """
                ).format(self._table("ingest_run")),
                (expected_rows, actual, actual, checksum, run_id),
            )
        return actual
```

No method commits. The FastAPI connection context controls the transaction.

- [ ] **Step 4: Implement the API router**

In `writer/backend/commodity_fundamentals_api.py`, implement:

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from commodity_fundamentals.ingest import ObservationInput, RunInput


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunCreateRequest(StrictRequest):
    run_id: UUID
    mode: str
    catalog_version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    requested_start: date
    requested_end: date
    started_at: datetime

    def to_domain(self) -> RunInput:
        return RunInput(
            run_id=str(self.run_id),
            mode=self.mode,
            catalog_version=self.catalog_version,
            config_hash=self.config_hash,
            requested_start=self.requested_start,
            requested_end=self.requested_end,
            started_at=self.started_at,
        )


class ObservationRecord(StrictRequest):
    catalog_version: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    observation_date: date
    published_at: datetime | None = None
    available_at: datetime
    recorded_at: datetime
    raw_value: Decimal
    source_unit: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    vintage_quality: str
    payload_hash: str = Field(min_length=1)

    def to_domain(self) -> ObservationInput:
        return ObservationInput(**self.model_dump())


class ObservationBatchRequest(StrictRequest):
    run_id: UUID
    observations: list[ObservationRecord] = Field(
        min_length=1,
        max_length=1000,
    )


class RunCompleteRequest(StrictRequest):
    expected_rows: int = Field(ge=0)
    manifest_checksum: str = Field(min_length=1)


CONFLICT_MARKERS = (
    "metadata mismatch",
    "idempotency payload mismatch",
    "completion checksum mismatch",
    "refusing to overwrite",
)


def _domain_http_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    status = 409 if any(marker in detail for marker in CONFLICT_MARKERS) else 422
    return HTTPException(status_code=status, detail=detail)


def create_router(*, service_dependency, auth_dependency):
    router = APIRouter(tags=["Commodity Fundamentals"])

    @router.post("/api/fundamentals/runs")
    def create_run(
        request: RunCreateRequest,
        service=Depends(service_dependency),
        _=Depends(auth_dependency),
    ):
        try:
            service.create_run(request.to_domain())
        except ValueError as exc:
            raise _domain_http_error(exc) from exc
        return {"status": "running", "run_id": str(request.run_id)}

    @router.post("/api/fundamentals/observations")
    def put_observations(
        request: ObservationBatchRequest,
        service=Depends(service_dependency),
        _=Depends(auth_dependency),
    ):
        try:
            stored = service.put_observations(
                str(request.run_id),
                [row.to_domain() for row in request.observations],
            )
        except ValueError as exc:
            raise _domain_http_error(exc) from exc
        return {"status": "accepted", "stored": stored}

    @router.post("/api/fundamentals/runs/{run_id}/raw-complete")
    def complete_run(
        run_id: UUID,
        request: RunCompleteRequest,
        service=Depends(service_dependency),
        _=Depends(auth_dependency),
    ):
        try:
            stored = service.complete_run(
                str(run_id),
                expected_rows=request.expected_rows,
                checksum=request.manifest_checksum,
            )
        except ValueError as exc:
            raise _domain_http_error(exc) from exc
        return {"status": "raw_complete", "stored": stored}

    return router
```

Unexpected database exceptions are not caught, so FastAPI returns HTTP 500
and the request-scoped database context rolls back.

- [ ] **Step 5: Register the router in canonical `writer/backend/main.py`**

Add imports for `FundamentalIngestService`,
`PostgresFundamentalRepository`, and `create_router`, then:

```python
def get_fundamental_service():
    with get_db_connection() as conn:
        yield FundamentalIngestService(
            PostgresFundamentalRepository(
                conn,
                schema="commodity_research",
            )
        )
```

Register the request-scoped dependency:

```python
app.include_router(
    create_router(
        service_dependency=get_fundamental_service,
        auth_dependency=verify_api_key,
    )
)
```

The `with` block makes one request use one database transaction. A normal
return commits through the existing context manager; an exception rolls back;
the connection is always closed.

- [ ] **Step 6: Verify API tests**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=.:writer/backend \
  /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_ingest.py \
  tests/commodity_fundamentals/test_api.py -q
```

Expected: all ingest and API tests pass.

- [ ] **Step 7: Commit API support**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/db.py \
  writer/backend/commodity_fundamentals_api.py \
  writer/backend/main.py \
  tests/commodity_fundamentals/test_api.py
git commit -m "feat: accept transactional fundamental observation batches"
```

### Task 5: Upload recovery packages through the Phase 5 client

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/uploader.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_uploader.py`
- Modify: `/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-wind.md`

- [ ] **Step 1: Write failing uploader tests**

Create `tests/commodity_fundamentals/test_uploader.py`:

```python
from types import SimpleNamespace

from commodity_fundamentals.uploader import upload_package


class FakePackage:
    run_payload = {"run_id": "run-1"}
    completion_payload = {"expected_rows": 2, "manifest_checksum": "abc"}

    def __init__(self):
        self.acknowledgements = []

    def observation_batches(self, size):
        assert size == 500
        yield [{"series_id": "a"}]
        yield [{"series_id": "b"}]

    def write_acknowledgements(self, rows):
        self.acknowledgements = list(rows)


def test_upload_disables_fallback_and_preserves_order():
    calls = []

    def post(url, fallback_url=None, **kwargs):
        calls.append((url, fallback_url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"status": "ok"},
            raise_for_status=lambda: None,
        )

    package = FakePackage()
    upload_package(
        package,
        primary_base="http://100.65.111.79:8000",
        fallback_base="http://100.75.102.44:8000",
        token="unit-test-token",
        post=post,
        batch_size=500,
    )

    assert [call[0].split("/")[-1] for call in calls] == [
        "runs",
        "observations",
        "observations",
        "raw-complete",
    ]
    assert all(call[2]["fallback_enabled"] is False for call in calls)
    assert len(package.acknowledgements) == 4
    assert all(
        call[2]["headers"] == {"Authorization": "Bearer unit-test-token"}
        for call in calls
    )
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_uploader.py -q
```

Expected: import failure for `uploader`.

- [ ] **Step 3: Implement ordered idempotent upload**

Create `commodity_fundamentals/uploader.py`:

```python
from __future__ import annotations

import argparse
from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path

import yaml

from commodity_fundamentals.recovery import RecoveryPackage


UPLOAD_ROW_FIELDS = {
    "series_id",
    "observation_date",
    "published_at",
    "available_at",
    "recorded_at",
    "raw_value",
    "source_unit",
    "currency",
    "vintage_quality",
}


class UploadPackage:
    def __init__(self, recovery: RecoveryPackage):
        self.recovery = recovery

    @classmethod
    def open(cls, root: str | Path) -> "UploadPackage":
        package = cls(RecoveryPackage.open(root))
        package._validate_rows()
        return package

    @property
    def run_payload(self) -> dict:
        manifest = self.recovery.manifest
        return {
            "run_id": manifest["run_id"],
            "mode": manifest["mode"],
            "catalog_version": manifest["catalog_version"],
            "config_hash": manifest["config_hash"],
            "requested_start": manifest["requested_start"],
            "requested_end": manifest["requested_end"],
            "started_at": manifest["created_at"],
        }

    @property
    def completion_payload(self) -> dict:
        expected = sum(
            int(entry["row_count"])
            for entry in self.recovery.manifest["chunks"].values()
        )
        manifest_bytes = (
            self.recovery.root / "manifest.json"
        ).read_bytes()
        return {
            "expected_rows": expected,
            "manifest_checksum": sha256(manifest_bytes).hexdigest(),
        }

    def observation_batches(self, size: int):
        if size < 1 or size > 1000:
            raise ValueError("batch size must be in [1, 1000]")
        batch = []
        for row in self._iter_rows():
            batch.append(self._api_row(row))
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch

    def write_acknowledgements(self, rows: list[dict]) -> None:
        final_path = self.recovery.root / "upload-acknowledgements.json"
        partial_path = final_path.with_name(final_path.name + ".partial")
        with partial_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(rows, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        partial_path.replace(final_path)

    def _validate_rows(self) -> None:
        if not self.recovery.trading_dates:
            raise ValueError("recovery package has no trading calendar")
        for row in self._iter_rows():
            missing = UPLOAD_ROW_FIELDS - set(row)
            if missing:
                raise ValueError(f"recovery row missing fields: {sorted(missing)}")
            null_required = sorted(
                field
                for field in (
                    "available_at",
                    "source_unit",
                    "currency",
                    "vintage_quality",
                )
                if row[field] is None
            )
            if null_required:
                raise ValueError(
                    f"recovery row has null PIT fields: {null_required}"
                )
            for timestamp_field in ("available_at", "recorded_at"):
                timestamp = datetime.fromisoformat(row[timestamp_field])
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError(
                        f"{timestamp_field} must be timezone-aware"
                    )

    def _iter_rows(self):
        for key in sorted(self.recovery.manifest["chunks"]):
            series_id, year_text = key.rsplit(":", 1)
            year = int(year_text)
            if not self.recovery.is_complete(series_id, year):
                raise ValueError(f"incomplete recovery chunk: {key}")
            path = self.recovery.root / self.recovery.manifest["chunks"][key]["path"]
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"invalid JSON row {path.name}:{line_number}"
                        ) from exc

    def _api_row(self, row: dict) -> dict:
        payload = {
            "catalog_version": self.recovery.manifest["catalog_version"],
            **{field: row[field] for field in sorted(UPLOAD_ROW_FIELDS)},
        }
        digest = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {**payload, "payload_hash": digest}


def upload_package(
    package,
    *,
    primary_base,
    fallback_base,
    token,
    post,
    batch_size,
):
    if not token:
        raise ValueError("writer API token is required")
    primary_base = primary_base.rstrip("/")
    fallback_base = fallback_base.rstrip("/") if fallback_base else None
    headers = {"Authorization": f"Bearer {token}"}
    acknowledgements = []

    def send(path: str, payload: dict) -> None:
        response = post(
            primary_base + path,
            fallback_url=(fallback_base + path) if fallback_base else None,
            json=payload,
            headers=headers,
            fallback_enabled=False,
        )
        response.raise_for_status()
        acknowledgements.append(
            {
                "path": path,
                "status_code": int(response.status_code),
                "response": response.json(),
            }
        )
        package.write_acknowledgements(acknowledgements)

    send("/api/fundamentals/runs", package.run_payload)
    for observations in package.observation_batches(batch_size):
        send(
            "/api/fundamentals/observations",
            {
                "run_id": package.run_payload["run_id"],
                "observations": observations,
            },
        )
    send(
        "/api/fundamentals/runs/"
        + package.run_payload["run_id"]
        + "/raw-complete",
        package.completion_payload,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m commodity_fundamentals.uploader"
    )
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    from http_client import post_with_fallback, resolve_endpoints
    from watchlist_loader import resolve_env_placeholders

    config = resolve_env_placeholders(
        yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    )
    main_server = config.get("main_server") or {}
    primary, fallback, _configured_fallback = resolve_endpoints(
        main_server,
        config.get("server_endpoints"),
    )
    token = str(main_server.get("api_key") or "")
    package = UploadPackage.open(args.package)
    upload_package(
        package,
        primary_base=primary,
        fallback_base=fallback,
        token=token,
        post=post_with_fallback,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
```

The CLI imports the reviewed Phase 5 client and always passes
`fallback_enabled=False`; Pi5 does not receive this pilot schema. The token is
used only in the Authorization header and never written to acknowledgements.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: uploader tests pass.

- [ ] **Step 5: Update the Windows upload command**

Add to the runbook:

```powershell
$env:PYTHONPATH = "D:\marketmonitor\realtime_upgrade;D:\marketmonitor\fundamentals"
$package = Read-Host "Exact recovery package directory"
if (-not (Test-Path (Join-Path $package "manifest.json"))) {
    throw "manifest.json not found in selected recovery package"
}
python -m commodity_fundamentals.uploader `
  --package $package `
  --config D:\marketmonitor\realtime_upgrade\config.yaml `
  --batch-size 500
```

The uploader verifies that the manifest `run_id` equals the directory's final
name before sending data. It never scans and uploads every artifact
automatically.

- [ ] **Step 6: Commit uploader support**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/uploader.py \
  tests/commodity_fundamentals/test_uploader.py \
  docs/operations/commodity-fundamentals-wind.md
git commit -m "feat: upload Wind recovery packages transactionally"
```

### Task 6: Enforce PIT availability and vintage selection

**Files:**
- Modify: `/home/elfbob/market-monitor/commodity_fundamentals/availability.py`
- Modify: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_availability.py`

- [ ] **Step 1: Write failing PIT tests**

Extend `tests/commodity_fundamentals/test_availability.py` with the
captured-live and selection cases below. Keep the unknown-release-time test
from Plan 1:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from commodity_fundamentals.availability import (
    effective_available_at,
    select_vintages,
)

CN = ZoneInfo("Asia/Shanghai")


def test_unknown_time_uses_next_real_trading_close():
    trading_dates = [
        date(2026, 9, 30),
        date(2026, 10, 9),
        date(2026, 10, 12),
    ]

    available = effective_available_at(
        observation_date=date(2026, 9, 30),
        published_at=None,
        recorded_at=datetime(2026, 10, 20, 10, tzinfo=CN),
        lag_rule={"calendar_days": 0, "time": None},
        unknown_time_policy="next_trading_close",
        vintage_quality="backfill_final",
        trading_dates=trading_dates,
    )

    assert available == datetime(2026, 10, 9, 15, tzinfo=CN)


def test_captured_live_cannot_backdate_before_recorded_at():
    available = effective_available_at(
        observation_date=date(2026, 7, 20),
        published_at=None,
        recorded_at=datetime(2026, 7, 27, 18, tzinfo=CN),
        lag_rule={"calendar_days": 0, "time": "15:00"},
        unknown_time_policy="next_trading_close",
        vintage_quality="captured_live",
        trading_dates=[date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 27)],
    )

    assert available == datetime(2026, 7, 27, 18, tzinfo=CN)


def test_strict_mode_excludes_backfill_final():
    rows = pd.DataFrame(
        {
            "series_id": ["x", "x"],
            "observation_date": [date(2020, 1, 1), date(2020, 1, 1)],
            "available_at": [
                datetime(2020, 1, 2, 15, tzinfo=CN),
                datetime(2026, 7, 27, 18, tzinfo=CN),
            ],
            "recorded_at": [
                datetime(2026, 7, 1, 18, tzinfo=CN),
                datetime(2026, 7, 27, 18, tzinfo=CN),
            ],
            "vintage_quality": ["backfill_final", "captured_live"],
            "raw_value": [100.0, 101.0],
        }
    )

    selected = select_vintages(
        rows,
        pit_mode="strict",
        signal_cutoff=datetime(2026, 7, 28, 15, tzinfo=CN),
    )

    assert selected.iloc[0]["raw_value"] == 101.0
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_availability.py -q
```

Expected: collection fails because `select_vintages` is not yet defined.

- [ ] **Step 3: Implement availability and selection**

Add the pandas import at the top of the existing module and append
`select_vintages`:

```python
import pandas as pd

def select_vintages(rows, *, pit_mode, signal_cutoff):
    signal_cutoff = _require_aware(signal_cutoff, "signal_cutoff")
    if rows.empty:
        return rows.copy()
    required = {
        "series_id",
        "observation_date",
        "available_at",
        "recorded_at",
        "vintage_quality",
    }
    missing = required - set(rows.columns)
    if missing:
        raise ValueError(f"vintage rows missing columns: {sorted(missing)}")
    eligible = rows[rows["available_at"] <= signal_cutoff].copy()
    if pit_mode == "strict":
        eligible = eligible[eligible["vintage_quality"] == "captured_live"]
        eligible = eligible[eligible["recorded_at"] <= signal_cutoff]
    elif pit_mode != "conservative":
        raise ValueError(f"unsupported pit_mode: {pit_mode}")
    return (
        eligible.sort_values(["series_id", "observation_date", "recorded_at"])
        .drop_duplicates(["series_id", "observation_date"], keep="last")
    )
```

Use the database's eligible `continuous_contract_ohlc` dates; never generate a
weekday-only calendar.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all PIT boundary tests pass.

- [ ] **Step 5: Commit PIT selection**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/availability.py \
  tests/commodity_fundamentals/test_availability.py
git commit -m "feat: enforce fundamental availability vintages"
```

### Task 7: Standardize spot, raw-price basis, and inventory

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/standardize.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_standardize.py`

- [ ] **Step 1: Write failing basis and inventory tests**

Create tests that lock these behaviors:

```python
from datetime import date

import pandas as pd
import pytest

from commodity_fundamentals.standardize import (
    aggregate_inventory,
    basis_available_at,
    build_basis,
    limited_forward_fill,
    regime_blackout_mask,
    select_primary_series,
)


def test_basis_uses_raw_contract_close_not_adjusted_close():
    spot = pd.Series([110.0], index=[date(2020, 1, 2)])
    futures = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "close_raw": [100.0],
            "close_fa": [250.0],
            "close_ba": [40.0],
        }
    ).set_index("trade_date")

    out = build_basis(spot, futures)

    assert out.iloc[0] == pytest.approx(0.10)


def test_basis_availability_is_latest_component():
    out = basis_available_at(
        spot_available_at=pd.Timestamp("2020-01-02 16:00", tz="Asia/Shanghai"),
        futures_close_at=pd.Timestamp("2020-01-02 15:00", tz="Asia/Shanghai"),
    )

    assert out == pd.Timestamp("2020-01-02 16:00", tz="Asia/Shanghai")


def test_inventory_rejects_heterogeneous_sum():
    components = pd.DataFrame(
        {
            "series_id": ["exchange", "social"],
            "source_unit": ["吨", "万吨"],
            "aggregation_rule": ["sum:total", "sum:total"],
            "value": [100.0, 2.0],
        }
    )

    with pytest.raises(ValueError, match="unit"):
        aggregate_inventory(components)


def test_forward_fill_stops_after_staleness_limit():
    values = pd.Series(
        [100.0],
        index=pd.Index([date(2020, 1, 2)], name="trade_date"),
    )
    calendar = [
        date(2020, 1, 2),
        date(2020, 1, 3),
        date(2020, 1, 6),
        date(2020, 1, 7),
    ]

    out = limited_forward_fill(values, calendar, max_staleness=2)

    assert out.loc[date(2020, 1, 6)] == 100.0
    assert pd.isna(out.loc[date(2020, 1, 7)])


def test_primary_selection_rejects_overlapping_active_series():
    rows = pd.DataFrame(
        {
            "series_id": ["m.inventory.north", "m.inventory.south"],
            "aggregation_rule": ["primary", "primary"],
            "valid_from": [date(2016, 1, 1), date(2019, 1, 1)],
            "valid_to": [None, None],
            "value": [100.0, 200.0],
        }
    )

    with pytest.raises(ValueError, match="exactly one active primary"):
        select_primary_series(rows, as_of=date(2020, 1, 2))


def test_source_regime_change_blacks_out_twenty_trading_days():
    calendar = pd.bdate_range("2020-01-01", periods=25).date.tolist()

    mask = regime_blackout_mask(
        calendar,
        regime_starts=[date(2020, 1, 1)],
        blackout_trading_days=20,
    )

    assert mask.iloc[:20].all()
    assert not mask.iloc[20:].any()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_standardize.py -q
```

Expected: import failure for `standardize`.

- [ ] **Step 3: Implement the pure standardizers**

Implement `commodity_fundamentals/standardize.py`:

```python
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def normalize_value(raw_value, *, multiplier, source_unit, target_unit):
    if source_unit != target_unit and multiplier == 1:
        raise ValueError("unit conversion requires an explicit multiplier")
    value = float(raw_value) * float(multiplier)
    if not np.isfinite(value):
        raise ValueError("normalized value must be finite")
    return value


def build_basis(spot, futures):
    raw = pd.to_numeric(futures["close_raw"], errors="coerce")
    return spot.reindex(raw.index) / raw.where(raw > 0) - 1.0


def basis_available_at(*, spot_available_at, futures_close_at):
    return max(spot_available_at, futures_close_at)


def aggregate_inventory(components):
    units = components["source_unit"].dropna().unique()
    groups = components["aggregation_rule"].dropna().unique()
    if len(units) != 1:
        raise ValueError("inventory sum requires one unit")
    if len(groups) != 1 or not str(groups[0]).startswith("sum:"):
        raise ValueError("inventory components need one approved sum group")
    return float(components["value"].sum())


def limited_forward_fill(values, calendar, *, max_staleness):
    out = values.reindex(calendar).ffill()
    source_pos = pd.Series(
        np.where(values.reindex(calendar).notna(), np.arange(len(calendar)), np.nan),
        index=calendar,
    ).ffill()
    age = pd.Series(np.arange(len(calendar)), index=calendar) - source_pos
    return out.where(age <= max_staleness)


def select_primary_series(rows: pd.DataFrame, *, as_of: date) -> pd.Series:
    required = {
        "series_id",
        "aggregation_rule",
        "valid_from",
        "valid_to",
    }
    missing = required - set(rows.columns)
    if missing:
        raise ValueError(f"series rows missing columns: {sorted(missing)}")
    valid_to_ok = rows["valid_to"].map(
        lambda value: pd.isna(value) or value >= as_of
    )
    active = rows[
        (rows["aggregation_rule"] == "primary")
        & (rows["valid_from"] <= as_of)
        & valid_to_ok
    ]
    if len(active) != 1:
        raise ValueError(
            "expected exactly one active primary series "
            f"as_of={as_of} found={len(active)}"
        )
    return active.iloc[0]


def regime_blackout_mask(
    calendar: list[date],
    *,
    regime_starts: list[date],
    blackout_trading_days: int = 20,
) -> pd.Series:
    if blackout_trading_days < 0:
        raise ValueError("blackout_trading_days must be non-negative")
    index = pd.Index(calendar, name="trade_date")
    mask = pd.Series(False, index=index)
    for boundary in sorted(set(regime_starts)):
        eligible = [value for value in calendar if value >= boundary]
        for value in eligible[:blackout_trading_days]:
            mask.loc[value] = True
    return mask
```

Every inventory catalog regime uses either one `primary` series or one
explicit group such as `sum:warehouse_receipts`, handled by
`aggregate_inventory`; two overlapping
`primary` rows fail rather than being averaged. `valid_from`/`valid_to` choose
the regime, and the returned blackout mask suppresses the first twenty
trading dates after every regime start.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all spot/basis/inventory tests pass.

- [ ] **Step 5: Commit standardization**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/standardize.py \
  tests/commodity_fundamentals/test_standardize.py
git commit -m "feat: standardize basis and inventory inputs"
```

### Task 8: Validate and evaluate versioned profit formulas

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/profit.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_profit.py`
- Create after catalog review: `/home/elfbob/market-monitor/commodity_fundamentals/profit_formulas.v1.yaml`

- [ ] **Step 1: Write failing formula tests**

Create `tests/commodity_fundamentals/test_profit.py`:

```python
from datetime import date

import pandas as pd
import pytest

from commodity_fundamentals.profit import Formula, evaluate_formula


def test_profit_waits_for_latest_component_availability():
    formula = Formula.from_mapping(
        {
            "formula_id": "m.crush",
            "formula_version": "v1",
            "product_code": "M",
            "effective_from": "2016-01-01",
            "effective_to": None,
            "output_unit": "元/吨",
            "fixed_cost": "50",
            "components": [
                {"series_id": "m.meal", "coefficient": "0.8"},
                {"series_id": "m.oil", "coefficient": "0.18"},
                {"series_id": "m.soybean", "coefficient": "-1"},
            ],
        }
    )
    values = {
        "m.meal": (3000.0, pd.Timestamp("2020-01-02 15:00", tz="Asia/Shanghai")),
        "m.oil": (6000.0, pd.Timestamp("2020-01-03 15:00", tz="Asia/Shanghai")),
        "m.soybean": (3300.0, pd.Timestamp("2020-01-02 15:00", tz="Asia/Shanghai")),
    }

    value, available_at = evaluate_formula(formula, values)

    assert value == pytest.approx(0.8 * 3000 + 0.18 * 6000 - 3300 - 50)
    assert available_at == pd.Timestamp("2020-01-03 15:00", tz="Asia/Shanghai")


def test_missing_component_returns_no_result():
    formula = Formula.from_mapping(
        {
            "formula_id": "ta.processing",
            "formula_version": "v1",
            "product_code": "TA",
            "effective_from": "2016-01-01",
            "effective_to": None,
            "output_unit": "元/吨",
            "fixed_cost": "0",
            "components": [
                {"series_id": "ta.output", "coefficient": "1"},
                {"series_id": "ta.px", "coefficient": "-0.655"},
            ],
        }
    )

    assert evaluate_formula(formula, {"ta.output": (5000.0, pd.Timestamp.now(tz="UTC"))}) is None
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_profit.py -q
```

Expected: import failure for `profit`.

- [ ] **Step 3: Implement formula validation and evaluation**

Create `commodity_fundamentals/profit.py`:

```python
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import yaml

from commodity_fundamentals.catalog import load_catalog
from commodity_fundamentals.models import PRODUCTS


@dataclass(frozen=True)
class FormulaComponent:
    series_id: str
    coefficient: Decimal


@dataclass(frozen=True)
class Formula:
    formula_id: str
    formula_version: str
    product_code: str
    effective_from: date
    effective_to: date | None
    output_unit: str
    fixed_cost: Decimal
    components: tuple[FormulaComponent, ...]

    @classmethod
    def from_mapping(cls, row: dict) -> "Formula":
        product = str(row["product_code"]).strip().upper()
        if product not in PRODUCTS:
            raise ValueError(f"unsupported product_code: {product}")
        component_rows = row.get("components")
        if not isinstance(component_rows, list) or not component_rows:
            raise ValueError("formula components must be non-empty")
        components = tuple(
            FormulaComponent(
                series_id=str(item["series_id"]).strip(),
                coefficient=Decimal(str(item["coefficient"])),
            )
            for item in component_rows
        )
        ids = [item.series_id for item in components]
        if len(ids) != len(set(ids)):
            raise ValueError("formula contains duplicate component series_id")
        if any(
            not item.series_id or not item.coefficient.is_finite()
            for item in components
        ):
            raise ValueError("formula component must be finite and identified")
        fixed_cost = Decimal(str(row.get("fixed_cost", "0")))
        if not fixed_cost.is_finite():
            raise ValueError("formula fixed_cost must be finite")
        effective_from = date.fromisoformat(str(row["effective_from"]))
        effective_to = (
            date.fromisoformat(str(row["effective_to"]))
            if row.get("effective_to")
            else None
        )
        if effective_to is not None and effective_to < effective_from:
            raise ValueError("formula effective_to precedes effective_from")
        output_unit = str(row["output_unit"]).strip()
        if not output_unit:
            raise ValueError("formula output_unit is required")
        return cls(
            formula_id=str(row["formula_id"]).strip(),
            formula_version=str(row["formula_version"]).strip(),
            product_code=product,
            effective_from=effective_from,
            effective_to=effective_to,
            output_unit=output_unit,
            fixed_cost=fixed_cost,
            components=components,
        )


def evaluate_formula(formula: Formula, values: dict):
    if any(component.series_id not in values for component in formula.components):
        return None
    total = -formula.fixed_cost
    available_at = []
    for component in formula.components:
        value, timestamp = values[component.series_id]
        numeric = Decimal(str(value))
        if not numeric.is_finite():
            raise ValueError("profit component value must be finite")
        total += component.coefficient * numeric
        available_at.append(timestamp)
    result = float(total)
    if not np.isfinite(result):
        raise ValueError("profit formula result must be finite")
    return result, max(available_at)


def validate_formula_set(formulas, catalog) -> None:
    catalog_by_id = {spec.series_id: spec for spec in catalog.series}
    seen_keys = set()
    by_product = {}
    for formula in formulas:
        key = (formula.formula_id, formula.formula_version)
        if key in seen_keys:
            raise ValueError(f"duplicate formula key: {key}")
        seen_keys.add(key)
        by_product.setdefault(formula.product_code, []).append(formula)
        for component in formula.components:
            spec = catalog_by_id.get(component.series_id)
            if spec is None:
                raise ValueError(
                    f"formula component absent from catalog: {component.series_id}"
                )
            if spec.currency != "CNY":
                raise ValueError(
                    f"formula component currency is not CNY: {component.series_id}"
                )
            if spec.target_unit != formula.output_unit:
                raise ValueError(
                    "formula component requires an explicit catalog conversion: "
                    f"{component.series_id} {spec.target_unit} -> "
                    f"{formula.output_unit}"
                )
    for product, product_formulas in by_product.items():
        ordered = sorted(product_formulas, key=lambda item: item.effective_from)
        for left, right in zip(ordered, ordered[1:]):
            if left.effective_to is None or left.effective_to >= right.effective_from:
                raise ValueError(f"overlapping formula ranges for {product}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m commodity_fundamentals.profit"
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--formulas", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true", required=True)
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    payload = yaml.safe_load(args.formulas.read_text(encoding="utf-8")) or {}
    formulas = tuple(
        Formula.from_mapping(row) for row in payload.get("formulas", [])
    )
    validate_formula_set(formulas, catalog)
    direct_products = {
        spec.product_code
        for spec in catalog.active_series()
        if spec.metric_role == "profit_direct"
    }
    formula_products = {formula.product_code for formula in formulas}
    covered = sorted(direct_products | formula_products)
    print(f"profit-covered products={len(covered)} symbols={','.join(covered)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create reviewed formula version 1**

Using the approved Plan 1 catalog, create formulas only for products lacking a
stable direct Wind profit series. Each formula must identify source series,
coefficients, fixed cost, output unit, and effective interval. The file passes:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m commodity_fundamentals.profit \
  --catalog commodity_fundamentals/catalog.v1.yaml \
  --formulas commodity_fundamentals/profit_formulas.v1.yaml \
  --validate-only
```

Expected: exit 0 and a summary showing at least seven products with either a
direct reviewed profit series or a valid formula. `RU` remains absent when no
defensible series passes review.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_profit.py -q
```

Expected: all formula tests pass.

- [ ] **Step 6: Commit formula support**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/profit.py \
  commodity_fundamentals/profit_formulas.v1.yaml \
  tests/commodity_fundamentals/test_profit.py
git commit -m "feat: add versioned commodity profit formulas"
```

### Task 9: Add quality and coverage gates

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/quality.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_quality.py`

- [ ] **Step 1: Write failing gate tests**

Create tests for:

```python
from datetime import date

import pandas as pd

from commodity_fundamentals.quality import (
    daily_coverage_failures,
    inventory_side_failure,
    target_coverage_failures,
)


def test_target_catalog_coverage_is_9_8_7():
    report = pd.DataFrame(
        {
            "metric": ["basis_rate"] * 9 + ["inventory"] * 8 + ["profit"] * 7,
            "product_code": (
                ["M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU"]
                + ["M", "RB", "CU", "AL", "TA", "PP", "MA", "BU"]
                + ["M", "RB", "CU", "AL", "TA", "PP", "MA"]
            ),
            "eligible": True,
        }
    )

    assert target_coverage_failures(report) == []


def test_daily_hard_gate_requires_six_products():
    report = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)] * 5,
            "metric": ["inventory"] * 5,
            "product_code": ["M", "RB", "CU", "AL", "TA"],
            "eligible": True,
        }
    )

    assert daily_coverage_failures(report) == [
        "2020-01-02 inventory coverage=5 required=6"
    ]


def test_inventory_cross_section_requires_two_each_side():
    scores = pd.Series({"M": 1.0, "RB": 0.5, "CU": 0.1, "AL": -0.1})

    assert inventory_side_failure(scores) == (
        "inventory needs at least 2 long and 2 short candidates"
    )
```

- [ ] **Step 2: Verify RED**

Run the focused quality test and expect import failure.

- [ ] **Step 3: Implement gate functions and reason codes**

Create `commodity_fundamentals/quality.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd


TARGET_COVERAGE = {"basis_rate": 9, "inventory": 8, "profit": 7}
DAILY_REQUIRED = 6


class QualityGateError(ValueError):
    """Candidate build violates a formal publication gate."""


def target_coverage_failures(report: pd.DataFrame) -> list[str]:
    eligible = report[report["eligible"].fillna(False)].copy()
    failures = []
    for metric, required in TARGET_COVERAGE.items():
        count = int(
            eligible.loc[
                eligible["metric"] == metric,
                "product_code",
            ].nunique()
        )
        if count < required:
            failures.append(
                f"target {metric} coverage={count} required={required}"
            )
    return failures


def daily_coverage_failures(report: pd.DataFrame) -> list[str]:
    eligible = report[report["eligible"].fillna(False)].copy()
    failures = []
    keys = report[["trade_date", "metric"]].drop_duplicates()
    for row in keys.sort_values(["trade_date", "metric"]).itertuples(index=False):
        count = int(
            eligible.loc[
                (eligible["trade_date"] == row.trade_date)
                & (eligible["metric"] == row.metric),
                "product_code",
            ].nunique()
        )
        if count < DAILY_REQUIRED:
            failures.append(
                f"{row.trade_date} {row.metric} "
                f"coverage={count} required={DAILY_REQUIRED}"
            )
    return failures


def inventory_side_failure(scores: pd.Series) -> str | None:
    finite = pd.to_numeric(scores, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    long_count = int((finite > 0).sum())
    short_count = int((finite < 0).sum())
    if long_count < 2 or short_count < 2:
        return "inventory needs at least 2 long and 2 short candidates"
    return None


def row_quality_failures(rows: pd.DataFrame) -> list[str]:
    failures = []
    sort_columns = [
        column for column in ("series_id", "trade_date") if column in rows
    ]
    ordered = rows.sort_values(sort_columns) if sort_columns else rows.copy()
    for row in ordered.to_dict("records"):
        identity = (
            f"{row.get('trade_date')}:{row.get('product_code')}:"
            f"{row.get('metric')}"
        )
        value = float(row.get("value", np.nan))
        if not np.isfinite(value):
            failures.append(f"nonfinite:{identity}")
            continue
        if row.get("metric") == "spot" and value <= 0:
            failures.append(f"nonpositive_spot:{identity}")
        if row.get("metric") == "inventory" and value < 0:
            failures.append(f"negative_inventory:{identity}")
        close_raw = row.get("close_raw")
        if close_raw is not None and (
            not np.isfinite(float(close_raw)) or float(close_raw) <= 0
        ):
            failures.append(f"nonpositive_close_raw:{identity}")
        stale = row.get("staleness_trading_days")
        maximum = row.get("max_staleness_trading_days")
        if stale is not None and maximum is not None and int(stale) > int(maximum):
            failures.append(f"stale:{identity}:{stale}>{maximum}")
        source_unit = row.get("source_unit")
        expected_unit = row.get("expected_unit")
        if expected_unit is not None and source_unit != expected_unit:
            failures.append(
                f"unit_drift:{identity}:{source_unit}!={expected_unit}"
            )
        if bool(row.get("regime_blackout", False)):
            failures.append(f"regime_blackout:{identity}")

    if {"series_id", "value"}.issubset(rows.columns):
        for series_id, group in ordered.groupby("series_id", sort=True):
            numeric = pd.to_numeric(group["value"], errors="coerce")
            ratio = numeric.abs() / numeric.shift(1).abs().replace(0, np.nan)
            for position in ratio[(ratio > 100) | (ratio < 0.01)].index:
                failures.append(f"scale_change:{series_id}:{position}")
    return sorted(set(failures))


def enforce_quality(failures: list[str], *, degraded_research: bool) -> list[str]:
    failures = sorted(set(failures))
    if failures and not degraded_research:
        raise QualityGateError("; ".join(failures))
    return failures
```

The builder records the returned deterministic rows for a degraded research
candidate but never sets that candidate current. A formal candidate raises
`QualityGateError` before publication.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_quality.py -q
git add commodity_fundamentals/quality.py \
  tests/commodity_fundamentals/test_quality.py
git commit -m "feat: gate commodity fundamental coverage"
```

Expected: quality tests pass and the commit succeeds.

### Task 10: Build and atomically publish daily fundamentals

**Files:**
- Modify: `/home/elfbob/market-monitor/commodity_fundamentals/db.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/builder.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/__main__.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_builder.py`

- [ ] **Step 1: Write failing build-publication tests**

Use an in-memory fake build repository:

```python
from datetime import datetime

import pytest

from commodity_fundamentals.builder import FundamentalBuilder
from commodity_fundamentals.quality import QualityGateError


def aware(value):
    return datetime.fromisoformat(value)


class FakeBuildRepo:
    def __init__(self, current):
        self.current = dict(current)
        self.builds = {}
        self.rows = {}

    def start_build(self, metadata):
        self.builds[metadata["build_version"]] = {
            **metadata,
            "status": "running",
        }

    def load_inputs(self, metadata):
        return [{"product_code": "M", "metric": "spot", "value": 1.0}]

    def write_candidate(self, build_version, rows):
        self.rows[build_version] = list(rows)

    def record_quality(self, build_version, failures):
        self.builds[build_version]["failures"] = list(failures)

    def mark_validated(self, build_version):
        self.builds[build_version]["status"] = "validated"

    def publish_current(self, build_version, pit_mode):
        self.current[pit_mode] = build_version
        self.builds[build_version]["status"] = "published"

    def mark_failed(self, build_version, error):
        self.builds[build_version]["status"] = "failed"
        self.builds[build_version]["error"] = str(error)


def identity_materializer(inputs, metadata):
    return inputs


def failing_quality_gate(rows, *, degraded_research):
    raise QualityGateError("daily inventory coverage=5 required=6")


def passing_quality_gate(rows, *, degraded_research):
    return []


def test_failed_build_never_replaces_current():
    repo = FakeBuildRepo(current={"conservative": "old"})
    builder = FundamentalBuilder(
        repo,
        materialize=identity_materializer,
        quality_gate=failing_quality_gate,
    )

    with pytest.raises(QualityGateError):
        builder.build_and_publish(
            build_version="candidate",
            catalog_version="v1",
            pit_mode="conservative",
            source_recorded_cutoff=aware("2026-07-27T18:00:00+08:00"),
            start=aware("2025-01-01T00:00:00+08:00").date(),
            end=aware("2025-01-31T00:00:00+08:00").date(),
            rule_type="standard",
        )

    assert repo.current == {"conservative": "old"}
    assert repo.builds["candidate"]["status"] == "failed"


def test_publish_switches_only_requested_pit_mode():
    repo = FakeBuildRepo(
        current={"conservative": "old-c", "strict": "old-s"}
    )
    builder = FundamentalBuilder(
        repo,
        materialize=identity_materializer,
        quality_gate=passing_quality_gate,
    )

    builder.build_and_publish(
        build_version="new-c",
        catalog_version="v1",
        pit_mode="conservative",
        source_recorded_cutoff=aware("2026-07-27T18:00:00+08:00"),
        start=aware("2025-01-01T00:00:00+08:00").date(),
        end=aware("2025-01-31T00:00:00+08:00").date(),
        rule_type="standard",
    )

    assert repo.current == {"conservative": "new-c", "strict": "old-s"}
```

- [ ] **Step 2: Verify RED**

Run the focused builder test and expect import failure.

- [ ] **Step 3: Implement the builder pipeline**

Create the orchestration in `commodity_fundamentals/builder.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol


class BuildRepository(Protocol):
    def start_build(self, metadata: dict) -> None:
        raise NotImplementedError

    def load_inputs(self, metadata: dict):
        raise NotImplementedError

    def write_candidate(self, build_version: str, rows) -> None:
        raise NotImplementedError

    def record_quality(self, build_version: str, failures: list[str]) -> None:
        raise NotImplementedError

    def mark_validated(self, build_version: str) -> None:
        raise NotImplementedError

    def publish_current(self, build_version: str, pit_mode: str) -> None:
        raise NotImplementedError

    def mark_failed(self, build_version: str, error: Exception) -> None:
        raise NotImplementedError


@dataclass
class FundamentalBuilder:
    repository: BuildRepository
    materialize: Callable
    quality_gate: Callable

    def build_and_publish(
        self,
        *,
        build_version: str,
        catalog_version: str,
        pit_mode: str,
        source_recorded_cutoff: datetime,
        start: date,
        end: date,
        rule_type: str,
        degraded_research: bool = False,
    ):
        if pit_mode not in {"conservative", "strict"}:
            raise ValueError(f"unsupported pit_mode: {pit_mode}")
        if end < start:
            raise ValueError("end precedes start")
        if (
            source_recorded_cutoff.tzinfo is None
            or source_recorded_cutoff.utcoffset() is None
        ):
            raise ValueError("source_recorded_cutoff must be timezone-aware")
        metadata = {
            "build_version": build_version,
            "catalog_version": catalog_version,
            "pit_mode": pit_mode,
            "source_recorded_cutoff": source_recorded_cutoff,
            "start": start,
            "end": end,
            "rule_type": rule_type,
            "degraded_research": degraded_research,
        }
        self.repository.start_build(metadata)
        try:
            inputs = self.repository.load_inputs(metadata)
            rows = self.materialize(inputs, metadata)
            self.repository.write_candidate(build_version, rows)
            failures = self.quality_gate(
                rows,
                degraded_research=degraded_research,
            )
            self.repository.record_quality(build_version, failures)
            if failures:
                if not degraded_research:
                    raise ValueError("; ".join(failures))
                self.repository.mark_validated(build_version)
                return rows
            self.repository.mark_validated(build_version)
            self.repository.publish_current(build_version, pit_mode)
            return rows
        except Exception as exc:
            self.repository.mark_failed(build_version, exc)
            raise
```

Implement `PostgresBuildRepository` in `commodity_fundamentals/db.py` with
these exact read filters:

```sql
SELECT o.*, c.*
FROM commodity_research.observation_vintage AS o
JOIN commodity_research.ingest_run AS r USING (run_id)
JOIN commodity_research.series_catalog AS c
  ON c.catalog_version = o.catalog_version
 AND c.series_id = o.series_id
WHERE r.status IN ('raw_complete', 'validated')
  AND o.catalog_version = %(catalog_version)s
  AND o.recorded_at <= %(source_recorded_cutoff)s
  AND o.observation_date <= %(end)s
ORDER BY o.series_id, o.observation_date, o.recorded_at;
```

For every signal date, call `select_vintages` with that date's 15:00 China
cutoff, then call the Task 7 standardizers and Task 8 formula evaluator. Load
futures using:

```sql
SELECT
    trade_date,
    base_symbol AS product_code,
    contract_used,
    close_raw
FROM public.continuous_contract_ohlc
WHERE rule_type = %(rule_type)s
  AND trade_date BETWEEN %(start)s AND %(end)s
  AND base_symbol = ANY(%(product_codes)s)
ORDER BY trade_date, base_symbol;
```

Write only the columns defined by `fundamental_daily`. The value lineage JSON
contains the source observation IDs and, for basis, `contract_used` and
`close_raw`; hash the canonical sorted JSON with SHA-256. `publish_current`
runs this exact transaction on the same connection:

```sql
UPDATE commodity_research.fundamental_build
SET is_current = FALSE, updated_at = now()
WHERE pit_mode = %(pit_mode)s AND is_current;

UPDATE commodity_research.fundamental_build
SET status = 'published',
    is_current = TRUE,
    published_at = now(),
    finished_at = COALESCE(finished_at, now()),
    updated_at = now()
WHERE build_version = %(build_version)s
  AND pit_mode = %(pit_mode)s
  AND status = 'validated';
```

Require the second `UPDATE` row count to equal one before commit. On an
exception, rollback that transaction; `mark_failed` runs in a new transaction
and never changes an existing current row.

Create `commodity_fundamentals/__main__.py` with subparsers whose build
arguments are:

```python
build_parser.add_argument("--catalog-version", required=True)
build_parser.add_argument(
    "--pit-mode", choices=["conservative", "strict"], required=True
)
build_parser.add_argument("--start", type=date.fromisoformat, required=True)
build_parser.add_argument("--end", type=date.fromisoformat, required=True)
build_parser.add_argument(
    "--source-recorded-cutoff",
    type=datetime.fromisoformat,
    required=True,
)
build_parser.add_argument("--rule-type", default="standard")
build_parser.add_argument("--degraded-research", action="store_true")
```

Add `load-catalog --catalog`, `load-formulas --formulas`, and
`audit --build-version` subcommands. Resolve the connection solely from the
required `DATABASE_URL` environment variable; never embed a host, password,
or API key.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_builder.py \
  tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_standardize.py \
  tests/commodity_fundamentals/test_profit.py \
  tests/commodity_fundamentals/test_quality.py -q
```

Expected: all builder and standardization tests pass.

- [ ] **Step 5: Commit builder support**

```bash
cd /home/elfbob/market-monitor
git add commodity_fundamentals/builder.py \
  commodity_fundamentals/db.py \
  commodity_fundamentals/__main__.py \
  tests/commodity_fundamentals/test_builder.py
git commit -m "feat: atomically publish PIT fundamental builds"
```

### Task 11: Deploy the pilot schema and writer at an approved checkpoint

**Files:**
- Deploy: `migration/add_commodity_research_20260727.sql`
- Deploy: `writer/backend/main.py`
- Deploy: `writer/backend/commodity_fundamentals_api.py`
- Deploy: `commodity_fundamentals/`

- [ ] **Step 1: Stop and obtain live-mutation approval**

Present:

- DDL migration diff;
- rollback file;
- Debian backup path;
- sync-health output;
- exact schema target `commodity_research`;
- writer deployment diff;
- catalog version and hash.

Do not execute the next steps until approval is granted.

- [ ] **Step 2: Create the required pre-DDL backup**

On Debian:

```bash
ssh elfbob@100.65.111.79 \
  "mkdir -p /home/elfbob/market-monitor-backups \
   && pg_dump -Fc -d market_monitor \
      -f /home/elfbob/market-monitor-backups/pre_commodity_research_20260727.dump"
```

Expected: command exits 0 and the dump file is non-empty.

- [ ] **Step 3: Apply only the Debian pilot DDL**

```bash
scp migration/add_commodity_research_20260727.sql \
  elfbob@100.65.111.79:/tmp/
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 \
   -v fundamental_schema=commodity_research \
   -d market_monitor \
   -f /tmp/add_commodity_research_20260727.sql"
```

Expected: transaction commits and product count is 9. Do not apply this schema
to Pi5 and do not add it to `sync_state`.

- [ ] **Step 4: Deploy writer code and package**

```bash
rsync -av \
  writer/backend/main.py \
  writer/backend/commodity_fundamentals_api.py \
  elfbob@100.65.111.79:/home/elfbob/market-monitor/local-server/backend/
rsync -av \
  commodity_fundamentals/ \
  elfbob@100.65.111.79:/home/elfbob/market-monitor/local-server/backend/commodity_fundamentals/
ssh elfbob@100.65.111.79 \
  "sudo systemctl restart market-monitor-writer \
   && sudo systemctl is-active market-monitor-writer"
```

Expected: `active`.

- [ ] **Step 5: Run an authenticated run-lifecycle smoke**

Use the API token without placing it in shell history. This fixed audit run
contains zero observations, so it proves authentication, request-scoped
commit, and completion before the catalog is loaded without creating a fake
market value:

```bash
read -rsp "Writer API token: " MARKET_WRITER_TOKEN
curl --fail-with-body \
  -H "Authorization: Bearer ${MARKET_WRITER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"run_id":"00000000-0000-4000-8000-202607270001","mode":"audit","catalog_version":"v1","config_hash":"pre-catalog-lifecycle-smoke","requested_start":"2026-07-27","requested_end":"2026-07-27","started_at":"2026-07-27T18:00:00+08:00"}' \
  http://100.65.111.79:8000/api/fundamentals/runs
curl --fail-with-body \
  -H "Authorization: Bearer ${MARKET_WRITER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"expected_rows":0,"manifest_checksum":"zero-row-lifecycle-smoke"}' \
  http://100.65.111.79:8000/api/fundamentals/runs/00000000-0000-4000-8000-202607270001/raw-complete
unset MARKET_WRITER_TOKEN
```

Retain this clearly identified `audit` row in the Debian-only research schema;
it contains no observation and never enters Pi5 synchronization. Task 12's
real January artifact exercises the observation-batch endpoint.

Expected API statuses:

```text
run create: 200 running
complete:   200 raw_complete
```

- [ ] **Step 6: Verify existing writer health**

```bash
curl --fail http://100.65.111.79:8000/health
ssh elfbob@100.65.111.79 \
  "journalctl -u market-monitor-writer --since '10 minutes ago' \
   | tail -n 100"
```

Expected: healthy response and no new traceback.

### Task 12: Load catalog, upload a small interval, and publish pilot builds

**Files:**
- Read: `commodity_fundamentals/catalog.v1.yaml`
- Read: `commodity_fundamentals/profit_formulas.v1.yaml`
- Read: Windows January 2025 recovery artifact

- [ ] **Step 1: Load immutable catalog and formulas**

On Debian with `DATABASE_URL` loaded from the service environment:

```bash
python -m commodity_fundamentals load-catalog \
  --catalog commodity_fundamentals/catalog.v1.yaml
python -m commodity_fundamentals load-formulas \
  --formulas commodity_fundamentals/profit_formulas.v1.yaml
```

Expected: inserted catalog/formula counts match the reviewed files; rerunning
returns the same counts without changes.

- [ ] **Step 2: Upload the January 2025 smoke package**

Run the Plan 1 uploader command on Windows for the exact selected manifest.

Expected: run status `raw_complete`; stored count matches the manifest.

- [ ] **Step 3: Build conservative January 2025 data**

```bash
python -m commodity_fundamentals build \
  --catalog-version v1 \
  --pit-mode conservative \
  --start 2025-01-01 \
  --end 2025-01-31 \
  --source-recorded-cutoff 2026-07-27T23:59:59+08:00 \
  --rule-type standard
```

Expected: a published conservative build meeting target catalog coverage and
daily hard gates.

- [ ] **Step 4: Build strict January 2025 data**

Run the same command with `--pit-mode strict`.

Expected: no historical `backfill_final` data is accepted. The command either
exits non-zero without changing the strict current pointer and records the
candidate as failed. It must not publish an empty build or relabel backfill
values as captured live.

- [ ] **Step 5: Verify raw-price basis**

Query a sample of basis rows joined to spot and
`public.continuous_contract_ohlc.close_raw`; recompute the ratio in SQL.

Expected: maximum absolute difference is below `1e-10`, and changing or
inspecting `close_fa`/`close_ba` is irrelevant.

### Task 13: Execute resumable 2016-onward backfill

**Files:**
- Runtime only: Windows recovery artifact
- Runtime only: Debian `commodity_research` rows

- [ ] **Step 1: Stop and obtain full-backfill approval**

Present:

- estimated source series and observation counts;
- Windows artifact disk requirement;
- Debian table growth estimate;
- precise historical date range;
- expected sync impact, which must be zero because the schema is not synced;
- current pilot quality report.

- [ ] **Step 2: Capture the full Wind range on Windows**

```powershell
python -m commodity_fundamentals.capture `
  --catalog D:\marketmonitor\fundamentals\catalog.v1.yaml `
  --start 2016-01-01 `
  --end 2026-07-27 `
  --mode backfill `
  --artifact-dir D:\marketmonitor\fundamentals\artifacts
```

Expected: every active series/year chunk complete, no `.partial` file, and the
manifest count equals the sum of JSONL rows.

- [ ] **Step 3: Upload the exact full manifest**

Use `commodity_fundamentals.uploader` with batch size 500 and fallback
disabled.

Expected: exact stored count, `raw_complete` status, and idempotent retry with
no row increase.

- [ ] **Step 4: Build and audit the conservative history**

```bash
python -m commodity_fundamentals build \
  --catalog-version v1 \
  --pit-mode conservative \
  --start 2016-01-01 \
  --end 2026-07-27 \
  --source-recorded-cutoff 2026-07-27T23:59:59+08:00 \
  --rule-type standard
python -m commodity_fundamentals audit \
  --build-version "$(python -m commodity_fundamentals current --pit-mode conservative)"
```

Expected:

- target coverage: basis 9/9, inventory at least 8/9, profit at least 7/9;
- daily formal floor: at least 6/9 per fundamental factor;
- no non-finite published values;
- no forward fill beyond series staleness;
- no source-regime blackout violations.

- [ ] **Step 5: Verify rerun reproducibility**

Rebuild with the same catalog, cutoff, PIT mode, dates, and rule type under a
new build version.

Expected: identical ordered `(trade_date, product_code, metric, value,
lineage_hash)` checksum. Promotion changes only the conservative current
pointer.

### Task 14: Add incremental operations and final verification

**Files:**
- Create: `/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-pipeline.md`
- Modify: Windows Task Scheduler configuration outside Git

- [ ] **Step 1: Write the pipeline runbook**

Include:

- catalog/formula promotion;
- daily revision-window capture;
- upload and raw-complete checks;
- conservative and strict build commands;
- coverage audit;
- recovery resume;
- writer health;
- backup and rollback;
- explicit statement that pilot rows are absent from Pi5 and `sync_state`.

- [ ] **Step 2: Configure a daily Windows task**

Schedule after Wind data is available and outside active market collection.
The task:

1. captures each catalog series over its configured revision lookback;
2. writes a recovery package;
3. uploads with fallback disabled;
4. leaves the package intact on any failure.

Do not automatically publish a build until the Linux audit command succeeds.

Install this PowerShell command body in the scheduled task so live captures
are always marked `captured_live`:

```powershell
$captureEnd = (Get-Date).Date
$captureStart = $captureEnd.AddDays(-62)
python -m commodity_fundamentals.capture `
  --catalog D:\marketmonitor\fundamentals\catalog.v1.yaml `
  --start $captureStart.ToString("yyyy-MM-dd") `
  --end $captureEnd.ToString("yyyy-MM-dd") `
  --mode incremental `
  --artifact-dir D:\marketmonitor\fundamentals\artifacts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

- [ ] **Step 3: Run the entire subsystem test suite**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=.:writer/backend \
  /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals -q
git diff --check
git status --short --branch
```

Expected: all subsystem tests pass, no whitespace errors, and no uncommitted
implementation files.

- [ ] **Step 4: Commit operations documentation**

```bash
cd /home/elfbob/market-monitor
git add docs/operations/commodity-fundamentals-pipeline.md
git commit -m "docs: operate commodity fundamentals pipeline"
```

- [ ] **Step 5: Record the Plan 3 handoff**

Record:

```text
schema name
catalog version and hash
formula versions and hashes
current conservative build version
current strict build version, if one exists
current view column contract
coverage report path
full-history ordered lineage checksum
```

Plan 3 must treat those values as immutable inputs to CTA integration.
