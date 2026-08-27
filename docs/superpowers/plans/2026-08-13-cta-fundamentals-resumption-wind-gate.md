# CTA Six-Factor Fundamentals Resumption — Wind Gate and Handoff

> Continue after Task 2 in `2026-08-13-cta-fundamentals-resumption.md`. The scope, architecture, stack, repository map, and production-mutation exclusions in that document remain authoritative.

## Task 3: Snapshot and compare the live Windows collector

**Artifacts:**
- Create remote: `D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip`
- Create local: `/tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip`
- Create local: `/tmp/cta-fundamentals-resumption-20260813/collector-source.diff`

- [ ] **Step 1: Refuse to overwrite an existing snapshot**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip') { throw 'snapshot target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\snapshots' | Out-Null\""
```

Expected: exit 0. If the path exists, inspect it and consistently choose a new date-stamped name.

- [ ] **Step 2: Snapshot, transfer, and hash-check the collector**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Compress-Archive -Path 'D:\marketmonitor\realtime_upgrade\*' -DestinationPath 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip'; Get-FileHash -Algorithm SHA256 'D:\marketmonitor\snapshots\wind-data-collecter-live-20260813.zip' | Format-List Hash,Path\"" | tee /tmp/cta-fundamentals-resumption-20260813/collector-remote-hash.txt
scp -P 2222 ghls@100.120.152.1:'D:/marketmonitor/snapshots/wind-data-collecter-live-20260813.zip' /tmp/cta-fundamentals-resumption-20260813/
sha256sum /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/collector-local-hash.txt
```

Expected: local and remote SHA-256 values match exactly.

- [ ] **Step 3: Compare code with the authoritative repository**

```bash
mkdir -p /tmp/cta-fundamentals-resumption-20260813/collector-compare
unzip -q /tmp/cta-fundamentals-resumption-20260813/wind-data-collecter-live-20260813.zip -d /tmp/cta-fundamentals-resumption-20260813/collector-compare
diff -ru --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' --exclude=config.yaml --exclude=watchlist.xlsx \
  /home/elfbob/market-monitor/data-collecter/realtime_upgrade \
  /tmp/cta-fundamentals-resumption-20260813/collector-compare \
  | tee /tmp/cta-fundamentals-resumption-20260813/collector-source.diff
```

Expected: no unexplained Python or SQL drift. Stop and reconcile source drift in a separately reviewed commit before deployment.

## Task 4: Deploy the reviewed package into an isolated Windows environment

**Artifacts:**
- Create local: `/tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip`
- Back up remote: `D:\marketmonitor\fundamentals-backups\pre-resume-20260813`
- Deploy remote: `D:\marketmonitor\fundamentals\commodity_fundamentals`
- Create remote: `D:\marketmonitor\fundamentals\.venv`

- [ ] **Step 1: Build a bundle from the exact green commit**

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/market-monitor-commodity-vertical-slice
test "$(git rev-parse HEAD)" = "$(cat /tmp/cta-fundamentals-resumption-20260813/deploy-source-head.txt)"
test -z "$(git status --short)"
zip -rq /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip commodity_fundamentals -x '*__pycache__*' '*.pyc'
sha256sum /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip | tee /tmp/cta-fundamentals-resumption-20260813/deploy-bundle-hash.txt
```

Expected: both guards pass and one bundle hash is recorded.

- [ ] **Step 2: Back up the existing remote fundamentals tree**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"if (Test-Path 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813') { throw 'backup target already exists' }; New-Item -ItemType Directory -Force 'D:\marketmonitor\fundamentals-backups' | Out-Null; Copy-Item -Recurse 'D:\marketmonitor\fundamentals' 'D:\marketmonitor\fundamentals-backups\pre-resume-20260813'\""
```

Expected: exit 0. Recovery is copying this directory back; keep it throughout the plan.

- [ ] **Step 3: Transfer, verify, and activate without deletion**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force 'D:\marketmonitor\incoming' | Out-Null\""
scp -P 2222 /tmp/cta-fundamentals-resumption-20260813/commodity-fundamentals-wind-deploy-20260813.zip ghls@100.120.152.1:'D:/marketmonitor/incoming/'
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"Get-FileHash -Algorithm SHA256 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' | Format-List Hash,Path\"" | tee /tmp/cta-fundamentals-resumption-20260813/deploy-remote-hash.txt
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"$stage='D:\marketmonitor\fundamentals-stage-20260813'; if (Test-Path $stage) { throw 'stage target already exists' }; Expand-Archive 'D:\marketmonitor\incoming\commodity-fundamentals-wind-deploy-20260813.zip' $stage; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813') { throw 'retained package target already exists' }; if (Test-Path 'D:\marketmonitor\fundamentals\commodity_fundamentals') { Move-Item 'D:\marketmonitor\fundamentals\commodity_fundamentals' 'D:\marketmonitor\fundamentals\commodity_fundamentals.pre-resume-20260813' }; Move-Item \"$stage\commodity_fundamentals\" 'D:\marketmonitor\fundamentals\commodity_fundamentals'\""
```

Expected: remote hash matches the local bundle; both new and retained old packages exist.

- [ ] **Step 4: Create the Python 3.11 venv and install only pinned PyYAML**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"py -3.11 -m venv --system-site-packages 'D:\marketmonitor\fundamentals\.venv'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m pip install PyYAML==6.0.3\""
```

Expected: PyYAML 6.0.3 is inside the venv; system WindPy and pandas remain untouched.

- [ ] **Step 5: Verify imports, Wind login, and CLIs**

```bash
ssh -p 2222 ghls@100.120.152.1 "powershell -NoProfile -Command \"$env:PYTHONPATH='D:\marketmonitor\fundamentals'; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -c \"import pandas,yaml; from WindPy import w; r=w.start(); print('yaml',yaml.__version__,'pandas',pandas.__version__,'wind',r.ErrorCode,w.isconnected())\"; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m commodity_fundamentals.preflight --help; & 'D:\marketmonitor\fundamentals\.venv\Scripts\python.exe' -m commodity_fundamentals.capture --help\""
```

Expected: YAML is `6.0.3`, Wind returns error code `0` and `True`, and both help commands exit 0. Make no data request if Wind login fails.
