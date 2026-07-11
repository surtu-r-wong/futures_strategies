# CTA Price/Volume Data Guard Implementation Plan

> **迁移注**（2026-07-11）：本文档随 CTA 剥离迁自 stock_selector；文中 `cta/`、`tests/test_cta_*`、`docs/superpowers/*` 等路径为当时原仓路径（历史记录，不改写），现行代码对应本仓 `cta_gtja/`、`tests/`、`docs/plans|specs/`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CTA price/volume backtests use clean per-symbol adjusted price lineages, exclude unusable adjusted series by default, and write the data-quality audit into outputs.

**Architecture:** Reuse `cta.data_quality` as the source of truth for per-symbol adjustment status. Attach a `data_quality` audit frame to `CTADataSet`, make the PostgreSQL reader choose `fa`/`ba`/raw per symbol according to policy, add a named `price_volume` factor set, and propagate the audit through CLI/output files. The upstream continuous-contract generator remains unchanged.

**Tech Stack:** Python 3.13, pandas, pytest, openpyxl, PostgreSQL public schema reader.

---

## File Structure

- `cta/data_quality.py`: add an audit builder that converts the existing quality report into inclusion and selected-lineage decisions.
- `cta/data.py`: add `CTADataSet.data_quality` metadata and preserve it through slicing.
- `cta/pg_source.py`: load all adjusted lineages, apply per-symbol lineage decisions, and attach the audit to `CTADataSet`.
- `cta/factors.py`: expose a named price/volume factor subset.
- `cta/__main__.py`: add `--factor-set`, `--adjustment-policy`, and `--allow-raw-fallback` CLI arguments; print a concise data-quality summary.
- `cta/backtest.py`: include `data_quality` in `CTABacktestResult` and write a `data_quality` Excel sheet.
- `docs/operations/cta-strategy-replication.md`: document the guarded price/volume smoke command.
- Tests:
  - `tests/test_cta_data_quality.py`
  - `tests/test_cta_pg_source.py`
  - `tests/test_cta_strategy.py`

---

### Task 1: Build Adjustment Audit Decisions

**Files:**
- Modify: `cta/data_quality.py`
- Test: `tests/test_cta_data_quality.py`

- [ ] **Step 1: Write the failing tests**

Append these tests to `tests/test_cta_data_quality.py`:

```python
from cta.data_quality import build_adjustment_audit


def test_adjustment_audit_excludes_raw_fallback_by_default():
    report = pd.DataFrame([
        {
            "base_symbol": "M",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 0,
            "fa_nonpos": 3,
            "status": "fa_corrupt",
            "recommended_adj": "ba",
        },
        {
            "base_symbol": "RU",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 2,
            "fa_nonpos": 4,
            "status": "both_corrupt",
            "recommended_adj": "raw",
        },
    ])

    audit = build_adjustment_audit(report, allow_raw_fallback=False).set_index("base_symbol")

    assert bool(audit.loc["M", "included"])
    assert audit.loc["M", "selected_adj"] == "ba"
    assert not bool(audit.loc["M", "raw_fallback"])
    assert not bool(audit.loc["RU", "included"])
    assert audit.loc["RU", "selected_adj"] == ""
    assert audit.loc["RU", "exclusion_reason"] == "both_adjusted_lineages_corrupt"


def test_adjustment_audit_allows_explicit_raw_fallback():
    report = pd.DataFrame([
        {
            "base_symbol": "RU",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 2,
            "fa_nonpos": 4,
            "status": "both_corrupt",
            "recommended_adj": "raw",
        },
    ])

    audit = build_adjustment_audit(report, allow_raw_fallback=True).set_index("base_symbol")

    assert bool(audit.loc["RU", "included"])
    assert audit.loc["RU", "selected_adj"] == "raw"
    assert bool(audit.loc["RU", "raw_fallback"])
    assert audit.loc["RU", "exclusion_reason"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py::test_adjustment_audit_excludes_raw_fallback_by_default tests/test_cta_data_quality.py::test_adjustment_audit_allows_explicit_raw_fallback -q
```

Expected: FAIL with `ImportError` or `AttributeError` because `build_adjustment_audit` does not exist.

