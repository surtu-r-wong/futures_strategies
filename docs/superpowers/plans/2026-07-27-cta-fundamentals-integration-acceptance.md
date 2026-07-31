# CTA Fundamentals Integration and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-07-31 trimmed per workspace anti-redundancy ruling** (vertical slice
> first; deferred items listed at bottom); original 11-task scope in git
> history at `e31e1e5`. There are no "formal" runs until the upstream
> price-adjustment fix lands and the user promotes CTA to monthly use, so
> everything that existed to certify run classes is deferred. What remains:
> raw prices for basis, one conservative build consumed with lineage, the
> `6/9` pre-weight coverage gate, and auditable comparison runs.

**Goal:** Make the CTA six-factor strategy consume the published conservative commodity fundamental build, preserve raw futures prices for basis, enforce the approved `6/9` daily coverage contract as a pre-weight gate, and produce auditable comparisons without treating the research deck's Sharpe ratio as an acceptance target.

**Architecture:** The PostgreSQL reader loads adjusted prices for trading and retains raw OHLC for economic calculations. A standard-fundamentals reader pivots exactly the latest complete conservative build and carries its lineage into the backtest result. Six-factor runs pass a pre-weight coverage gate before missing scores become zero weights; synthetic test fixtures may disable enforcement explicitly, but there is no run-class taxonomy.

**Review tiers:** PIT-critical reader behavior (the `available_at <= trade_date 15:00` defense clauses and single-build selection) and basis calculation (raw-close basis in `factors.py`) = **Tier 1** (full review). All other code = **Tier 2** (single combined review). Docs (`docs/cta-fundamentals.md`) = **Tier 3** (self-verify).

**Tech Stack:** Python 3.13, pandas, NumPy, psycopg2, argparse, openpyxl, pytest

---

**Design reference:** `docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`
(run-class / file-provenance sections superseded by the 2026-07-31 trim; see
Deferred at bottom)

**Prerequisites:**

1. Complete `docs/superpowers/plans/2026-07-27-wind-fundamentals-catalog-capture.md`.
2. Complete the trimmed
   `docs/superpowers/plans/2026-07-27-commodity-fundamentals-store-materialize.md`.
3. A `complete` conservative build exists in
   `commodity_research.fundamental_build` / `fundamental_daily`.

**Repository:** `/home/elfbob/claude-code/futures_strategies`

## File map

| File | Responsibility |
|---|---|
| `cta_gtja/data.py` | Carry fundamental lineage and audit frames with the in-memory dataset |
| `cta_gtja/pg_source.py` | Load raw/adjusted futures prices and the standard conservative build |
| `cta_gtja/coverage.py` | `6/9` daily gate and inventory two-sided check with reason codes |
| `cta_gtja/factors.py` | Use published basis first and raw close for the only permitted fallback |
| `cta_gtja/strategies.py` | Run coverage checks before missing scores become zero weights |
| `cta_gtja/backtest.py` | Propagate fundamental audit data and write it to Excel |
| `cta_gtja/__main__.py` | Minimal source routing with safe defaults |
| `tests/test_cta_pg_source.py` | SQL selection, raw-price preservation, and metadata tests |
| `tests/test_cta_strategy.py` | Factor, result, and report regression tests |
| `tests/test_cta_fundamental_coverage.py` | `6/9` and inventory two-sided gate tests |
| `tests/test_cta_fundamental_pit.py` | Future-vintage mutation and price-volume control tests |
| `docs/cta-fundamentals.md` | Operator contract, examples, and interpretation limits |

The approved pilot universe is:

```python
PILOT_FUNDAMENTAL_SYMBOLS = (
    "M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU",
)
```

`AU` and `AG` are not members of this pilot. Do not silently substitute them
for a missing pilot product.

### Task 1: Freeze the current CTA control behavior

**Files:**
- Read: `cta_gtja/data.py`, `cta_gtja/pg_source.py`, `cta_gtja/factors.py`, `cta_gtja/strategies.py`
- Create: `tests/test_cta_fundamental_pit.py`

