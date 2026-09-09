# Continuous Market-Break Segments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the Guosen continuous-signal panel to cross genuine contract-market hiatuses without fabricating a roll price, while preserving an explicit boundary that downstream indicators cannot cross.

**Architecture:** `adjustment_factors` classifies every contract transition as a normal overlapping roll, a provably disjoint market break, or an invalid/missing-data roll. It emits both the cumulative factor and a per-product integer segment. `build_panel` carries that segment into every Parquet row, and the production script audits segment counts before monthly minute queries.

**Tech Stack:** Python 3.13/3.12, pandas, fastparquet/pyarrow, pytest, PostgreSQL, SSH/WSL2.

---

## File map

- Modify `cta_continuous/continuous.py`: classify roll boundaries and emit `continuity_segment`.
- Modify `cta_continuous/panel.py`: require and persist the segment mapping as `int64`.
- Modify `scripts/continuous/build_panel.py`: wire factor-table segments into monthly shards and print the audit summary.
- Modify `tests/test_continuous_roll.py`: prove disjoint breaks reset, while overlapping missing data still fails.
- Modify `tests/test_continuous_panel.py`: prove segment propagation, fail-closed mappings, and Parquet dtype.
- Modify `docs/superpowers/plans/2026-08-27-guosen-continuous-signal.md`: align fidelity decision D16 with the approved market-break design.

### Task 1: Classify roll boundaries in the factor table

**Files:**
- Modify: `tests/test_continuous_roll.py`
- Modify: `cta_continuous/continuous.py`

- [ ] **Step 1: Write the failing disjoint-market test**

Add a test whose old and new contract histories are strictly separated:

```python
def test_adjustment_starts_a_new_segment_for_disjoint_contract_histories():
    old = DominantChoice(
        date(2018, 3, 30), "FU", "FU1804.SHF", 1, 1, date(2018, 3, 29)
    )
    new = DominantChoice(
        date(2018, 7, 17), "FU", "FU1901.SHF", 1, 1, date(2018, 7, 16)
    )
    factors = adjustment_factors(
        (old, new),
        closes={
            (date(2018, 3, 30), "FU1804.SHF"): 3200.0,
            (date(2018, 7, 16), "FU1901.SHF"): 2800.0,
        },
    )
    assert list(factors["adj_factor"]) == [1.0, 1.0]
    assert list(factors["continuity_segment"]) == [0, 1]
```

Amend `test_adjustment_refuses_to_default_a_missing_roll_close` so the two date ranges overlap without sharing an effective close:

```python
closes[(D[0], "RB2501.SHF")] = 249.0
closes.pop((D[3], "RB2501.SHF"))
with pytest.raises(ValueError, match="roll_close_missing"):
    adjustment_factors(choices, closes=closes)
```

- [ ] **Step 2: Run the two boundary tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_continuous_roll.py::test_adjustment_starts_a_new_segment_for_disjoint_contract_histories \
  tests/test_continuous_roll.py::test_adjustment_refuses_to_default_a_missing_roll_close -q
```

Expected: the new test fails with the current `roll_close_missing`; the missing-data test also continues to raise.

- [ ] **Step 3: Emit a deterministic per-product segment**

In `adjustment_factors`, initialize `segment = 0` with each new product. For a changed canonical contract, keep the existing common-close path; when no eligible common date exists, use this exact classification:

```python
old_dates = set(old_closes)
new_dates = set(new_closes)
market_break = (
    bool(old_dates)
    and bool(new_dates)
    and max(old_dates) < min(new_dates)
)
if market_break:
    segment += 1
    factor = 1.0
else:
    raise ValueError(
        "roll_close_missing: 展期判定日前没有新旧合约共同收盘价，"
        "且合约历史不是前后分离的市场断代；"
        f"not_after={choice.selected_from} {previous.contract!r} "
        f"-> {choice.contract!r}"
    )
```

Add `"continuity_segment": segment` to each record and return columns in this order:

```python
["product", "trade_date", "contract", "adj_factor", "continuity_segment"]
```

- [ ] **Step 4: Run all roll tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_continuous_roll.py -q`

Expected: all tests pass, including AU latest-common-close and CZCE alias coverage.

- [ ] **Step 5: Perform mutation validation**

Temporarily replace `segment += 1` with `segment += 0`, clear `cta_continuous/__pycache__`, and rerun only the new disjoint-market test.

