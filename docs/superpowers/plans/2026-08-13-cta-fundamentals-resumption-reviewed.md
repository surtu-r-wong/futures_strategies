# CTA Six-Factor Fundamentals Resumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Approve a real Wind fundamentals catalog, pass the 9/8/7 coverage gate, verify a January 2025 recovery package, and prepare—but not execute—the production-change approval packet.

**Architecture:** Windows owns Wind evidence and raw recovery artifacts; Linux owns code, catalog review, tests, and evidence summaries; Debian production remains read-only throughout this plan. Every stage is fail-closed and records commit IDs, run IDs, and hashes from actual output.

**Tech Stack:** Python 3.11, WindPy, pandas, PyYAML 6.0.3, PowerShell/OpenSSH, pytest, Ruff, Git.

---

## Boundaries and paths

- Design: `/home/elfbob/claude-code/futures_strategies/docs/superpowers/specs/2026-08-13-cta-fundamentals-resumption-design.md`
- Upstream worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice`
- Futures worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals`
- Windows SSH: `ssh -p 2222 ghls@100.120.152.1`
- Windows collector: `D:\marketmonitor\realtime_upgrade`
- Windows fundamentals root: `D:\marketmonitor\fundamentals`
- Evidence root: `/tmp/cta-fundamentals-resumption-20260813`

This plan does not perform production backup/DDL, writer deployment, uploader execution, full-history capture, aggregate build, or CTA comparison. Those require a separately approved follow-on plan.

## Task 1: Record clean baselines

**Files:** read both worktrees; create only `/tmp/cta-fundamentals-resumption-20260813/*-head.txt`.

- [ ] Run:

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short
git log -3 --oneline
mkdir -p /tmp/cta-fundamentals-resumption-20260813

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

Expected: design commit `eb01e97` is visible; upstream is clean on `feature/commodity-fundamentals-vertical-slice` at `3b38f41` or a reviewed descendant; futures is clean on `feature/commodity-fundamentals` and contains `0a0e383` or a documented descendant. Preserve all unrelated user files.

## Task 2: Make the upstream baseline fully green

**Files:**
- Modify: `commodity_fundamentals/availability.py:5`
- Modify: `tests/commodity_fundamentals/test_profit.py:4`
- Test: `tests/commodity_fundamentals/`

- [ ] Reproduce exactly two unused-import failures:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
```

Expected: unused `pandas as pd` in `availability.py` and unused `date` in `test_profit.py`; stop if anything else appears.

- [ ] Use `apply_patch` to delete `import pandas as pd` and change:

```python
from datetime import date, datetime, timezone
```

to:

```python
from datetime import datetime, timezone
```

- [ ] Verify and commit:

```bash
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
git add commodity_fundamentals/availability.py tests/commodity_fundamentals/test_profit.py
git commit -m "chore: clean commodity fundamentals imports"
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt
```

Expected: Ruff exits 0 and pytest reports `327 passed`.

## Task 3: Snapshot and diff the live collector

**Artifacts:** remote `D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip`; local matching archive/hash/diff under the evidence root.

- [ ] Refuse overwrite, create the snapshot, transfer it, and compare hashes:

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip') { throw 'snapshot target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\snapshots' | Out-Null; Compress-Archive -Path 'D:\marketmonitor\realtime_upgrade\*' -DestinationPath 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip'; Get-FileHash -Algorithm SHA256 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip' | Format-List Hash,Path\"" | tee /tmp/cta-fundamentals-resumption-20260813/collector-remote-hash.txt
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/snapshots/wind-data-collecter-live-20260813.zip' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/collector-local-hash.txt
```

Expected: hashes match. If the target already exists, inspect it and consistently choose another date-stamped name.

- [ ] Diff code, excluding runtime and secret-bearing files:

```bash
mkdir -p /tmp/cta-fundamentals-resumption-20260813/collector-compare
unzip -q /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip -d /tmp/cta-fundamentals-resumption-20260813/collector-compare
diff -ru --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' --exclude=config.yaml --exclude=watchlist.xlsx /home/elfbob/market-monitor/data-collecter/realtime_upgrade /tmp/cta-fundamentals-resumption-20260813/collector-compare | tee /tmp/cta-fundamentals-resumption-20260813/collector-source.diff
```

Expected: no unexplained Python/SQL drift. Reconcile drift in a separately reviewed commit before continuing.

## Task 4: Deploy the reviewed package to an isolated Wind environment