- [ ] **Step 1: Record the starting tree and focused baseline**

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short --branch
.venv/bin/python -m pytest \
  tests/test_cta_strategy.py \
  tests/test_cta_pg_source.py -q
```

Expected: the existing CTA tests pass. Stop if an unrelated user change
overlaps a file in this plan.

- [ ] **Step 2: Add a price-volume independence test**

Create `tests/test_cta_fundamental_pit.py` with deterministic
`complete_price_frame` / `complete_fundamental_frame` module-local fixtures
(at least 300 business dates, the exact nine pilot symbols, values generated
from date and symbol positions — no random numbers) and a regression that
runs `run_medium_equal_weight` with `price_volume_cta_factors()` on the
original dataset and on a copy whose `spot`/`basis_rate`/`inventory`/`profit`
columns are scaled by 1000 and shifted by 777, then asserts identical
`weights`, `period_returns`, and `equity`.

- [ ] **Step 3: Run the control test and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_fundamental_pit.py -q
git add tests/test_cta_fundamental_pit.py
git commit -m "test: freeze CTA price-volume control behavior"
```

Expected: PASS before production changes. This test is the control that must
remain green throughout the plan.

### Task 2: Preserve raw prices and extend the dataset audit contract

**Files:**
- Modify: `cta_gtja/data.py`
- Modify: `cta_gtja/pg_source.py`
- Modify: `tests/test_cta_pg_source.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing raw-price and dataset tests**

Add a test that `_apply_adjustment_policy` keeps `open_raw`/`high_raw`/
`low_raw`/`close_raw` alongside the selected adjusted `open`/`high`/`low`/
`close` (adjusted lineage still populates the trading columns; raw columns
are carried for basis and audit only). Add a test that `CTADataSet.slice`
filters the new `fundamental_quality` frame by `product_code` and copies
`fundamental_metadata` (equal content, different object).

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_pg_source.py tests/test_cta_strategy.py -q
```

Expected: failures for missing raw columns and new dataset fields.

- [ ] **Step 3: Extend `CTADataSet` and preserve raw OHLC**

Add fields after `data_quality`:

```python
fundamental_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
fundamental_metadata: dict[str, object] = field(default_factory=dict)
```

`fundamental_metadata` is a lineage record (source, pit_mode, build_version,
catalog_version, cutoff, schema, materialized_daily) — it labels where data
came from; it does not certify anything. In `slice`, filter
`fundamental_quality` by `product_code` when present and copy the metadata
with `dict(...)`. In `from_dir`, leave both fields empty with
`{"source": "files-unverified", "materialized_daily": False}` — file input
stays an uncertified research convenience (see Deferred).

Update `normalize_prices` so `open_raw`, `high_raw`, `low_raw`, `close_raw`
join the numeric price columns, and add the four raw columns to
`_apply_adjustment_policy`'s `base_cols`.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_pg_source.py tests/test_cta_strategy.py -q
git add cta_gtja/data.py cta_gtja/pg_source.py \
  tests/test_cta_pg_source.py tests/test_cta_strategy.py
git commit -m "feat: preserve raw CTA prices and fundamental audit"
```

### Task 3: Load the published conservative build from PostgreSQL

**Files:**
- Modify: `cta_gtja/pg_source.py`
- Modify: `cta_gtja/__main__.py`
- Modify: `tests/test_cta_pg_source.py`

- [ ] **Step 1: Write failing standard-loader tests** *(Tier 1 scope)*

Monkeypatch `_read_sql` and use `/* cta-standard-values */` and
`/* cta-standard-audit */` SQL marker comments to keep the two mocked result
contracts deterministic. Lock:

1. the loader pivots long-form `(trade_date, symbol, metric, value)` rows
   into `spot`/`basis_rate`/`inventory`/`profit` columns, one row per
   `(trade_date, symbol)`;
2. metadata records `build_version`, `catalog_version`,
   `pit_mode="conservative"`, `schema`, `materialized_daily=True`;
3. every executed SQL contains both defense clauses:
   `status = 'complete'` build selection and
   `available_at <= ((trade_date::timestamp + time '15:00') AT TIME ZONE 'Asia/Shanghai')`;
4. more than one build version or catalog version in the result raises
   `ValueError` matching `"exactly one"`; duplicate metric rows and empty
   results also raise;
5. the audit query returns lineage columns (`lineage_hash`, `available_at`,
   `vintage_quality`, `staleness_trading_days`, ...) and its build version
   must equal the value query's.

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_pg_source.py -q
```

