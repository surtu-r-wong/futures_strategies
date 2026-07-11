# Continuous Ratio Adjustment Fix Implementation Plan

> **迁移注**（2026-07-11）：本文档随 CTA 剥离迁自 stock_selector；文中 `cta/`、`tests/test_cta_*`、`docs/superpowers/*` 等路径为当时原仓路径（历史记录，不改写），现行代码对应本仓 `cta_gtja/`、`tests/`、`docs/plans|specs/`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix pi's continuous-contract adjusted price generator so `ba` and `fa` prices use ratio adjustment instead of additive gap adjustment, and prevent non-positive adjusted prices from being written.

**Architecture:** Work on the latest scripts in `pi:/home/pi/market-monitor/backend`, which is not a git repository, so every remote edit starts with timestamped backups. Add a small synthetic regression script on pi, patch `continuous_generator.py` and `continuous_generator_nh.py`, add pre-write validation, then regenerate only affected symbols after the pi database accepts connections. Keep local `stock_selector` changes limited to documentation and audit-runbook updates.

**Tech Stack:** Python 3.11 on pi, pandas, psycopg2, shell over `ssh pi`, local Python 3.13 docs/tests in `stock_selector`.

---

## File Structure

- Remote modify: `pi:/home/pi/market-monitor/backend/continuous_generator.py`
  - Standard rule generator.
  - Uses prior-date old close and roll-date new close for adjustment basis.
  - Needs ratio-based `calculate_adjustments` and `validate_generated_prices`.
- Remote modify: `pi:/home/pi/market-monitor/backend/continuous_generator_nh.py`
  - Nanhua rule generator.
  - Uses same-day old/new open prices for adjustment basis.
  - Needs ratio-based `calculate_adjustments` and `validate_generated_prices`.
- Remote create: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`
  - Synthetic regression script; no database dependency.
  - Proves contango and backwardation rolls are continuous under ratio adjustment.
- Remote create: `pi:/home/pi/market-monitor/backend/scan_adjustment_quality.py`
  - Database quality scan mirroring downstream CTA audit, using pi's `continuous_config.yaml`.
- Local modify: `cta/data_quality.py`
  - Update stale upstream path in module docstring and CLI message from `/home/elfbob/claude-code/continuous/` to `pi:/home/pi/market-monitor/backend/`.
- Local modify: `docs/operations/cta-strategy-replication.md`
  - Note that `ba_factor` and `fa_factor` are multiplicative after the pi fix.
  - Note the pi database endpoint must be confirmed before regeneration.

---

### Task 1: Back Up Remote Continuous Scripts

**Files:**
- Read: `pi:/home/pi/market-monitor/backend/continuous_generator.py`
- Read: `pi:/home/pi/market-monitor/backend/continuous_generator_nh.py`
- Read: `pi:/home/pi/market-monitor/backend/continuous_daily_runner.py`
- Create: `pi:/home/pi/market-monitor/backend/backups/continuous_fix_<timestamp>/...`

- [ ] **Step 1: Confirm remote path and database state**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && pwd && ls -l continuous_generator.py continuous_generator_nh.py continuous_daily_runner.py'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python - <<'"'"'PY'"'"'
import urllib.parse
import yaml

cfg = yaml.safe_load(open("continuous_config.yaml"))
url = cfg["database"]["url"]
p = urllib.parse.urlparse(url)
print(f"host={p.hostname} port={p.port} db={p.path.lstrip('/')} user={p.username}")
PY'
ssh pi 'pg_isready -h 100.75.102.44 -p 5432 -d market_monitor -U admin'
```

Expected:

- First command lists the three files.
- Second command prints `host=100.75.102.44 port=5432 db=market_monitor user=admin`.
- Third command may still report `rejecting connections`; if so, continue code-only tasks but do not regenerate database rows.

- [ ] **Step 2: Create timestamped backups**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && ts=$(date +%Y%m%d_%H%M%S) && mkdir -p backups/continuous_fix_$ts && cp continuous_generator.py continuous_generator_nh.py continuous_daily_runner.py continuous_config.yaml backups/continuous_fix_$ts/ && ls -l backups/continuous_fix_$ts'
```

Expected: output lists copied `continuous_generator.py`, `continuous_generator_nh.py`, `continuous_daily_runner.py`, and `continuous_config.yaml`.

- [ ] **Step 3: Record checksums before editing**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && sha256sum continuous_generator.py continuous_generator_nh.py continuous_daily_runner.py continuous_config.yaml'
```

