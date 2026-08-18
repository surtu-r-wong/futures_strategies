# Commodity Fundamentals Store and Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-07-31 trimmed per workspace anti-redundancy ruling** (vertical slice
> first; deferred items listed at bottom); original 14-task scope in git
> history at `e31e1e5`. This plan now delivers exactly what its one consumer
> (the CTA six-factor strategy) exercises: a simple append-only observation
> store, one conservative PIT mode, one materialized daily build with
> coverage validation, and a resumable 2016-onward backfill.

**Goal:** Persist approved Wind observations in a simple append-only research store whose `run_id` column records which capture run produced each row, build one conservative point-in-time daily fundamentals table with coverage validation, and complete a resumable 2016-onward backfill.

**Architecture:** Recovery packages upload through the writer's existing import idiom (`/api/data/daily` + `ALLOWED_TABLES` / `TABLE_FIELDS` / `build_insert_sql`, the same mechanism every `position_data`-style import uses) into an append-only observation table with `ON CONFLICT DO NOTHING`. A Linux builder selects the conservative vintage (only observations available on or before each build date's 15:00 China close), standardizes spot/inventory/profit inputs, calculates basis from the unadjusted dominant-contract close, validates coverage, and records one complete build per run. Consumers read the latest `complete` build; there is no promotion machinery.

**Review tiers:** PIT vintage selection (`availability.select_vintages` and the builder's per-date cutoff application) and basis calculation (`standardize.build_basis` and its raw-close input) = **Tier 1** (full review). All other code = **Tier 2** (single combined review). Docs and config (runbook, `profit_formulas.v1.yaml`) = **Tier 3** (self-verify).

**Tech Stack:** PostgreSQL 15, psycopg2, pandas, NumPy, PyYAML, Python 3.13, pytest

---

**Design reference:**
`/home/elfbob/claude-code/futures_strategies/docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`
(the design's dual-PIT / transactional-endpoint / promotion sections are
superseded by the 2026-07-31 trim; see Deferred at bottom)

**Prerequisite:** Complete
`docs/superpowers/plans/2026-07-27-wind-fundamentals-catalog-capture.md`
and record its catalog version, config hash, recovery `run_id`, and manifest
checksum.

**Repository:** `/home/elfbob/market-monitor`

## File map

| File | Responsibility |
|---|---|
| `migration/add_commodity_research_20260727.sql` | Parameterized research-schema DDL (trimmed) |
| `migration/drop_commodity_research_20260727.sql` | Explicit rollback DDL |
| `writer/market-monitor/backend/main.py` | Register the two fundamentals tables in the existing import idiom |
| `commodity_fundamentals/uploader.py` | Recovery-package upload through the Phase 5 client to `/api/data/daily` |
| `commodity_fundamentals/availability.py` | Conservative vintage selection (extends the Plan 1 module) |
| `commodity_fundamentals/standardize.py` | Unit conversion, primary selection, spot/basis/inventory |
| `commodity_fundamentals/profit.py` | Versioned profit-formula validation and evaluation |
| `commodity_fundamentals/quality.py` | Coverage and row-quality validation |
| `commodity_fundamentals/builder.py` | Reproducible conservative build pipeline |
| `commodity_fundamentals/db.py` | Safe identifiers, connections, catalog loading, build repository |
| `commodity_fundamentals/__main__.py` | `load-catalog`, `build`, `audit` CLI |
| `commodity_fundamentals/profit_formulas.v1.yaml` | Reviewed profit definitions |
| `tests/commodity_fundamentals/test_schema_sql.py` | Migration contract tests |
| `tests/commodity_fundamentals/test_writer_tables.py` | Import-idiom SQL tests for the two new tables |
| `tests/commodity_fundamentals/test_uploader.py` | Ordered idempotent upload tests |
| `tests/commodity_fundamentals/test_availability.py` | Conservative PIT boundary tests |
| `tests/commodity_fundamentals/test_standardize.py` | Basis and inventory tests |
| `tests/commodity_fundamentals/test_profit.py` | Formula and component-availability tests |
| `tests/commodity_fundamentals/test_quality.py` | Coverage and row-quality tests |
| `tests/commodity_fundamentals/test_builder.py` | Build lifecycle and determinism tests |
| `docs/operations/commodity-fundamentals-pipeline.md` | DDL, backfill, recovery, and operations runbook |

### Task 1: Define the trimmed research schema with safety-checked DDL

**Files:**
- Read: `/home/elfbob/market-monitor/migration/SCHEMA_CHANGES.md`
- Create: `/home/elfbob/market-monitor/migration/add_commodity_research_20260727.sql`
- Create: `/home/elfbob/market-monitor/migration/drop_commodity_research_20260727.sql`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_schema_sql.py`

- [ ] **Step 1: Re-read the DDL safety card and confirm the pilot is unsynced**

Run:

```bash
sed -n '1,130p' /home/elfbob/market-monitor/migration/SCHEMA_CHANGES.md
ssh elfbob@100.65.111.79 \
  "psql -d market_monitor -Atc \
  \"SELECT schema_name, table_name, last_run_status
    FROM sync_state
    WHERE schema_name='commodity_research'
    ORDER BY table_name\""
```

Expected: the backup, sync-health, rollback, non-synchronized-object, and Pi5
rules are visible in the execution transcript, and the `sync_state` query
returns no rows. The pilot schema is Debian-only and stays out of
Pi5 synchronization.

- [ ] **Step 2: Write failing migration-contract tests**

Create `tests/commodity_fundamentals/test_schema_sql.py` asserting:

1. the migration contains exactly these objects and no others from the
   original 14-task scope: `product_mapping`, `series_catalog`,
   `fundamental_ingest_run`, `fundamental_observation`, `fundamental_build`,
   `fundamental_daily`;
2. version keys survive:
   `PRIMARY KEY (catalog_version, series_id)` on `series_catalog` and
   `UNIQUE (run_id, catalog_version, series_id, observation_date)` on
   `fundamental_observation`;
3. the trim holds: the SQL contains neither `is_current` nor `'strict'` nor
   `current_fundamental_daily` (guards against the deferred promotion/dual-mode
   machinery leaking back in);
4. the migration does not `ALTER` any `public.*` table
   (`spot_prices`, `inventory`, `commodities_spot_prices`).

- [ ] **Step 3: Verify RED**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_schema_sql.py -q
```

Expected: failures because the migration file is absent.

- [ ] **Step 4: Write the parameterized migration**

Create `migration/add_commodity_research_20260727.sql`. It accepts a psql
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

CREATE TABLE IF NOT EXISTS :"fundamental_schema".fundamental_ingest_run (
    run_id UUID PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('backfill', 'incremental', 'audit')),
    catalog_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    requested_start DATE NOT NULL,
    requested_end DATE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    manifest_sha256 TEXT,
    manifest_row_count BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".fundamental_observation (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL
        REFERENCES :"fundamental_schema".fundamental_ingest_run(run_id),
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (catalog_version, series_id)
        REFERENCES :"fundamental_schema".series_catalog(catalog_version, series_id),
    UNIQUE (run_id, catalog_version, series_id, observation_date)
);

