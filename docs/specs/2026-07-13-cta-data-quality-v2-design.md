# CTA Data Quality V2 Design

Date: 2026-07-13

## Context

The CTA project can run guarded price/volume research from
`public.continuous_contract_ohlc`, but its current quality scan only classifies
non-positive adjusted `open` and `close` values. That check verified all 79
`standard` symbols as usable, while a broader inspection found two gaps:

- `nanhua` has one `BR` row on 2026-01-07 whose raw, backward-adjusted, and
  forward-adjusted open/close prices are all non-positive.
- The corresponding `standard` row has `low_raw=0`, which the existing scan
  does not inspect.

The source row in `public.futures_daily` for `BR2603.SHF` on that date contains
zero OHLC, volume, and open interest. This project must report that upstream
problem accurately without writing the database or changing historical
backtest behavior. The wider EOD chain is also stale: `futures_daily` and both
continuous-contract rule types currently end on 2026-04-29.

Wind WSD quota is unavailable, so this slice is intentionally limited to
read-only audit logic, deterministic tests, and documentation.

## Goals

- Audit all available OHLC fields for raw, backward-adjusted, and
  forward-adjusted price lineages.
- Report non-finite derived return/index values.
- Report table and per-symbol freshness using a configurable calendar-day
  threshold.
- Report inferred missing trade dates inside each symbol's observed lifetime.
- Add an opt-in strict mode suitable for schedulers and CI.
- Preserve existing adjustment-selection behavior for historical strategy
  reads.
- Keep all database access read-only and independent of WSD.

## Non-Goals

- Do not fetch or backfill market data.
- Do not write, repair, or delete database rows.
- Do not patch the upstream continuous-contract generators in this repository.
- Do not change factor calculations, portfolio construction, or backtest
  parameters.
- Do not make database freshness a prerequisite for explicitly bounded
  historical backtests.
- Do not introduce an exchange-calendar dependency in this slice.

## Approach

Extend `cta_gtja/data_quality.py` with pure pandas helpers and CLI policy
evaluation. This keeps the audit near the existing lineage classifier and
reuses the current read-only database connection path.

Two alternatives were rejected:

- A new `data_health.py` module would separate concepts but duplicate the
  existing database scan and adjustment-audit vocabulary.
- A standalone operations script would be quick but difficult to reuse and
  unit-test.

No new runtime dependency is required.

## Compatibility Boundary

`summarize_adjustment_quality()` and `build_adjustment_audit()` remain the
contract used by `cta_gtja.pg_source`. Their existing columns and adjustment
selection semantics stay unchanged:

- `raw_nonpos`, `ba_nonpos`, and `fa_nonpos` continue to count rows with a
  non-positive `open` or `close` in that lineage.
- `status` remains one of `ok`, `fa_corrupt`, `ba_corrupt`, or `both_corrupt`.
- `recommended_adj` continues to prefer `fa`, then `ba`, then explicit raw
  fallback.

The V2 scan adds separate full-OHLC columns instead of broadening the old
columns. This prevents a zero high/low value from silently changing the price
lineage selected by existing historical backtests.

## Per-Symbol Audit

The database scan loads these fields for one `rule_type`:

- `base_symbol`, `trade_date`
- `open_raw`, `high_raw`, `low_raw`, `close_raw`
- `open_ba`, `high_ba`, `low_ba`, `close_ba`
- `open_fa`, `high_fa`, `low_fa`, `close_fa`
- `daily_return`, `return_index`

The result remains one row per symbol and preserves all existing adjustment
columns. It adds:

- `{lineage}_ohlc_nonpos`: rows where any available OHLC value is less than or
  equal to zero.
- `{lineage}_ohlc_nonfinite`: rows where any available OHLC value is NaN,
  positive infinity, or negative infinity.
- `daily_return_nonfinite`: non-null daily returns that are positive or
  negative infinity.
- `return_index_nonfinite`: non-null return-index values that are positive or
  negative infinity.
- `first_trade_date` and `last_trade_date`.
- `lag_to_rule_max_days`: calendar days between the symbol's final row and the
  final row for the scanned rule type.
- `missing_trade_dates`: dates present in the rule-type calendar, bounded by
  the symbol's own first and last dates, but absent for that symbol.

Null return values are not counted as non-finite because the first observation
of a calculated return series may legitimately be null. Full-OHLC nulls are
counted as non-finite because a price bar is not usable without all four
fields.

