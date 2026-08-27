# CTA Six-Factor Fundamentals Wind Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a user-approved Wind catalog and versioned profit formulas, pass the 9/8/7 gate, verify a January 2025 recovery package, and prepare the evidence for a separate production-change approval.

**Architecture:** Windows owns licensed Wind calls and raw artifacts; Linux owns reviewed code, catalog/formula review, tests, and aggregate evidence; Debian production stays unchanged. Each boundary is fail-closed and uses observed commit IDs, run IDs, and hashes.

**Tech Stack:** Python 3.11, WindPy, pandas, PyYAML 6.0.3, PowerShell/OpenSSH, pytest, Ruff, Git.

---

## Scope

- Design authority: `/home/elfbob/claude-code/futures_strategies/docs/superpowers/specs/2026-08-13-cta-fundamentals-resumption-design.md`
- Upstream worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice`
- Futures worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals`
- Windows SSH: `ssh -p 2222 ghls@100.120.152.1`
- Windows collector: `D:\marketmonitor\realtime_upgrade`
- Windows fundamentals: `D:\marketmonitor\fundamentals`
- Local evidence: `/tmp/cta-fundamentals-resumption-20260813`

This plan does not perform production backup/DDL, writer deployment, catalog/recovery upload, full-history capture, aggregate build, CTA comparison, or branch merge. Each requires a later explicit decision.

## Task 1: Freeze clean baselines

**Files:** read both worktrees; create only `/tmp/cta-fundamentals-resumption-20260813/*-head.txt`.

- [ ] **Step 1: Record repository state and preserve unrelated work**

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short
git log -3 --oneline
mkdir -p /tmp/cta-fundamentals-resumption-20260813
```

Expected: design commit `eb01e97` is present. Do not edit, stage, or remove unrelated Carry files or other user work.

- [ ] **Step 2: Assert both implementation worktrees are clean**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
test -z "$(git status --short)"
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/upstream-head.txt
git log --oneline /home/elfbob/market-monitor/main..HEAD

cd /home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals
test -z "$(git status --short)"
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/futures-head.txt
```

Expected: upstream is on `feature/commodity-fundamentals-vertical-slice` at reviewed commit `3b38f41` or a reviewed descendant; futures is on `feature/commodity-fundamentals` and includes lineage commit `0a0e383` or a documented descendant.

## Task 2: Restore a green upstream baseline

**Files:**
- Modify: `commodity_fundamentals/availability.py:5`
- Modify: `tests/commodity_fundamentals/test_profit.py:4`
- Test: `tests/commodity_fundamentals/`

- [ ] **Step 1: Reproduce the known static failures**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
```

Expected: exactly two `F401` failures: unused `pandas as pd` in `availability.py`, and unused `date` in `test_profit.py`. Stop if any additional failure appears.

- [ ] **Step 2: Apply the minimal cleanup**

Use `apply_patch` to delete `import pandas as pd` and change:

```python
from datetime import date, datetime, timezone
```

to:

```python
from datetime import datetime, timezone
```

- [ ] **Step 3: Verify and commit**

```bash
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
git add commodity_fundamentals/availability.py tests/commodity_fundamentals/test_profit.py
git commit -m "chore: clean commodity fundamentals imports"
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt
```

Expected: Ruff exits 0, pytest reports `327 passed`, and one two-file commit is created.

## Task 3: Snapshot and compare the live collector

**Artifacts:** remote ZIP `D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip`; matching local ZIP/hash/diff under the evidence directory.

- [ ] **Step 1: Refuse overwrite, snapshot, transfer, and verify hash**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip') { throw 'snapshot target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\snapshots' | Out-Null; Compress-Archive -Path 'D:\marketmonitor\realtime_upgrade\*' -DestinationPath 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip'; Get-FileHash -Algorithm SHA256 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip' | Format-List Hash,Path\"" | tee /tmp/cta-fundamentals-resumption-20260813/collector-remote-hash.txt
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/snapshots/wind-data-collecter-live-20260813.zip' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/collector-local-hash.txt
```

Expected: hashes match. If the remote target exists, inspect it and consistently choose another date-stamped name.

- [ ] **Step 2: Compare tracked source, excluding runtime/secret files**

