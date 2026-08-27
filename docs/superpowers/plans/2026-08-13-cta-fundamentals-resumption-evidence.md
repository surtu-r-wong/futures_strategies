# CTA Six-Factor Fundamentals Resumption — Evidence, Smoke, and Handoff

> Continue after Task 4 in `2026-08-13-cta-fundamentals-resumption-wind-gate.md`. The scope, architecture, stack, repository map, and production-mutation exclusions in `2026-08-13-cta-fundamentals-resumption.md` remain authoritative.

## Task 5: Build and approve the real Wind catalog

**Files:**
- Read: `.worktrees/market-monitor-commodity-vertical-slice/docs/data/commodity-fundamentals/catalog-v1-pending.csv`
- Read: `.worktrees/market-monitor-commodity-vertical-slice/commodity_fundamentals/models.py`
- Create after review: `D:\marketmonitor\fundamentals\catalog-v1.yaml`
- Create after review: `/tmp/cta-fundamentals-resumption-20260813/catalog-v1-evidence.csv`

- [ ] **Step 1: Use the 27 pending rows only as a discovery checklist**

For each of `M`, `RB`, `CU`, `AL`, `TA`, `PP`, `MA`, `BU`, and `RU`, discover candidates for spot price, inventory, and either direct profit or every component of the reviewed profit formula. Never map a legacy database name to a Wind code by inference. Each accepted code must be displayed by the Wind terminal or returned by an explicit Wind API metadata/history check.

- [ ] **Step 2: Record evidence for every candidate**

The evidence CSV has these exact columns:

```text
product_code,metric_role,series_id,source_code,source_name,frequency,api_method,field_name,source_unit,target_unit,currency,scale_multiplier,aggregation_rule,date_semantics,release_calendar_days,release_time,unknown_time_policy,max_staleness_trading_days,valid_from,valid_to,first_date,last_date,point_count,evidence_method,evidence_timestamp,decision
```

`decision` is only `accept` or `reject`. Record actual metadata, first/last dates, and point counts; do not put raw series values in the report.

- [ ] **Step 3: Pause for explicit user confirmation**

Present accepted rows grouped by product. The user confirms the exact Wind code, source name, role, frequency, API method, field, units, currency, conversion, aggregation, date semantics, release lag, unknown-time policy, staleness limit, and validity interval. Do not create the catalog before confirmation. If credible evidence is absent, retain the gap and let coverage fail.

- [ ] **Step 4: Generate the catalog from confirmed evidence**

The YAML root contains only `catalog_version` and `series`. Each `SeriesSpec` mapping contains exactly:

```text
series_id,source_code,source_name,product_code,metric_role,frequency,api_method,field_name,wind_options,source_unit,target_unit,currency,scale_multiplier,aggregation_rule,date_semantics,release_lag_rule,unknown_time_policy,max_staleness_trading_days,valid_from,valid_to,active
```

Use `release_lag_rule: {calendar_days: N, time: HH:MM}` with confirmed values. `edb` requires empty `field_name`; `wsd` requires a nonempty field. Do not add AU or AG.

- [ ] **Step 5: Validate the catalog contract**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"$env:PYTHONPATH='D:\marketmonitor\fundamentals'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m commodity_fundamentals load-catalog --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml'\""
```

Expected: loader prints catalog version/config hash and exits 0. Schema, duplicate, API-field, unit, validity, and role errors block the task.

## Task 6: Run preflight and enforce the 9/8/7 gate

**Artifacts:**
- Create remote: `D:\marketmonitor\fundamentals\reports\catalog-v1-preflight.csv`
- Copy local: `/tmp/cta-fundamentals-resumption-20260813/catalog-v1.yaml`
- Copy local: `/tmp/cta-fundamentals-resumption-20260813/catalog-v1-preflight.csv`

- [ ] **Step 1: Run the required history preflight**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\reports' | Out-Null; $env:PYTHONPATH='D:\marketmonitor\fundamentals'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2016-01-01 --end 2026-08-13 --output 'D:\marketmonitor\fundamentals\reports\catalog-v1-preflight.csv'\""
```

Expected: exit 0 and one summary row per active series.

- [ ] **Step 2: Copy only catalog metadata and aggregate report to Linux**

```bash
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/catalog-v1.yaml' /tmp/cta-fundamentals-resumption-20260813/
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/reports/catalog-v1-preflight.csv' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/catalog-v1.yaml /tmp/cta-fundamentals-resumption-20260813/catalog-v1-preflight.csv | tee /tmp/cta-fundamentals-resumption-20260813/preflight-artifact-hashes.txt
```