The inferred missing-date count is diagnostic only. Without an authoritative
exchange calendar, it can include legitimate listing, delisting, or
market-specific gaps.

## Freshness

Freshness uses calendar days so it requires no WSD or exchange-calendar call.

- The CLI uses the host's current date as `as_of`.
- Pure helper functions accept an explicit `as_of` date for deterministic
  tests.
- The default maximum lag is five calendar days.
- `--max-lag-days N` overrides the threshold for a scan.
- Table lag is `as_of - rule_max_trade_date`.
- Per-symbol lag is `rule_max_trade_date - symbol_last_trade_date`.

Freshness applies only to the quality-scan command. It is not added to
`load_public_cta_data()`, so an explicitly bounded historical backtest remains
valid even when the live table is stale.

## CLI Behavior

Existing usage remains valid:

```bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard
```

New options:

```text
--strict
--max-lag-days N
```

Default mode prints the complete summary and exits zero after a successful
query, even when it reports data-quality findings. Existing `--csv` behavior
continues to write the per-symbol report when explicitly requested.

Strict mode prints each failure reason and exits with status 1 when any of the
following is true:

- the query returns no symbols;
- an adjusted lineage has an existing corruption status;
- any lineage has a full-OHLC non-positive or non-finite row;
- `daily_return` or `return_index` contains infinity;
- the rule-type table is more than `max_lag_days` behind `as_of`;
- any symbol is more than `max_lag_days` behind the rule-type maximum date.

Inferred missing trade dates are printed and written to CSV but do not, by
themselves, fail strict mode.

Database connection and SQL errors continue to propagate in both modes and
therefore return a non-zero process status. Default reporting does not mask
infrastructure failures.

## Internal Interfaces

The implementation will keep policy logic testable without a database:

- A pure full-OHLC/date-summary helper accepts a DataFrame and returns one row
  per symbol.
- A merge helper combines the V2 columns with the existing adjustment-quality
  report.
- A pure strict-policy helper accepts the combined report, `as_of`, and
  `max_lag_days`, then returns a list of human-readable failure reasons.
- The CLI is responsible only for argument parsing, read-only scan execution,
  formatting, optional CSV output, and the final exit code.

## Testing

Extend `tests/test_cta_data_quality.py` with focused synthetic cases:

- A zero `high` or `low` is caught by the V2 OHLC audit while the legacy
  open/close status contract remains unchanged.
- OHLC NaN/infinity is counted as non-finite.
- Return/index infinity is counted, while an expected null return is not.
- First/last dates and per-symbol lag are correct.
- Missing dates are counted only inside the symbol's observed lifetime.
- A fixed `as_of` date exercises the five-day boundary.
- A healthy report yields no strict failures.
- Empty, corrupt, non-finite, stale-table, and lagging-symbol reports yield
  explicit strict failure reasons.

The existing PG-source and strategy tests must remain green. CLI behavior can
be tested through the pure policy/formatting helpers rather than requiring a
live database in pytest.

## Documentation

- Update `docs/operations/cta-strategy-replication.md` with default and strict
  scan examples, threshold semantics, and exit behavior.
- Update `README.md` to state that both continuous rule types have caught up to
  `futures_daily` through 2026-04-29, while the whole EOD chain remains stopped
  on that date.
- Add a dated status note to the historical CTA replication plan instead of
  rewriting its original checkboxes.

## Verification

Run the complete local test suite:

```bash
.venv/bin/python -m pytest -q
```

Run both default live scans:

```bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua
```

Run strict scans:

```bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard --strict
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua --strict
```

Given the database state on 2026-07-13, strict scans are expected to exit 1
because the table is stale. The `nanhua` scan must additionally identify the
known `BR` anomaly. These expected live failures demonstrate enforcement; the
synthetic unit tests prove the corresponding clean strict path.

## Acceptance Criteria

- Existing adjustment-selection behavior and columns remain compatible.
- Full OHLC, return/index, freshness, and inferred gap fields are present in
  the per-symbol report.
- Default scans report findings without failing.
- Strict scans return status 1 with actionable reasons for current stale or
  corrupt data.
- All local tests pass.
- Documentation reflects the current 2026-04-29 data boundary and the new CLI
  policy.
- No WSD request, data backfill, or database mutation occurs.