- [ ] **Step 3: Implement the audit builder**

Add this function to `cta/data_quality.py` after `summarize_adjustment_quality`:

```python
def build_adjustment_audit(
    quality_report: pd.DataFrame,
    *,
    allow_raw_fallback: bool = False,
) -> pd.DataFrame:
    """Convert a quality report into selected-lineage and inclusion decisions.

    Raw fallback is deliberately opt-in because raw continuous prices do not
    solve roll-adjusted return continuity.
    """
    if quality_report.empty:
        return pd.DataFrame(
            columns=[
                "base_symbol",
                "n_rows",
                "raw_nonpos",
                "ba_nonpos",
                "fa_nonpos",
                "status",
                "recommended_adj",
                "selected_adj",
                "included",
                "raw_fallback",
                "exclusion_reason",
            ]
        )

    rows = []
    for rec in quality_report.to_dict("records"):
        recommended = str(rec["recommended_adj"])
        selected = recommended
        included = True
        raw_fallback = recommended == "raw"
        exclusion_reason = ""
        if raw_fallback and not allow_raw_fallback:
            selected = ""
            included = False
            exclusion_reason = "both_adjusted_lineages_corrupt"
        rows.append(
            {
                **rec,
                "selected_adj": selected,
                "included": bool(included),
                "raw_fallback": bool(raw_fallback and included),
                "exclusion_reason": exclusion_reason,
            }
        )
    return pd.DataFrame.from_records(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -q
```

Expected: PASS, including the two new audit tests.

- [ ] **Step 5: Commit**

```bash
git add cta/data_quality.py tests/test_cta_data_quality.py
git commit -m "feat(cta): build adjustment lineage audit"
```

---

### Task 2: Preserve Data-Quality Metadata on CTADataSet

**Files:**
- Modify: `cta/data.py`
- Test: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write the failing test**

Append this test to `tests/test_cta_strategy.py`:

```python
def test_data_slice_preserves_data_quality_for_symbols():
    data = _sample_cta_data()
    quality = pd.DataFrame([
        {"base_symbol": "CU", "selected_adj": "fa", "included": True},
        {"base_symbol": "AL", "selected_adj": "ba", "included": True},
        {"base_symbol": "RB", "selected_adj": "fa", "included": True},
        {"base_symbol": "TA", "selected_adj": "fa", "included": True},
    ])
    data = CTADataSet(prices=data.prices, fundamentals=data.fundamentals, data_quality=quality)

    sliced = data.slice(symbols=["CU", "RB"], start=date(2020, 3, 1), end=date(2020, 6, 30))

    assert sorted(sliced.data_quality["base_symbol"].tolist()) == ["CU", "RB"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py::test_data_slice_preserves_data_quality_for_symbols -q
```

Expected: FAIL with `TypeError: CTADataSet.__init__() got an unexpected keyword argument 'data_quality'`.

- [ ] **Step 3: Implement metadata preservation**

Modify `cta/data.py`:

```python
from dataclasses import dataclass, field
```

Update the dataclass:

```python
@dataclass(frozen=True)
class CTADataSet:
    prices: pd.DataFrame
    fundamentals: pd.DataFrame
    data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
```

Update `from_dir`:

```python
return cls(
    prices=normalize_prices(prices),
    fundamentals=normalize_fundamentals(fundamentals),
)
```

No extra `data_quality` argument is needed for file-mode data.

Update `slice`:

```python
quality = self.data_quality.copy()
if symbols is not None and not quality.empty and "base_symbol" in quality.columns:
    quality = quality[quality["base_symbol"].isin(symbols)].copy()
return CTADataSet(prices=prices, fundamentals=fundamentals, data_quality=quality)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py::test_data_slice_preserves_data_quality_for_symbols -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cta/data.py tests/test_cta_strategy.py
git commit -m "feat(cta): preserve data quality metadata"
```

---

### Task 3: Apply Adjustment Policy in the PostgreSQL Reader

**Files:**
- Modify: `cta/pg_source.py`
- Create: `tests/test_cta_pg_source.py`

- [ ] **Step 1: Write failing tests for lineage selection**