CREATE INDEX IF NOT EXISTS fundamental_observation_lookup
    ON :"fundamental_schema".fundamental_observation
       (catalog_version, series_id, observation_date, available_at, recorded_at);

CREATE TABLE IF NOT EXISTS :"fundamental_schema".fundamental_build (
    build_version TEXT PRIMARY KEY,
    catalog_version TEXT NOT NULL,
    pit_mode TEXT NOT NULL DEFAULT 'conservative'
        CHECK (pit_mode = 'conservative'),
    source_recorded_cutoff TIMESTAMPTZ NOT NULL,
    formula_version TEXT,
    formula_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    input_rows BIGINT NOT NULL DEFAULT 0,
    output_rows BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL,
    error_summary TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

Notes on what this schema deliberately does NOT contain (see Deferred):
no `profit_formula` table (formulas stay in reviewed YAML; the build records
`formula_version` + `formula_hash`), no `current_fundamental_daily` view, no
`is_current` promotion index, no ingest-run status lifecycle columns.
Consumers select the latest `complete` build.

- [ ] **Step 5: Add explicit rollback SQL**

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

- [ ] **Step 6: Verify migration tests pass, then transaction-test on Debian**

Run the pytest command from Step 3 (expected: all pass), then:

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
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 -d market_monitor \
   -c 'DROP SCHEMA commodity_research_test CASCADE'"
```

Expected: count `9`, then `DROP SCHEMA` of the disposable test schema only.

- [ ] **Step 7: Commit the schema migration**

```bash
cd /home/elfbob/market-monitor
git add migration/add_commodity_research_20260727.sql \
  migration/drop_commodity_research_20260727.sql \
  tests/commodity_fundamentals/test_schema_sql.py
git commit -m "feat: define trimmed commodity fundamentals schema"
```

### Task 2: Ingest through the existing writer import idiom

The writer already exposes a generic `/api/data/daily` endpoint: the request
carries `{"table": <name>, "data": [<row dicts>]}`, the table is validated
against `ALLOWED_TABLES`, and `build_insert_sql` maps each row to a per-table
`INSERT ... ON CONFLICT ...` statement. Every `position_data`-style import in
this workspace uses this path. This task registers the two fundamentals
tables in that idiom instead of building a dedicated transactional router.
Auditability of "which capture run produced this row" is preserved by the
`run_id` column on every observation plus the Linux-side count verification
in Task 5; idempotent re-upload comes from `ON CONFLICT ... DO NOTHING`.

> **Writer source lock (2026-07-29):** the authoritative tracked writer is
> `writer/market-monitor/backend/main.py`, which the live preflight matched to
> `/home/elfbob/market-monitor/local-server/backend/main.py`. The legacy
> `writer/backend/main.py` is behind the deployment and must not be edited,
> tested, or deployed. Re-run the live diff immediately before deployment and
> stop on drift.

**Files:**
- Modify: `/home/elfbob/market-monitor/writer/market-monitor/backend/main.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/uploader.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_writer_tables.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_uploader.py`
- Modify: `/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-wind.md`

- [ ] **Step 1: Compare the deployed Debian writer with Git before touching it**

```bash
scp elfbob@100.65.111.79:/home/elfbob/market-monitor/local-server/backend/main.py \
  /tmp/market-monitor-writer-main.live.py
diff -u \
  /home/elfbob/market-monitor/writer/market-monitor/backend/main.py \
  /tmp/market-monitor-writer-main.live.py
```

Expected: no output, or a reviewed bounded diff. Stop if the deployed file has
unrelated changes that are absent from Git.

- [ ] **Step 2: Write failing tests for the two table registrations**

Create `tests/commodity_fundamentals/test_writer_tables.py` (insert
`writer/market-monitor/backend` into `sys.path` in the module). Assert:

1. `ALLOWED_TABLES` contains `fundamental_ingest_run` and
   `fundamental_observation`;
2. `build_insert_sql("fundamental_observation", row)` produces SQL that
   targets `commodity_research.fundamental_observation` (schema-qualified)
   and ends with
   `ON CONFLICT (run_id, catalog_version, series_id, observation_date) DO NOTHING`
   — append-only, never `DO UPDATE`;
3. `build_insert_sql("fundamental_ingest_run", row)` targets
   `commodity_research.fundamental_ingest_run` with
   `ON CONFLICT (run_id) DO NOTHING`;
4. values are ordered exactly per `TABLE_FIELDS` for both tables.

Create `tests/commodity_fundamentals/test_uploader.py` with an injected fake
`post` callable, asserting:

1. the run row is posted before any observation batch, and batches preserve
   manifest order;
2. every request body is `{"table": ..., "data": [...]}` against
   `/api/data/daily` with the Bearer token header and
   `fallback_enabled=False` (Pi5 never receives pilot rows);
3. every observation row carries `run_id` and `catalog_version` plus the nine
   recovery fields (`series_id`, `observation_date`, `published_at`,
   `available_at`, `recorded_at`, `raw_value`, `source_unit`, `currency`,
   `vintage_quality`);
4. rows with a null `available_at`/`source_unit`/`currency`/`vintage_quality`
   or a naive timestamp are rejected before anything is sent;
5. an incomplete recovery chunk (per the Plan 1 manifest) aborts the upload.

- [ ] **Step 3: Verify RED**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=.:writer/market-monitor/backend \
  /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_writer_tables.py \
  tests/commodity_fundamentals/test_uploader.py -q
```

Expected: failures for the missing registrations and the missing uploader.

- [ ] **Step 4: Register the tables and implement the uploader**

In `writer/market-monitor/backend/main.py`:

- add both names to `ALLOWED_TABLES`;
- add `TABLE_FIELDS['fundamental_ingest_run']` =
  `run_id, mode, catalog_version, config_hash, requested_start,
  requested_end, started_at, manifest_sha256, manifest_row_count`;
- add `TABLE_FIELDS['fundamental_observation']` =
  `run_id, catalog_version, series_id, observation_date, published_at,
  available_at, recorded_at, raw_value, source_unit, currency,
  vintage_quality`;
- add the two `build_insert_sql` branches with the schema-qualified
  `commodity_research.` table names and the `DO NOTHING` conflict clauses
  from Step 2. Follow the existing branch style exactly.

Create `commodity_fundamentals/uploader.py`:

- `UploadPackage.open(root)` wraps the Plan 1 `RecoveryPackage`, validates
  every row (required fields present, PIT fields non-null, timestamps
  timezone-aware, all manifest chunks complete with matching checksums);
- `run_row()` builds the `fundamental_ingest_run` dict from the manifest,
  including `manifest_sha256` (SHA-256 of `manifest.json`) and
  `manifest_row_count` (sum of chunk `row_count`);
- `observation_batches(size)` yields row dicts in sorted-chunk order,
  `1 <= size <= 500`;
- `upload_package(package, *, base_url, token, post, batch_size)` posts the
  run row, then each batch, to `base_url + "/api/data/daily"` with
  `headers={"Authorization": f"Bearer {token}"}` and
  `fallback_enabled=False`, writing an atomic
  `upload-acknowledgements.json` after each response (Plan 1's
  partial-then-replace pattern);
- the `main()` CLI imports the reviewed Phase 5 client
  (`http_client.post_with_fallback`) and reads the endpoint/token from the
  collector's `config.yaml` exactly as the live collector does. The token is
  used only in the header and never written to acknowledgements.

Because every insert is `DO NOTHING` on the run-scoped unique key, re-running
the uploader after any interruption is safe and converges; there is no
completion handshake to resume.

- [ ] **Step 5: Verify GREEN, update the Windows runbook, and commit**

Run the command from Step 3 (expected: all pass). Add the upload command to
`docs/operations/commodity-fundamentals-wind.md`:

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
name before sending data; it never scans and uploads every artifact
automatically.

```bash
cd /home/elfbob/market-monitor
git add writer/market-monitor/backend/main.py \
  commodity_fundamentals/uploader.py \
  tests/commodity_fundamentals/test_writer_tables.py \
  tests/commodity_fundamentals/test_uploader.py \
  docs/operations/commodity-fundamentals-wind.md
git commit -m "feat: ingest Wind fundamentals via the writer import idiom"
```

### Task 3: Conservative vintage selection, standardization, and profit formulas

**Files:**
- Modify: `/home/elfbob/market-monitor/commodity_fundamentals/availability.py`
- Modify: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_availability.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/standardize.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_standardize.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/profit.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_profit.py`
- Create after catalog review: `/home/elfbob/market-monitor/commodity_fundamentals/profit_formulas.v1.yaml`

- [ ] **Step 1: Write failing conservative-selection tests** *(Tier 1 scope)*

Extend `tests/commodity_fundamentals/test_availability.py`. Keep the Plan 1
unknown-release-time test and the captured-live no-backdating test for
`effective_available_at`, then add selection cases for the single
conservative mode:

```python
def test_conservative_selection_excludes_unavailable_and_keeps_latest_recording():
    rows = pd.DataFrame(
        {
            "series_id": ["x", "x", "x"],
            "observation_date": [date(2020, 1, 1)] * 3,
            "available_at": [
                datetime(2020, 1, 2, 15, tzinfo=CN),
                datetime(2020, 1, 2, 15, tzinfo=CN),
                datetime(2026, 7, 27, 18, tzinfo=CN),   # not yet available
            ],
            "recorded_at": [
                datetime(2026, 7, 1, 18, tzinfo=CN),
                datetime(2026, 7, 20, 18, tzinfo=CN),   # later recording wins
                datetime(2026, 7, 27, 18, tzinfo=CN),
            ],
            "vintage_quality": ["backfill_final"] * 2 + ["captured_live"],
            "raw_value": [100.0, 100.5, 101.0],
        }
    )

    selected = select_vintages(
        rows,
        signal_cutoff=datetime(2020, 1, 2, 15, tzinfo=CN),
    )

    assert len(selected) == 1
    assert selected.iloc[0]["raw_value"] == 100.5
```

Also assert that missing required columns raise `ValueError` and that an
empty frame returns an empty copy.

- [ ] **Step 2: Implement conservative `select_vintages`** *(Tier 1)*

Append to `commodity_fundamentals/availability.py`:

```python
import pandas as pd

def select_vintages(rows, *, signal_cutoff):
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
    return (
        eligible.sort_values(["series_id", "observation_date", "recorded_at"])
        .drop_duplicates(["series_id", "observation_date"], keep="last")
    )
```

Conservative is the only mode: an observation is eligible when it was
available on or before the signal cutoff, and the latest recording of an
eligible observation wins. Use the database's eligible
`continuous_contract_ohlc` dates as the calendar; never generate a
weekday-only calendar.

- [ ] **Step 3: Write failing standardization tests and implement**

Create `tests/commodity_fundamentals/test_standardize.py` locking these
behaviors (test bodies as in the original plan, git `e31e1e5`):

- basis uses the raw contract close, never `close_fa`/`close_ba`
  (`build_basis(spot, futures)` = `spot / close_raw - 1` with non-positive
  raw closes masked) — *Tier 1*;
- `basis_available_at` is the latest of the spot availability and the
  futures close;
- inventory sums reject heterogeneous units and require one approved
  `sum:` group (`aggregate_inventory`);
- `limited_forward_fill` stops after the series' `max_staleness` trading
  days;
- `select_primary_series` rejects overlapping active `primary` rows
  (exactly one active as of a date);
- `regime_blackout_mask` suppresses the first 20 trading dates after every
  source-regime start.

Implement `commodity_fundamentals/standardize.py` with `normalize_value`,
`build_basis`, `basis_available_at`, `aggregate_inventory`,
`limited_forward_fill`, `select_primary_series`, and `regime_blackout_mask`
exactly as specified in the original plan (the module was already
mode-agnostic; carry it over unchanged).

- [ ] **Step 4: Write failing profit-formula tests and implement**

Create `tests/commodity_fundamentals/test_profit.py`:

- a formula's value is the coefficient-weighted component sum minus fixed
  cost, and its `available_at` is the latest component availability;
- a missing component yields no result (`None`), never a partial sum.

Implement `commodity_fundamentals/profit.py` with `Formula.from_mapping`
(product allow-list, non-empty unique finite components, effective-interval
validation), `evaluate_formula`, `validate_formula_set` (components exist in
the catalog, CNY currency, unit match, no overlapping effective ranges per
product), and a `--validate-only` CLI, as in the original plan. Formulas live
only in the reviewed YAML file; there is no formula DB table. The builder
records the formula file's version and SHA-256 hash on every build
(Task 4), which is the whole versioning story for this slice.

- [ ] **Step 5: Create reviewed formula version 1** *(Tier 3 config, user-reviewed values)*

Using the approved Plan 1 catalog, create `profit_formulas.v1.yaml` only for
products lacking a stable direct Wind profit series. Validate:

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m commodity_fundamentals.profit \
  --catalog commodity_fundamentals/catalog.v1.yaml \
  --formulas commodity_fundamentals/profit_formulas.v1.yaml \
  --validate-only
```

Expected: exit 0 and at least seven products with either a direct reviewed
profit series or a valid formula. `RU` remains absent when no defensible
series passes review.

- [ ] **Step 6: Verify GREEN and commit**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_standardize.py \
  tests/commodity_fundamentals/test_profit.py -q
git add commodity_fundamentals/availability.py \
  commodity_fundamentals/standardize.py \
  commodity_fundamentals/profit.py \
  commodity_fundamentals/profit_formulas.v1.yaml \
  tests/commodity_fundamentals/test_availability.py \
  tests/commodity_fundamentals/test_standardize.py \
  tests/commodity_fundamentals/test_profit.py
git commit -m "feat: conservative vintages, standardization, profit formulas"
```

### Task 4: Build daily fundamentals with coverage validation

**Files:**
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/quality.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/builder.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/db.py`
- Create: `/home/elfbob/market-monitor/commodity_fundamentals/__main__.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_quality.py`
- Create: `/home/elfbob/market-monitor/tests/commodity_fundamentals/test_builder.py`

- [ ] **Step 1: Write failing coverage and builder tests**

`tests/commodity_fundamentals/test_quality.py` locks:

- target catalog coverage `basis_rate 9/9, inventory ≥8/9, profit ≥7/9`
  (`target_coverage_failures`);
- the daily hard floor of 6 products per fundamental metric per date
  (`daily_coverage_failures`, reason format
  `"<date> <metric> coverage=<n> required=6"`);
- row-quality reason codes (`row_quality_failures`): non-finite values,
  non-positive spot, negative inventory, non-positive `close_raw`, staleness
  beyond the series maximum, unit drift, regime-blackout rows, and >100x /
  <0.01x scale jumps per series.

`tests/commodity_fundamentals/test_builder.py`, against an in-memory fake
build repository, locks:

- a build whose coverage validation fails is marked `failed` with the joined
  reasons and writes no usable rows for consumers (status never becomes
  `complete`);
- a passing build is marked `complete` exactly once;
- two runs over identical inputs produce identical ordered
  `(trade_date, product_code, metric, value, lineage_hash)` sequences.

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals/test_quality.py \
  tests/commodity_fundamentals/test_builder.py -q
```

Expected: import failures for `quality` and `builder`.

- [ ] **Step 3: Implement quality gates and the builder pipeline**

`commodity_fundamentals/quality.py`: implement `target_coverage_failures`,
`daily_coverage_failures` (`DAILY_REQUIRED = 6`,
`TARGET_COVERAGE = {"basis_rate": 9, "inventory": 8, "profit": 7}`), and
`row_quality_failures` as specified in the original plan. There is no
`degraded_research` escape: `enforce_quality(failures)` raises
`QualityGateError` whenever failures exist. A build either validates clean or
is recorded `failed` with its deterministic reason list in `error_summary`.

`commodity_fundamentals/builder.py`: a `FundamentalBuilder` over a
`BuildRepository` protocol with `start_build` → `load_inputs` →
`materialize` → `write_candidate` → quality validation →
`mark_complete` / `mark_failed`. Any exception marks the build `failed` and
re-raises. There is no publish/promotion step.

`commodity_fundamentals/db.py`: `PostgresBuildRepository` using
`psycopg2.sql.Identifier` for the schema. Input query (conservative source
pull; the per-date cutoff is applied by `select_vintages` — *Tier 1*):

```sql
SELECT o.*, c.*
FROM commodity_research.fundamental_observation AS o
JOIN commodity_research.series_catalog AS c
  ON c.catalog_version = o.catalog_version
 AND c.series_id = o.series_id
WHERE o.catalog_version = %(catalog_version)s
  AND o.recorded_at <= %(source_recorded_cutoff)s
  AND o.observation_date <= %(end)s
ORDER BY o.series_id, o.observation_date, o.recorded_at;
```

For every signal date, call `select_vintages` with that date's 15:00
Asia/Shanghai cutoff, then the Task 3 standardizers and formula evaluator.
Load futures for basis using the unadjusted close:

```sql
SELECT trade_date, base_symbol AS product_code, contract_used, close_raw
FROM public.continuous_contract_ohlc
WHERE rule_type = %(rule_type)s
  AND trade_date BETWEEN %(start)s AND %(end)s
  AND base_symbol = ANY(%(product_codes)s)
ORDER BY trade_date, base_symbol;
```

Write only the columns defined by `fundamental_daily`. The lineage JSON
contains the source observation IDs and, for basis, `contract_used` and
`close_raw`; hash the canonical sorted JSON with SHA-256. `mark_complete`
sets `status='complete'`, `finished_at=now()`, and the row counts in one
transaction; `mark_failed` runs in a new transaction.

`commodity_fundamentals/__main__.py` subcommands:

- `load-catalog --catalog` — insert catalog rows
  `ON CONFLICT (catalog_version, series_id) DO NOTHING`; re-running returns
  the same counts. Print a warning if any existing row's `config_hash`
  differs from the loaded file (detectable drift; enforcement deferred);
- `build --catalog-version --start --end --source-recorded-cutoff
  --rule-type standard` (pit mode is always conservative; no flag);
- `audit --build-version` — re-run the coverage and row-quality checks
  against stored rows and print the ordered lineage checksum;
- `current` — print the latest `complete` build version
  (`ORDER BY finished_at DESC LIMIT 1`), the exact query the CTA reader
  uses.

Resolve the connection solely from the required `DATABASE_URL` environment
variable; never embed a host, password, or API key.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals -q
git add commodity_fundamentals/quality.py \
  commodity_fundamentals/builder.py \
  commodity_fundamentals/db.py \
  commodity_fundamentals/__main__.py \
  tests/commodity_fundamentals/test_quality.py \
  tests/commodity_fundamentals/test_builder.py
git commit -m "feat: build validated conservative daily fundamentals"
```

### Task 5: Deploy, smoke, and resumable 2016-onward backfill

**Files:**
- Deploy: `migration/add_commodity_research_20260727.sql`, `writer/market-monitor/backend/main.py`, `commodity_fundamentals/`
- Create: `/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-pipeline.md`

- [ ] **Step 1: Stop and obtain live-mutation approval**

Present: the DDL migration diff, the rollback file, the Debian backup path,
sync-health output, the exact schema target `commodity_research`, the writer
deployment diff, and the catalog version and hash. Do not execute the next
steps until approval is granted.

- [ ] **Step 2: Back up, apply the Debian-only DDL, and deploy the writer**

```bash
ssh elfbob@100.65.111.79 \
  "mkdir -p /home/elfbob/market-monitor-backups \
   && pg_dump -Fc -d market_monitor \
      -f /home/elfbob/market-monitor-backups/pre_commodity_research_20260727.dump"
scp migration/add_commodity_research_20260727.sql \
  elfbob@100.65.111.79:/tmp/
ssh elfbob@100.65.111.79 \
  "psql -v ON_ERROR_STOP=1 \
   -v fundamental_schema=commodity_research \
   -d market_monitor \
   -f /tmp/add_commodity_research_20260727.sql"
rsync -av writer/market-monitor/backend/main.py \
  elfbob@100.65.111.79:/home/elfbob/market-monitor/local-server/backend/
ssh elfbob@100.65.111.79 \
  "sudo systemctl restart market-monitor-writer \
   && sudo systemctl is-active market-monitor-writer"
curl --fail http://100.65.111.79:8000/health
```

Expected: non-empty dump, product count 9, `active`, healthy response, and no
new traceback in the writer journal. Do not apply this schema to Pi5 and do
not add it to `sync_state`.

- [ ] **Step 3: Load the catalog and smoke the January 2025 artifact**

On Debian (`DATABASE_URL` from the service environment):

```bash
python -m commodity_fundamentals load-catalog \
  --catalog commodity_fundamentals/catalog.v1.yaml
```

Then run the Task 2 uploader command on Windows for the Plan 1
January 2025 recovery artifact, and verify the counts from Linux:

```sql
SELECT r.run_id, r.manifest_row_count, count(o.id) AS stored
FROM commodity_research.fundamental_ingest_run r
LEFT JOIN commodity_research.fundamental_observation o USING (run_id)
GROUP BY r.run_id, r.manifest_row_count;
```

Expected: `stored = manifest_row_count` for the smoke run. Re-running the
uploader must not change either number (idempotent `DO NOTHING`).

- [ ] **Step 4: Build January 2025 and verify raw-price basis**

```bash
python -m commodity_fundamentals build \
  --catalog-version v1 \
  --start 2025-01-01 \
  --end 2025-01-31 \
  --source-recorded-cutoff 2026-07-27T23:59:59+08:00 \
  --rule-type standard
```

Expected: one `complete` conservative build meeting the target and daily
coverage gates. Then query a sample of basis rows joined to spot and
`public.continuous_contract_ohlc.close_raw` and recompute the ratio in SQL:
maximum absolute difference below `1e-10`, with `close_fa`/`close_ba`
irrelevant to the result. *(Tier 1 verification.)*

- [ ] **Step 5: Stop for full-backfill approval, then execute 2016-onward**

Present the estimated source series/observation counts, Windows artifact disk
requirement, Debian table growth estimate, and the exact date range; sync
impact must be zero because the schema is not synced. Then:

```powershell
python -m commodity_fundamentals.capture `
  --catalog D:\marketmonitor\fundamentals\catalog.v1.yaml `
  --start 2016-01-01 `
  --end 2026-07-27 `
  --mode backfill `
  --artifact-dir D:\marketmonitor\fundamentals\artifacts
```

Resumability: an interrupted capture resumes with `--resume` (Plan 1
recovery-package chunk skipping); an interrupted upload is simply re-run
(append-only `DO NOTHING`). Upload the full manifest with batch size 500,
verify `stored = manifest_row_count` from Linux, then:

```bash
python -m commodity_fundamentals build \
  --catalog-version v1 \
  --start 2016-01-01 \
  --end 2026-07-27 \
  --source-recorded-cutoff 2026-07-27T23:59:59+08:00 \
  --rule-type standard
python -m commodity_fundamentals audit \
  --build-version "$(python -m commodity_fundamentals current)"
```

Expected audit results:

- target coverage: basis 9/9, inventory at least 8/9, profit at least 7/9;
- daily floor: at least 6/9 per fundamental metric;
- no non-finite stored values;
- no forward fill beyond series staleness;
- no source-regime blackout violations.

Verify rerun reproducibility: rebuild with the same catalog, cutoff, dates,
and rule type under a new build version and confirm an identical ordered
`(trade_date, product_code, metric, value, lineage_hash)` checksum.

- [ ] **Step 6: Write the runbook, run the suite, and record the Plan 3 handoff**

Write `docs/operations/commodity-fundamentals-pipeline.md` *(Tier 3)*
covering: catalog/formula versioning, capture/upload/count-verification,
build and audit commands, recovery resume, writer health, backup and
rollback, the explicit statement that pilot rows are absent from Pi5 and
`sync_state`, and a pointer to this plan's Deferred section for everything
intentionally not built.

```bash
cd /home/elfbob/market-monitor
PYTHONPATH=.:writer/market-monitor/backend \
  /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest tests/commodity_fundamentals -q
git diff --check
git status --short --branch
git add docs/operations/commodity-fundamentals-pipeline.md
git commit -m "docs: operate trimmed commodity fundamentals pipeline"
```

Record for Plan 3 as immutable inputs:

```text
schema name (commodity_research)
catalog version and hash
formula version and hash
latest complete conservative build version
coverage report path
full-history ordered lineage checksum
```

## Deferred until CTA is trusted and in monthly use

Recorded per the 2026-07-31 anti-redundancy ruling (vertical slice first;
deferred generality is recorded, not built). Each item stays out until the
CTA six-factor strategy survives the upstream price-adjustment fix and the
user promotes it to monthly use. Original designs: git history at `e31e1e5`.

- **Strict PIT mode** (captured_live-only vintage selection): no strict
  history exists yet and the only consumer runs conservative.
- **Dual-mode build machinery** (pit_mode plumbing, per-mode current
  pointers): only one mode is built; the column stays CHECK-pinned to
  `conservative`.
- **Atomic promotion / publish gating** (`is_current` partial unique index,
  `current_fundamental_daily` view, publish transaction, `published` status):
  single-writer research flow; the reader selects the latest `complete`
  build and records its version in every result.
- **Dedicated transactional FastAPI endpoint** (`/api/fundamentals/*`
  run-lifecycle service/repository, payload-hash conflict detection,
  row-count completion handshake): the existing `/api/data/daily` import
  idiom with `ON CONFLICT DO NOTHING` plus Linux-side count verification
  preserves run-level auditability at a fraction of the machinery.
- **Immutable catalog snapshot ceremony beyond a version column**:
  `series_catalog` keeps `catalog_version` + `config_hash` and `load-catalog`
  warns on hash drift; DB-side immutability enforcement and drift rejection
  are deferred.
- **Profit formula DB registry** (`profit_formula` table): formulas stay in
  reviewed YAML; builds record the formula version and hash in lineage.
- **Daily incremental Windows scheduled capture task**: primarily feeds the
  deferred strict mode; conservative history extends by re-running the
  backfill capture over a recent window.
- **Degraded-research build publication path** (`--degraded-research`): a
  build either passes coverage validation (`complete`) or is `failed`.
