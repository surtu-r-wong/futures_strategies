# Known-Absence List for the Commodity Fundamentals Daily Gate

- Date: 2026-08-18
- Status: approved in conversation
- Scope: let a reviewed, exactly-enumerated list of `(trade_date, metric)` pairs
  be excluded from a fundamentals build, so the full 2016-2026 window can be
  published without weakening any gate
- Implementation repository: `market-monitor` (upstream). `futures_strategies`
  needs no code change.

## 1. Problem

A read-only dry run of `2016-01-01 .. 2026-04-29` against the uploaded Wind
history produces 82,964 candidate rows and window coverage 9/9/7, but five
daily-gate failures on three dates out of 2,500:

```text
2017-02-06  profit     coverage=5 required=6
2020-02-07  basis_rate coverage=5   profit coverage=3
2020-02-10  basis_rate coverage=5   profit coverage=3
```

The cause is established and is not a defect. All three dates fall on the fifth
or sixth trading day after an extended Chinese New Year closure, during which
the daily spot and profit-component series stop publishing and then age past
their `max_staleness_trading_days = 3` limit. Coverage recovers by itself once
a fresh observation arrives. Evidence:
`market-monitor` `docs/operations/commodity-fundamentals-full-history-capture-20260817.md`.

`enforce_quality` is fail-closed for the whole build, so those five failures
block the entire ten-year window.

## 2. Decision

Three options were considered.

**Raise the staleness limits (rejected).** Lifting `max_staleness_trading_days`
from 3 to 6 on the daily spot and profit-component series would admit those
dates, but 2020-02-07 and 2020-02-10 are the first COVID week, when commodities
opened limit-down. Carrying a pre-holiday spot price across that gap does not
recover data; it manufactures a systematically wrong basis at exactly the dates
where the error is largest. It also requires a new catalog version and a fresh
review.

**Tolerate N failing dates (rejected).** A bounded global tolerance weakens
fail-closed semantics everywhere, including for failures nobody has looked at.

**Named absence list (chosen).** The builder accepts a reviewed file naming the
exact `(trade_date, metric)` pairs that may be missing. Anything not on the list
still fails the build. The published window becomes 2016-01-04 .. 2026-04-29
with three documented holes, and no value is fabricated.

A fourth shape, building the clean segments separately and unioning them in the
consumer, was measured and set aside: `cta_gtja/pg_source.py` selects exactly one
`build_version` and raises `expected exactly one build_version` otherwise, so it
would require a consumer-side change as well.

## 3. Why the rows are dropped rather than kept

Waiving the gate while keeping the partial cross-section is not viable.
`cta_gtja/strategies.py:172` calls the consumer coverage gate with
`required_products=6`, so a stored five-product slice would simply fail
downstream instead of upstream. A waived pair therefore contributes no rows at
all: the date becomes a true hole for that metric.

Granularity is `(trade_date, metric)`, not date. On 2020-02-07 inventory
coverage is 8 and that data is good; only `basis_rate` and `profit` are waived.
`spot` is outside `_DAILY_METRICS` and is not gated on any date, so it is
unaffected.

## 4. The artifact

A new reviewed file `known-absent-v1.yaml`, sitting alongside `catalog-v1.yaml`
and `profit-formulas-v1.yaml`:

```yaml
absence_version: v1
absences:
  - trade_date: 2017-02-06
    metric: profit
    reason: >
      Spring Festival closure 2017-01-27..02-02. The daily profit-component
      series do not publish again until 2017-02-07, so five of seven products
      exceed their three-trading-day staleness limit.
```

Validation, all hard failures:

- entries are exact `(trade_date, metric)` pairs; no ranges and no wildcards
- `metric` must be one of `basis_rate`, `inventory`, `profit`
- `reason` is required and non-empty
- duplicate pairs are rejected
- the file's SHA-256 is passed as `--absence-sha256` and verified before use,
  exactly as `--formulas` / `--formula-sha256` already are

## 5. Gate semantics

A waiver removes one date-metric slice from the daily coverage check and from
the build output. It does nothing else:

- the window-level target gate (`basis_rate` 9, `inventory` 8, `profit` 7)
  still applies
- every row-level check — staleness, unit drift, scale change, non-finite,
  non-positive spot, negative inventory, regime blackout — still applies to
  every row that is stored

So a waiver can only ever remove a slice. It can never admit a bad value.

Two window rules keep the list honest:

- entries outside `[start, end]` are ignored, not errors, so one file serves any
  build window
