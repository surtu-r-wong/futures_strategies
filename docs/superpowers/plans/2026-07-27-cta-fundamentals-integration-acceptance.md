# CTA Fundamentals Integration and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the CTA six-factor strategy consume the published point-in-time commodity fundamental layer, preserve raw futures prices for basis, enforce the approved `6/9` daily coverage contract, and produce auditable comparisons without treating the research deck's Sharpe ratio as an acceptance target.

**Architecture:** The PostgreSQL reader loads adjusted prices for trading and retains raw OHLC for economic calculations. A separate standard-fundamentals reader pivots exactly one current published build for the requested PIT mode and carries its lineage into the backtest result. Formal six-factor runs pass a pre-weight coverage gate; legacy public tables remain available only through an explicit degraded-research option. File input can be formal only when an adjacent metadata file proves that it is an exported published build.

**Tech Stack:** Python 3.13, pandas, NumPy, psycopg2, argparse, openpyxl, pytest

---

**Design reference:** `docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`

**Prerequisites:**

1. Complete `docs/superpowers/plans/2026-07-27-wind-fundamentals-catalog-capture.md`.
2. Complete `docs/superpowers/plans/2026-07-27-commodity-fundamentals-store-materialize.md`.
3. Publish a conservative build in
   `commodity_research.current_fundamental_daily`.

**Repository:** `/home/elfbob/claude-code/futures_strategies`

## File map

| File | Responsibility |
|---|---|
| `cta_gtja/data.py` | Carry fundamental metadata and quality with the in-memory dataset |
| `cta_gtja/pg_source.py` | Load raw/adjusted futures prices and select standard, legacy, or no fundamentals |
| `cta_gtja/coverage.py` | Formal/degraded coverage evaluation and reason codes |
| `cta_gtja/factors.py` | Use published basis first and raw close for the only permitted fallback |
| `cta_gtja/strategies.py` | Run coverage checks before missing scores become zero weights |
| `cta_gtja/backtest.py` | Propagate fundamental audit data and write it to Excel |
| `cta_gtja/__main__.py` | Expose source, PIT, coverage, and schema choices with safe defaults |
| `tests/test_cta_pg_source.py` | SQL selection, raw-price preservation, and metadata tests |
| `tests/test_cta_strategy.py` | Factor, result, CLI-adjacent, and report regression tests |
| `tests/test_cta_fundamental_coverage.py` | `6/9` and inventory two-sided gate tests |
| `tests/test_cta_fundamental_pit.py` | Future-vintage mutation and price-volume control tests |
| `docs/cta-fundamentals.md` | Operator contract, examples, and interpretation limits |

The approved pilot universe is:

```python
PILOT_FUNDAMENTAL_SYMBOLS = (
    "M",
    "RB",
    "CU",
    "AL",
    "TA",
    "PP",
    "MA",
    "BU",
    "RU",
)
```

`AU` and `AG` are not members of this pilot. Do not silently substitute them
for a missing pilot product.

### Task 1: Freeze the current CTA control behavior

**Files:**
- Read: `cta_gtja/data.py`
- Read: `cta_gtja/pg_source.py`
- Read: `cta_gtja/factors.py`
- Read: `cta_gtja/strategies.py`
- Modify: `tests/test_cta_strategy.py`
- Create: `tests/test_cta_fundamental_pit.py`

- [ ] **Step 1: Record the starting tree and focused baseline**

Run:

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

Create `tests/test_cta_fundamental_pit.py` with a deterministic dataset and
this regression:

```python
import pandas as pd
from pandas.testing import assert_frame_equal, assert_series_equal

from cta_gtja.data import CTADataSet
from cta_gtja.factors import price_volume_cta_factors
from cta_gtja.strategies import run_medium_equal_weight


def test_price_volume_control_is_independent_of_fundamental_values(
    complete_price_frame,
    complete_fundamental_frame,
):
    original = CTADataSet(
        prices=complete_price_frame,
        fundamentals=complete_fundamental_frame,
    )
    changed_fundamentals = complete_fundamental_frame.copy()
    for column in ("spot", "basis_rate", "inventory", "profit"):
        changed_fundamentals[column] = (
            changed_fundamentals[column].astype(float) * 1000.0 + 777.0
        )
    changed = CTADataSet(
        prices=complete_price_frame.copy(),
        fundamentals=changed_fundamentals,
    )

    left = run_medium_equal_weight(
        original,
        factors=price_volume_cta_factors(),
        cost_bps=0.0,
    )
    right = run_medium_equal_weight(
        changed,
        factors=price_volume_cta_factors(),
        cost_bps=0.0,
    )

    assert_frame_equal(left.weights, right.weights)
    assert_series_equal(left.period_returns, right.period_returns)
    assert_series_equal(left.equity, right.equity)
```

Define `complete_price_frame` and `complete_fundamental_frame` as module-local
pytest fixtures. Use at least 300 business dates and the exact nine pilot
symbols so all long-window factors can warm up. Generate values
deterministically from date and symbol positions; do not use random numbers.

- [ ] **Step 3: Run the control test**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_fundamental_pit.py::test_price_volume_control_is_independent_of_fundamental_values \
  -q
```

Expected: PASS before production changes. This test is the control that must
remain green throughout the plan.

- [ ] **Step 4: Commit only the baseline control**

```bash
cd /home/elfbob/claude-code/futures_strategies
git add tests/test_cta_fundamental_pit.py
git commit -m "test: freeze CTA price-volume control behavior"
```

### Task 2: Preserve raw prices and extend the dataset audit contract

**Files:**
- Modify: `cta_gtja/data.py`
- Modify: `cta_gtja/pg_source.py`
- Modify: `tests/test_cta_pg_source.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing raw-price and dataset tests**

Add:

```python
def test_apply_adjustment_policy_preserves_raw_ohlc():
    raw = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "symbol": ["RB"],
            "contract": ["RB2005.SHF"],
            "open_raw": [3500.0],
            "high_raw": [3520.0],
            "low_raw": [3480.0],
            "close_raw": [3510.0],
            "open_ba": [7000.0],
            "high_ba": [7040.0],
            "low_ba": [6960.0],
            "close_ba": [7020.0],
            "open_fa": [1750.0],
            "high_fa": [1760.0],
            "low_fa": [1740.0],
            "close_fa": [1755.0],
        }
    )
    quality = pd.DataFrame(
        {
            "base_symbol": ["RB"],
            "selected_adj": ["ba"],
            "included": [True],
        }
    )

    out = _apply_adjustment_policy(raw, quality)

    assert out.loc[0, "close"] == 7020.0
    assert out.loc[0, "close_raw"] == 3510.0
    assert out.loc[0, "open_raw"] == 3500.0


def test_slice_preserves_fundamental_audit_and_metadata():
    data = CTADataSet(
        prices=_prices(("M", "RB")),
        fundamentals=_fundamentals(("M", "RB")),
        data_quality=pd.DataFrame({"base_symbol": ["M", "RB"]}),
        fundamental_quality=pd.DataFrame(
            {
                "trade_date": [date(2020, 1, 2)] * 2,
                "product_code": ["M", "RB"],
            }
        ),
        fundamental_metadata={
            "source": "standard",
            "pit_mode": "conservative",
            "build_version": "build-c-1",
            "materialized_daily": True,
            "formal_eligible": True,
        },
    )

    sliced = data.slice(symbols=["M"])

    assert sliced.fundamental_quality["product_code"].tolist() == ["M"]
    assert sliced.fundamental_metadata == data.fundamental_metadata
    assert sliced.fundamental_metadata is not data.fundamental_metadata
```

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py \
  tests/test_cta_strategy.py::test_slice_preserves_fundamental_audit_and_metadata \
  -q