```bash
mkdir -p /tmp/cta-fundamentals-resumption-20260813/collector-compare
unzip -q /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip -d /tmp/cta-fundamentals-resumption-20260813/collector-compare
diff -ru --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' --exclude=config.yaml --exclude=watchlist.xlsx /home/elfbob/market-monitor/data-collecter/realtime_upgrade /tmp/cta-fundamentals-resumption-20260813/collector-compare | tee /tmp/cta-fundamentals-resumption-20260813/collector-source.diff
```

Expected: no unexplained Python or SQL drift. Reconcile drift in a separately reviewed commit before continuing.

## Task 4: Deploy the reviewed package into an isolated Windows venv

**Artifacts:** local deployment ZIP; remote backup `D:\marketmonitor\fundamentals-backups\pre-resume-20260813`; retained old package; `D:\marketmonitor\fundamentals\.venv`.

- [ ] **Step 1: Build from the exact green commit**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
test "$(git rev-parse HEAD)" = "$(cat /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt)"
test -z "$(git status --short)"
zip -rq /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip commodity_fundamentals -x '*__pycache__*' '*.pyc'
sha256sum /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/deploy-bundle-hash.txt
```

- [ ] **Step 2: Back up, transfer, hash-check, and activate without deletion**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813') { throw 'backup target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals-backups' | Out-Null; Copy-Item -Recurse 'D:\marketmonitor\fundamentals' 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813'; New-Item -ItemType Directory -Force 'D:\marketmonitor\incoming' | Out-Null\""
scp -P 2222 /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip ghls@100.120.152.1:'D:/marketmonitor/incoming/'
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Get-FileHash -Algorithm SHA256 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' | Format-List Hash,Path; if (Test-Path 'D:\marketmonitor\fundamentals-stage-20260813') { throw 'stage target already exists' }; Expand-Archive 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' 'D:\marketmonitor\fundamentals-stage-20260813'; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813') { throw 'retained target already exists' }; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals') { Move-Item 'D:\marketmonitor\fundamentals\commodity_fundamentals' 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813' }; Move-Item 'D:\marketmonitor\fundamentals-stage-20260813\commodity_fundamentals' 'D:\marketmonitor\fundamentals\commodity_fundamentals'\"" | tee /tmp/cta-fundamentals-resumption-20260813/deploy-remote-hash.txt
```

Expected: remote/local hashes match and both rollback copies exist.

- [ ] **Step 3: Create the venv and verify timezone, Wind, and three CLIs**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"py -3.11 -m venv --system-site-packages 'D:\marketmonitor\fundamentals\.venv'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m pip install PyYAML==6.0.3; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -c \"import pandas,yaml; from zoneinfo import ZoneInfo; from WindPy import w; r=w.start(); print('yaml',yaml.__version__,'pandas',pandas.__version__,'tz',ZoneInfo('Asia/Shanghai'),'wind',r.ErrorCode,w.isconnected())\"; & '.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --help; & '.venv\Scripts\python.exe' -m commodity_fundamentals.capture --help; & '.venv\Scripts\python.exe' -m commodity_fundamentals.uploader --help\""
```

Expected: PyYAML `6.0.3`, timezone `Asia/Shanghai`, Wind error `0`/connected `True`, and all help commands exit 0. Install no other dependency and make no source call if Wind fails.

## Task 5: Build catalog and formula evidence

**Files:**
- Read: `docs/data/commodity-fundamentals/catalog-v1-pending.csv`
- Create runtime: `D:\marketmonitor\fundamentals\catalog-v1.yaml`
- Create runtime: `D:\marketmonitor\fundamentals\profit-formulas-v1.yaml`
- Create local: `/tmp/cta-fundamentals-resumption-20260813/catalog-v1-evidence.csv`

- [ ] **Step 1: Discover candidates without inference**

For every `M/RB/CU/AL/TA/PP/MA/BU/RU`, inspect Wind terminal/API candidates for spot, inventory, and direct profit or every reviewed formula component. Legacy database labels are locators only, never proof.

- [ ] **Step 2: Record aggregate evidence**

Use these exact CSV columns:

```text
product_code,metric_role,series_id,source_code,source_name,frequency,api_method,field_name,source_unit,target_unit,currency,scale_multiplier,aggregation_rule,date_semantics,release_calendar_days,release_time,unknown_time_policy,max_staleness_trading_days,valid_from,valid_to,first_date,last_date,point_count,evidence_method,evidence_timestamp,decision
```

`decision` is `accept` or `reject`. Store metadata/date range/counts only, not raw series values.

- [ ] **Step 3: Obtain explicit user confirmation**

Present accepted rows grouped by product. Confirm every code, name, role, frequency, API/field, unit/currency/conversion, aggregation, date/release rule, staleness, validity interval, and each formula coefficient/fixed cost/output unit/effective interval. Keep unproved roles uncovered; do not use proxies.

- [ ] **Step 4: Create exact contract files from confirmed evidence**

Catalog root is exactly `catalog_version` plus `series`; each series has the exact fields enforced by `SeriesSpec.from_mapping`. `edb` has empty `field_name`; `wsd` has a nonempty field; AU/AG are absent.

Formula root is `formulas`; each row has `formula_id`, `formula_version`, `product_code`, nonempty `components` of `series_id`/`coefficient`, `fixed_cost`, `output_unit`, `effective_from`, and `effective_to`.

- [ ] **Step 5: Validate without invoking the database-backed main CLI**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -c \"from pathlib import Path; from commodity_fundamentals.catalog import load_catalog; c=load_catalog(Path(r'D:\marketmonitor\fundamentals\catalog-v1.yaml')); print(c.catalog_version,c.config_hash,len(c.active_series()))\"; & '.venv\Scripts\python.exe' -m commodity_fundamentals.profit --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --formulas 'D:\marketmonitor\fundamentals\profit-formulas-v1.yaml' --validate-only\""
```