Create `tests/test_cta_pg_source.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from cta.pg_source import _apply_adjustment_policy


def _prices():
    return pd.DataFrame([
        {
            "trade_date": "2026-01-02",
            "symbol": "M",
            "contract": "M2601",
            "open_raw": 10.0,
            "open_ba": 100.0,
            "open_fa": -1.0,
            "close_raw": 11.0,
            "close_ba": 101.0,
            "close_fa": -2.0,
            "volume": 1000,
            "open_interest": 200,
        },
        {
            "trade_date": "2026-01-02",
            "symbol": "RU",
            "contract": "RU2601",
            "open_raw": 20.0,
            "open_ba": -3.0,
            "open_fa": -4.0,
            "close_raw": 21.0,
            "close_ba": -5.0,
            "close_fa": -6.0,
            "volume": 2000,
            "open_interest": 300,
        },
    ])


def test_apply_adjustment_policy_uses_selected_lineage_and_excludes_default_raw():
    audit = pd.DataFrame([
        {
            "base_symbol": "M",
            "selected_adj": "ba",
            "included": True,
            "status": "fa_corrupt",
            "recommended_adj": "ba",
            "raw_fallback": False,
            "exclusion_reason": "",
        },
        {
            "base_symbol": "RU",
            "selected_adj": "",
            "included": False,
            "status": "both_corrupt",
            "recommended_adj": "raw",
            "raw_fallback": False,
            "exclusion_reason": "both_adjusted_lineages_corrupt",
        },
    ])

    out = _apply_adjustment_policy(_prices(), audit)

    assert out["symbol"].tolist() == ["M"]
    assert out.loc[0, "open"] == pytest.approx(100.0)
    assert out.loc[0, "close"] == pytest.approx(101.0)
    assert out.loc[0, "adjustment_lineage"] == "ba"


def test_apply_adjustment_policy_allows_explicit_raw_rows():
    audit = pd.DataFrame([
        {
            "base_symbol": "RU",
            "selected_adj": "raw",
            "included": True,
            "status": "both_corrupt",
            "recommended_adj": "raw",
            "raw_fallback": True,
            "exclusion_reason": "",
        },
    ])

    out = _apply_adjustment_policy(_prices(), audit)

    assert out["symbol"].tolist() == ["RU"]
    assert out.loc[0, "open"] == pytest.approx(20.0)
    assert out.loc[0, "close"] == pytest.approx(21.0)
    assert out.loc[0, "adjustment_lineage"] == "raw"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_pg_source.py -q
```

Expected: FAIL because `_apply_adjustment_policy` does not exist.

- [ ] **Step 3: Implement policy selection**

Modify imports in `cta/pg_source.py`:

```python
from cta.data_quality import build_adjustment_audit, summarize_adjustment_quality
```

Update `load_public_cta_data` signature:

```python
def load_public_cta_data(
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    rule_type: str = "standard",
    config_path=None,
    use_test: bool = False,
    include_financial: bool = False,
    adjustment_policy: str = "recommended",
    allow_raw_fallback: bool = False,
) -> CTADataSet:
```

In `load_public_cta_data`, call `_load_prices` with the new arguments and return:

```python
prices, quality = _load_prices(
    conn,
    start=start,
    end=end,
    symbols=symbols,
    rule_type=rule_type,
    include_financial=include_financial,
    adjustment_policy=adjustment_policy,
    allow_raw_fallback=allow_raw_fallback,
)
...
return CTADataSet(
    prices=normalize_prices(prices),
    fundamentals=normalize_fundamentals(fundamentals),
    data_quality=quality,
)
```

Update `_load_prices` to return `(prices, quality)` and load lineages:

```python
def _load_prices(
    conn,
    *,
    start: date | None,
    end: date | None,
    symbols: list[str] | None,
    rule_type: str,
    include_financial: bool,
    adjustment_policy: str,
    allow_raw_fallback: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
```

Replace selected price columns in SQL with all lineages:

```sql
SELECT
    trade_date,
    base_symbol AS symbol,
    contract_used AS contract,
    open_raw, open_ba, open_fa,
    high_raw, high_ba, high_fa,
    low_raw, low_ba, low_fa,
    close_raw, close_ba, close_fa,
    volume,
    oi AS open_interest,
    turnover,
    daily_return,
    pure_price_return,
    roll_contribution
FROM public.continuous_contract_ohlc
```

