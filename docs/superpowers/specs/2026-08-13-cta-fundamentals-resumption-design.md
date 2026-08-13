# CTA Six-Factor Fundamentals Resumption Design

- Date: 2026-08-13
- Status: approved in conversation; pending written-spec review
- Scope: resume the GTJA CTA six-factor fundamentals path from its existing
  Wind handoff boundary through measured strategy comparisons

## 1. Current state

The project is not paused inside the strategy algorithm. The remaining work is
a sequential handoff across three already-implemented components:

1. `market-monitor` branch `feature/commodity-fundamentals-vertical-slice`
   contains 17 commits implementing catalog validation, Wind capture, durable
   recovery packages, append-only ingestion, conservative vintage selection,
   standardization, coverage checks, and a daily builder. Its focused suite
   passes 327 tests.
2. `futures_strategies` branch `feature/commodity-fundamentals` contains eight
   consumer-side commits implementing the standard reader, raw-price basis,
   coverage gates, PIT evidence, lineage, and comparison reporting. Its full
   suite passes 356 tests and its focused Ruff check passes.
3. The licensed Windows host is reachable at
   `ghls@100.120.152.1:2222`. It has Python 3.11.9, WindPy, pandas, tzdata,
   `D:\marketmonitor\realtime_upgrade`, and an earlier deployment of the
   capture package under `D:\marketmonitor\fundamentals`. It lacks PyYAML and
   has no reviewed catalog, preflight report, or recovery smoke artifact.

The Debian production database still has no `commodity_research` schema or
tables, has no matching `sync_state` rows, and both `futures_daily` and
`continuous_contract_ohlc` end on 2026-04-29. A fundamentals build must use
2026-04-29 as its explicit end date until that separate EOD chain is restored.

## 2. Goal and success condition

Resume the original nine-product fundamentals pilot for
`M, RB, CU, AL, TA, PP, MA, BU, RU`, publish one audited conservative build,
and run the medium, high, and price-volume control comparisons through the
available futures calendar.

Success requires all of the following:

- every Wind catalog field has reviewable source evidence rather than an
  invented code or proxy;
- preflight passes spot 9/9, inventory at least 8/9, and direct-profit or
  reviewed formula-component coverage at least 7/9;
- a January 2025 recovery smoke has a stable run ID, verified chunks, and a
  recorded manifest SHA-256;
- a Debian conservative build reaches `status='complete'`, passes coverage and
  lineage audits, and reproduces sampled basis as `spot / close_raw - 1`;
- the three CTA comparisons record build version, catalog version, coverage,
  lineage, metrics, and workbook paths;
- neither feature branch is merged automatically. Merge remains a separate
  evidence-based decision.

## 3. Approaches considered

### 3.1 Chosen: direct SSH with sequential gates

Drive the Windows work through its existing OpenSSH service, keep the live
collector read-only, and advance only when each evidence gate passes. This
minimizes manual transfer errors while preserving the existing production
approval boundaries.

### 3.2 Not chosen: extend the existing HTTP Wind Gateway

Adding generic EDB/WSD catalog endpoints would turn a deliberately thin,
user-maintained transport into a second fundamentals interface. It would
expand code and configuration without removing the need to review Wind field
semantics.

### 3.3 Fallback only: operator-assisted PowerShell handoff

If SSH becomes unavailable, use the same commands and hashes through an
operator-run PowerShell session. This preserves the design but adds a manual
relay at each evidence boundary.

## 4. Component boundaries and data flow

```text
Wind terminal and WindPy on Windows
  -> reviewed catalog + aggregate preflight report
  -> durable January recovery package (raw data remains off Git)
  -> approved Debian-only append-only observation store
  -> conservative fundamental_daily build through 2026-04-29
  -> futures_strategies standard reader and coverage gate
  -> medium / high / price-volume comparison workbooks
  -> explicit merge decision
```

- Windows owns licensed extraction and recovery artifacts. It does not build
  strategy signals or write PostgreSQL directly.
- `market-monitor` owns catalog validation, upload, schema, standardization,
  publication, and build audits.
- `futures_strategies` owns read-only consumption, factor construction,
  cross-sectional coverage gates, backtests, and result lineage.
- Pi5 receives no pilot schema or rows. `commodity_research` remains outside
  the synchronization chain during the pilot.

## 5. Execution stages

### Stage 1: lock the Windows source and runtime

1. Create a timestamped ZIP snapshot of
   `D:\marketmonitor\realtime_upgrade` without altering the directory.
2. Copy the snapshot to Linux and compare it with the tracked collector,
   excluding only documented runtime files such as logs and caches. Stop on
   unreviewed drift.
3. Back up the existing `D:\marketmonitor\fundamentals` package before
   refreshing it from the clean vertical-slice commit. Record the commit and
   transfer SHA-256.
4. Create `D:\marketmonitor\fundamentals\.venv` with Python 3.11 and
   `--system-site-packages`, then install only the pinned PyYAML dependency
   into that environment. This reuses the verified system WindPy, pandas, and
   tzdata without modifying the Python environment used by the running Wind
   Gateway.
5. Verify imports, `ZoneInfo("Asia/Shanghai")`, and all three CLI help entry
   points before making a Wind source call.

