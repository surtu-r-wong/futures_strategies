# Commodity Fundamentals Data Design

**Date:** 2026-07-27

**Status:** Draft for written review

**Scope:** Wind-backed basis, inventory, and profit inputs for the GTJA CTA
replication

## 1. Context

The GTJA product deck attributes most of the medium- and high-volatility
portfolio return contribution to three fundamental factors: basis, inventory,
and profit. The current implementation cannot make a meaningful comparison:

- `public.spot_prices` has 9,328 rows and covers only `M`.
- `public.inventory` has 2,610 rows and covers only `M`.
- `public.commodities_spot_prices` has 895,668 rows, but its observations stop
  in January 2021.
- The existing tables have import or migration timestamps from 2026 rather
  than historical publication vintages. They cannot prove what was known at
  each historical signal date.
- `cta_gtja.pg_source` averages the available spot and inventory rows and
  exposes no source, publication-time, revision, or staleness lineage.
- `BasisFactor` currently falls back to a continuous adjusted close. A
  spot/futures level ratio must instead use the unadjusted price of the actual
  dominant contract on that date.

The first delivery covers:

```text
M, RB, CU, AL, TA, PP, MA, BU, RU
```

`AU` and `AG` are explicitly excluded.

Wind WSD/EDB is the authoritative source. Historical Wind values are accepted
under a conservative point-in-time policy: inferred publication delays are
enforced, but historical terminal values are disclosed as final backfill
values rather than misrepresented as reconstructable vintages.

## 2. Goals

1. Create an auditable, catalog-driven store for commodity fundamental
   observations and their captured revisions.
2. Backfill the selected products from 2016-01-01 through the latest date
   available from Wind.
3. Materialize availability-aware daily spot, inventory, and profit inputs.
4. Prevent future data, stale data, unit mismatches, and weak proxies from
   silently entering CTA signals.
5. Make fundamental coverage visible and enforceable before a formal
   six-factor backtest.
6. Preserve the price/volume strategy as an unaffected control.

## 3. Non-goals

- Reconstruct historical revisions that Wind does not expose.
- Claim that `backfill_final` observations are strict historical vintages.
- Cover the whole commodity-futures market in the first delivery.
- Include `AU` or `AG`.
- Force a profit proxy onto a product when no economically stable series or
  formula passes review.
- Replace or rewrite `spot_prices`, `inventory`, or
  `commodities_spot_prices`.
- Treat the product deck's reported Sharpe ratio as an acceptance target.
- Build an intraday trading or execution system.

## 4. Chosen architecture

```text
Windows Wind data machine
  -> catalog-driven WSD/EDB collector
  -> durable local recovery package
  -> Phase 5 HTTP write path
  -> commodity_research raw observation store
  -> Linux standardization and profit builder
  -> versioned fundamental_daily publication
  -> cta_gtja read-only adapter and coverage audit
```

### 4.1 Ownership boundaries

- **Market Monitor** owns Wind extraction, write APIs, database migrations,
  raw observations, and the standard-data build.
- **futures_strategies** owns the CTA read adapter, factor construction,
  coverage gates, reports, and backtest verification.
- **The Windows Wind machine** only extracts and uploads source observations.
  It does not aggregate products, calculate profit formulas, or run the CTA
  backtest.
- **The Debian primary** initially hosts `commodity_research`. The research
  schema is not added to the two-way sync configuration during the pilot.
  Promotion to a synchronized production schema is a separate operational
  decision after the data contract is stable.

Before changing the collector, the implementation must retrieve the current
running copy from the Windows data machine and compare it with the repository
backup. The running Windows copy is the operational source of truth.

### 4.2 Why the pilot is not synchronized

The Market Monitor DDL policy defaults new research and rebuildable data to
non-synchronized storage. Keeping the pilot on the Debian primary:

- avoids introducing partially validated tables into the live two-way sync
  chain;