```

Expected: failures for missing raw columns and new dataset fields.

- [ ] **Step 3: Extend `CTADataSet`**

Add fields after `data_quality`:

```python
fundamental_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
fundamental_metadata: dict[str, object] = field(default_factory=dict)
```

In `slice`, filter `fundamental_quality` by `product_code` when present, copy
the metadata with `dict(self.fundamental_metadata)`, and return all five
fields. In `from_dir`, leave the new fields empty until Task 4 supplies file
metadata.

Update `normalize_prices` so these columns are numeric when present:

```python
PRICE_NUMBER_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "volume",
    "amount",
    "turnover",
    "open_interest",
)
```

- [ ] **Step 4: Preserve raw OHLC in the adjustment output**

In `_apply_adjustment_policy`, add existing `open_raw`, `high_raw`, `low_raw`,
and `close_raw` columns to `base_cols`. Continue to populate `open`, `high`,
`low`, and `close` from the audited trading lineage. Raw fields are carried
for basis and audit only; they do not replace the selected trading lineage.

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py \
  tests/test_cta_strategy.py -q
git add cta_gtja/data.py cta_gtja/pg_source.py \
  tests/test_cta_pg_source.py tests/test_cta_strategy.py
git commit -m "feat: preserve raw CTA prices and fundamental audit"
```

Expected: focused tests pass and the commit succeeds.

### Task 3: Load one published PIT fundamentals build from PostgreSQL

**Files:**
- Modify: `cta_gtja/pg_source.py`
- Modify: `tests/test_cta_pg_source.py`

- [ ] **Step 1: Write failing standard-loader tests**

Add tests that monkeypatch `_read_sql` and inspect both SQL and parameters:

```python
def test_standard_loader_selects_one_current_pit_build(monkeypatch):
    calls = []

    def fake_read_sql(sql, conn, *, params):
        calls.append((sql, params))
        if "/* cta-standard-values */" in sql:
            return pd.DataFrame(
                {
                    "trade_date": [date(2020, 1, 2)] * 4,
                    "symbol": ["M"] * 4,
                    "metric": ["spot", "basis_rate", "inventory", "profit"],
                    "value": [3100.0, 0.05, 200000.0, 150.0],
                    "build_version": ["build-c-1"] * 4,
                    "catalog_version": ["v1"] * 4,
                    "source_recorded_cutoff": [
                        pd.Timestamp("2026-07-27 18:00", tz="Asia/Shanghai")
                    ] * 4,
                }
            )
        if "/* cta-standard-audit */" in sql:
            return pd.DataFrame(
                {
                    "trade_date": [date(2020, 1, 2)],
                    "product_code": ["M"],
                    "metric": ["basis_rate"],
                    "build_version": ["build-c-1"],
                    "catalog_version": ["v1"],
                    "pit_mode": ["conservative"],
                    "source_recorded_cutoff": [
                        pd.Timestamp("2026-07-27 18:00", tz="Asia/Shanghai")
                    ],
                    "available_at": [
                        pd.Timestamp("2020-01-02 14:00", tz="Asia/Shanghai")
                    ],
                    "source_observation_date": [date(2020, 1, 2)],
                    "vintage_quality": ["backfill_final"],
                    "staleness_trading_days": [0],
                    "lineage_hash": ["abc"],
                    "lineage": [{"spot": "m.spot.primary"}],
                }
            )
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr("cta_gtja.pg_source._read_sql", fake_read_sql)

    frame, audit, metadata = _load_standard_fundamentals(
        object(),
        start=date(2020, 1, 1),
        end=date(2020, 12, 31),
        symbols=["M"],
        pit_mode="conservative",
        schema="commodity_research",
    )

    assert frame.loc[0, "basis_rate"] == 0.05
    assert metadata["build_version"] == "build-c-1"
    assert metadata["formal_eligible"] is True
    assert audit.loc[0, "lineage_hash"] == "abc"
    assert all(call[1]["pit_mode"] == "conservative" for call in calls)
    assert all("available_at <=" in call[0] for call in calls)


def test_standard_loader_rejects_multiple_current_build_versions(monkeypatch):
    monkeypatch.setattr(
        "cta_gtja.pg_source._read_sql",
        lambda *args, **kwargs: pd.DataFrame(
            {
                "trade_date": [date(2020, 1, 2), date(2020, 1, 3)],
                "symbol": ["M", "M"],
                "metric": ["spot", "spot"],
                "value": [1.0, 2.0],
                "build_version": ["build-c-1", "build-c-2"],
                "catalog_version": ["v1", "v1"],
            }
        ),
    )

    with pytest.raises(ValueError, match="exactly one current build"):
        _load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M"],
            pit_mode="conservative",
            schema="commodity_research",
        )
```

The explicit SQL marker comments keep the two mocked result contracts
deterministic.

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py -q
```

Expected: import or attribute failure for `_load_standard_fundamentals`.

- [ ] **Step 3: Implement safe schema qualification and value selection**

Add:

```python
PILOT_FUNDAMENTAL_SYMBOLS = (
    "M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU",
)
ALLOWED_FUNDAMENTAL_SCHEMAS = frozenset({"commodity_research"})


def _fundamental_relation(schema: str) -> str:
    if schema not in ALLOWED_FUNDAMENTAL_SCHEMAS:
        raise ValueError(f"unsupported fundamentals schema: {schema!r}")
    return f"{schema}.current_fundamental_daily"
```

Use `_fundamental_relation` only after allow-list validation; never interpolate
an arbitrary CLI string into SQL.

Implement `_load_standard_fundamentals` with two queries against the same
relation. The value query is long-form:

```sql
/* cta-standard-values */
SELECT
    trade_date,
    product_code AS symbol,
    metric,
    value::float AS value,
    build_version,
    catalog_version
FROM commodity_research.current_fundamental_daily
WHERE pit_mode = %(pit_mode)s
  AND available_at <=
      ((trade_date::timestamp + time '15:00')
       AT TIME ZONE 'Asia/Shanghai')
  AND trade_date >= %(start)s
  AND trade_date <= %(end)s
  AND product_code = ANY(%(symbols)s)
ORDER BY product_code, trade_date, metric
```

Build optional clauses instead of passing `None` for absent start/end/symbols.
Pivot `metric` into `spot`, `basis_rate`, `inventory`, and `profit`, preserving
one row per `(trade_date, symbol)`. Reject duplicate metric rows, no rows, more
than one build version, or more than one catalog version.

The audit query selects:

```sql
/* cta-standard-audit */
SELECT
    trade_date,
    product_code,
    metric,
    build_version,
    catalog_version,
    pit_mode,
    source_observation_date,
    available_at,
    series_id,
    formula_id,
    vintage_quality,
    staleness_trading_days,
    lineage,
    lineage_hash
FROM commodity_research.current_fundamental_daily
WHERE pit_mode = %(pit_mode)s
  AND available_at <=
      ((trade_date::timestamp + time '15:00')
       AT TIME ZONE 'Asia/Shanghai')
```

Apply the same date and symbol clauses. Return:

```python
metadata = {
    "source": "standard",
    "pit_mode": pit_mode,
    "build_version": build_version,
    "catalog_version": catalog_version,
    "schema": schema,
    "materialized_daily": True,
    "formal_eligible": True,
}
```

Use this complete loader body after `_fundamental_relation`:

```python
STANDARD_METRICS = ("spot", "basis_rate", "inventory", "profit")