Expected: four SHA-256 rows. Keep this output in the task notes.

---

### Task 2: Add Synthetic Ratio-Adjustment Regression Script

**Files:**
- Create: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`
- Test: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`

- [ ] **Step 1: Create the failing regression script**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && cat > test_ratio_adjustments.py <<'"'"'PY'"'"'
from __future__ import annotations

from datetime import date
import math

import pandas as pd

from continuous_generator import ContinuousContractGenerator
from continuous_generator_nh import ContinuousContractGeneratorNH


D0 = date(2026, 1, 1)
D1 = date(2026, 1, 2)


def _base_continuous(old_price: float, new_price: float) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "trade_date": D0,
            "contract_used": "OLD.EX",
            "open_raw": old_price,
            "high_raw": old_price,
            "low_raw": old_price,
            "close_raw": old_price,
            "volume": 100,
            "oi": 1000,
            "is_rolling": False,
            "roll_weight": None,
            "old_contract": None,
            "new_contract": None,
        },
        {
            "trade_date": D1,
            "contract_used": "NEW.EX",
            "open_raw": new_price,
            "high_raw": new_price,
            "low_raw": new_price,
            "close_raw": new_price,
            "volume": 120,
            "oi": 1200,
            "is_rolling": False,
            "roll_weight": None,
            "old_contract": None,
            "new_contract": None,
        },
    ])


def _standard_contracts(old_price: float, new_price: float) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "trade_date": D0,
            "symbol": "OLD.EX",
            "open": old_price,
            "high": old_price,
            "low": old_price,
            "close": old_price,
            "volume": 100,
            "oi": 1000,
        },
        {
            "trade_date": D1,
            "symbol": "NEW.EX",
            "open": new_price,
            "high": new_price,
            "low": new_price,
            "close": new_price,
            "volume": 120,
            "oi": 1200,
        },
    ])


def _nanhua_contracts(old_price: float, new_price: float) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "trade_date": D1,
            "symbol": "OLD.EX",
            "open": old_price,
            "high": old_price,
            "low": old_price,
            "close": old_price,
            "volume": 100,
            "oi": 1000,
        },
        {
            "trade_date": D1,
            "symbol": "NEW.EX",
            "open": new_price,
            "high": new_price,
            "low": new_price,
            "close": new_price,
            "volume": 120,
            "oi": 1200,
        },
    ])


def _rollover() -> list[dict]:
    return [
        {
            "roll_start_date": D1,
            "roll_end_date": None,
            "old_contract": "OLD.EX",
            "new_contract": "NEW.EX",
        }
    ]


def _assert_close(actual: float, expected: float) -> None:
    assert math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12), (
        actual,
        expected,
    )


def _assert_ratio_adjusted(out: pd.DataFrame, old_price: float, new_price: float) -> None:
    out = out.sort_values("trade_date").reset_index(drop=True)
    ratio = new_price / old_price

    _assert_close(out.loc[0, "close_ba"], new_price)
    _assert_close(out.loc[1, "close_ba"], new_price)
    _assert_close(out.loc[0, "open_ba"], new_price)
    _assert_close(out.loc[1, "open_ba"], new_price)
    _assert_close(out.loc[0, "ba_factor"], ratio)
    _assert_close(out.loc[1, "ba_factor"], 1.0)

    _assert_close(out.loc[0, "close_fa"], old_price)
    _assert_close(out.loc[1, "close_fa"], old_price)
    _assert_close(out.loc[0, "open_fa"], old_price)
    _assert_close(out.loc[1, "open_fa"], old_price)
    _assert_close(out.loc[0, "fa_factor"], 1.0)
    _assert_close(out.loc[1, "fa_factor"], 1.0 / ratio)

    for col in ("open_ba", "close_ba", "open_fa", "close_fa"):
        assert (out[col] > 0).all(), (col, out[["trade_date", col]])