- permits schema iteration without breaking the Pi5 worker;
- makes the later production promotion explicit and reviewable.

While the schema exists only on Debian, the Wind uploader uses the Phase 5
client with fallback disabled for this endpoint. If Debian is unavailable, it
retains the durable local recovery package and retries later. It must not send
rows to a Pi5 endpoint that does not have the schema.

## 5. Data model

All timestamps use `TIMESTAMPTZ`. Business timestamps are interpreted in
`Asia/Shanghai`.

### 5.1 `commodity_research.product_mapping`

One row per supported futures product.

Core fields:

- `product_code`: canonical futures symbol and primary key.
- `product_name`
- `exchange`
- `currency`
- `futures_quote_unit`
- `active_from`, `active_to`
- `active`
- `updated_at`

The table contains the nine in-scope products and no `AU` or `AG` rows for this
catalog version.

### 5.2 `commodity_research.series_catalog`

One immutable catalog row per semantic use of a Wind series. A Wind source
code may appear in more than one semantic binding, but extraction de-duplicates
the actual API request.

Core fields:

- `series_id`: stable human-readable identifier and primary key.
- `catalog_version`
- `source`: fixed to `wind` in the first delivery.
- `source_code`
- `source_name`
- `product_code`
- `metric_role`: `spot`, `inventory`, `profit_direct`,
  `profit_component`, or `conversion_component`.
- `frequency`
- `source_unit`, `target_unit`, `currency`
- `scale_multiplier`
- `aggregation_rule`
- `date_semantics`
- `release_lag_rule`
- `unknown_time_policy`
- `max_staleness_trading_days`
- `valid_from`, `valid_to`
- `active`
- `config_hash`
- `updated_at`

Catalog validation rejects an active row without a source code, units,
date semantics, an explicit release rule, and a staleness limit.
Published catalog rows are immutable. A mapping, timing, or conversion change
creates a new `catalog_version`; it does not update an already published
version in place.

Default staleness limits are five trading days for daily series, ten trading
days for weekly series, and twenty-five trading days for monthly series. A
catalog override requires a documented source rationale and a new catalog
version.

### 5.3 `commodity_research.ingest_run`

One row per caller-stable extraction and publication attempt.

Core fields:

- `run_id`: UUID primary key reused by retries.
- `mode`: `backfill`, `incremental`, `audit`, or `rebuild`.
- `catalog_version`, `config_hash`
- `requested_start`, `requested_end`
- `started_at`, `finished_at`
- `status`: `running`, `raw_complete`, `validated`, `published`, or `failed`.
- requested, received, rejected, and published row counts.
- `recovery_artifact_checksum`
- `error_summary`
- `build_version`
- `is_current`
- `updated_at`

Only one successfully published run can be current. Promotion switches the
current run within the same transaction that completes the publication.
The DDL enforces this invariant with a partial unique index over the current
row.

### 5.4 `commodity_research.observation_vintage`

Append-only raw observation snapshots.

Core fields:

- surrogate primary key
- `run_id`
- `series_id`
- `observation_date`
- `published_at`, nullable when Wind does not provide it
- `available_at`
- `recorded_at`
- `raw_value`
- `source_unit`, `currency`
- `vintage_quality`: `backfill_final`, `captured_live`, or
  `legacy_unverified`
- `payload_hash`
- `created_at`, `updated_at`

The idempotency key is `(run_id, series_id, observation_date)`. Replaying a
run cannot create duplicates. A later run may append a different value for the
same series and observation date, thereby preserving a captured revision.
Raw values are never overwritten by the standard-data builder.

### 5.5 `commodity_research.profit_formula`

Immutable snapshots of reviewed profit definitions.

Core fields:

- `formula_id`
- `product_code`
- `formula_version`
- `effective_from`, `effective_to`
- component series identifiers, coefficients, and operators
- output unit
- fixed-cost assumptions, when applicable
- `config_hash`
- `active`
- `created_at`, `updated_at`