Expected: no raw observations leave Windows.

- [ ] **Step 3: Validate schema and coverage**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
PYTHONPATH=. /home/elfbob/market-monitor/venv/bin/python - <<'PY'
from pathlib import Path
import pandas as pd
from commodity_fundamentals.catalog import load_catalog

root = Path('/tmp/cta-fundamentals-resumption-20260813')
catalog = load_catalog(root / 'catalog-v1.yaml')
report = pd.read_csv(root / 'catalog-v1-preflight.csv')
expected = [
    'series_id', 'product_code', 'metric_role', 'source_code', 'source_name',
    'frequency', 'source_unit', 'target_unit', 'currency', 'first_date',
    'last_date', 'point_count', 'missing_count', 'nonpositive_count',
]
assert report.columns.tolist() == expected, report.columns.tolist()
assert report['series_id'].is_unique
assert set(report['series_id']) == {s.series_id for s in catalog.series if s.active}
usable = report.loc[report['point_count'].fillna(0).astype(int) > 0]
spot = set(usable.loc[usable.metric_role == 'spot', 'product_code'])
inventory = set(usable.loc[usable.metric_role == 'inventory', 'product_code'])
profit = set(usable.loc[usable.metric_role.isin(['profit_direct', 'profit_component']), 'product_code'])
coverage = {'spot': len(spot), 'inventory': len(inventory), 'profit': len(profit)}
print(coverage)
assert coverage['spot'] == 9
assert coverage['inventory'] >= 8
assert coverage['profit'] >= 7
PY
```

Expected: all assertions pass. If they fail, return to Task 5 with new evidence; do not lower thresholds or use proxies.

## Task 7: Verify a January 2025 recovery smoke package

**Artifacts:**
- Create remote under: `D:\marketmonitor\fundamentals\artifacts\`
- Copy local: `/tmp/cta-fundamentals-resumption-20260813/smoke-manifest.json`
- Record local: `/tmp/cta-fundamentals-resumption-20260813/smoke-manifest-hash.txt`

- [ ] **Step 1: Generate and record the smoke UUID**

```bash
smoke_run_id=$(/home/elfbob/claude-code/futures_strategies/.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')
printf '%s\n' "$smoke_run_id" | tee /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt
```

Expected: one valid UUID, reused from the file in all remaining commands.

- [ ] **Step 2: Capture January 2025**

```bash
smoke_run_id=$(cat /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt)
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\artifacts' | Out-Null; $env:PYTHONPATH='D:\marketmonitor\fundamentals'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m commodity_fundamentals.capture --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2025-01-01 --end 2025-01-31 --mode backfill --artifact-dir 'D:\marketmonitor\fundamentals\artifacts' --run-id '$smoke_run_id'\""
```

Expected: exit 0 and every active-series/year chunk is complete.

- [ ] **Step 3: Open it with the production recovery validator**

```bash
smoke_run_id=$(cat /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt)
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"$env:PYTHONPATH='D:\marketmonitor\fundamentals'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -c \"from pathlib import Path; from commodity_fundamentals.catalog import load_catalog; from commodity_fundamentals.recovery import RecoveryPackage; c=load_catalog(Path(r'D:\marketmonitor\fundamentals\catalog-v1.yaml')); p=RecoveryPackage.open(Path(r'D:\marketmonitor\fundamentals\artifacts\$smoke_run_id')); active=[s for s in c.series if s.active]; assert all(p.is_complete(s.series_id,2025) for s in active); print('run_id',p.manifest.run_id,'active_series',len(active),'complete',True)\"\""
```

Expected: `complete True` and matching run ID. Hash, identity, partial-status, or trading-date-order errors block the task.

- [ ] **Step 4: Copy and audit only the manifest**

```bash
smoke_run_id=$(cat /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt)
scp -P 2222 "ghls@100.120.152.1:D:/marketmonitor/fundamentals/artifacts/$smoke_run_id/manifest.json" /tmp/cta-fundamentals-resumption-20260813/smoke-manifest.json
sha256sum /tmp/cta-fundamentals-resumption-20260813/smoke-manifest.json | tee /tmp/cta-fundamentals-resumption-20260813/smoke-manifest-hash.txt
/home/elfbob/claude-code/futures_strategies/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from uuid import UUID

root = Path('/tmp/cta-fundamentals-resumption-20260813')
manifest = json.loads((root / 'smoke-manifest.json').read_text())
run_id = (root / 'smoke-run-id.txt').read_text().strip()
assert str(UUID(run_id)) == run_id
assert manifest['run_id'] == run_id
assert manifest['mode'] == 'backfill'
assert manifest['requested_start'] == '2025-01-01'
assert manifest['requested_end'] == '2025-01-31'
assert manifest['chunks']
assert all(c['status'] == 'complete' for c in manifest['chunks'])
assert all(int(c['row_count']) > 0 for c in manifest['chunks'])
print({'run_id': run_id, 'chunks': len(manifest['chunks']), 'rows': sum(int(c['row_count']) for c in manifest['chunks'])})
PY
```

Expected: all assertions pass. Raw chunks stay on Windows and are not committed.

## Task 8: Commit the evidence handoff and write the production approval packet

**Files:**
- Create: `.worktrees/market-monitor-commodity-vertical-slice/docs/data/commodity-fundamentals/catalog-v1.yaml`
- Create: `.worktrees/market-monitor-commodity-vertical-slice/docs/data/commodity-fundamentals/catalog-v1-evidence.csv`
- Create: `.worktrees/market-monitor-commodity-vertical-slice/docs/data/commodity-fundamentals/catalog-v1-preflight.csv`
- Modify: `.worktrees/market-monitor-commodity-vertical-slice/docs/operations/commodity-fundamentals-wind.md`
- Create: `.worktrees/market-monitor-commodity-vertical-slice/docs/operations/commodity-fundamentals-production-approval.md`

- [ ] **Step 1: Copy only approved non-raw artifacts into the branch**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
cp /tmp/cta-fundamentals-resumption-20260813/catalog-v1.yaml docs/data/commodity-fundamentals/catalog-v1.yaml
cp /tmp/cta-fundamentals-resumption-20260813/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-evidence.csv
cp /tmp/cta-fundamentals-resumption-20260813/catalog-v1-preflight.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv
```

Expected: only metadata and aggregate counts are copied; no raw observations, credentials, connection strings, or tokens.

- [ ] **Step 2: Update the Wind runbook from recorded output**

Record deploy-source SHA, bundle SHA, catalog version/config hash/SHA, 9/8/7 counts, report SHA, smoke UUID/manifest SHA/chunk and row counts, Windows backup paths, and rollback instructions. State explicitly that no production DDL, writer deploy, upload, full-history capture, or CTA comparison occurred.

- [ ] **Step 3: Write, but do not execute, the production approval packet**

Include the read-only proof that production tables are absent and `sync_rows=0`; exact database backup destination; DDL order/transactions/verification/rollback; writer diff/deploy/restart/health/rollback; catalog uploader command; duplicate and row-count checks; a request to run the disposable-schema transaction test; and an explicit stop before full-history capture and CTA comparison.

- [ ] **Step 4: Verify all source and evidence**

```bash
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
git status --short
rg -n "password|passwd|secret|token|postgresql://|raw_value" docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
```

Expected: Ruff exits 0, pytest reports at least `327 passed`, whitespace is clean, and any sensitive/raw-data scan hit is resolved before staging.

- [ ] **Step 5: Commit exactly the handoff files**

```bash
git add docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
git diff --cached --check
git diff --cached --name-only
git commit -m "docs: approve Wind fundamentals catalog handoff"
```

Expected: exactly the five named files are committed.

- [ ] **Step 6: Stop at the production decision gate**

Report the commit SHA, test results, hashes, coverage, smoke UUID/manifest hash, Windows rollback paths, and approval-packet path. Request explicit approval for a new production plan. Do not begin backup, DDL, writer deploy, upload, full-history capture, aggregate build, or CTA comparison.

## Completion criteria

- Ruff and the complete upstream commodity-fundamentals suite are green.
- The live collector is snapshotted and unexplained source drift is absent.
- The reviewed package runs in its isolated Windows Python 3.11 venv with PyYAML 6.0.3 and Wind connected.
- Catalog rows have Wind evidence and explicit user confirmation.
- Coverage is spot 9/9, inventory at least 8/9, and profit at least 7/9.
- The January 2025 package opens through `RecoveryPackage`; all chunks and hashes validate.
- Only catalog metadata, evidence summaries, preflight summaries, and manifest aggregates are committed.
- The production approval packet is complete and production remains unchanged.