def _assert_no_rollover_factors_are_one(out: pd.DataFrame) -> None:
    out = out.sort_values("trade_date").reset_index(drop=True)
    for idx, raw_price in enumerate([100.0, 120.0]):
        _assert_close(out.loc[idx, "ba_factor"], 1.0)
        _assert_close(out.loc[idx, "fa_factor"], 1.0)
        _assert_close(out.loc[idx, "close_ba"], raw_price)
        _assert_close(out.loc[idx, "close_fa"], raw_price)


def test_standard_ratio_adjustment_contango() -> None:
    gen = object.__new__(ContinuousContractGenerator)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 120.0),
        _rollover(),
        _standard_contracts(100.0, 120.0),
    )
    _assert_ratio_adjusted(out, 100.0, 120.0)


def test_standard_ratio_adjustment_backwardation() -> None:
    gen = object.__new__(ContinuousContractGenerator)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 80.0),
        _rollover(),
        _standard_contracts(100.0, 80.0),
    )
    _assert_ratio_adjusted(out, 100.0, 80.0)


def test_standard_no_rollover_factors_are_one() -> None:
    gen = object.__new__(ContinuousContractGenerator)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 120.0),
        [],
        _standard_contracts(100.0, 120.0),
    )
    _assert_no_rollover_factors_are_one(out)


def test_nanhua_ratio_adjustment_contango() -> None:
    gen = object.__new__(ContinuousContractGeneratorNH)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 120.0),
        _rollover(),
        _nanhua_contracts(100.0, 120.0),
    )
    _assert_ratio_adjusted(out, 100.0, 120.0)


def test_nanhua_ratio_adjustment_backwardation() -> None:
    gen = object.__new__(ContinuousContractGeneratorNH)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 80.0),
        _rollover(),
        _nanhua_contracts(100.0, 80.0),
    )
    _assert_ratio_adjusted(out, 100.0, 80.0)


def test_nanhua_no_rollover_factors_are_one() -> None:
    gen = object.__new__(ContinuousContractGeneratorNH)
    out = gen.calculate_adjustments(
        _base_continuous(100.0, 120.0),
        [],
        _nanhua_contracts(100.0, 120.0),
    )
    _assert_no_rollover_factors_are_one(out)