Expected: attribute failure for `_load_standard_fundamentals`.

- [ ] **Step 3: Implement the loader** *(Tier 1)*

Add:

```python
PILOT_FUNDAMENTAL_SYMBOLS = (
    "M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU",
)
ALLOWED_FUNDAMENTAL_SCHEMAS = frozenset({"commodity_research"})
```

Validate the schema against the allow-list before it appears in any SQL;
never interpolate an arbitrary CLI string.

`_load_standard_fundamentals(conn, *, start, end, symbols, schema)` runs two
queries against `fundamental_daily` joined to `fundamental_build`, both
restricted to the single latest complete conservative build:

```sql
/* cta-standard-values */
SELECT d.trade_date,
       d.product_code AS symbol,
       d.metric,
       d.value::float AS value,
       d.build_version,
       d.catalog_version,
       b.source_recorded_cutoff
FROM commodity_research.fundamental_daily AS d
JOIN commodity_research.fundamental_build AS b
  ON b.build_version = d.build_version
WHERE b.status = 'complete'
  AND b.pit_mode = 'conservative'
  AND b.build_version = (
      SELECT build_version
      FROM commodity_research.fundamental_build
      WHERE status = 'complete' AND pit_mode = 'conservative'
      ORDER BY finished_at DESC
      LIMIT 1
  )
  AND d.available_at <=
      ((d.trade_date::timestamp + time '15:00')
       AT TIME ZONE 'Asia/Shanghai')
ORDER BY d.product_code, d.trade_date, d.metric
```

Append optional `trade_date >= %(start)s`, `trade_date <= %(end)s`, and
`product_code = ANY(%(symbols)s)` clauses instead of passing `None`. The
`available_at` clause is defense-in-depth on top of the builder's own PIT
cutoff. The audit query uses the same WHERE shape and selects the lineage
columns (`source_observation_date`, `available_at`, `series_id`,
`formula_id`, `vintage_quality`, `staleness_trading_days`, `lineage`,
`lineage_hash`).

Reject: empty results ("no complete conservative build"), duplicate
`(trade_date, symbol, metric)` rows, more than one `build_version` or
`catalog_version`, and a value/audit build-version mismatch. Pivot to the
four metric columns, filling absent metrics with NaN. Return
`(wide_frame, audit_frame, metadata)` with:

```python
metadata = {
    "source": "standard",
    "pit_mode": "conservative",
    "build_version": build_version,
    "catalog_version": catalog_version,
    "source_recorded_cutoff": source_recorded_cutoff,
    "schema": schema,
    "materialized_daily": True,
}
```

- [ ] **Step 4: Route sources minimally**

Add to `load_public_cta_data`:

```python
fundamentals_source: str = "standard"
fundamentals_schema: str = "commodity_research"
```

- `standard`: call `_load_standard_fundamentals`;
- `legacy`: call the existing loader unchanged, renamed
  `_load_legacy_fundamentals`, with metadata
  `{"source": "legacy", "materialized_daily": False}` — a diagnostic path
  for the sparse M-only public tables, left untouched;
- `none`: empty fundamentals/audit with `{"source": "none"}`.

Reject any other value. In `cta_gtja.__main__` add:

```python
parser.add_argument(
    "--fundamentals-source",
    choices=["auto", "standard", "legacy", "none"],
    default="auto",
)
```

`auto` resolves to `standard` for `six_factor` and `none` otherwise.
Reject `six_factor` with `fundamentals_source=none`. For a six-factor run
with no explicit `--symbols`, use the exact pilot tuple; a price-volume run
keeps the existing all-price-symbol behavior (so `AU`/`AG` stay available
there but never enter the pilot fundamental run). Print one lineage line per
run:

```text
fundamentals: source=standard pit_mode=conservative build=build-c-1 catalog=v1
```