- an entry inside `[start, end]` whose pair would have passed the six-product
  floor anyway fails the build with `unused_absence:{date}:{metric}`. When the
  underlying data improves, the stale entry must be removed.

## 6. Code changes

All in `market-monitor`.

New module `commodity_fundamentals/absences.py`:

- `Absence(trade_date, metric, reason)`
- `load_absences(path, expected_sha256) -> (absence_version, absence_hash, absences)`
- `apply_absences(rows, absences, *, start, end) -> (kept_rows, failures)`,
  which counts distinct `product_code` per `(trade_date, metric)` directly from
  the candidate rows. A pair with no rows counts as zero and is therefore
  genuinely absent.

`commodity_fundamentals/__main__.py`:

1. `build` and `audit` gain `--absences` and `--absence-sha256`. Both are
   optional, but supplying one requires the other. Omitting them means no
   waivers, which is the stricter behaviour and is what a short-window build
   should use.
2. `BuildArtifacts` carries `absence_version`, `absence_hash`, `absences`;
   `_load_build_artifacts` loads them.
3. The `materialize` wrapper calls `apply_absences` and stashes any
   `unused_absence` failures on the `QualityGate` instance, mirroring the
   existing `quality_gate.trade_dates = _trade_dates(inputs)` pattern.
4. `quality_failures(rows, *, trade_dates=None, absences=())` drops waived
   `(trade_date, metric)` keys from the coverage report entirely. Dropping the
   key matters: `_coverage_report` synthesises a `__missing__` placeholder for
   any pair with no rows, which would otherwise re-fail at `coverage=0`.
5. `audit` cross-checks the supplied file's hash against the `absence_hash`
   recorded for that build, so the stored column does real work.

`commodity_fundamentals/builder.py`: `FundamentalBuilder.build` accepts
`absence_version` and `absence_hash` and puts them in `metadata`. The write
order is unchanged — `write_candidate` still runs before the gate, and it
already receives filtered rows because filtering happens inside `materialize`.

`commodity_fundamentals/db.py`: `start_build` persists the two new columns.

## 7. Database change

```sql
ALTER TABLE commodity_research.fundamental_build
    ADD COLUMN absence_version TEXT,
    ADD COLUMN absence_hash TEXT;
```

plus two constraints: both columns null or both set, and `absence_hash` matching
`^[0-9a-f]{64}$` when present. A `drop_` rollback script accompanies it.

`commodity_research` has no `sync_state` rows and does not exist on Pi5, so this
is a single-end change on a two-row table. The safety card in
`market-monitor/migration/SCHEMA_CHANGES.md` is still read first and the
`sync_state` check still run, and a `pg_dump -Fc -n commodity_research` backup is
taken before the DDL.

## 8. Execution order

Each step needs its evidence before the next one starts.

1. Upstream worktree, TDD, full suite green. Baseline is 341 passed with
   `PYTHONPATH=writer/market-monitor/backend venv/bin/python -m pytest tests/commodity_fundamentals -q`
   — the repository has four `main.py` files and the wrong one yields three
   failures in `test_writer_tables.py`.
2. Write `known-absent-v1.yaml` with the five entries.
3. Dry-run the full window read-only, without `start_build`, `write_candidate`
   or `mark_complete`. Roughly eight minutes and 520 MB RSS. Require zero
   failures and zero `unused_absence`.
4. Backup, DDL, merge to `main`.
5. Production build. Running it from this machine requires rewriting the writer
   URL host from `@localhost:` to `@100.65.111.79:`.
6. Verify the build row carries the version and hash, and that
   `fundamental_daily` is missing exactly the five waived slices and nothing
   else.
7. Consumer verification, below.
8. Record the run in `market-monitor/docs/operations/` and update this
   repository's `docs/ROADMAP.md`.

## 9. Consumer verification

No consumer code changes, but no consumer behaviour is assumed either. Run the
CTA six-factor path over the published window and measure what the basis and
profit sleeves do on 2020-02-07 and 2020-02-10, where their matrices have no
row. Holding the prior day's weights is the defensible outcome — no new signal,
no new trade — but it must be observed, not inferred.

## 10. Out of scope

- The 2026-04-29 upper bound. The builder's calendar comes from
  `continuous_contract_ohlc`, which stops there because the EOD chain is down.
- Any catalog change, including staleness limits.
- Merging `feature/commodity-fundamentals`. Its acceptance still requires the
  medium, high and price-volume control comparisons on a real build.