**Artifacts:** local bundle; remote immutable backup `D:\marketmonitor\fundamentals-backups\pre-resume-20260813`; retained old package; venv `D:\marketmonitor\fundamentals\.venv`.

- [ ] Build from the recorded green commit:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
test "$(git rev-parse HEAD)" = "$(cat /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt)"
test -z "$(git status --short)"
zip -rq /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip commodity_fundamentals -x '*__pycache__*' '*.pyc'
sha256sum /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/deploy-bundle-hash.txt
```

- [ ] Back up, transfer, hash-check, and activate without deleting old files:

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813') { throw 'backup target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals-backups' | Out-Null; Copy-Item -Recurse 'D:\marketmonitor\fundamentals' 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813'; New-Item -ItemType Directory -Force 'D:\marketmonitor\incoming' | Out-Null\""
scp -P 2222 /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip ghls@100.120.152.1:'D:/marketmonitor/incoming/'
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Get-FileHash -Algorithm SHA256 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' | Format-List Hash,Path; if (Test-Path 'D:\marketmonitor\fundamentals-stage-20260813') { throw 'stage target already exists' }; Expand-Archive 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' 'D:\marketmonitor\fundamentals-stage-20260813'; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813') { throw 'retained target already exists' }; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals') { Move-Item 'D:\marketmonitor\fundamentals\commodity_fundamentals' 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813' }; Move-Item 'D:\marketmonitor\fundamentals-stage-20260813\commodity_fundamentals' 'D:\marketmonitor\fundamentals\commodity_fundamentals'\"" | tee /tmp/cta-fundamentals-resumption-20260813/deploy-remote-hash.txt
```

Expected: remote and local bundle hashes match; backup and retained package both exist.

- [ ] Create the venv and verify Wind/CLIs:

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"py -3.11 -m venv --system-site-packages 'D:\marketmonitor\fundamentals\.venv'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m pip install PyYAML==6.0.3; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -c \"import pandas,yaml; from WindPy import w; r=w.start(); print('yaml',yaml.__version__,'pandas',pandas.__version__,'wind',r.ErrorCode,w.isconnected())\"; & '.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --help; & '.venv\Scripts\python.exe' -m commodity_fundamentals.capture --help\""
```

Expected: PyYAML `6.0.3`, Wind error code `0`/connected `True`, and both CLIs exit 0. Install no other dependency and make no data request if Wind fails.

## Task 5: Establish the real catalog from Wind evidence

**Files:** read `docs/data/commodity-fundamentals/catalog-v1-pending.csv` only as a checklist; create runtime `D:\marketmonitor\fundamentals\catalog-v1.yaml` and local aggregate evidence CSV.

- [ ] For each of `M/RB/CU/AL/TA/PP/MA/BU/RU`, inspect Wind candidates for spot, inventory, and direct profit or all reviewed formula components. Never infer a Wind code from legacy database labels.

- [ ] Record one evidence row per candidate with exact columns:

```text
product_code,metric_role,series_id,source_code,source_name,frequency,api_method,field_name,source_unit,target_unit,currency,scale_multiplier,aggregation_rule,date_semantics,release_calendar_days,release_time,unknown_time_policy,max_staleness_trading_days,valid_from,valid_to,first_date,last_date,point_count,evidence_method,evidence_timestamp,decision
```

Only `accept` or `reject` is valid in `decision`. Store metadata and aggregate counts, never raw values.

- [ ] Present accepted rows grouped by product and pause for explicit user confirmation of every field, unit, conversion, date/release rule, staleness rule, and validity interval. Missing credible evidence remains missing; do not proxy it.

- [ ] After confirmation, create YAML with root keys `catalog_version` and `series`. Each series uses the exact `SeriesSpec` fields in `commodity_fundamentals/models.py`; EDB has empty `field_name`, WSD has a nonempty field, and AU/AG are excluded.

- [ ] Validate:

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -m commodity_fundamentals load-catalog --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml'\""
```

Expected: version/config hash prints and the loader exits 0; any contract error blocks preflight.

## Task 6: Enforce the 9/8/7 preflight gate