There is no `--pit-mode` flag (the store publishes conservative only) and no
coverage-policy run classification (see Deferred).

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_pg_source.py -q
git add cta_gtja/pg_source.py cta_gtja/__main__.py tests/test_cta_pg_source.py
git commit -m "feat: load published conservative fundamentals for CTA"
```

### Task 4: Enforce the 6/9 coverage gate before portfolio conversion

**Files:**
- Create: `cta_gtja/coverage.py`
- Create: `tests/test_cta_fundamental_coverage.py`
- Modify: `cta_gtja/strategies.py`
- Modify: `cta_gtja/backtest.py`

- [ ] **Step 1: Write failing coverage tests**

Create `tests/test_cta_fundamental_coverage.py` locking:

1. six of nine finite products per metric per date passes; five raises
   `FundamentalCoverageError` matching
   `"2020-01-02 basis_rate coverage=5 required=6"`;
2. inventory requires at least two long (`score > 0`) and two short
   (`score < 0`) candidates, evaluated only from the first date with at
   least four finite inventory scores (preserves the warm-up without
   waiving later failures); failure message
   `"inventory long=<n> short=<n> required_each=2"`;
3. finite values are counted, not merely non-null objects;
4. with `enforce=False` the same audit rows are returned with `status="fail"`
   and the deterministic `reason` strings, and nothing raises — this is the
   synthetic-fixture escape hatch, not a run classification.

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_fundamental_coverage.py -q
```

Expected: import failure for `cta_gtja.coverage`.

- [ ] **Step 3: Implement the gate and wire it before `.fillna(0.0)`**

`cta_gtja/coverage.py` implements `FundamentalCoverageError`,
`evaluate_daily_fundamental_coverage(matrices, *, symbols, required_products,
enforce)` and `evaluate_inventory_sides(scores, *, required_each, enforce)`,
both returning one audit row per date/metric with the columns:

```python
COVERAGE_COLUMNS = [
    "trade_date", "check", "metric",
    "available_products", "required_products",
    "long_candidates", "short_candidates", "required_each_side",
    "status", "reason",
]
```

and raising one error containing the sorted failure reasons when
`enforce=True` (the default).

In `cta_gtja/strategies.py`, extend `build_factor_sleeves` and both public
strategy entry points with `enforce_coverage: bool = True`. When fundamental
factors are requested: build the raw `basis_rate`/`inventory`/`profit`
matrices, evaluate the daily 6-product gate, compute factor scores, evaluate
inventory two-sided coverage on the unfilled score matrix, and only then
convert missing factor weights to zero. `build_factor_sleeves` returns a
third value `coverage_audit`; both callers unpack it and pass
`fundamental_coverage=coverage_audit` to `CTABacktester.run`. Do not catch
`FundamentalCoverageError` in the strategy layer — the CLI terminates the
run with its precise reason.

In `cta_gtja/backtest.py`, add to `CTABacktestResult`:

```python
fundamental_coverage: pd.DataFrame = field(default_factory=pd.DataFrame)
```

and the matching `CTABacktester.run` keyword (copied, defaulting to empty).

The existing four-symbol `_sample_cta_data` fixture cannot satisfy 6/9:
update its six-factor calls to pass `enforce_coverage=False` explicitly and
assert the audit records the failures. Do not change the default.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_fundamental_coverage.py tests/test_cta_strategy.py -q
git add cta_gtja/coverage.py cta_gtja/strategies.py cta_gtja/backtest.py \
  tests/test_cta_fundamental_coverage.py tests/test_cta_strategy.py
git commit -m "feat: gate CTA fundamental cross-section coverage"
```

### Task 5: Lock raw-price basis and bounded missing-data behavior

**Files:**
- Modify: `cta_gtja/factors.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing basis invariants** *(Tier 1 scope)*

Add tests locking:

1. the basis fallback divides spot by `close_raw` (a 4x-adjusted `close`
   must not change the result: spot 110, close_raw 100, adjusted close 400
   → basis 0.10);
2. with no published `basis_rate` and no `close_raw`, basis stays missing —
   the adjusted close is never used;