### Stage 2: build and review the catalog

1. Use legacy configuration and database rows only to locate candidates.
   Candidate presence is not approval evidence.
2. Verify each active series with the Wind terminal indicator browser and a
   locally licensed WindPy response: code, API method, field, source name,
   units, frequency, currency, observation semantics, release rule, staleness,
   validity interval, and conversion or formula.
3. Do not accept pre-calculated basis. Every basis input is a reviewed raw spot
   series; the futures leg is the unadjusted dominant-contract `close_raw`.
4. Do not force a profit proxy. Use `profit_direct` or reviewed
   `profit_component` rows and a versioned formula. Products without defensible
   profit evidence remain uncovered and count against the 7/9 gate.
5. Present fields whose semantics cannot be proven from Wind evidence to the
   user for confirmation. Do not create `catalog.v1.yaml` from guesses.

### Stage 3: run the non-mutating Wind gates

1. Validate the complete catalog with `load_catalog`.
2. Run preflight from 2016-01-01 through 2026-08-13 and write only aggregate
   per-series statistics: date range, counts, missing counts, and non-positive
   counts. Raw observations do not enter Git.
3. Evaluate the 9/8/7 target gate. Any failure stops the workflow before
   capture, upload, DDL, or writer deployment.
4. If the gate passes, run a new caller-stable January 2025 capture smoke.
   Require one complete checksum-verified chunk per active series, no partial
   files, and no failed chunks.
5. Record the catalog version and config hash, approved coverage, recovery run
   ID, and manifest SHA-256. Copy only the reviewed catalog and aggregate
   preflight report back to the upstream branch.

### Stage 4: prepare a separate production-mutation packet

The Wind gate does not authorize database or service changes. Before requesting
production approval, assemble:

- the reviewed catalog and coverage evidence;
- the exact recovery package identity and manifest hash;
- a fresh tracked-versus-live writer diff with no unrelated drift;
- live proof that `commodity_research` is absent from `sync_state`;
- migration and rollback diffs, the exact Debian-only schema name, a verified
  backup destination, and disposable-schema transaction-test output.

Only an explicit approval for those exact targets authorizes the Debian backup,
DDL, writer deployment, catalog load, or recovery-package upload.

### Stage 5: publish and audit a smoke build

After production approval:

1. Apply only the Debian `commodity_research` migration and deploy only the
   authoritative nested writer file.
2. Upload the explicitly selected recovery package with fallback disabled.
   Require stored rows to equal the manifest row count; repeat upload and prove
   idempotence.
3. Build January 2025 in conservative mode, require `status='complete'`, and
   audit coverage, units, staleness, scale jumps, blackout leakage, basis
   arithmetic, and ordered lineage.
4. A failed build remains failed and invisible to the CTA reader. It is never
   relabeled or silently degraded.

### Stage 6: full history and CTA comparisons

Full 2016-onward capture and upload require a second approval after presenting
the exact series count, date range, Wind-call estimate, Windows disk estimate,
Debian growth estimate, and zero-sync-impact evidence.

After an audited full-history build through 2026-04-29:

1. synchronize the consumer feature branch with current `master` without
   merging it;
2. rerun its full tests and focused Ruff checks;
3. run medium equal-weight, high composite, and price-volume control with the
   same dates and tradable universe;
4. record build/catalog versions, coverage minimum and failures, lineage,
   annual return, volatility, Sharpe, drawdown, turnover, and workbook paths;
5. interpret differences as measured strategy evidence, not as a requirement
   to match the report's headline Sharpe.

## 6. Failure handling and recovery

- Source drift, missing dependencies, unverified Wind fields, gate failures,
  checksum mismatches, partial chunks, writer drift, unexpected sync rows,
  count mismatches, or failed build audits all stop the next stage.
- A complete recovery chunk is immutable. Retry capture under a new run ID if
  its validation fails; resume an interrupted run only with its original ID
  and unchanged request identity.
- Back up an existing remote package before refresh. Restore that timestamped
  backup if CLI import checks fail.
- Never print credentials or copy raw licensed observations into Git.
- The destructive rollback SQL is never run without a fresh verified dump and
  separate approval for the exact schema.

## 7. Verification

- Upstream local baseline: 327 focused commodity-fundamentals tests pass using
  the authoritative writer path in `PYTHONPATH`.
- Consumer baseline: 356 tests pass; focused Ruff check passes.
- Windows: dependency imports, timezone, WindPy, CLI help, deployed hashes,
  preflight output schema, recovery manifest, and every chunk checksum pass.
- Debian: backup is non-empty, schema remains unsynchronized, writer health has
  no new traceback, upload counts match, build status is complete, basis sample
  error is below `1e-10`, and repeat build lineage checksum is identical.
- Strategy: all three comparison runs share dates/universe and carry complete
  build, catalog, coverage, and code provenance.

## 8. Non-goals

- No new commodity strategy or Carry behavior change.
- No repair of the separate EOD daily-update chain in this work.
- No strict historical-vintage claim for Wind `backfill_final` observations.
- No AU or AG coverage, automatic proxy invention, intraday execution, live
  scheduler, Pi5 schema, or sync-chain change.
- No automatic merge of either feature branch.