def _run() -> None:
    tests = [
        test_standard_ratio_adjustment_contango,
        test_standard_ratio_adjustment_backwardation,
        test_standard_no_rollover_factors_are_one,
        test_nanhua_ratio_adjustment_contango,
        test_nanhua_ratio_adjustment_backwardation,
        test_nanhua_no_rollover_factors_are_one,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    _run()
PY'
```

- [ ] **Step 2: Run script to verify it fails on the current additive implementation**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected: FAIL with an `AssertionError` showing adjusted prices do not match ratio-adjusted continuity.

---

### Task 3: Patch Standard Generator to Use Ratio Adjustment

**Files:**
- Modify: `pi:/home/pi/market-monitor/backend/continuous_generator.py`
- Test: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`

- [ ] **Step 1: Replace the standard `calculate_adjustments` body**

Edit `continuous_generator.py` inside `ContinuousContractGenerator.calculate_adjustments`.

Keep the existing function signature and empty-data branch. First update the no-rollover branch so `ba_factor` and `fa_factor` are `1.0`, not `0`:

```python
        if len(rollovers) == 0:
            logger.info("无换月事件，直接使用原始价格")
            continuous_df['ba_factor'] = 1.0
            continuous_df['fa_factor'] = 1.0
            for price_type in ['open', 'high', 'low', 'close']:
                continuous_df[f'{price_type}_ba'] = continuous_df[f'{price_type}_raw']
                continuous_df[f'{price_type}_fa'] = continuous_df[f'{price_type}_raw']
            return continuous_df
```

Then replace the section beginning at:

```python
# 计算每次换月的价差
rollover_gaps = []
```

through the final adjusted-price assignment before:

```python
logger.info("✓ 复权计算完成")
```

with:

```python
        # 计算每次换月的价格比例
        rollover_gaps = []
        for rollover in rollovers:
            roll_date = rollover['roll_start_date']
            old_contract = rollover['old_contract']
            new_contract = rollover['new_contract']

            # 获取旧合约最后一天和新合约第一天的价格，保持 standard 规则原有口径
            prev_date = continuous_df[continuous_df['trade_date'] < roll_date]['trade_date'].max()

            if pd.isna(prev_date):
                logger.warning(f"换月 {old_contract} → {new_contract} 找不到前一日数据，跳过")
                continue

            old_data = contracts_df[(contracts_df['trade_date'] == prev_date) &
                                    (contracts_df['symbol'] == old_contract)]
            new_data = contracts_df[(contracts_df['trade_date'] == roll_date) &
                                    (contracts_df['symbol'] == new_contract)]

            if not old_data.empty and not new_data.empty:
                old_close = float(old_data['close'].values[0])
                new_close = float(new_data['close'].values[0])
                if old_close <= 0 or new_close <= 0:
                    logger.warning(
                        f"  换月 {old_contract} → {new_contract} 价格非正，跳过: "
                        f"old={old_close}, new={new_close}"
                    )
                    continue
                ratio = new_close / old_close
                gap = new_close - old_close

                rollover_gaps.append({
                    'roll_date': roll_date,
                    'ratio': ratio,
                    'gap': gap,
                    'old_close': old_close,
                    'new_close': new_close,
                    'old_contract': old_contract,
                    'new_contract': new_contract,
                })

                logger.info(
                    f"  换月比例: {old_contract}({old_close:.2f}) → "
                    f"{new_contract}({new_close:.2f}), ratio={ratio:.8f}, 价差={gap:.2f}"
                )

        if len(rollover_gaps) == 0:
            logger.info("无有效换月比例，使用原始价格")
            continuous_df['ba_factor'] = 1.0
            continuous_df['fa_factor'] = 1.0
            for price_type in ['open', 'high', 'low', 'close']:
                continuous_df[f'{price_type}_ba'] = continuous_df[f'{price_type}_raw']
                continuous_df[f'{price_type}_fa'] = continuous_df[f'{price_type}_raw']
            return continuous_df

        # 前复权：从最新往历史，历史价格乘以后续换月比例，映射到最新合约价格基准
        ba_factor = 1.0
        continuous_df = continuous_df.sort_values('trade_date', ascending=False).copy()
        ba_factors = []
        rollover_gaps_ba = rollover_gaps.copy()

        for idx, row in continuous_df.iterrows():
            date = row['trade_date']

            for rg in rollover_gaps_ba[:]:
                if date < rg['roll_date']:
                    ba_factor *= rg['ratio']
                    rollover_gaps_ba.remove(rg)

            ba_factors.append(ba_factor)

        continuous_df['ba_factor'] = ba_factors
        for price_type in ['open', 'high', 'low', 'close']:
            continuous_df[f'{price_type}_ba'] = (
                continuous_df[f'{price_type}_raw'] * continuous_df['ba_factor']
            )

        # 后复权：从历史往最新，未来价格乘以换月比例倒数，映射到初始合约价格基准
        continuous_df = continuous_df.sort_values('trade_date', ascending=True).copy()
        fa_factor = 1.0
        fa_factors = []
        rollover_gaps_fa = rollover_gaps.copy()

        for idx, row in continuous_df.iterrows():
            date = row['trade_date']

            for rg in rollover_gaps_fa[:]:
                if date >= rg['roll_date']:
                    fa_factor *= 1.0 / rg['ratio']
                    rollover_gaps_fa.remove(rg)

            fa_factors.append(fa_factor)

        continuous_df['fa_factor'] = fa_factors
        for price_type in ['open', 'high', 'low', 'close']:
            continuous_df[f'{price_type}_fa'] = (
                continuous_df[f'{price_type}_raw'] * continuous_df['fa_factor']
            )
```

- [ ] **Step 2: Run regression script and confirm only Nanhua tests still fail**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected: the three standard tests pass, then script fails on the first Nanhua ratio or no-rollover test.

---

### Task 4: Patch Nanhua Generator to Use Ratio Adjustment

**Files:**
- Modify: `pi:/home/pi/market-monitor/backend/continuous_generator_nh.py`
- Test: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`

- [ ] **Step 1: Replace the Nanhua `calculate_adjustments` body**

Edit `continuous_generator_nh.py` inside `ContinuousContractGeneratorNH.calculate_adjustments`.

Keep the existing function signature and empty-data branch. First update the no-rollover branch so `ba_factor` and `fa_factor` are `1.0`, not `0`:

```python
        if len(rollovers) == 0:
            logger.info("无换月事件，直接使用原始价格")
            continuous_df['ba_factor'] = 1.0
            continuous_df['fa_factor'] = 1.0
            for price_type in ['open', 'high', 'low', 'close']:
                continuous_df[f'{price_type}_ba'] = continuous_df[f'{price_type}_raw']
                continuous_df[f'{price_type}_fa'] = continuous_df[f'{price_type}_raw']
            return continuous_df
```

Then replace the section beginning at:

```python
# ========== 计算每次换月的价差（同日开盘价法）==========
rollover_gaps = []
```

through the final adjusted-price assignment before:

```python
logger.info("✓ 复权计算完成")
```

with:

```python
        # ========== 计算每次换月的价格比例（同日开盘价法）==========
        rollover_gaps = []
        for rollover in rollovers:
            roll_date = rollover['roll_start_date']
            old_contract = rollover['old_contract']
            new_contract = rollover['new_contract']

            old_data = contracts_df[(contracts_df['trade_date'] == roll_date) &
                                    (contracts_df['symbol'] == old_contract)]
            new_data = contracts_df[(contracts_df['trade_date'] == roll_date) &
                                    (contracts_df['symbol'] == new_contract)]

            if not old_data.empty and not new_data.empty:
                old_price = float(old_data['open'].values[0])
                new_price = float(new_data['open'].values[0])
                if old_price <= 0 or new_price <= 0:
                    logger.warning(
                        f"  换月 {old_contract} → {new_contract} 在 {roll_date} 价格非正，跳过: "
                        f"old={old_price}, new={new_price}"
                    )
                    continue
                ratio = new_price / old_price
                gap = new_price - old_price

                rollover_gaps.append({
                    'roll_date': roll_date,
                    'ratio': ratio,
                    'gap': gap,
                    'old_price': old_price,
                    'new_price': new_price,
                })

                logger.info(
                    f"  换月比例(开盘价): {old_contract}({old_price:.2f}) → "
                    f"{new_contract}({new_price:.2f}), ratio={ratio:.8f}, 价差={gap:.2f}"
                )
            else:
                logger.warning(f"  换月 {old_contract} → {new_contract} 在 {roll_date} 找不到同日价格")
                continue

        if len(rollover_gaps) == 0:
            logger.info("无有效换月比例，使用原始价格")
            continuous_df['ba_factor'] = 1.0
            continuous_df['fa_factor'] = 1.0
            for price_type in ['open', 'high', 'low', 'close']:
                continuous_df[f'{price_type}_ba'] = continuous_df[f'{price_type}_raw']
                continuous_df[f'{price_type}_fa'] = continuous_df[f'{price_type}_raw']
            return continuous_df

        # ========== 前复权 (Back Adjusted) ==========
        # 历史价格乘以后续换月比例，映射到最新合约价格基准
        ba_factor = 1.0
        continuous_df = continuous_df.sort_values('trade_date', ascending=False).copy()
        ba_factors = []
        rollover_gaps_ba = rollover_gaps.copy()

        for idx, row in continuous_df.iterrows():
            date = row['trade_date']

            for rg in rollover_gaps_ba[:]:
                if date < rg['roll_date']:
                    ba_factor *= rg['ratio']
                    rollover_gaps_ba.remove(rg)

            ba_factors.append(ba_factor)

        continuous_df['ba_factor'] = ba_factors
        for price_type in ['open', 'high', 'low', 'close']:
            continuous_df[f'{price_type}_ba'] = (
                continuous_df[f'{price_type}_raw'] * continuous_df['ba_factor']
            )

        # ========== 后复权 (Forward Adjusted) ==========
        # 未来价格乘以换月比例倒数，映射到初始合约价格基准
        continuous_df = continuous_df.sort_values('trade_date', ascending=True).copy()
        fa_factor = 1.0
        fa_factors = []
        rollover_gaps_fa = rollover_gaps.copy()

        for idx, row in continuous_df.iterrows():
            date = row['trade_date']

            for rg in rollover_gaps_fa[:]:
                if date >= rg['roll_date']:
                    fa_factor *= 1.0 / rg['ratio']
                    rollover_gaps_fa.remove(rg)

            fa_factors.append(fa_factor)

        continuous_df['fa_factor'] = fa_factors
        for price_type in ['open', 'high', 'low', 'close']:
            continuous_df[f'{price_type}_fa'] = (
                continuous_df[f'{price_type}_raw'] * continuous_df['fa_factor']
            )
```

- [ ] **Step 2: Run regression script and confirm all ratio tests pass**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected:

```text
PASS test_standard_ratio_adjustment_contango
PASS test_standard_ratio_adjustment_backwardation
PASS test_standard_no_rollover_factors_are_one
PASS test_nanhua_ratio_adjustment_contango
PASS test_nanhua_ratio_adjustment_backwardation
PASS test_nanhua_no_rollover_factors_are_one
```

---

### Task 5: Add Pre-Write Validation

**Files:**
- Modify: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`
- Modify: `pi:/home/pi/market-monitor/backend/continuous_generator.py`
- Modify: `pi:/home/pi/market-monitor/backend/continuous_generator_nh.py`
- Test: `pi:/home/pi/market-monitor/backend/test_ratio_adjustments.py`

- [ ] **Step 1: Append validation tests to the remote regression script**

Append this code before `_run()` in `test_ratio_adjustments.py`:

```python
def _valid_generated_frame() -> pd.DataFrame:
    df = _base_continuous(100.0, 120.0)
    for lineage in ("ba", "fa"):
        for field in ("open", "high", "low", "close"):
            df[f"{field}_{lineage}"] = df[f"{field}_raw"]
    df["ba_factor"] = 1.0
    df["fa_factor"] = 1.0
    df["return_index"] = [1000.0, 1001.0]
    df["daily_return"] = [0.0, 0.001]
    return df


def test_standard_validation_rejects_non_positive_adjusted_price() -> None:
    gen = object.__new__(ContinuousContractGenerator)
    df = _valid_generated_frame()
    df.loc[0, "close_fa"] = 0.0
    try:
        gen.validate_generated_prices("BAD", df)
    except ValueError as exc:
        assert "close_fa" in str(exc), str(exc)
    else:
        raise AssertionError("validate_generated_prices should reject close_fa=0")


def test_nanhua_validation_rejects_non_finite_return_index() -> None:
    gen = object.__new__(ContinuousContractGeneratorNH)
    df = _valid_generated_frame()
    df.loc[1, "return_index"] = float("inf")
    try:
        gen.validate_generated_prices("BAD", df)
    except ValueError as exc:
        assert "return_index" in str(exc), str(exc)
    else:
        raise AssertionError("validate_generated_prices should reject infinite return_index")
```

Update `_run()` to include the two new tests:

```python
def _run() -> None:
    tests = [
        test_standard_ratio_adjustment_contango,
        test_standard_ratio_adjustment_backwardation,
        test_standard_no_rollover_factors_are_one,
        test_nanhua_ratio_adjustment_contango,
        test_nanhua_ratio_adjustment_backwardation,
        test_nanhua_no_rollover_factors_are_one,
        test_standard_validation_rejects_non_positive_adjusted_price,
        test_nanhua_validation_rejects_non_finite_return_index,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
```

- [ ] **Step 2: Run script to verify validation tests fail**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected: ratio tests pass, then FAIL with `AttributeError` because `validate_generated_prices` does not exist.

- [ ] **Step 3: Add validation method to both generator classes**

Add this method immediately before `save_to_database` in both `continuous_generator.py` and `continuous_generator_nh.py`:

```python
    def validate_generated_prices(self, base_symbol: str, continuous_df: pd.DataFrame):
        """Reject generated rows with invalid prices or returns before database writes."""
        required_positive = [
            'open_raw', 'close_raw',
            'open_ba', 'close_ba',
            'open_fa', 'close_fa',
        ]
        missing = [col for col in required_positive if col not in continuous_df.columns]
        if missing:
            raise ValueError(f"{base_symbol} missing generated columns: {missing}")

        problems = []
        for col in required_positive:
            bad = continuous_df[continuous_df[col].isna() | (continuous_df[col] <= 0)]
            if not bad.empty:
                sample = bad[['trade_date', col]].head(5).to_dict('records')
                problems.append(f"{col} has {len(bad)} non-positive/null rows, sample={sample}")

        for col in ['daily_return', 'return_index']:
            if col in continuous_df.columns:
                finite_mask = np.isfinite(continuous_df[col].astype(float))
                if not finite_mask.all():
                    bad = continuous_df.loc[~finite_mask, ['trade_date', col]].head(5).to_dict('records')
                    problems.append(f"{col} has non-finite rows, sample={bad}")

        if problems:
            raise ValueError(f"{base_symbol} generated invalid continuous prices: " + "; ".join(problems))
```

- [ ] **Step 4: Call validation before saving in both `generate` methods**

In both generator files, replace:

```python
            # 7. 保存数据库
            self.save_to_database(base_symbol, dominant_df, continuous_df, rollovers, contracts_df)
```

For `continuous_generator.py`, the comment currently says `# 7. 保存数据库（传入 contracts_df）`. Replace the relevant lines with:

```python
            # 7. 写库前校验
            self.validate_generated_prices(base_symbol, continuous_df)

            # 8. 保存数据库
            self.save_to_database(base_symbol, dominant_df, continuous_df, rollovers, contracts_df)
```

For `continuous_generator_nh.py`, replace with the same code.

- [ ] **Step 5: Run script and confirm all tests pass**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected:

```text
PASS test_standard_ratio_adjustment_contango
PASS test_standard_ratio_adjustment_backwardation
PASS test_standard_no_rollover_factors_are_one
PASS test_nanhua_ratio_adjustment_contango
PASS test_nanhua_ratio_adjustment_backwardation
PASS test_nanhua_no_rollover_factors_are_one
PASS test_standard_validation_rejects_non_positive_adjusted_price
PASS test_nanhua_validation_rejects_non_finite_return_index
```

---

### Task 6: Add Remote Database Quality Scanner

**Files:**
- Create: `pi:/home/pi/market-monitor/backend/scan_adjustment_quality.py`
- Test: run scanner only when pi database accepts connections.

- [ ] **Step 1: Create scanner script**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && cat > scan_adjustment_quality.py <<'"'"'PY'"'"'
from __future__ import annotations

import argparse
import yaml
import psycopg2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule-type", default="standard")
    parser.add_argument("--symbols", default="")
    args = parser.parse_args()

    cfg = yaml.safe_load(open("continuous_config.yaml"))
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    symbol_clause = ""
    params = [args.rule_type]
    if symbols:
        symbol_clause = "AND base_symbol = ANY(%s)"
        params.append(symbols)

    sql = f"""
    WITH per_symbol AS (
      SELECT base_symbol,
             count(*) AS n_rows,
             sum(CASE WHEN open_raw <= 0 OR close_raw <= 0 THEN 1 ELSE 0 END) AS raw_nonpos,
             sum(CASE WHEN open_ba <= 0 OR close_ba <= 0 THEN 1 ELSE 0 END) AS ba_nonpos,
             sum(CASE WHEN open_fa <= 0 OR close_fa <= 0 THEN 1 ELSE 0 END) AS fa_nonpos
      FROM continuous_contract_ohlc
      WHERE rule_type = %s
        {symbol_clause}
      GROUP BY base_symbol
    ), classified AS (
      SELECT *,
             CASE
               WHEN fa_nonpos = 0 AND ba_nonpos = 0 THEN 'ok'
               WHEN ba_nonpos = 0 THEN 'fa_corrupt'
               WHEN fa_nonpos = 0 THEN 'ba_corrupt'
               ELSE 'both_corrupt'
             END AS status,
             CASE
               WHEN fa_nonpos = 0 THEN 'fa'
               WHEN ba_nonpos = 0 THEN 'ba'
               ELSE 'raw'
             END AS recommended_adj
      FROM per_symbol
    )
    SELECT base_symbol, n_rows, raw_nonpos, ba_nonpos, fa_nonpos, status, recommended_adj
    FROM classified
    ORDER BY status, base_symbol
    """

    with psycopg2.connect(cfg["database"]["url"]) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    affected = [row for row in rows if row[5] != "ok"]
    print(f"rule_type={args.rule_type} symbols_scanned={len(rows)} affected={len(affected)}")
    for row in rows:
        print("\t".join(str(x) for x in row))
    if affected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
PY'
```

- [ ] **Step 2: Run only the database health check**

Run:

```bash
ssh pi 'pg_isready -h 100.75.102.44 -p 5432 -d market_monitor -U admin'
```

Expected: if output is not `accepting connections`, stop before running scanner or regeneration.

- [ ] **Step 3: Run scanner when database accepts connections**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type standard --symbols I,CJ,RU'
```

Expected before regeneration: may fail with affected rows. This establishes the pre-fix database symptom on pi's target database.

---

### Task 7: Regenerate Affected Symbols on Pi

**Files:**
- Execute: `pi:/home/pi/market-monitor/backend/continuous_daily_runner.py`
- Verify: `pi:/home/pi/market-monitor/backend/scan_adjustment_quality.py`

- [ ] **Step 1: Confirm pi database accepts connections**

Run:

```bash
ssh pi 'pg_isready -h 100.75.102.44 -p 5432 -d market_monitor -U admin'
```

Expected: `100.75.102.44:5432 - accepting connections`.

- [ ] **Step 2: Regenerate known affected symbols**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python continuous_daily_runner.py --symbol I --rule all'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python continuous_daily_runner.py --symbol CJ --rule all'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python continuous_daily_runner.py --symbol RU --rule all'
```

Expected: each command logs `standard: 成功 1, 失败 0` and `nanhua: 成功 1, 失败 0`.

- [ ] **Step 3: Scan regenerated symbols**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type standard --symbols I,CJ,RU'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type nanhua --symbols I,CJ,RU'
```

Expected: both commands exit 0 and print `affected=0`.

- [ ] **Step 4: Regenerate all symbols**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python continuous_daily_runner.py --all --rule all'
```

Expected: final summary logs `standard: 成功 79, 失败 0`, `nanhua: 成功 79, 失败 0`, and total success 158.

- [ ] **Step 5: Scan all regenerated symbols**

Run:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type standard'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type nanhua'
```

Expected: both commands exit 0 and print `affected=0`.

---

### Task 8: Update Local Documentation and Audit Messages

**Files:**
- Modify: `cta/data_quality.py`
- Modify: `docs/operations/cta-strategy-replication.md`

- [x] **Step 1: Update stale upstream path in `cta/data_quality.py`**

Replace both references to `/home/elfbob/claude-code/continuous/` with:

```text
pi:/home/pi/market-monitor/backend/
```

Also adjust the final CLI message to:

```python
    print(
        "\nupstream fix lives on pi at /home/pi/market-monitor/backend/ "
        "(continuous_generator.py = standard, continuous_generator_nh.py = nanhua); "
        "the old additive forward/back-adjust step emitted non-positive prices."
    )
```

- [x] **Step 2: Update CTA runbook**

Append this note to `docs/operations/cta-strategy-replication.md` under `价格/量价 guarded smoke`:

```markdown
上游 continuous 修复后，`ba_factor` 与 `fa_factor` 表示乘法复权因子，不再表示绝对价差偏移。
重算前必须确认 pi 上 `continuous_config.yaml` 指向的数据库是 CTA 要读取的目标库。
```

- [x] **Step 3: Run local focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py tests/test_cta_pg_source.py tests/test_cta_strategy.py -q
```

Expected: all tests pass.

- [x] **Step 4: Commit local documentation update**

Run:

```bash
git add cta/data_quality.py docs/operations/cta-strategy-replication.md
git commit -m "docs(cta): document continuous ratio adjustment fix"
```

---

## Final Verification

- [x] Run remote synthetic regression:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python test_ratio_adjustments.py'
```

Expected: all eight synthetic tests pass.

- [x] Run remote quality scans after pi database regeneration:

```bash
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type standard'
ssh pi 'cd /home/pi/market-monitor/backend && venv/bin/python scan_adjustment_quality.py --rule-type nanhua'
```

Expected: both print `affected=0`.

- [x] Run local focused CTA tests:

```bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py tests/test_cta_pg_source.py tests/test_cta_strategy.py -q
```

Expected: all tests pass.

- [x] Run local downstream scan against the configured CTA database:

```bash
.venv/bin/python -m cta.data_quality
```

Expected: if local `stock_selector` still points at `100.65.111.79`, this may continue to report old affected symbols until that database is regenerated or repointed. Do not treat local scan as proof of pi regeneration unless the endpoint is confirmed.

- [x] Run CTA price/volume smoke against the intended regenerated database:

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

Expected: exits 0 and writes `/tmp/cta_price_volume_guarded_smoke.xlsx` with a `data_quality` sheet.