- [ ] Run and retrieve only aggregate artifacts:

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\reports' | Out-Null; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2016-01-01 --end 2026-08-13 --output 'D:\marketmonitor\fundamentals\reports\catalog-v1-preflight.csv'\""
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/catalog-v1.yaml' /tmp/cta-fundamentals-resumption-20260813/
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/fundamentals/reports/catalog-v1-preflight.csv' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/catalog-v1.yaml /tmp/cta-fundamentals-resumption-20260813/catalog-v1-preflight.csv | tee /tmp/cta-fundamentals-resumption-20260813/preflight-artifact-hashes.txt
```

- [ ] Use a read-only Python check to assert the report has exactly the columns defined by `preflight.py`, unique active `series_id` values, spot coverage `9`, inventory coverage at least `8`, and `profit_direct|profit_component` coverage at least `7`.

Expected: all assertions pass. Return to Task 5 on failure; never reduce thresholds or use proxies.

## Task 7: Capture and validate January 2025

- [ ] Generate a UUID, capture, and validate through `RecoveryPackage`:

```bash
smoke_run_id=$(/home/elfbob/claude-code/futures_strategies/.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')
printf '%s\n' "$smoke_run_id" | tee /tmp/cta-fundamentals-resumption-20260813/smoke-run-id.txt
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals\artifacts' | Out-Null; Set-Location 'D:\marketmonitor\fundamentals'; & '.venv\Scripts\python.exe' -m commodity_fundamentals.capture --catalog 'D:\marketmonitor\fundamentals\catalog-v1.yaml' --start 2025-01-01 --end 2025-01-31 --mode backfill --artifact-dir 'D:\marketmonitor\fundamentals\artifacts' --run-id '$smoke_run_id'; & '.venv\Scripts\python.exe' -c \"from pathlib import Path; from commodity_fundamentals.catalog import load_catalog; from commodity_fundamentals.recovery import RecoveryPackage; c=load_catalog(Path(r'D:\marketmonitor\fundamentals\catalog-v1.yaml')); p=RecoveryPackage.open(Path(r'D:\marketmonitor\fundamentals\artifacts\$smoke_run_id')); active=[s for s in c.series if s.active]; assert all(p.is_complete(s.series_id,2025) for s in active); print(p.manifest.run_id,len(active),True)\"\""
```

Expected: matching UUID and `True`; all chunks, identities, hashes, statuses, and trading-date ordering validate.

- [ ] Copy only `manifest.json`, hash it, and assert locally that run ID/mode/dates match, every chunk is `complete`, and every `row_count` is positive. Print aggregate chunks and rows. Raw chunks remain on Windows.

## Task 8: Commit evidence and stop at the production gate

**Files:** add approved catalog/evidence/preflight summaries; update `docs/operations/commodity-fundamentals-wind.md`; add `docs/operations/commodity-fundamentals-production-approval.md`.

- [ ] Use `apply_patch` to add the exact contents of the validated catalog, evidence CSV, and preflight CSV. Their contents come from Tasks 5–7 and therefore must not be guessed in advance.

- [ ] Update the Wind runbook with recorded commit/hash/coverage/smoke/rollback values. Write the production approval packet with current read-only absence/sync proof, exact backup target, DDL transaction/verification/rollback, writer diff/deploy/health/rollback, upload and duplicate checks, and a request to run a disposable-schema transaction test. Execute none of it.

- [ ] Verify and commit:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
rg -n "password|passwd|secret|token|postgresql://|raw_value" docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
git add docs/data/commodity-fundamentals/catalog-v1.yaml docs/data/commodity-fundamentals/catalog-v1-evidence.csv docs/data/commodity-fundamentals/catalog-v1-preflight.csv docs/operations/commodity-fundamentals-wind.md docs/operations/commodity-fundamentals-production-approval.md
git diff --cached --check
git diff --cached --name-only
git commit -m "docs: approve Wind fundamentals catalog handoff"
```

Expected: Ruff passes, at least `327 passed`, no sensitive/raw artifact is staged, and exactly five named files are committed.

- [ ] Report commit SHA, all tests, catalog/config/report hashes, coverage, smoke UUID/manifest hash, and Windows rollback paths. Request approval for the production follow-on plan and stop.

## Completion criteria

- Green Ruff and full upstream fundamentals tests.
- Snapshotted collector with no unexplained source drift.
- Isolated Windows Python 3.11 environment with PyYAML 6.0.3 and connected Wind.
- User-confirmed catalog with direct Wind evidence.
- Coverage: spot 9/9, inventory at least 8/9, profit at least 7/9.
- January 2025 recovery package passes `RecoveryPackage` integrity validation.
- Only non-raw evidence is committed.
- Production remains unchanged and has a complete approval packet.