Expected: catalog version/hash/count and formula-covered products print; contract errors stop the task. Do not use `python -m commodity_fundamentals load-catalog` here because it is a database mutation command.

## Task 6: Run preflight and enforce 9/8/7

- [ ] **Step 1: Run 2016-01-01 through 2026-08-13 and retrieve summaries**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\reports' | Out-Null; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2016-01-01 --end 2026-08-13 --output 'D:\marketmonitor\fundamentals\reports\catalog-v1-preflight.csv'\""
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/catalog-v1.yaml' /tmp/cta-fundamentals-resumption-20260813/
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/profit-formulas-v1.yaml' /tmp/cta-fundamentals-resumption-20260813/
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/reports/catalog-v1-preflight.csv' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/catalog-v1.yaml /tmp/cta-fundamentals-resumption-20260813/profit-formulas-v1.yaml /tmp/cta-fundamentals-resumption-20260813/catalog-v1-preflight.csv | tee /tmp/cta-fundamentals-resumption-20260813/preflight-artifact-hashes.txt
```

- [ ] **Step 2: Check report identity and complete-formula coverage**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
PYTHONPATH=. /home/elfbob/market-monitor/venv/bin/python - <<'PY'
from pathlib import Path
import pandas as pd
import yaml
from commodity_fundamentals.catalog import load_catalog
from commodity_fundamentals.profit import Formula, validate_formula_set

root = Path('/tmp/cta-fundamentals-resumption-20260813')
catalog = load_catalog(root / 'catalog-v1.yaml')
payload = yaml.safe_load((root / 'profit-formulas-v1.yaml').read_text())
formulas = tuple(Formula.from_mapping(row) for row in payload['formulas'])
validate_formula_set(formulas, catalog)
report = pd.read_csv(root / 'catalog-v1-preflight.csv')
expected = ['series_id','product_code','metric_role','source_code','source_name','frequency','source_unit','target_unit','currency','first_date','last_date','point_count','missing_count','nonpositive_count']
assert report.columns.tolist() == expected
assert report['series_id'].is_unique
assert set(report['series_id']) == {s.series_id for s in catalog.active_series()}
usable = report.loc[report.point_count.fillna(0).astype(int) > 0]
usable_ids = set(usable.series_id)
spot = set(usable.loc[usable.metric_role == 'spot', 'product_code'])
inventory = set(usable.loc[usable.metric_role == 'inventory', 'product_code'])
profit = set(usable.loc[usable.metric_role == 'profit_direct', 'product_code'])
profit |= {f.product_code for f in formulas if all(c.series_id in usable_ids for c in f.components)}
coverage = {'spot': len(spot), 'inventory': len(inventory), 'profit': len(profit)}
print(coverage)
assert coverage['spot'] == 9
assert coverage['inventory'] >= 8
assert coverage['profit'] >= 7
PY
```

Expected: assertions pass. On failure, return to Task 5; never reduce thresholds or substitute proxies.