3. for a dataset whose metadata has `materialized_daily=True`, a missing
   final `basis_rate`/`inventory`/`profit` row stays NaN in factor scores —
   factor code must not forward-fill past the standard layer's staleness
   decision.

- [ ] **Step 2: Verify RED, then implement** *(Tier 1)*

Replace `BasisFactor.compute`:

```python
def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
    basis = data.fundamental_matrix("basis_rate", symbols=symbols)
    if basis.empty:
        spot = data.fundamental_matrix("spot", symbols=symbols)
        close_raw = data.price_matrix("close_raw", symbols=symbols)
        if spot.empty or close_raw.empty:
            return _empty_like(data, symbols)
        positive_raw = close_raw.where(close_raw > 0)
        basis = spot.reindex_like(positive_raw) / positive_raw - 1.0
    return _align_fundamental(basis, data, symbols)
```

The fallback exists for legacy/file diagnostics; a standard build is
expected to contain `basis_rate`. Add one helper and use it in
`InventoryFactor` and `ProfitFactor` as well:

```python
def _align_fundamental(values, data, symbols):
    aligned = values.reindex(index=data.dates, columns=symbols)
    if data.fundamental_metadata.get("materialized_daily") is True:
        return aligned
    return aligned.ffill()
```

The standard builder already applies the catalog's maximum staleness and
intentionally omits expired values; re-forward-filling in the strategy would
defeat that PIT decision. Legacy/file inputs keep the existing forward fill.

- [ ] **Step 3: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_strategy.py tests/test_cta_fundamental_coverage.py -q
git add cta_gtja/factors.py tests/test_cta_strategy.py
git commit -m "fix: use raw futures prices for CTA basis"
```

### Task 6: Lineage-auditable results, PIT proof, and comparison runs

**Files:**
- Modify: `cta_gtja/backtest.py`, `cta_gtja/__main__.py`
- Modify: `tests/test_cta_strategy.py`, `tests/test_cta_fundamental_pit.py`
- Create: `docs/cta-fundamentals.md`
- Produce (not committed): `output/cta_fundamental_medium_conservative*.xlsx`, `output/cta_fundamental_composite_conservative*.xlsx`, `output/cta_price_volume_control*.xlsx`

- [ ] **Step 1: Carry lineage into every result and workbook**

Failing tests first: a result built with fundamental metadata writes
`fundamental_coverage`, `fundamental_lineage`, and one-row
`fundamental_build` sheets (stable columns `source, pit_mode, build_version,
catalog_version, source_recorded_cutoff, schema, materialized_daily`); a
price-volume result writes no coverage/lineage sheets and a `fundamental_build`
sheet with `source="none"`. Then:

- add `fundamental_lineage: pd.DataFrame` and
  `fundamental_metadata: dict[str, object]` fields to `CTABacktestResult`,
  populated in `CTABacktester.run` from
  `self.data.fundamental_quality.copy()` and
  `dict(self.data.fundamental_metadata)`;
- extend `write_cta_outputs` with the three sheets (coverage/lineage only
  when non-empty; the build sheet always, `source` defaulting to
  `"unknown"`); keep JSON lineage dictionaries out of the build sheet;
- add a `_fundamental_coverage_summary` CLI line, e.g.
  `fundamental_coverage: rows=4980 failed=0 minimum=6`, printing the failed
  count and first three deterministic reasons when enforcement was disabled.

- [ ] **Step 2: Prove no future-fundamental dependency** *(Tier 1 scope)*

Add to `tests/test_cta_fundamental_pit.py`, using the Task 1 fixtures with
`materialized_daily=True` metadata: mutate all four fundamental columns
strictly after date index 260 by `*= -1000.0` and assert `weights`,
`period_returns`, and `equity` are identical through the cutoff. Ensure the
fixture inventory produces at least two positive and two negative scores on
every post-warm-up date so the run exercises the enforced gate. Add loader
defense tests asserting the standard SQL contains the `status = 'complete'`
single-build selection and the
`available_at <= ... 15:00 ... Asia/Shanghai` clause.

- [ ] **Step 3: Run the complete local regression suite**

```bash
cd /home/elfbob/claude-code/futures_strategies
git diff --check
.venv/bin/python -m pytest \
  tests/test_cta_data_quality.py \
  tests/test_cta_pg_source.py \
  tests/test_cta_strategy.py \
  tests/test_cta_fundamental_coverage.py \
  tests/test_cta_fundamental_pit.py -q