The version-controlled catalog/formula files are the review authority. The
database rows are immutable snapshots of the approved version used by a
particular build.

### 5.6 `commodity_research.fundamental_daily`

Rebuildable long-form strategy input.

Core fields:

- `build_version`
- `trade_date`
- `product_code`
- `metric`: `spot`, `basis_rate`, `inventory`, or `profit`
- `value`
- source observation date and `available_at`
- `series_id` or `formula_id`
- `catalog_version`
- `vintage_quality`
- `staleness_trading_days`
- `lineage_hash`
- `created_at`

The unique key is `(build_version, trade_date, product_code, metric)`.
A read-only current view selects the `build_version` belonging to the current
published `ingest_run`.

## 6. Point-in-time policy

### 6.1 Time fields

- `observation_date` describes the economic period represented by a value.
- `published_at` is the source publication timestamp when Wind supplies one.
- `available_at` is the earliest timestamp the strategy is permitted to use.
- `recorded_at` is when this system actually captured the value.

These fields are not interchangeable.

### 6.2 Availability calculation

1. Use a reliable source publication timestamp when available.
2. Otherwise apply the reviewed `release_lag_rule` to the observation date.
3. If the publication time of day is unknown, treat the value as available
   only for the next trading day's close signal.
4. A value can enter the signal for trading date `D` only when
   `available_at` is no later than that signal's cutoff.
5. A signal formed after the close of `D` executes at the open of the next
   trading day.
6. Forward filling begins only after `available_at` and stops after the
   catalog staleness limit.

Trading-day alignment uses the eligible commodity dates from
`continuous_contract_ohlc` for the selected rule type. It never substitutes a
weekday-only calendar.

### 6.3 Historical and live vintages

- Historical WSD/EDB retrieval is marked `backfill_final`.
- Repeated forward collection is marked `captured_live`.
- Imported legacy tables, if used for comparison, are marked
  `legacy_unverified`.

The default `conservative` research mode permits `backfill_final` using its
inferred `available_at` and reports the associated revision risk. The
`strict` mode permits only values genuinely captured by the system by the
relevant time. Strict mode therefore does not pretend to provide a pre-2026
history when no historical vintages exist.

For `captured_live`, effective availability is the later of the source-derived
availability and `recorded_at`. A value first captured today can never be made
available to yesterday's signal merely because its economic observation date
is older.

If a captured value changes, the new snapshot is appended. Selection uses the
latest vintage permissible under the requested mode and signal cutoff.

## 7. Catalog bootstrap and review

Exact proprietary Wind codes are configuration data discovered on the Wind
terminal, not hard-coded design assumptions. Before any backfill, the collector
must generate a preflight inventory containing:

- source code and source name;
- product and metric role;
- units and currency;
- frequency and date semantics;
- first and last observation date;
- point count and missingness;
- candidate publication lag;
- candidate staleness rule;
- candidate primary/fallback status.

The reviewed inventory is frozen as catalog version 1. An unsupported
product/metric is recorded with a reason code; it is not represented by an
empty source code or a guessed proxy.
The full historical backfill does not start until the reviewed catalog meets
the first-delivery coverage targets in Section 9. If Wind cannot meet them,
the pipeline stops after preflight and returns the uncovered product/metric
list for an explicit scope decision.

Selection priority is:

1. direct economic meaning;
2. history from 2016 or earlier;
3. stable definition;
4. defensible availability timing;
5. low missingness.

The legacy database is useful only for overlap checks. It cannot fill a Wind
gap in a formal build.

## 8. Standardization rules

### 8.1 Basis

Each product has one reviewed primary spot benchmark. Before use, its currency,
tax convention, grade, region, and quote unit are normalized to the futures
contract's quote convention.

For each date:

```text
basis_rate = normalized_spot / dominant_contract_close_raw - 1
```