After `_read_sql`:

```python
raw = _read_sql(sql, conn, params=params)
if raw.empty:
    empty_quality = pd.DataFrame(columns=["base_symbol", "selected_adj", "included"])
    empty_prices = pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])
    return empty_prices, empty_quality
if adjustment_policy != "recommended":
    raise ValueError(f"unsupported CTA adjustment_policy: {adjustment_policy!r}")
quality_report = summarize_adjustment_quality(
    raw.rename(columns={"symbol": "base_symbol"})
)
quality = build_adjustment_audit(
    quality_report,
    allow_raw_fallback=allow_raw_fallback,
)
prices = _apply_adjustment_policy(raw, quality)
if prices.empty:
    raise ValueError("CTA price reader excluded all symbols under the adjustment policy")
return prices, quality
```

Add `_apply_adjustment_policy` near `_read_sql`:

```python
def _apply_adjustment_policy(prices: pd.DataFrame, quality: pd.DataFrame) -> pd.DataFrame:
    """Select open/close from each symbol's audited adjustment lineage."""
    if prices.empty:
        return prices.copy()
    decisions = quality[quality["included"]].copy()
    if decisions.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])

    merged = prices.merge(
        decisions[["base_symbol", "selected_adj"]],
        left_on="symbol",
        right_on="base_symbol",
        how="inner",
    )
    if merged.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])

    base_cols = [
        c for c in [
            "trade_date",
            "symbol",
            "contract",
            "volume",
            "open_interest",
            "turnover",
            "daily_return",
            "pure_price_return",
            "roll_contribution",
            "selected_adj",
        ]
        if c in merged.columns
    ]
    out = merged[base_cols].copy()
    out = out.rename(columns={"selected_adj": "adjustment_lineage"})

    for field in ("open", "high", "low", "close"):
        if not any(f"{field}_{lineage}" in merged.columns for lineage in ("raw", "ba", "fa")):
            continue
        values = []
        for _, row in merged.iterrows():
            values.append(row[f"{field}_{row['selected_adj']}"])
        out[field] = values
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_pg_source.py tests/test_cta_data_quality.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cta/pg_source.py tests/test_cta_pg_source.py
git commit -m "feat(cta): apply adjustment policy in pg reader"
```

---

### Task 4: Add Price/Volume Factor Set

**Files:**
- Modify: `cta/factors.py`
- Modify: `cta/__main__.py`
- Test: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cta_strategy.py`:

```python
from cta.factors import cta_factors_for_set


def test_price_volume_factor_set_excludes_fundamental_factors():
    factors = cta_factors_for_set("price_volume")

    assert [f.name for f in factors] == [
        "long_rule_momentum",
        "long_cross_momentum",
        "price_volume_corr",
    ]