.venv/bin/python -m pytest -q
```

Expected: all CTA tests pass; if an unrelated pre-existing failure appears in
the full suite, preserve its exact output and prove the focused CTA suite
still passes — do not weaken a test to obtain green.

- [ ] **Step 4: Run the comparison runs against the published build**

Six-factor conservative (medium equal weight, then `high_composite` with the
same flags):

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy medium_equal_weight \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_fundamental_medium_conservative
```

Price-volume control:

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set price_volume \
  --fundamentals-source none \
  --strategy both \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_price_volume_control
```

Expected: each six-factor run identifies exactly one conservative build and
catalog version, reports zero coverage failures, and its workbook carries the
build, lineage, and coverage sheets; no `AU`/`AG` weight appears; the control
runs without querying or claiming a fundamental build. If the published
build starts later than 2019-01-01, the gate must fail with the first
uncovered date — document the actual first covered date and re-run with
`--start` set to exactly that date; do not silently truncate in the loader.

⚠️ Interpretation limit (applies until the upstream price-adjustment fix
lands): these are data-path comparison runs. Their return series are not yet
trustworthy strategy evidence, and the research deck's Sharpe (`2.43`),
return (`17.14%`), and holding-period figures are **not acceptance
targets** — not now, not after the fix. Acceptance is: PIT isolation proven,
raw-basis correctness, coverage enforcement, reproducibility, and lineage in
every report.

- [ ] **Step 5: Record evidence and hand off** *(Tier 3)*

Create `docs/cta-fundamentals.md` with the two operator examples above, the
statement that `legacy` is diagnostic only, the interpretation-limit note,
and an evidence table per run: timestamp, source, build version, catalog
version, date range, universe, coverage minimum and failed rows, annualized
return/vol, Sharpe, max drawdown, turnover, output workbook path. Record the
medium equal-weight, high-composite, and price-volume control side by side
and interpret differences as data/factor evidence.

```bash
cd /home/elfbob/claude-code/futures_strategies
git add docs/cta-fundamentals.md cta_gtja/backtest.py cta_gtja/__main__.py \
  tests/test_cta_strategy.py tests/test_cta_fundamental_pit.py
git commit -m "feat: report CTA fundamental lineage and PIT evidence"
git status --short --branch
```

Do not commit generated `.xlsx`/`.png` files. Handoff report: build and
catalog versions, achieved target coverage (basis/inventory/profit), the
actual covered backtest interval, test commands and pass counts, workbook
locations, and any products that remain missing or degraded with their
catalog reason codes. Do not summarize success with the deck Sharpe — the
deliverable is an auditable fundamental data path and a reproducible
six-factor comparison.

## Deferred until CTA is trusted and in monthly use

Recorded per the 2026-07-31 anti-redundancy ruling (vertical slice first;
deferred generality is recorded, not built). There are no formal runs until
the upstream price-adjustment fix lands and the user promotes CTA to monthly
use. Original designs: git history at `e31e1e5`.

- **Formal/degraded run-class gating** (`--coverage-policy`,
  `formal_eligible` certification chain, the CLI rejection matrix over
  source × policy × symbols): nothing to certify yet; the gate enforces by
  default and synthetic fixtures opt out with a plain boolean.
- **Legacy-table degraded-research option machinery** (certification and
  labeling built around the legacy loader): the legacy loader remains an
  unmodified diagnostic path selected explicitly.
- **File-input provenance proofs** (`fundamentals.metadata.json` +
  `fundamentals_quality` file contract validating claimed exports): file
  datasets stay uncertified research convenience and cannot claim build
  lineage.
- **Strict PIT mode plumbing** (`--pit-mode`, strict loader parameters, the
  strict-readiness acceptance run): the store publishes conservative only;
  no strict history exists.
- **Run-class certification in reports** (formal stamps in workbooks and
  summaries): reports record lineage; they do not certify runs.