Expected: FAIL with observed segments `[0, 0]`. Restore `segment += 1`, clear the cache again, and rerun to PASS.

- [ ] **Step 6: Commit the factor-table change**

```bash
git add cta_continuous/continuous.py tests/test_continuous_roll.py
git commit -m "feat: segment discontinuous dominant histories"
```

### Task 2: Carry segment identity through the panel schema

**Files:**
- Modify: `tests/test_continuous_panel.py`
- Modify: `cta_continuous/panel.py`

- [ ] **Step 1: Write failing panel propagation tests**

Extend `_panel()` with deterministic segments and pass them to `build_panel`:

```python
segments = {key: index for index, key in enumerate(sorted(contexts))}
return contexts, build_panel(
    contexts=contexts,
    source=source,
    pricing_basis_by_exchange={},
    multiplier_resolver=lambda candidate, frame: 10,
    adjustment_factor_by_key=factors,
    continuity_segment_by_key=segments,
)
```

Add:

```python
def test_panel_carries_the_product_days_continuity_segment():
    contexts, panel = _panel()
    observed = (
        panel[["trade_date", "product", "continuity_segment"]]
        .drop_duplicates()
        .assign(trade_date=lambda frame: frame["trade_date"].dt.date)
    )
    expected = {key: index for index, key in enumerate(sorted(contexts))}
    assert {
        (row.trade_date, row.product): row.continuity_segment
        for row in observed.itertuples(index=False)
    } == expected


def test_panel_refuses_a_missing_continuity_segment():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    with pytest.raises(ValueError, match="panel_continuity_segment_missing"):
        build_panel(
            contexts=contexts,
            source=_FakeSource(contexts),
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame: 10,
            adjustment_factor_by_key={key: 1.0 for key in contexts},
            continuity_segment_by_key={},
        )
```

Add `assert restored["continuity_segment"].dtype == "int64"` to the Parquet round-trip test. Update every existing direct `build_panel` call to pass an explicit mapping.

- [ ] **Step 2: Run panel tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_continuous_panel.py -q`

Expected: `TypeError` for the unknown `continuity_segment_by_key` argument or missing output column.

- [ ] **Step 3: Add the panel field and fail-closed wiring**

Add `"continuity_segment"` immediately after `"adj_factor"` in `PANEL_COLUMNS`. Add a keyword argument to both builders:

```python
def build_session_bars(..., adj_factor: float = 1.0, continuity_segment: int = 0):
    ...
    row["continuity_segment"] = continuity_segment


def build_panel(..., adjustment_factor_by_key, continuity_segment_by_key):
    missing_segments = sorted(set(contexts) - set(continuity_segment_by_key))
    if missing_segments:
        raise ValueError(
            "panel_continuity_segment_missing: 缺少品种日连续分段；"
            f"first={missing_segments[0]!r} count={len(missing_segments)}"
        )
```

Pass `continuity_segment=int(continuity_segment_by_key[key])` into `build_session_bars`. Normalize both integer fields explicitly:

```python
for column in ("continuity_segment", "multiplier"):
    out[column] = pd.to_numeric(out[column]).astype("int64")
```

- [ ] **Step 4: Run panel and roll tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_continuous_panel.py tests/test_continuous_roll.py -q
```

Expected: all tests pass and the Parquet test confirms `int64`.

- [ ] **Step 5: Perform mutation validation**

Temporarily write `"continuity_segment": 0` in `build_session_bars`, clear `cta_continuous/__pycache__`, and run `test_panel_carries_the_product_days_continuity_segment`.

Expected: FAIL because the second context must carry segment 1. Restore the mapping and rerun to PASS.

- [ ] **Step 6: Commit the panel-schema change**

```bash
git add cta_continuous/panel.py tests/test_continuous_panel.py
git commit -m "feat: persist continuous market segments in panel"
```

### Task 3: Wire production audit and fidelity documentation

**Files:**
- Modify: `scripts/continuous/build_panel.py`
- Modify: `docs/superpowers/plans/2026-08-27-guosen-continuous-signal.md`

- [ ] **Step 1: Wire the segment mapping into the monthly builder**

After `factor_rows = adjustment_factors(...)`, build:

```python
segment_by_key = {
    (row.trade_date, row.product): int(row.continuity_segment)
    for row in factor_rows.itertuples(index=False)
}
segment_total = factor_rows[["product", "continuity_segment"]].drop_duplicates().shape[0]
break_total = segment_total - factor_rows["product"].nunique()
```