def _standard_clauses(*, start, end, symbols, pit_mode):
    clauses = [
        "pit_mode = %(pit_mode)s",
        "available_at <= ((trade_date::timestamp + time '15:00') "
        "AT TIME ZONE 'Asia/Shanghai')",
    ]
    params = {"pit_mode": pit_mode}
    if start is not None:
        clauses.append("trade_date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("trade_date <= %(end)s")
        params["end"] = end
    if symbols:
        clauses.append("product_code = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    return clauses, params


def _one_value(frame: pd.DataFrame, column: str):
    values = frame[column].dropna().drop_duplicates().tolist()
    if len(values) != 1:
        raise ValueError(
            f"standard fundamentals require exactly one current {column}; "
            f"found={values}"
        )
    return values[0]


def _load_standard_fundamentals(
    conn,
    *,
    start,
    end,
    symbols,
    pit_mode,
    schema,
):
    if pit_mode not in {"conservative", "strict"}:
        raise ValueError(f"unsupported pit_mode: {pit_mode}")
    relation = _fundamental_relation(schema)
    clauses, params = _standard_clauses(
        start=start,
        end=end,
        symbols=symbols,
        pit_mode=pit_mode,
    )
    where = " AND ".join(clauses)
    values = _read_sql(
        f"""
        /* cta-standard-values */
        SELECT
            trade_date,
            product_code AS symbol,
            metric,
            value::float AS value,
            build_version,
            catalog_version,
            source_recorded_cutoff
        FROM {relation}
        WHERE {where}
        ORDER BY product_code, trade_date, metric
        """,
        conn,
        params=params,
    )
    if values.empty:
        raise ValueError(
            f"no current {pit_mode} standard fundamental build in {schema}"
        )
    duplicate = values.duplicated(
        ["trade_date", "symbol", "metric"],
        keep=False,
    )
    if duplicate.any():
        keys = values.loc[
            duplicate,
            ["trade_date", "symbol", "metric"],
        ].to_dict("records")
        raise ValueError(f"duplicate standard fundamental metrics: {keys[:10]}")
    build_version = str(_one_value(values, "build_version"))
    catalog_version = str(_one_value(values, "catalog_version"))
    source_recorded_cutoff = _one_value(values, "source_recorded_cutoff")

    audit = _read_sql(
        f"""
        /* cta-standard-audit */
        SELECT
            trade_date,
            product_code,
            metric,
            build_version,
            catalog_version,
            pit_mode,
            source_recorded_cutoff,
            source_observation_date,
            available_at,
            series_id,
            formula_id,
            vintage_quality,
            staleness_trading_days,
            lineage,
            lineage_hash
        FROM {relation}
        WHERE {where}
        ORDER BY product_code, trade_date, metric
        """,
        conn,
        params=params,
    )
    if audit.empty:
        raise ValueError("standard fundamental audit query returned no rows")
    if str(_one_value(audit, "build_version")) != build_version:
        raise ValueError("standard value and audit build versions differ")
    if _one_value(audit, "pit_mode") != pit_mode:
        raise ValueError("standard audit returned a different pit_mode")

    wide = (
        values.pivot(
            index=["trade_date", "symbol"],
            columns="metric",
            values="value",
        )
        .rename_axis(columns=None)
        .reset_index()
    )
    for metric in STANDARD_METRICS:
        if metric not in wide:
            wide[metric] = float("nan")
    wide = wide[["trade_date", "symbol", *STANDARD_METRICS]]
    metadata = {
        "source": "standard",
        "pit_mode": pit_mode,
        "build_version": build_version,
        "catalog_version": catalog_version,
        "source_recorded_cutoff": source_recorded_cutoff,
        "schema": schema,
        "materialized_daily": True,
        "formal_eligible": True,
    }
    return wide, audit, metadata
```

- [ ] **Step 4: Route `load_public_cta_data` explicitly**

Add parameters:

```python
fundamentals_source: str = "standard"
pit_mode: str = "conservative"
fundamentals_schema: str = "commodity_research"
```

Accepted sources are:

- `standard`: call `_load_standard_fundamentals`;
- `legacy`: call the existing loader, renamed
  `_load_legacy_fundamentals`, and attach
  `{"source": "legacy", "formal_eligible": False,
  "materialized_daily": False}`;
- `none`: return an empty fundamental frame, empty audit, and
  `{"source": "none", "formal_eligible": False,
  "materialized_daily": False}`.

Reject any other value. The legacy SQL remains unchanged and therefore remains
an explicit diagnostic path for the sparse M-only tables; it is not the
default for six-factor execution.

Replace the existing unconditional `_load_fundamentals` call with:

```python
if fundamentals_source == "standard":
    fundamentals, fundamental_quality, fundamental_metadata = (
        _load_standard_fundamentals(
            conn,
            start=start,
            end=end,
            symbols=symbols,
            pit_mode=pit_mode,
            schema=fundamentals_schema,
        )
    )
elif fundamentals_source == "legacy":
    fundamentals = _load_legacy_fundamentals(
        conn,
        start=start,
        end=end,
        symbols=symbols,
    )
    fundamental_quality = pd.DataFrame()
    fundamental_metadata = {
        "source": "legacy",
        "pit_mode": None,
        "materialized_daily": False,
        "formal_eligible": False,
    }
elif fundamentals_source == "none":
    fundamentals = pd.DataFrame(columns=["trade_date", "symbol"])
    fundamental_quality = pd.DataFrame()
    fundamental_metadata = {
        "source": "none",
        "pit_mode": None,
        "materialized_daily": False,
        "formal_eligible": False,
    }
else:
    raise ValueError(
        f"unsupported fundamentals_source: {fundamentals_source!r}"
    )

return CTADataSet(
    prices=normalize_prices(prices),
    fundamentals=normalize_fundamentals(fundamentals),
    data_quality=quality,
    fundamental_quality=fundamental_quality,
    fundamental_metadata=fundamental_metadata,
)
```

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_pg_source.py -q
git add cta_gtja/pg_source.py tests/test_cta_pg_source.py
git commit -m "feat: load published PIT fundamentals for CTA"
```

Expected: all PostgreSQL source tests pass.

### Task 4: Make file input auditable and configure CLI routing

**Files:**
- Modify: `cta_gtja/data.py`
- Modify: `cta_gtja/__main__.py`
- Modify: `tests/test_cta_strategy.py`
- Create: `docs/cta-fundamentals.md`

- [ ] **Step 1: Write failing file-metadata tests**

Add:

```python
def test_file_dataset_is_formal_only_with_matching_metadata(tmp_path):
    _write_prices(tmp_path / "prices.csv")
    _write_fundamentals(tmp_path / "fundamentals.csv")
    (tmp_path / "fundamentals.metadata.json").write_text(
        json.dumps(
            {
                "source": "standard-export",
                "pit_mode": "conservative",
                "build_version": "build-c-1",
                "catalog_version": "v1",
                "source_recorded_cutoff": "2026-07-27T18:00:00+08:00",
                "materialized_daily": True,
                "formal_eligible": True,
                "symbols": [
                    "M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU"
                ],
                "metrics": ["spot", "basis_rate", "inventory", "profit"],
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "product_code": ["M"],
            "metric": ["basis_rate"],
            "lineage_hash": ["abc"],
        }
    ).to_csv(tmp_path / "fundamentals_quality.csv", index=False)

    data = CTADataSet.from_dir(tmp_path)

    assert data.fundamental_metadata["build_version"] == "build-c-1"
    assert data.fundamental_metadata["formal_eligible"] is True


def test_file_dataset_without_metadata_is_degraded(tmp_path):
    _write_prices(tmp_path / "prices.csv")
    _write_fundamentals(tmp_path / "fundamentals.csv")

    data = CTADataSet.from_dir(tmp_path)

    assert data.fundamental_metadata["source"] == "files-unverified"
    assert data.fundamental_metadata["formal_eligible"] is False
```

Also test invalid JSON, missing required keys, an unsupported PIT mode, and a
metadata symbol absent from the data. Each must raise a descriptive
`ValueError`; do not silently downgrade malformed metadata.

- [ ] **Step 2: Verify RED**

Run the focused new tests and expect failures because metadata is not read.

- [ ] **Step 3: Implement `fundamentals.metadata.json` loading**

Use `json.loads` and validate exact required keys. When
`fundamentals_quality.csv` or `fundamentals_quality.parquet` exists, read it
into `fundamental_quality`; reject having both. Missing quality is permitted
for an unverified file source but makes `formal_eligible=False`.

For a claimed standard export, require:

```python
FORMAL_METADATA_KEYS = {
    "source",
    "pit_mode",
    "build_version",
    "catalog_version",
    "source_recorded_cutoff",
    "materialized_daily",
    "formal_eligible",
    "symbols",
    "metrics",
}
```

Require `materialized_daily is True`, all four metrics, a non-empty build and
catalog version, and a timezone-aware recorded cutoff. Copy metadata before
storing it on the frozen dataclass.

Add these helpers to `cta_gtja/data.py` and call them from `from_dir`:

```python
import json


FORMAL_METADATA_KEYS = {
    "source",
    "pit_mode",
    "build_version",
    "catalog_version",
    "source_recorded_cutoff",
    "materialized_daily",
    "formal_eligible",
    "symbols",
    "metrics",
}


def _read_fundamental_quality(root: Path) -> pd.DataFrame:
    csv_path = root / "fundamentals_quality.csv"
    parquet_path = root / "fundamentals_quality.parquet"
    if csv_path.exists() and parquet_path.exists():
        raise ValueError("keep only one fundamentals_quality file")
    if csv_path.exists():
        return pd.read_csv(csv_path)
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    return pd.DataFrame()


def _read_fundamental_metadata(
    root: Path,
    fundamentals: pd.DataFrame,
    quality: pd.DataFrame,
) -> dict[str, object]:
    path = root / "fundamentals.metadata.json"
    if not path.exists():
        return {
            "source": "files-unverified",
            "materialized_daily": False,
            "formal_eligible": False,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid fundamental metadata JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("fundamental metadata must be an object")
    missing = FORMAL_METADATA_KEYS - set(payload)
    if missing:
        raise ValueError(f"fundamental metadata missing keys: {sorted(missing)}")
    if payload["source"] != "standard-export":
        raise ValueError("verified file source must be standard-export")
    if payload["pit_mode"] not in {"conservative", "strict"}:
        raise ValueError("fundamental metadata has unsupported pit_mode")
    if payload["materialized_daily"] is not True:
        raise ValueError("standard export must be materialized_daily")
    if not payload["build_version"] or not payload["catalog_version"]:
        raise ValueError("standard export needs build and catalog versions")
    cutoff = pd.Timestamp(payload["source_recorded_cutoff"])
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("source_recorded_cutoff must be timezone-aware")
    required_metrics = {"spot", "basis_rate", "inventory", "profit"}
    if set(payload["metrics"]) != required_metrics:
        raise ValueError("standard export must declare all four metrics")
    data_symbols = set(fundamentals["symbol"].astype(str).unique())
    metadata_symbols = {str(value) for value in payload["symbols"]}
    absent = sorted(metadata_symbols - data_symbols)
    if absent:
        raise ValueError(f"metadata symbols absent from fundamentals: {absent}")
    metadata = dict(payload)
    if quality.empty:
        metadata["formal_eligible"] = False
        metadata["quality_reason"] = "fundamentals_quality file missing"
    elif payload["formal_eligible"] is not True:
        metadata["formal_eligible"] = False
    return metadata
```

Replace `from_dir`'s return with:

```python
quality = _read_fundamental_quality(root)
metadata = _read_fundamental_metadata(root, fundamentals, quality)
return cls(
    prices=normalize_prices(prices),
    fundamentals=normalize_fundamentals(fundamentals),
    fundamental_quality=quality,
    fundamental_metadata=metadata,
)
```

- [ ] **Step 4: Add CLI options and safe defaults**

In `cta_gtja.__main__`, add:

```text
--fundamentals-source auto|standard|legacy|none
--pit-mode conservative|strict
--coverage-policy formal|degraded
--fundamentals-schema commodity_research
```

Resolution rules:

```python
def _resolve_fundamentals_source(factor_set, requested):
    if requested != "auto":
        return requested
    return "standard" if factor_set == "six_factor" else "none"
```

Reject these combinations before loading:

- `six_factor` with `fundamentals_source=none`;
- `coverage_policy=formal` with `fundamentals_source=legacy`;
- `coverage_policy=formal` with an unverified file dataset;
- a standard source whose PIT mode differs from file metadata;
- fewer than six requested pilot symbols in a formal six-factor run;
- any formal six-factor symbol outside the nine-product pilot.

For a formal six-factor run with no explicit `--symbols`, use the exact pilot
tuple. For a price-volume run with no explicit symbols, retain the existing
all-price-symbol behavior. `AU` and `AG` therefore remain available to an
explicit price-volume run but never enter the pilot fundamental run.

Print:

```text
fundamentals: source=standard pit_mode=conservative build=build-c-1 catalog=v1
coverage_policy: formal
fundamental_universe: M,RB,CU,AL,TA,PP,MA,BU,RU
```

Add the parser arguments exactly:

```python
parser.add_argument(
    "--fundamentals-source",
    choices=["auto", "standard", "legacy", "none"],
    default="auto",
)
parser.add_argument(
    "--pit-mode",
    choices=["conservative", "strict"],
    default="conservative",
)
parser.add_argument(
    "--coverage-policy",
    choices=["formal", "degraded"],
    default="formal",
)
parser.add_argument(
    "--fundamentals-schema",
    choices=["commodity_research"],
    default="commodity_research",
)
```

Add and call this validation before strategy execution:

```python
def _resolve_fundamental_run(args, requested_symbols, data=None):
    source = _resolve_fundamentals_source(
        args.factor_set,
        args.fundamentals_source,
    )
    symbols = requested_symbols
    if args.factor_set == "six_factor" and symbols is None:
        symbols = list(PILOT_FUNDAMENTAL_SYMBOLS)
    if args.factor_set == "six_factor" and source == "none":
        raise SystemExit("six_factor cannot use fundamentals_source=none")
    if args.coverage_policy == "formal" and source == "legacy":
        raise SystemExit("formal coverage cannot use legacy fundamentals")
    if args.factor_set == "six_factor" and args.coverage_policy == "formal":
        unsupported = sorted(set(symbols or []) - set(PILOT_FUNDAMENTAL_SYMBOLS))
        if unsupported:
            raise SystemExit(
                f"formal symbols outside the fundamental pilot: {unsupported}"
            )
        if len(set(symbols or [])) < 6:
            raise SystemExit("formal six_factor requires at least 6 pilot symbols")
    if data is not None and args.source == "files":
        metadata = data.fundamental_metadata
        if args.coverage_policy == "formal" and (
            metadata.get("formal_eligible") is not True
        ):
            raise SystemExit("formal file run requires verified export metadata")
        file_mode = metadata.get("pit_mode")
        if file_mode is not None and file_mode != args.pit_mode:
            raise SystemExit(
                f"file pit_mode={file_mode} differs from requested {args.pit_mode}"
            )
    return source, symbols
```

Call once before PostgreSQL loading to resolve source/symbols and once after
file loading with `data=data` to validate file provenance. Pass source,
PIT mode, schema and coverage policy through the loader and strategy calls.

- [ ] **Step 5: Document operator examples**

Create `docs/cta-fundamentals.md` with exact examples:

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --pit-mode conservative \
  --coverage-policy formal \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy medium_equal_weight \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --output-prefix output/cta_fundamental_medium_conservative
```

and:

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set price_volume \
  --fundamentals-source none \
  --coverage-policy degraded \
  --strategy both \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --output-prefix output/cta_price_volume_control
```

State that `legacy` is diagnostic only and that deck Sharpe, return, win rate,
and holding period are contextual comparisons rather than pass/fail criteria.

- [ ] **Step 6: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_strategy.py -q
git add cta_gtja/data.py cta_gtja/__main__.py \
  tests/test_cta_strategy.py docs/cta-fundamentals.md
git commit -m "feat: configure auditable CTA fundamental sources"
```

### Task 5: Enforce formal coverage before portfolio conversion

**Files:**
- Create: `cta_gtja/coverage.py`
- Create: `tests/test_cta_fundamental_coverage.py`
- Modify: `cta_gtja/backtest.py`
- Modify: `cta_gtja/strategies.py`

- [ ] **Step 1: Write failing daily coverage tests**

Create:

```python
from datetime import date

import numpy as np
import pandas as pd
import pytest

from cta_gtja.coverage import (
    FundamentalCoverageError,
    evaluate_daily_fundamental_coverage,
    evaluate_inventory_sides,
)

PILOT = ["M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU"]


def test_formal_daily_gate_accepts_six_of_nine():
    idx = pd.Index([date(2020, 1, 2)], name="trade_date")
    matrices = {
        metric: pd.DataFrame(
            [[1.0] * 6 + [np.nan] * 3],
            index=idx,
            columns=PILOT,
        )
        for metric in ("basis_rate", "inventory", "profit")
    }

    audit = evaluate_daily_fundamental_coverage(
        matrices,
        symbols=PILOT,
        policy="formal",
        required_products=6,
    )

    assert set(audit["status"]) == {"pass"}
    assert set(audit["available_products"]) == {6}


def test_formal_daily_gate_rejects_five_of_nine():
    idx = pd.Index([date(2020, 1, 2)], name="trade_date")
    matrices = {
        metric: pd.DataFrame(
            [[1.0] * 5 + [np.nan] * 4],
            index=idx,
            columns=PILOT,
        )
        for metric in ("basis_rate", "inventory", "profit")
    }

    with pytest.raises(
        FundamentalCoverageError,
        match="2020-01-02 basis_rate coverage=5 required=6",
    ):
        evaluate_daily_fundamental_coverage(
            matrices,
            symbols=PILOT,
            policy="formal",
            required_products=6,
        )


def test_inventory_requires_two_long_and_two_short_candidates():
    scores = pd.DataFrame(
        [[0.5, 0.2, 0.1, 0.0, 0.0, 0.0, np.nan, np.nan, np.nan]],
        index=pd.Index([date(2020, 2, 3)], name="trade_date"),
        columns=PILOT,
    )

    with pytest.raises(
        FundamentalCoverageError,
        match="inventory long=3 short=0 required_each=2",
    ):
        evaluate_inventory_sides(scores, policy="formal", required_each=2)


def test_degraded_policy_records_identical_failures_without_raising():
    idx = pd.Index([date(2020, 1, 2)], name="trade_date")
    matrices = {
        "basis_rate": pd.DataFrame(
            [[1.0] * 5 + [np.nan] * 4],
            index=idx,
            columns=PILOT,
        )
    }

    audit = evaluate_daily_fundamental_coverage(
        matrices,
        symbols=PILOT,
        policy="degraded",
        required_products=6,
    )

    assert audit.loc[0, "status"] == "fail"
    assert audit.loc[0, "reason"] == (
        "2020-01-02 basis_rate coverage=5 required=6"
    )
```

- [ ] **Step 2: Verify RED**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_fundamental_coverage.py -q
```

Expected: import failure for `cta_gtja.coverage`.

- [ ] **Step 3: Implement deterministic coverage audit**

Define:

```python
FUNDAMENTAL_FACTOR_METRICS = {
    "basis": "basis_rate",
    "inventory": "inventory",
    "profit": "profit",
}

COVERAGE_COLUMNS = [
    "trade_date",
    "check",
    "metric",
    "available_products",
    "required_products",
    "long_candidates",
    "short_candidates",
    "required_each_side",
    "status",
    "reason",
]
```

`evaluate_daily_fundamental_coverage` must:

1. reindex each matrix to the requested symbols;
2. count finite values, not merely non-null object values;
3. emit one row per date and metric;
4. return all rows under `degraded`;
5. raise one `FundamentalCoverageError` containing the sorted failure reasons
   under `formal`.

`evaluate_inventory_sides` evaluates only dates on or after the first row with
at least four finite inventory scores. Count `score > 0` as long and
`score < 0` as short; zeros do not count. This preserves the 20-day inventory
warm-up without waiving failures once the factor becomes active.

Implement the complete module as:

```python
from __future__ import annotations

import numpy as np
import pandas as pd


FUNDAMENTAL_FACTOR_METRICS = {
    "basis": "basis_rate",
    "inventory": "inventory",
    "profit": "profit",
}

COVERAGE_COLUMNS = [
    "trade_date",
    "check",
    "metric",
    "available_products",
    "required_products",
    "long_candidates",
    "short_candidates",
    "required_each_side",
    "status",
    "reason",
]


class FundamentalCoverageError(ValueError):
    """Formal CTA fundamental coverage contract failed."""


def _finish(rows: list[dict], *, policy: str) -> pd.DataFrame:
    if policy not in {"formal", "degraded"}:
        raise ValueError(f"unsupported coverage policy: {policy}")
    audit = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
    failures = sorted(
        audit.loc[audit["status"] == "fail", "reason"].dropna().unique()
    )
    if failures and policy == "formal":
        raise FundamentalCoverageError("; ".join(failures))
    return audit


def evaluate_daily_fundamental_coverage(
    matrices: dict[str, pd.DataFrame],
    *,
    symbols: list[str],
    policy: str,
    required_products: int,
) -> pd.DataFrame:
    rows = []
    for metric in sorted(matrices):
        matrix = matrices[metric].reindex(columns=symbols)
        for trade_date, values in matrix.sort_index().iterrows():
            numeric = pd.to_numeric(values, errors="coerce").to_numpy(float)
            available = int(np.isfinite(numeric).sum())
            passed = available >= required_products
            reason = (
                ""
                if passed
                else f"{trade_date} {metric} coverage={available} "
                f"required={required_products}"
            )
            rows.append(
                {
                    "trade_date": trade_date,
                    "check": "daily_fundamental_coverage",
                    "metric": metric,
                    "available_products": available,
                    "required_products": required_products,
                    "long_candidates": pd.NA,
                    "short_candidates": pd.NA,
                    "required_each_side": pd.NA,
                    "status": "pass" if passed else "fail",
                    "reason": reason,
                }
            )
    return _finish(rows, policy=policy)


def evaluate_inventory_sides(
    scores: pd.DataFrame,
    *,
    policy: str,
    required_each: int,
) -> pd.DataFrame:
    finite_counts = scores.apply(
        lambda row: int(
            np.isfinite(pd.to_numeric(row, errors="coerce").to_numpy(float)).sum()
        ),
        axis=1,
    )
    active = finite_counts[finite_counts >= required_each * 2]
    if active.empty:
        return _finish(
            [
                {
                    "trade_date": pd.NaT,
                    "check": "inventory_two_sided",
                    "metric": "inventory",
                    "available_products": int(finite_counts.max()) if len(finite_counts) else 0,
                    "required_products": required_each * 2,
                    "long_candidates": 0,
                    "short_candidates": 0,
                    "required_each_side": required_each,
                    "status": "fail",
                    "reason": "inventory has no active score date",
                }
            ],
            policy=policy,
        )
    rows = []
    for trade_date, values in scores.loc[active.index[0]:].iterrows():
        numeric = pd.to_numeric(values, errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        long_count = int((numeric > 0).sum())
        short_count = int((numeric < 0).sum())
        passed = long_count >= required_each and short_count >= required_each
        reason = (
            ""
            if passed
            else f"{trade_date} inventory long={long_count} "
            f"short={short_count} required_each={required_each}"
        )
        rows.append(
            {
                "trade_date": trade_date,
                "check": "inventory_two_sided",
                "metric": "inventory",
                "available_products": int(numeric.size),
                "required_products": required_each * 2,
                "long_candidates": long_count,
                "short_candidates": short_count,
                "required_each_side": required_each,
                "status": "pass" if passed else "fail",
                "reason": reason,
            }
        )
    return _finish(rows, policy=policy)
```

- [ ] **Step 4: Call the gate before `.fillna(0.0)`**

Extend both strategy entry points and `build_factor_sleeves` with:

```python
coverage_policy: str = "formal"
```

At the start of `build_factor_sleeves`, identify requested fundamental
factors. If none are present, attach an empty audit and do not require
fundamental metadata. Otherwise:

1. reject `formal` when
   `data.fundamental_metadata["formal_eligible"] is not True`;
2. build raw `basis_rate`, `inventory`, and `profit` matrices;
3. evaluate the daily `6`-product gate;
4. compute each factor score;
5. evaluate inventory two-sided coverage on the unfilled score matrix;
6. only then convert valid missing factor weights to zero.

Return a third value:

```python
weights_by_factor, factor_returns, coverage_audit
```

Update both callers. Do not catch `FundamentalCoverageError` in the strategy
layer; the CLI must terminate the formal run with its precise reason.

Replace `build_factor_sleeves` with:

```python
def build_factor_sleeves(
    data: CTADataSet,
    *,
    factors: list[CTAFactor],
    symbols: list[str] | None = None,
    coverage_policy: str = "formal",
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    symbols = symbols or data.symbols
    if not symbols:
        raise ValueError("CTA strategy needs at least one symbol")
    fundamental_names = {
        factor.name
        for factor in factors
        if factor.name in FUNDAMENTAL_FACTOR_METRICS
    }
    audits = []
    if fundamental_names:
        if (
            coverage_policy == "formal"
            and data.fundamental_metadata.get("formal_eligible") is not True
        ):
            raise FundamentalCoverageError(
                "formal run requires a published standard fundamental build"
            )
        metrics = {
            FUNDAMENTAL_FACTOR_METRICS[name]: data.fundamental_matrix(
                FUNDAMENTAL_FACTOR_METRICS[name],
                symbols=symbols,
            ).reindex(index=data.dates, columns=symbols)
            for name in sorted(fundamental_names)
        }
        audits.append(
            evaluate_daily_fundamental_coverage(
                metrics,
                symbols=symbols,
                policy=coverage_policy,
                required_products=6,
            )
        )

    weights_by_factor = {}
    for factor in factors:
        scores = factor.compute(data, symbols)
        if factor.name == "inventory":
            audits.append(
                evaluate_inventory_sides(
                    scores,
                    policy=coverage_policy,
                    required_each=2,
                )
            )
        weights_by_factor[factor.name] = (
            factor_weights(factor, scores)
            .reindex(columns=symbols)
            .fillna(0.0)
        )

    asset_returns = forward_open_returns(data, symbols)
    factor_returns = pd.DataFrame(
        {
            name: portfolio_returns(weights, asset_returns)
            for name, weights in weights_by_factor.items()
        }
    )
    coverage_audit = (
        pd.concat(audits, ignore_index=True)
        if audits
        else pd.DataFrame(columns=COVERAGE_COLUMNS)
    )
    return weights_by_factor, factor_returns, coverage_audit
```

Import `COVERAGE_COLUMNS`, `FUNDAMENTAL_FACTOR_METRICS`,
`FundamentalCoverageError`, `evaluate_daily_fundamental_coverage`, and
`evaluate_inventory_sides` from `cta_gtja.coverage`.

Add `coverage_policy` to both public strategy signatures, unpack the third
return value, and pass it to the backtester:

```python
weights_by_factor, factor_returns, coverage_audit = build_factor_sleeves(
    data,
    factors=factors,
    symbols=symbols,
    coverage_policy=coverage_policy,
)

result = CTABacktester(
    data,
    cost_bps=cost_bps,
    target_vol=MEDIUM_EQUAL_WEIGHT.target_vol,
    max_leverage=MEDIUM_EQUAL_WEIGHT.max_leverage,
).run(
    weights,
    factor_allocations=allocations,
    factor_returns=factor_returns,
    fundamental_coverage=coverage_audit,
)
return result
```

Pass `fundamental_coverage=coverage_audit` to the existing `CTABacktester.run`
call in `run_high_composite`, retaining its existing 12% target and allocation
logic.
In `cta_gtja/backtest.py`, add this field and keyword now so Task 5 remains
green independently:

```python
@dataclass
class CTABacktestResult:
    weights: pd.DataFrame
    period_returns: pd.Series
    turnover: pd.Series
    cost: pd.Series
    equity: pd.Series
    metrics: dict[str, float]
    factor_allocations: pd.DataFrame
    factor_returns: pd.DataFrame
    data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
    fundamental_coverage: pd.DataFrame = field(default_factory=pd.DataFrame)
```

Extend `CTABacktester.run` with
`fundamental_coverage: pd.DataFrame | None = None` and set:

```python
fundamental_coverage=(
    fundamental_coverage.copy()
    if fundamental_coverage is not None
    else pd.DataFrame()
),
```

The existing `_sample_cta_data` fixture contains only four symbols and is not
a formal-universe fixture. Update its six-factor calls explicitly:

```python
weights_by_factor, factor_returns, coverage = build_factor_sleeves(
    data,
    factors=default_cta_factors(),
    symbols=data.symbols,
    coverage_policy="degraded",
)
assert (coverage["status"] == "fail").any()

result = run_medium_equal_weight(
    data,
    symbols=data.symbols,
    cost_bps=1.0,
    coverage_policy="degraded",
)
result = run_high_composite(
    data,
    symbols=data.symbols,
    cost_bps=1.0,
    coverage_policy="degraded",
)
```

Apply the same explicit `coverage_policy="degraded"` to the two
future-price-perturbation calls that use `_sample_cta_data`. Do not change the
default from `formal`; small synthetic tests must opt into degradation.

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_fundamental_coverage.py \
  tests/test_cta_strategy.py -q
git add cta_gtja/coverage.py cta_gtja/strategies.py \
  tests/test_cta_fundamental_coverage.py tests/test_cta_strategy.py
git commit -m "feat: gate CTA fundamental cross-section coverage"
```

### Task 6: Lock raw-price basis and bounded missing-data behavior

**Files:**
- Modify: `cta_gtja/factors.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing basis invariants**

Add `BasisFactor`, `InventoryFactor`, and `ProfitFactor` to the existing
`cta_gtja.factors` import, then add:

```python
def test_basis_fallback_uses_raw_close_never_adjusted_close():
    prices = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "symbol": ["M"],
            "open": [400.0],
            "close": [400.0],
            "open_raw": [100.0],
            "close_raw": [100.0],
        }
    )
    fundamentals = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "symbol": ["M"],
            "spot": [110.0],
        }
    )

    scores = BasisFactor().compute(
        CTADataSet(prices=prices, fundamentals=fundamentals),
        ["M"],
    )

    assert scores.loc[date(2020, 1, 2), "M"] == pytest.approx(0.10)


def test_basis_without_published_value_or_raw_close_stays_missing():
    prices = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "symbol": ["M"],
            "open": [400.0],
            "close": [400.0],
        }
    )
    fundamentals = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)],
            "symbol": ["M"],
            "spot": [110.0],
        }
    )

    scores = BasisFactor().compute(
        CTADataSet(prices=prices, fundamentals=fundamentals),
        ["M"],
    )

    assert pd.isna(scores.loc[date(2020, 1, 2), "M"])
```

Add a standard-data staleness regression:

```python
def test_standard_daily_fundamentals_are_not_forward_filled_past_missing_row():
    dates = pd.bdate_range("2020-01-01", periods=61).date.tolist()
    prices = pd.DataFrame(
        {
            "trade_date": dates,
            "symbol": "M",
            "open": np.arange(61, dtype=float) + 100.0,
            "close": np.arange(61, dtype=float) + 100.0,
            "open_raw": np.arange(61, dtype=float) + 100.0,
            "close_raw": np.arange(61, dtype=float) + 100.0,
        }
    )
    fundamentals = pd.DataFrame(
        {
            "trade_date": dates,
            "symbol": "M",
            "basis_rate": np.linspace(-0.1, 0.1, 61),
            "inventory": np.linspace(100.0, 160.0, 61),
            "profit": np.linspace(-50.0, 50.0, 61),
        }
    )
    fundamentals.loc[60, ["basis_rate", "inventory", "profit"]] = np.nan
    data = CTADataSet(
        prices=prices,
        fundamentals=fundamentals,
        fundamental_metadata={
            "source": "standard",
            "materialized_daily": True,
        },
    )

    assert pd.isna(BasisFactor().compute(data, ["M"]).iloc[-1, 0])
    assert pd.isna(InventoryFactor().compute(data, ["M"]).iloc[-1, 0])
    assert pd.isna(ProfitFactor().compute(data, ["M"]).iloc[-1, 0])
```

- [ ] **Step 2: Verify RED**

Run the three new factor tests. The raw basis expectation fails because the
current code uses the selected adjusted `close`; the staleness test fails
because factor code forward-fills.

- [ ] **Step 3: Implement the basis invariant**

Replace `BasisFactor.compute` with:

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

The fallback exists for explicit degraded file/legacy diagnostics. A formal
standard build is expected to contain `basis_rate`.

- [ ] **Step 4: Respect standard-layer staleness**

For a dataset whose metadata has `materialized_daily=True`, reindex basis,
inventory, and profit without forward fill. The standard builder already
applies the catalog's maximum staleness and intentionally omits expired
values. Re-forward-filling in the strategy would defeat that PIT decision.

For legacy or unverified files, preserve the existing factor forward fill,
but these inputs cannot pass the formal gate. Implement one helper:

```python
def _align_fundamental(
    values: pd.DataFrame,
    data: CTADataSet,
    symbols: list[str],
) -> pd.DataFrame:
    aligned = values.reindex(index=data.dates, columns=symbols)
    if data.fundamental_metadata.get("materialized_daily") is True:
        return aligned
    return aligned.ffill()
```

Use the helper in the other two fundamental factors:

```python
class InventoryFactor(CTAFactor):
    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        inventory = data.fundamental_matrix("inventory", symbols=symbols)
        if inventory.empty:
            return _empty_like(data, symbols)
        inventory = _align_fundamental(inventory, data, symbols)
        return -inventory.pct_change(
            self.lookback_days,
            fill_method=None,
        )


class ProfitFactor(CTAFactor):
    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        profit = data.fundamental_matrix("profit", symbols=symbols)
        if profit.empty:
            return _empty_like(data, symbols)
        profit = _align_fundamental(profit, data, symbols)
        zscore = _rolling_zscore(
            profit,
            self.lookback_days,
            self.min_periods,
        )
        return -zscore
```

- [ ] **Step 5: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_strategy.py \
  tests/test_cta_fundamental_coverage.py -q
git add cta_gtja/factors.py tests/test_cta_strategy.py
git commit -m "fix: use raw futures prices for CTA basis"
```

### Task 7: Carry fundamental audit into every result and workbook

**Files:**
- Modify: `cta_gtja/backtest.py`
- Modify: `cta_gtja/strategies.py`
- Modify: `cta_gtja/__main__.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing report tests**

Import `_fundamental_coverage_summary` alongside `_data_quality_summary`, and
import `CTABacktestResult` alongside `write_cta_outputs`, then add:

```python
def _result_with_fundamental_audit():
    idx = pd.Index([date(2020, 1, 2)], name="trade_date")
    return CTABacktestResult(
        weights=pd.DataFrame({"M": [0.0]}, index=idx),
        period_returns=pd.Series([0.0], index=idx, name="net_return"),
        turnover=pd.Series([0.0], index=idx, name="turnover"),
        cost=pd.Series([0.0], index=idx, name="cost"),
        equity=pd.Series([1.0], index=idx, name="equity"),
        metrics={"sharpe": 0.0},
        factor_allocations=pd.DataFrame(index=idx),
        factor_returns=pd.DataFrame(index=idx),
        fundamental_coverage=pd.DataFrame(
            {
                "trade_date": [date(2020, 1, 2)],
                "status": ["pass"],
                "available_products": [9],
            }
        ),
        fundamental_lineage=pd.DataFrame(
            {
                "trade_date": [date(2020, 1, 2)],
                "product_code": ["M"],
                "lineage_hash": ["abc"],
            }
        ),
        fundamental_metadata={
            "source": "standard",
            "pit_mode": "conservative",
            "build_version": "build-c-1",
            "catalog_version": "v1",
            "source_recorded_cutoff": "2026-07-27T18:00:00+08:00",
            "schema": "commodity_research",
            "materialized_daily": True,
            "formal_eligible": True,
        },
    )


def test_write_cta_outputs_includes_fundamental_audit_sheets(tmp_path):
    result = _result_with_fundamental_audit()

    xlsx, _ = write_cta_outputs(result, tmp_path / "cta")
    sheets = pd.ExcelFile(xlsx).sheet_names

    assert "fundamental_coverage" in sheets
    assert "fundamental_lineage" in sheets
    assert "fundamental_build" in sheets
    build = pd.read_excel(xlsx, sheet_name="fundamental_build")
    assert build.loc[0, "build_version"] == "build-c-1"
    assert build.loc[0, "pit_mode"] == "conservative"


def test_price_volume_result_does_not_fabricate_fundamental_rows(tmp_path):
    result = _result_with_fundamental_audit()
    result.fundamental_coverage = pd.DataFrame()
    result.fundamental_lineage = pd.DataFrame()
    result.fundamental_metadata = {
        "source": "none",
        "formal_eligible": False,
    }

    xlsx, _ = write_cta_outputs(result, tmp_path / "price_volume")
    sheets = pd.ExcelFile(xlsx).sheet_names

    assert "fundamental_coverage" not in sheets
    assert "fundamental_lineage" not in sheets
    build = pd.read_excel(xlsx, sheet_name="fundamental_build")
    assert build.loc[0, "source"] == "none"
```

- [ ] **Step 2: Verify RED**

Run the focused workbook tests and expect missing fields/sheets.

- [ ] **Step 3: Extend `CTABacktestResult`**

Add:

```python
fundamental_lineage: pd.DataFrame = field(default_factory=pd.DataFrame)
fundamental_metadata: dict[str, object] = field(default_factory=dict)
```

Task 5 already added the coverage field and keyword. Add these constructor
arguments in `CTABacktester.run`:

```python
fundamental_lineage=self.data.fundamental_quality.copy(),
fundamental_metadata=dict(self.data.fundamental_metadata),
```

- [ ] **Step 4: Write auditable workbook sheets**

In `write_cta_outputs`:

- write `fundamental_coverage` when non-empty;
- write `fundamental_lineage` when non-empty;
- always write one-row `fundamental_build` from metadata, with stable columns:

```python
[
    "source",
    "pit_mode",
    "build_version",
    "catalog_version",
    "source_recorded_cutoff",
    "schema",
    "materialized_daily",
    "formal_eligible",
]
```

Do not place JSON lineage dictionaries in the build sheet. The lineage sheet
already carries the source-level detail.

Add this code inside the existing `ExcelWriter` block:

```python
if not result.fundamental_coverage.empty:
    result.fundamental_coverage.to_excel(
        writer,
        sheet_name="fundamental_coverage",
        index=False,
    )
if not result.fundamental_lineage.empty:
    result.fundamental_lineage.to_excel(
        writer,
        sheet_name="fundamental_lineage",
        index=False,
    )
build_columns = [
    "source",
    "pit_mode",
    "build_version",
    "catalog_version",
    "source_recorded_cutoff",
    "schema",
    "materialized_daily",
    "formal_eligible",
]
build_row = {
    column: result.fundamental_metadata.get(column)
    for column in build_columns
}
if not build_row["source"]:
    build_row["source"] = "unknown"
pd.DataFrame([build_row], columns=build_columns).to_excel(
    writer,
    sheet_name="fundamental_build",
    index=False,
)
```

- [ ] **Step 5: Print coverage summary**

Add `_fundamental_coverage_summary` in the CLI. Example:

```text
fundamental_coverage: rows=4980 failed=0 minimum=6
```

For a formal run, any failure has already stopped execution. For a degraded
run, print failed count and the first three deterministic reasons.

```python
def _fundamental_coverage_summary(coverage: pd.DataFrame) -> str:
    if coverage is None or coverage.empty:
        return "rows=0 failed=0 minimum=unknown"
    failed = coverage[coverage["status"] == "fail"]
    available = pd.to_numeric(
        coverage.get("available_products"),
        errors="coerce",
    ).dropna()
    minimum = int(available.min()) if not available.empty else "unknown"
    summary = f"rows={len(coverage)} failed={len(failed)} minimum={minimum}"
    reasons = sorted(
        reason
        for reason in failed.get("reason", pd.Series(dtype=str)).dropna().unique()
        if reason
    )[:3]
    return summary + (" reasons=" + " | ".join(reasons) if reasons else "")
```

Import pandas in `cta_gtja.__main__` and print the function's result for each
completed job before writing its outputs.

- [ ] **Step 6: Verify GREEN and commit**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest tests/test_cta_strategy.py -q
git add cta_gtja/backtest.py cta_gtja/strategies.py \
  cta_gtja/__main__.py tests/test_cta_strategy.py
git commit -m "feat: report CTA fundamental lineage and coverage"
```

### Task 8: Prove the strategy has no future-fundamental dependency

**Files:**
- Modify: `tests/test_cta_fundamental_pit.py`
- Modify: `tests/test_cta_pg_source.py`

- [ ] **Step 1: Add a future-mutation test**

Using the deterministic 300-date, nine-symbol fixtures from Task 1:

```python
def test_future_fundamental_mutation_cannot_change_past_weights_or_nav(
    complete_price_frame,
    complete_fundamental_frame,
):
    cutoff = sorted(complete_price_frame["trade_date"].unique())[260]
    metadata = {
        "source": "standard",
        "pit_mode": "conservative",
        "build_version": "build-c-1",
        "catalog_version": "v1",
        "materialized_daily": True,
        "formal_eligible": True,
    }
    original = CTADataSet(
        prices=complete_price_frame,
        fundamentals=complete_fundamental_frame,
        fundamental_metadata=metadata,
    )
    mutated_frame = complete_fundamental_frame.copy()
    future = mutated_frame["trade_date"] > cutoff
    mutated_frame.loc[
        future, ["spot", "basis_rate", "inventory", "profit"]
    ] *= -1000.0
    mutated = CTADataSet(
        prices=complete_price_frame.copy(),
        fundamentals=mutated_frame,
        fundamental_metadata=dict(metadata),
    )

    left = run_medium_equal_weight(
        original,
        coverage_policy="formal",
        cost_bps=0.0,
    )
    right = run_medium_equal_weight(
        mutated,
        coverage_policy="formal",
        cost_bps=0.0,
    )

    assert_frame_equal(
        left.weights.loc[:cutoff],
        right.weights.loc[:cutoff],
    )
    assert_series_equal(
        left.period_returns.loc[:cutoff],
        right.period_returns.loc[:cutoff],
    )
    assert_series_equal(
        left.equity.loc[:cutoff],
        right.equity.loc[:cutoff],
    )
```

Ensure fixture inventory changes produce at least two positive and two
negative inventory scores on every post-warm-up date so this test exercises
the formal path rather than failing its coverage precondition.

- [ ] **Step 2: Add loader defense tests**

Assert the standard SQL contains both:

```text
pit_mode = %(pit_mode)s
available_at <= ((trade_date::timestamp + time '15:00') AT TIME ZONE 'Asia/Shanghai')
```

Also test that requesting `strict` passes `strict` to both value and lineage
queries and cannot accidentally return the conservative build.

- [ ] **Step 3: Run PIT and control tests**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_fundamental_pit.py \
  tests/test_cta_pg_source.py -q
```

Expected: the future-mutation and price-volume-control tests both pass.

- [ ] **Step 4: Commit PIT acceptance tests**

```bash
cd /home/elfbob/claude-code/futures_strategies
git add tests/test_cta_fundamental_pit.py tests/test_cta_pg_source.py
git commit -m "test: prove CTA fundamental point-in-time isolation"
```

### Task 9: Run the complete local regression suite

**Files:**
- Read: all files modified in Tasks 1–8

- [ ] **Step 1: Run formatting-independent repository checks**

```bash
cd /home/elfbob/claude-code/futures_strategies
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 2: Run the full CTA suite**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_data_quality.py \
  tests/test_cta_pg_source.py \
  tests/test_cta_strategy.py \
  tests/test_cta_fundamental_coverage.py \
  tests/test_cta_fundamental_pit.py -q
```

Expected: all CTA tests pass.

- [ ] **Step 3: Run the full repository suite**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest -q
```

Expected: all repository tests pass. If an unrelated pre-existing failure
appears, preserve its exact output and prove the focused CTA suite still
passes; do not weaken a test to obtain green.

- [ ] **Step 4: Review final source diff**

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short
git diff --stat
git diff -- \
  cta_gtja/data.py \
  cta_gtja/pg_source.py \
  cta_gtja/coverage.py \
  cta_gtja/factors.py \
  cta_gtja/strategies.py \
  cta_gtja/backtest.py \
  cta_gtja/__main__.py
```

Expected: only planned changes, no secrets, no generated workbooks, and no
hard-coded database credentials.

### Task 10: Run live acceptance against the published conservative build

**Files:**
- Produce: `output/cta_fundamental_medium_conservative.xlsx`
- Produce: `output/cta_fundamental_medium_conservative_equity.png`
- Produce: `output/cta_fundamental_composite_conservative_*.xlsx`
- Produce: `output/cta_price_volume_control_*.xlsx`
- Append results: `docs/cta-fundamentals.md`

- [ ] **Step 1: Read-only verify the current build**

Run against Debian through the repository's configured connection:

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --pit-mode conservative \
  --coverage-policy formal \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy medium_equal_weight \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_fundamental_medium_conservative
```

Expected:

- the command identifies exactly one conservative build and catalog version;
- the formal gate reports no failed rows;
- the workbook contains build, lineage, and coverage sheets;
- no `AU` or `AG` weight appears.

If the current published build starts later than 2019-01-01, the formal gate
must fail with the first uncovered date. Re-run only after documenting the
actual first formal date, then set `--start` to that exact date. Do not
silently truncate inside the loader.

- [ ] **Step 2: Run the conservative composite strategy**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --pit-mode conservative \
  --coverage-policy formal \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy high_composite \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_fundamental_composite_conservative
```

Expected: successful output with the same build/catalog identity and zero
formal coverage failures.

- [ ] **Step 3: Run the price-volume control**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set price_volume \
  --fundamentals-source none \
  --coverage-policy degraded \
  --strategy both \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_price_volume_control
```

Expected: successful control outputs without querying or claiming a
fundamental build.

- [ ] **Step 4: Audit strict-mode readiness without inventing history**

Attempt a strict run only for the actual interval covered by a published
`captured_live` strict build:

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --pit-mode strict \
  --coverage-policy formal \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy medium_equal_weight \
  --start 2026-07-27 \
  --end 2026-07-27 \
  --output-prefix output/cta_fundamental_strict_readiness
```

Expected at initial deployment: an explicit no-current-build or insufficient
coverage result is acceptable. Do not relabel `backfill_final` history as
strict and do not treat lack of a usable strict backtest window as a defect.
Replace the two dates with the actual captured-live interval once it contains
enough history for the factor warm-ups.

- [ ] **Step 5: Record evidence, not a headline target**

Append an acceptance table to `docs/cta-fundamentals.md` with:

```text
run timestamp
source and PIT mode
build version
catalog version
date range
universe
coverage minimum and failed rows
annualized return
annualized volatility
Sharpe
maximum drawdown
turnover
output workbook
```

Record the medium equal-weight, high composite, and price-volume control side
by side. Interpret differences as data/factor evidence. Acceptance requires
PIT isolation, raw-basis correctness, coverage, reproducibility, and report
lineage; it does not require matching the deck's `17.14%`, `2.43`, or `2.90`.

- [ ] **Step 6: Commit the acceptance record**

Do not commit generated `.xlsx` or `.png` files unless the repository's
existing policy explicitly tracks outputs. Commit only the updated runbook:

```bash
cd /home/elfbob/claude-code/futures_strategies
git add docs/cta-fundamentals.md
git commit -m "docs: record CTA fundamental acceptance evidence"
```

### Task 11: Final verification and handoff

**Files:**
- Read: Git history and both prerequisite plan outcomes

- [ ] **Step 1: Re-run required evidence**

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py \
  tests/test_cta_strategy.py \
  tests/test_cta_fundamental_coverage.py \
  tests/test_cta_fundamental_pit.py -q
git diff --check
git status --short --branch
```

Expected: tests pass, no whitespace errors, and only explicitly retained
output artifacts are untracked.

- [ ] **Step 2: Verify the three non-negotiable assertions**

Read the tests and live workbook evidence and confirm:

1. basis uses `close_raw`, never `close_ba`, `close_fa`, or selected `close`;
2. changing fundamentals strictly after a cutoff cannot change weights,
   returns, or equity through that cutoff;
3. a formal run cannot reach factor weighting with fewer than six eligible
   products or fewer than two inventory candidates on either side.

- [ ] **Step 3: Handoff**

Report:

- conservative build and catalog versions;
- achieved target catalog coverage (`basis/inventory/profit`);
- actual formal backtest interval;
- all test commands and pass counts;
- locations of the three comparison workbooks;
- strict-mode readiness status;
- any products that remain missing or degraded and their catalog reason codes.

Do not summarize success with the deck Sharpe. The deliverable is an
auditable fundamental data path and a reproducible six-factor comparison.
