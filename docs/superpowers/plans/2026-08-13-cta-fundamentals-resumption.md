# CTA Six-Factor Fundamentals Resumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a verified, user-approved Wind commodity-fundamentals catalog, pass the 9/8/7 coverage gate, complete a January 2025 recovery smoke run, and assemble the evidence needed to request a separate production-change approval.

**Architecture:** Keep Windows Wind collection, Linux catalog review, and Debian production deployment as three fail-closed stages. This plan changes only the clean upstream implementation branch and the isolated Windows fundamentals directory. It does not alter production PostgreSQL, deploy the production writer, upload recovery data, run full-history capture, or switch CTA research to six-factor mode.

**Tech Stack:** Python 3.11, pandas, PyYAML 6.0.3, WindPy, PowerShell/OpenSSH, pytest, Ruff, Git.

---

## Scope and repository map

- Design authority: `docs/superpowers/specs/2026-08-13-cta-fundamentals-resumption-design.md`
- Main repository: `/home/elfbob/claude-code/futures_strategies`
- Upstream implementation worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice`
- Upstream branch: `feature/commodity-fundamentals-vertical-slice`
- Futures integration worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals`
- Windows SSH: `ssh -p 2222 ghls@100.120.152.1`
- Windows collector: `D:\marketmonitor\realtime_upgrade`
- Windows fundamentals root: `D:\marketmonitor\fundamentals`
- Production host: `100.65.111.79`

Production backup/DDL, writer deployment, upload, full-history capture, aggregate build, and CTA comparison require a newly approved follow-on plan.

## Task 1: Freeze the three starting baselines

**Files:**
- Read: `docs/superpowers/specs/2026-08-13-cta-fundamentals-resumption-design.md`
- Read: `.worktrees/market-monitor-commodity-vertical-slice/`
- Read: `.worktrees/commodity-fundamentals/`
- Create outside Git: `/tmp/cta-fundamentals-resumption-20260813/`

- [ ] **Step 1: Confirm the main repository state**

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short
git log -3 --oneline
```

Expected: `eb01e97 docs: design CTA fundamentals resumption` is present. Preserve the unrelated Carry plan files and all other user-owned changes.

- [ ] **Step 2: Confirm both implementation worktrees are clean**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
git status --short
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git log --oneline /home/elfbob/market-monitor/main..HEAD

cd /home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals
git status --short
git rev-parse --abbrev-ref HEAD
git log --oneline --decorate -10
```

Expected: upstream is clean on `feature/commodity-fundamentals-vertical-slice` at reviewed commit `3b38f41` or a reviewed descendant; futures integration is clean on `feature/commodity-fundamentals` and contains the lineage ending at `0a0e383` or a documented descendant.

- [ ] **Step 3: Record exact starting SHAs**

```bash
mkdir -p /tmp/cta-fundamentals-resumption-20260813
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/upstream-head.txt
cd /home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/futures-head.txt
```

Expected: each evidence file contains one 40-character SHA.

## Task 2: Restore a green upstream implementation baseline

**Files:**
- Modify: `.worktrees/market-monitor-commodity-vertical-slice/commodity_fundamentals/availability.py:5`
- Modify: `.worktrees/market-monitor-commodity-vertical-slice/tests/commodity_fundamentals/test_profit.py:4`
- Test: `.worktrees/market-monitor-commodity-vertical-slice/tests/commodity_fundamentals/`

- [ ] **Step 1: Reproduce the two Ruff failures**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
```

Expected: exactly two `F401` failures: unused `pandas as pd` in `availability.py` and unused `date` in `test_profit.py`. Investigate before editing if anything else appears.

- [ ] **Step 2: Remove only those imports**

```diff
diff --git a/commodity_fundamentals/availability.py b/commodity_fundamentals/availability.py
--- a/commodity_fundamentals/availability.py
+++ b/commodity_fundamentals/availability.py
@@
-import pandas as pd
diff --git a/tests/commodity_fundamentals/test_profit.py b/tests/commodity_fundamentals/test_profit.py
--- a/tests/commodity_fundamentals/test_profit.py
+++ b/tests/commodity_fundamentals/test_profit.py
@@
-from datetime import date, datetime, timezone
+from datetime import datetime, timezone
```

- [ ] **Step 3: Verify and commit the cleanup**

```bash
/home/elfbob/miniconda3/bin/ruff check commodity_fundamentals tests/commodity_fundamentals
PYTHONPATH=.:writer/market-monitor/backend /home/elfbob/market-monitor/venv/bin/python -m pytest tests/commodity_fundamentals --rootdir=. -q -p no:cacheprovider
git diff --check
git add commodity_fundamentals/availability.py tests/commodity_fundamentals/test_profit.py
git commit -m "chore: clean commodity fundamentals imports"
git rev-parse HEAD | tee /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt
```

Expected: Ruff exits 0, pytest reports `327 passed`, and one focused two-file commit is created.