Update the summary to include `连续段 {segment_total:,}，断代 {break_total:,}`. Pass the exact monthly subset:

```python
continuity_segment_by_key={key: segment_by_key[key] for key in contexts},
```

- [ ] **Step 2: Align D16 with the approved classification**

Replace D16's final sentence with an explicit split:

```markdown
新旧历史严格前后分离时开启新 `continuity_segment`、因子重置为 1；日期区间重叠却没有共同有效收盘，或任一侧完全无有效收盘时仍硬失败。
```

- [ ] **Step 3: Run static and targeted verification**

Run:

```bash
.venv/bin/python -m py_compile \
  cta_continuous/continuous.py cta_continuous/panel.py \
  scripts/continuous/build_panel.py
.venv/bin/python -m pytest tests/test_continuous_roll.py tests/test_continuous_panel.py -q
git diff --check
```

Expected: compilation succeeds, all targeted tests pass, and `git diff --check` is silent.

- [ ] **Step 4: Commit production wiring**

```bash
git add scripts/continuous/build_panel.py \
  docs/superpowers/plans/2026-08-27-guosen-continuous-signal.md
git commit -m "feat: audit continuous market breaks"
```

### Task 4: Verify locally and resume WSL2 panel construction

**Files:**
- No source changes expected.
- Output: WSL2 `/tmp/continuous-panel-smoke-2023-01/panel-2023-01.parquet`
- Output: WSL2 `output/continuous/panel/panel-YYYY-MM.parquet`
- Log: WSL2 `logs/continuous_panel_full.log`

- [ ] **Step 1: Run the complete local suite**

Run: `.venv/bin/python -m pytest -q`

Expected: at least 1333 tests pass with no failures; existing Parquet/font warnings are allowed.

- [ ] **Step 2: Bundle and fast-forward the isolated WSL2 clone**

Create and transfer a bundle for the exact new HEAD, then run:

```bash
git -C /home/ghls/futures_strategies_continuous_20260827 fetch \
  /tmp/futures-cta-continuous-<HEAD>.bundle feature/cta-continuous-signal
git -C /home/ghls/futures_strategies_continuous_20260827 merge --ff-only FETCH_HEAD
```

Expected: remote HEAD equals local HEAD. Do not touch `/home/ghls/futures_strategies` or retransmit/print credentials.

- [ ] **Step 3: Run remote targeted tests**

Run from the isolated clone:

```bash
.venv/bin/python -m pytest tests/test_continuous_roll.py tests/test_continuous_panel.py -q
```

Expected: all targeted tests pass on Python 3.12.

- [ ] **Step 4: Re-run the one-month smoke build**

```bash
timeout 1200 .venv/bin/python scripts/continuous/build_panel.py \
  --start 2023-01 --end 2023-01 \
  --out /tmp/continuous-panel-smoke-2023-01
```

Expected: the log reports at least one market break, writes `panel-2023-01.parquet`, and does not raise for LU, AU, or FU.

- [ ] **Step 5: Inspect the shard**

Read the Parquet file and print only non-sensitive audit data:

```python
print(len(panel), list(panel.columns))
print(panel["continuity_segment"].dtype, panel["adj_factor"].isna().sum())
print(panel[["product", "continuity_segment"]].drop_duplicates().shape[0])
```

Expected: nonzero rows, `continuity_segment` present as `int64`, and no missing `adj_factor`.

- [ ] **Step 6: Start the resumable full-history job**

From `/home/ghls/futures_strategies_continuous_20260827`:

```bash
mkdir -p logs output/continuous/panel
setsid nohup .venv/bin/python scripts/continuous/build_panel.py \
  --start 2011-01 --end 2026-01 \
  --out output/continuous/panel \
  > logs/continuous_panel_full.log 2>&1 < /dev/null &
```

Record the returned PID. Judge liveness with `ps -p PID -o pid=,etimes=,%cpu=,%mem=,rss=,stat=,args=` and inspect only the task log. Existing shards must be skipped on restart.

- [ ] **Step 7: Update the parent execution plan checkpoint**

Record the smoke-shard row count/schema, full-history PID, log path, first successful monthly shard, and any remaining authoritative session-rule gap before beginning Task 6 backtesting.