`dominant_contract_close_raw` is the unadjusted close of the actual
`contract_used` for that date. Ratio-adjusted `fa` or `ba` continuous levels
must never be used in this calculation. A missing or non-positive raw contract
price produces `NaN`. The derived basis becomes usable at the later of the
spot `available_at` and the futures close cutoff. Any currency or unit
conversion series is subject to the same rule.

### 8.2 Inventory

- Prefer a stable total-inventory series.
- Sum components only when their regions are mutually exclusive and their
  definitions and units match.
- Never average different inventory types or alternative vendors.
- A primary/fallback source switch requires an explicit effective date and
  overlap review.
- The twenty trading days following a definition or source regime break are
  ineligible for the 20-day inventory-change factor.
- Frequency-aware forward filling observes `available_at` and the staleness
  limit.

The CTA factor remains the negative 20-trading-day inventory percentage
change. Zero, negative, stale, or insufficient-history inputs produce `NaN`.

### 8.3 Profit

Use a direct Wind profit or processing-margin series when it has stable
economic meaning and sufficient history. Otherwise use an approved formula:

```text
profit = sum(output_yield_i * output_price_i)
       - sum(input_quantity_j * input_price_j)
       - reviewed_variable_cost
```

No implicit currency, tax, or unit conversion is allowed. The calculated
profit becomes available only when every required component is available; its
`available_at` is the latest component availability. Formula changes create a
new version and affect only their declared effective interval.

The intended economic concepts are:

| Product | Profit concept |
|---|---|
| M | soybean crushing profit |
| RB | long-process rebar mill profit |
| CU | copper smelting profit or processing return |
| AL | electrolytic aluminium profit |
| TA | PTA processing fee or production profit |
| PP | reviewed mainstream production-route profit |
| MA | coal-to-methanol profit |
| BU | refinery asphalt production profit |
| RU | a stable production/processing profit series, if one passes review |

`RU` remains missing when no defensible series passes review.

The CTA profit factor remains the negative rolling 252-trading-day z-score
with at least 60 valid observations.

## 9. Coverage policy

The first-delivery targets are:

- basis: 9 of 9 products;
- inventory: at least 8 of 9 products;
- profit: at least 7 of 9 products.

A formal six-factor backtest requires each fundamental factor to cover at least
6 of 9 products on a trading date. The cross-sectional inventory factor must
also produce at least two eligible long and two eligible short candidates.

Below the hard threshold:

- the default formal mode fails with a clear coverage error;
- an explicitly selected research-degraded mode may continue;
- the report highlights the affected dates and products;
- missing values never become zero signals.

Reports include daily product/factor availability, observation dates,
staleness, vintage quality, and reason codes.

## 10. Collection and publication

### 10.1 Backfill

- Request 2016-01-01 through the latest Wind date.
- Chunk requests by source series and year.
- Assign a caller-stable `run_id`.
- Write every successful response block to a durable local recovery package
  before upload.
- Record response metadata and checksums.
- Resume at the first incomplete block after an interruption.

### 10.2 Incremental collection

Each active series defines a revision lookback window. The daily job re-queries
that window, compares payload hashes and values, and appends changed vintages.
A periodic full-history audit checks for silent source changes without
automatically republishing them.

### 10.3 Publication

1. Complete and validate raw ingestion.
2. Build a new version of `fundamental_daily`.
3. Run schema, value, lineage, coverage, and staleness checks.
4. Compare the new build with the current build.
5. In one transaction, mark the new run published and current.

Failed builds remain non-current and can be inspected or rebuilt. A partial
build can never replace the current published view.

## 11. Failure handling

- **Wind quota, timeout, or disconnect:** bounded retry, then checkpoint and
  stop without discarding successful blocks.
- **Writer or database unavailable:** retain the recovery package and retry
  the same `run_id`.