def test_six_factor_set_remains_default():
    factors = cta_factors_for_set("six_factor")

    assert [f.name for f in factors] == [
        "basis",
        "inventory",
        "profit",
        "long_rule_momentum",
        "long_cross_momentum",
        "price_volume_corr",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py::test_price_volume_factor_set_excludes_fundamental_factors tests/test_cta_strategy.py::test_six_factor_set_remains_default -q
```

Expected: FAIL because `cta_factors_for_set` does not exist.

- [ ] **Step 3: Implement factor-set helpers**

Add to `cta/factors.py` after `default_cta_factors`:

```python
def price_volume_cta_factors() -> list[CTAFactor]:
    """Price/volume-only CTA factors usable before fundamentals are standardized."""
    return [
        LongRuleMomentumFactor(),
        LongCrossSectionMomentumFactor(),
        PriceVolumeCorrelationFactor(),
    ]


def cta_factors_for_set(name: str) -> list[CTAFactor]:
    """Resolve a named CTA factor set."""
    if name == "six_factor":
        return default_cta_factors()
    if name == "price_volume":
        return price_volume_cta_factors()
    raise ValueError(f"unknown CTA factor set: {name!r}")
```

Modify `cta/__main__.py` imports:

```python
from cta.factors import cta_factors_for_set
```

Add CLI argument:

```python
parser.add_argument(
    "--factor-set",
    choices=["six_factor", "price_volume"],
    default="six_factor",
    help="CTA factor set; price_volume avoids sparse fundamental factors",
)
```

Before building jobs:

```python
factors = cta_factors_for_set(args.factor_set)
```

Pass `factors=factors` into `run_medium_equal_weight` and `run_high_composite`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cta/factors.py cta/__main__.py tests/test_cta_strategy.py
git commit -m "feat(cta): add price volume factor set"
```

---

### Task 5: Write Data-Quality Audit to Outputs

**Files:**
- Modify: `cta/backtest.py`
- Test: `tests/test_cta_strategy.py`

- [ ] **Step 1: Write failing output test**

Append to `tests/test_cta_strategy.py`:

```python
from cta.backtest import write_cta_outputs


def test_write_cta_outputs_includes_data_quality_sheet(tmp_path):
    data = _sample_cta_data()
    quality = pd.DataFrame([
        {
            "base_symbol": "CU",
            "status": "ok",
            "recommended_adj": "fa",
            "selected_adj": "fa",
            "included": True,
            "raw_fallback": False,
            "exclusion_reason": "",
        }
    ])
    data = CTADataSet(prices=data.prices, fundamentals=data.fundamentals, data_quality=quality)
    result = run_medium_equal_weight(
        data,
        symbols=data.symbols,
        factors=cta_factors_for_set("price_volume"),
        cost_bps=1.0,
    )

    xlsx, _ = write_cta_outputs(result, tmp_path / "cta_guarded")

    sheets = pd.ExcelFile(xlsx).sheet_names
    assert "data_quality" in sheets
    written = pd.read_excel(xlsx, sheet_name="data_quality")
    assert written.loc[0, "base_symbol"] == "CU"
    assert written.loc[0, "selected_adj"] == "fa"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py::test_write_cta_outputs_includes_data_quality_sheet -q
```

Expected: FAIL because `data_quality` sheet is not written.

- [ ] **Step 3: Add audit metadata to backtest result**

Modify imports in `cta/backtest.py`:

```python
from dataclasses import dataclass, field
```

Add a field to `CTABacktestResult`:

```python
data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
```

In `CTABacktester.run`, set:

```python
data_quality=self.data.data_quality.copy(),
```

Modify `write_cta_outputs`:

```python
if not result.data_quality.empty:
    result.data_quality.to_excel(writer, sheet_name="data_quality", index=False)
```

Place this after writing `factor_returns`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cta/backtest.py tests/test_cta_strategy.py
git commit -m "feat(cta): write data quality audit sheet"
```

---

### Task 6: CLI Reader Policy and Summary

**Files:**
- Modify: `cta/__main__.py`
- Test: `tests/test_cta_strategy.py`

- [ ] **Step 1: Add a focused summary helper test**

Append to `tests/test_cta_strategy.py`:

```python
from cta.__main__ import _data_quality_summary


def test_data_quality_summary_counts_retained_excluded_and_raw():
    quality = pd.DataFrame([
        {"base_symbol": "A", "included": True, "raw_fallback": False},
        {"base_symbol": "B", "included": True, "raw_fallback": True},
        {"base_symbol": "C", "included": False, "raw_fallback": False},
    ])

    assert _data_quality_summary(quality) == "symbols retained=2 excluded=1 raw_fallback=1"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py::test_data_quality_summary_counts_retained_excluded_and_raw -q
```

Expected: FAIL because `_data_quality_summary` does not exist.

- [ ] **Step 3: Implement CLI policy arguments and summary**

In `cta/__main__.py`, add arguments:

```python
parser.add_argument(
    "--adjustment-policy",
    choices=["recommended"],
    default="recommended",
    help="price-lineage policy for public-pg source",
)
parser.add_argument(
    "--allow-raw-fallback",
    action="store_true",
    help="allow raw prices only for symbols whose adjusted lineages are both corrupt",
)
```

Pass them into `load_public_cta_data`:

```python
adjustment_policy=args.adjustment_policy,
allow_raw_fallback=args.allow_raw_fallback,
```

Add helper near `_parse_symbols`:

```python
def _data_quality_summary(quality) -> str:
    if quality is None or quality.empty or "included" not in quality.columns:
        return "symbols retained=unknown excluded=unknown raw_fallback=0"
    retained = int(quality["included"].fillna(False).astype(bool).sum())
    excluded = int((~quality["included"].fillna(False).astype(bool)).sum())
    raw = (
        int(quality["raw_fallback"].fillna(False).astype(bool).sum())
        if "raw_fallback" in quality.columns else 0
    )
    return f"symbols retained={retained} excluded={excluded} raw_fallback={raw}"
```

After loading `data`, print:

```python
print(f"data_quality: {_data_quality_summary(data.data_quality)}")
print(f"factor_set: {args.factor_set}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cta/__main__.py tests/test_cta_strategy.py
git commit -m "feat(cta): expose guarded reader policy in cli"
```

---

### Task 7: Runbook Update and Verification

**Files:**
- Modify: `docs/operations/cta-strategy-replication.md`

- [ ] **Step 1: Update runbook with guarded smoke command**

Add this section after the existing `运行` section in `docs/operations/cta-strategy-replication.md`:

```markdown
## 价格/量价 guarded smoke

在上游连续合约复权根因修复前，优先跑价格/量价 guarded smoke。该路径只启用
`long_rule_momentum`、`long_cross_momentum`、`price_volume_corr` 三个因子，并按
`cta.data_quality` 的 per-symbol 判定选择 `fa` 或 `ba` 价格线；两条复权线都坏的品种默认剔除。

```bash
.venv/bin/python -m cta \
  --source public-pg \
  --strategy both \
  --factor-set price_volume \
  --adjustment-policy recommended \
  --start 2019-01-01 \
  --end 2025-09-30
```

若显式加 `--allow-raw-fallback`，两条复权线都坏的品种会使用 raw 价格。该结果只能作为研究排查，
不能视作连续复权回测。

输出 Excel 包含 `data_quality` sheet，用于审计每个品种的 `selected_adj`、`status`、
`raw_fallback` 和剔除原因。
```

- [ ] **Step 2: Run unit tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py tests/test_cta_data_quality.py tests/test_cta_pg_source.py -q
```

Expected: PASS.

- [ ] **Step 3: Run live data-quality scan**

Run:

```bash
.venv/bin/python -m cta.data_quality
```

Expected: command exits 0 and prints scanned symbol count plus status breakdown.

- [ ] **Step 4: Run guarded CTA smoke**

Run:

```bash
.venv/bin/python -m cta \
  --source public-pg \
  --strategy medium_equal_weight \
  --factor-set price_volume \
  --adjustment-policy recommended \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --output-prefix /tmp/cta_price_volume_guarded_smoke
```

Expected: command exits 0, prints `data_quality:` and writes:

- `/tmp/cta_price_volume_guarded_smoke.xlsx`
- `/tmp/cta_price_volume_guarded_smoke_equity.png`

Open the workbook with pandas to verify sheets:

```bash
.venv/bin/python - <<'PY'
import pandas as pd
x = pd.ExcelFile('/tmp/cta_price_volume_guarded_smoke.xlsx')
print(x.sheet_names)
assert 'data_quality' in x.sheet_names
PY
```

Expected: list includes `data_quality`.

- [ ] **Step 5: Commit**

```bash
git add docs/operations/cta-strategy-replication.md
git commit -m "docs(cta): document guarded price volume smoke"
```

---

## Final Verification

- [ ] Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py tests/test_cta_data_quality.py tests/test_cta_pg_source.py -q
```

Expected: all tests pass.

- [ ] Run:

```bash
.venv/bin/python -m cta.data_quality
```

Expected: exits 0 and reports the live continuous-contract quality status.

- [ ] Run:

```bash
.venv/bin/python -m cta \
  --source public-pg \
  --strategy medium_equal_weight \
  --factor-set price_volume \
  --adjustment-policy recommended \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --output-prefix /tmp/cta_price_volume_guarded_smoke
```

Expected: exits 0 and writes an Excel workbook with `data_quality`.
