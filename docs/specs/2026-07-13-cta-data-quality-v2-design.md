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

The adjusted lineages do not make an invalid source bar trustworthy. On the
same `standard` BR roll date, the selected forward-adjusted close is positive
but still creates an artificial approximately -38%/+67% move across adjacent
days. Any lineage with a non-positive or infinite OHLC value therefore marks
the underlying trade-date bar as suspicious across all lineages. The V2 report
must make that relationship explicit even when the selected lineage remains
positive and finite.

Historical null bars are a separate condition. The live table contains 783
`standard` and 579 `nanhua` rows with at least one null OHLC value, concentrated
in old or inactive products such as `RU`, `ZC`, `WR`, `FU`, and `OI`. These
rows are diagnostic historical incompleteness, not an actionable reason for a
permanently failing scheduler gate.

Wind WSD quota is unavailable, so this slice is intentionally limited to
read-only audit logic, deterministic tests, and documentation.

## Goals

- Audit all available OHLC fields for raw, backward-adjusted, and
  forward-adjusted price lineages.
- Report missing and infinite OHLC values as distinct conditions.
- Report impossible as well as infinite derived return/index values.
- Report table freshness against a configurable calendar-day threshold and
  per-symbol lag as a diagnostic.
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
lineage selected by existing historical backtests. It also adds a cross-lineage
suspicious-bar count so a positive adjusted price is not presented as evidence
that an invalid source bar is safe.

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
- `{lineage}_ohlc_incomplete`: rows where any OHLC value is null or NaN.
- `{lineage}_ohlc_infinite`: rows where any non-null OHLC value is positive or
  negative infinity.
- `suspicious_bar_count`: unique trade dates where any lineage has a
  non-positive or infinite OHLC value. A suspicious date applies to the whole
  source bar, not only to the lineage that exposed it.
- `daily_return_invalid`: non-null daily returns that are positive/negative
  infinity or less than or equal to -1. A simple daily return at or below
  -100% is arithmetically impossible for a positive price series.
- `return_index_invalid`: non-null return-index values that are
  positive/negative infinity or less than or equal to zero.
- `first_trade_date` and `last_trade_date`.
- `lag_to_rule_max_days`: calendar days between the symbol's final row and the
  final row for the scanned rule type.
- `missing_trade_dates`: dates present in the rule-type calendar, bounded by
  the symbol's own first and last dates, but absent for that symbol.

Null return values are not counted as invalid because the first observation of
a calculated return series may legitimately be null. Full-OHLC nulls are
counted only as `incomplete` and remain diagnostic; they do not fail strict
mode. This prevents known historical null bars from making the scheduler gate
permanently red.

PostgreSQL `numeric` columns arrive through psycopg2 as `Decimal` objects.
Before infinity checks or vectorized comparisons, the pure pandas helper must
coerce audited numeric columns to floating-point values. Missingness must be
captured before or during coercion so database NULL/NaN values remain
`incomplete` rather than being conflated with infinity.

The inferred missing-date count is diagnostic only. Without an authoritative
exchange calendar, it can include legitimate listing, delisting, or
market-specific gaps.

## Freshness

Freshness uses calendar days so it requires no WSD or exchange-calendar call.

- The CLI uses the host's current date as `as_of`.
- Pure helper functions accept an explicit `as_of` date for deterministic
  tests.
- The default maximum lag is ten calendar days, avoiding routine false alarms
  during Spring Festival and National Day closures.
- `--max-lag-days N` overrides the threshold for a scan.
- Table lag is `as_of - rule_max_trade_date`.
- Per-symbol lag is `rule_max_trade_date - symbol_last_trade_date` and remains
  diagnostic only. Without product lifecycle metadata, an inactive or delisted
  symbol cannot be distinguished reliably from a broken updater.

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
- any lineage has a full-OHLC non-positive or infinite row;
- `daily_return` is infinite or less than or equal to -1;
- `return_index` is infinite or less than or equal to zero;
- the rule-type table is more than `max_lag_days` behind `as_of`.

Incomplete OHLC rows, per-symbol lag, and inferred missing trade dates are
printed and written to CSV but do not, by themselves, fail strict mode. All
three are ambiguous without authoritative lifecycle and exchange-calendar
metadata.

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
- OHLC null/NaN is counted as incomplete, while infinity is counted separately.
- A legacy incomplete bar does not produce a strict failure.
- Return/index infinity and impossible finite values are counted, while an
  expected null return is not.
- A defect in any lineage increments the cross-lineage suspicious-bar count.
- First/last dates and per-symbol lag are correct.
- Missing dates are counted only inside the symbol's observed lifetime.
- A fixed `as_of` date exercises the ten-day boundary.
- A healthy report yields no strict failures.
- Empty, corrupt, infinite, impossible-derived, and stale-table reports yield
  explicit strict failure reasons.
- Incomplete and lagging-symbol reports remain diagnostic without producing
  strict failure reasons.

The existing PG-source and strategy tests must remain green. CLI behavior can
be tested through the pure policy/formatting helpers rather than requiring a
live database in pytest.

## Documentation

- Update `docs/operations/cta-strategy-replication.md` with default and strict
  scan examples, threshold semantics, and exit behavior.
- Update `README.md` to state that both continuous rule types have caught up to
  `futures_daily` through 2026-04-29, while the whole EOD chain remains stopped
  on that date.
- Update the `Futures Strategies` section in the parent
  `/home/elfbob/claude-code/CLAUDE.md`, which still states that continuous data
  ends on 2026-03-06.
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

Given the database state on 2026-07-13, both strict scans are expected to exit
1 because the table is stale and because the `BR` source bar makes at least one
lineage non-positive. The `nanhua` scan must additionally report its
`daily_return <= -1` and `return_index <= 0`. Incomplete historical bars and
inactive-symbol lag must appear in diagnostics without adding strict failure
reasons. These expected live failures demonstrate enforcement; synthetic unit
tests prove the corresponding clean strict path.

## Acceptance Criteria

- Existing adjustment-selection behavior and columns remain compatible.
- Full OHLC incompleteness/infinity/non-positive counts, cross-lineage
  suspicious-bar counts, impossible return/index values, freshness, and
  inferred gap fields are present in the per-symbol report.
- Default scans report findings without failing.
- Strict scans return status 1 with actionable reasons for current stale or
  corrupt data.
- Historical incomplete bars and inactive-symbol lag remain diagnostic and do
  not make strict mode permanently fail.
- All local tests pass.
- Documentation reflects the current 2026-04-29 data boundary and the new CLI
  policy.
- No WSD request, data backfill, or database mutation occurs.