- **Duplicate retry:** use the idempotency key; row counts remain unchanged.
- **Unknown unit, currency, or date semantics:** reject the active catalog row.
- **Duplicate dates, non-finite values, negative inventory, or implausible
  scale jumps:** quarantine the affected series and block publication.
- **Missing formula component:** emit `NaN`; never calculate a partial profit.
- **Coverage failure:** block formal publication or formal backtest according
  to the selected mode.
- **Build failure:** preserve the previous current version.

Recovery packages are archived only after database acknowledgement, count
reconciliation, and validation succeed.

## 12. CTA integration

`cta_gtja` reads the current standard daily view through a read-only adapter.
The adapter:

- applies the selected PIT mode;
- pivots the long metrics into the existing `CTADataSet` contract;
- preserves metadata needed for coverage reporting;
- obtains raw dominant-contract prices for basis;
- keeps adjusted continuous prices for momentum and portfolio returns;
- refuses duplicate product/date/metric rows.

The existing file source remains supported. A file-based fundamental dataset
must carry the same availability and vintage fields to qualify for a formal
PIT run.

## 13. Verification

### 13.1 Unit and property tests

- Exact availability boundaries for pre-close, post-close, and unknown-time
  releases.
- No forward fill before availability or after staleness.
- Future observations and later captured revisions cannot alter earlier
  signals.
- Basis is invariant to changes in `fa` and `ba` adjustment factors.
- Heterogeneous inventory series cannot be summed or averaged.
- Source-regime breaks create the required factor blackout.
- Profit remains missing until every component is eligible.
- Formula-version changes affect only their effective interval.
- Replaying a `run_id` is idempotent.
- An interrupted build cannot become current.

### 13.2 Data acceptance

- Validate expected points using each source's actual release frequency.
- Report gaps, stale intervals, unit changes, definition changes, and scale
  breaks from 2016 onward.
- Reconcile overlapping `M` and legacy data for direction, scale, and
  correlation without requiring equality between different definitions.
- Meet the Section 9 target coverage. Unsupported product/metric reasons are
  still recorded, but recording a reason does not waive the target.

### 13.3 Strategy acceptance

Run both strategies for 2019-01-01 through 2025-09-30:

- `medium_equal_weight`
- `high_composite`

Acceptance requires:

- the price/volume-only control has no unexplained change;
- basis, inventory, and profit generate genuine non-zero positions, returns,
  and contributions;
- daily coverage appears in the report;
- perturbing future fundamental data leaves historical signals and net asset
  values unchanged;
- the backtest uses the next-open execution convention.

The product deck's Sharpe ratio is a directional comparison only. Coverage,
lineage, point-in-time behavior, and reproducibility are the acceptance
criteria.

## 14. Operational gates

Before live DDL:

1. follow the Market Monitor DDL safety checklist;
2. back up the Debian database or affected objects;
3. verify current two-way sync health;
4. prepare rollback SQL;
5. confirm the new research schema is absent from `sync_state` and sync
   configuration.

Before full Wind backfill:

1. pass the Wind connection and catalog dry run;
2. freeze catalog version 1;
3. validate a small date range for all selected source series;
4. verify idempotent upload and rebuild;
5. confirm available disk space and expected row count;
6. schedule the larger write outside active market hours.

No production-schema promotion or Pi5 synchronization is included in the first
implementation plan.

## 15. Delivery sequence

1. Retrieve and compare the running Windows collector.
2. Generate and review the Wind series preflight inventory.
3. Add the Debian research schema, write API, and idempotent raw ingestion.
4. Implement catalog validation and standard-data construction.
5. Backfill a small validation interval and inspect lineage.
6. Backfill 2016 onward in resumable chunks.
7. Integrate the read-only CTA adapter and coverage gates.
8. Run unit, integration, PIT mutation, and strategy acceptance tests.
9. Add the daily incremental job and operational runbook.
10. Evaluate production promotion only after the pilot is stable.