## Task 7: Capture and validate January 2025

- [ ] **Step 1: Generate a stable UUID and capture**

```bash
smoke_run_id=$(/home/elfbob/claude-code/futures_strategies/.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')
printf '%s\n' "$smoke_run_id" | tee /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\artifacts' | Out-Null; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -m commodity_fundamentals.capture --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2025-01-01 --end 2025-01-31 --mode backfill --artifact-dir 'D:\marketmonitor\fundamentals\artifacts' --run-id '$smoke_run_id'\""
```

- [ ] **Step 2: Validate every active-series chunk through `RecoveryPackage`**

```bash
smoke_run_id=$(cat /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt)
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -c \"from pathlib import Path; from commodity_fundamentals.catalog import load_catalog; from commodity_fundamentals.recovery import RecoveryPackage; c=load_catalog(Path(r'D:\marketmonitor\fundamentals\catalog-v1.yaml')); p=RecoveryPackage.open(Path(r'D:\marketmonitor\fundamentals\artifacts\$smoke_run_id')); active=c.active_series(); assert all(p.is_complete(s.series_id,2025) for s in active); print(p.manifest['run_id'],len(active),True)\"\""
```

Expected: matching UUID and `True`; identity, order, status, path, and file hashes validate.

- [ ] **Step 3: Copy and audit only the manifest**

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
chunks = list(manifest['chunks'].values())
assert chunks
assert all(chunk['status'] == 'complete' for chunk in chunks)
assert all(int(chunk['row_count']) > 0 for chunk in chunks)
print({'run_id': run_id, 'chunks': len(chunks), 'rows': sum(int(c['row_count']) for c in chunks)})
PY
```

Expected: assertions pass. Raw chunks remain on Windows and never enter Git.

## Task 8: Commit evidence and prepare the production decision gate

**Files:**
- Create: `docs/data/commodity-fundamentals/catalog-v1.yaml`
- Create: `docs/data/commodity-fundamentals/profit-formulas-v1.yaml`
- Create: `docs/data/commodity-fundamentals/catalog-v1-evidence.csv`
- Create: `docs/data/commodity-fundamentals/catalog-v1-preflight.csv`
- Modify: `docs/operations/commodity-fundamentals-wind.md`
- Create: `docs/operations/commodity-fundamentals-production-approval.md`

- [ ] **Step 1: Add validated non-raw artifacts with `apply_patch`**

Use the exact validated contents from Tasks 5–7. Do not use shell copy/write commands. Confirm the files contain metadata and aggregate counts only.

- [ ] **Step 2: Write exact handoff and approval evidence**

Record deploy commit/bundle hash, catalog version/config/hash, formula hash, 9/8/7 counts, report hash, smoke UUID/manifest hash/chunk/row counts, and Windows backup/rollback paths. The production packet must contain fresh read-only absence/sync proof; writer diff; database backup destination; DDL order/transaction/verification/rollback; upload/idempotence checks; and the exact request to authorize a disposable-schema transaction test. It must state that no production action has run.

- [ ] **Step 3: Verify and commit exactly six files**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
rg -n "password|passwd|secret|token|postgresql://|raw_value" docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/profit-formulas-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
git add docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/profit-formulas-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
git diff --cached --check
git diff --cached --name-only
git commit -m "docs: approve Wind fundamentals catalog handoff"
```

Expected: Ruff passes, pytest reports at least `327 passed`, the scan has no unresolved sensitive/raw-data hit, and exactly six named files are committed.

- [ ] **Step 4: Stop**

Report the commit SHA, tests, all hashes, coverage, smoke UUID, and rollback paths. Request separate approval for the disposable-schema transaction test and subsequent production plan. Do not start production backup, DDL, writer deployment, upload, full history, build, CTA comparison, or merge.

## Completion criteria

- Green Ruff and full upstream fundamentals tests.
- Snapshot with no unexplained collector source drift.
- Isolated Windows Python 3.11/PyYAML 6.0.3 environment with timezone, Wind, and all three CLIs verified.
- User-confirmed catalog/formulas backed by direct Wind evidence.
- Spot 9/9, inventory at least 8/9, and complete direct/formula profit at least 7/9.
- January 2025 package passes `RecoveryPackage` identity and checksum validation.
- Only non-raw evidence is committed.
- Production remains unchanged with a concrete approval packet ready.
