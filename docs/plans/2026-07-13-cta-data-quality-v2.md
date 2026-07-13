# CTA Data Quality V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only continuous-contract health audit and an opt-in strict scheduler gate without changing historical CTA lineage selection or requiring WSD/data backfill.

**Architecture:** Keep the legacy open/close adjustment classifier unchanged for PG reader compatibility. Add separate pure pandas helpers for full-OHLC health, date diagnostics, and strict-policy evaluation; the CLI merges those results, reports diagnostics by default, and exits 1 only under the approved strict conditions.

**Tech Stack:** Python 3.13, pandas, numpy, psycopg2, argparse, pytest, PostgreSQL public schema

---

## File Map

- Modify: cta_gtja/data_quality.py
  - Preserve legacy adjustment classification.
  - Add full-OHLC/date health summary, combined report, strict policy, formatting, SQL fields, and CLI flags.
- Modify: tests/test_cta_data_quality.py
  - Add deterministic synthetic tests for Decimal coercion, incomplete/infinite/non-positive OHLC, impossible derived values, gaps, freshness, strict policy, and CLI exits.
- Modify: README.md
  - Correct the current 2026-04-29 data boundary.
- Modify: docs/operations/cta-strategy-replication.md
  - Document default/strict scans and cross-lineage interpretation.
- Modify: docs/plans/2026-05-31-cta-strategy-replication-plan.md
  - Add a dated status note without rewriting historical checkboxes.
- Modify outside this Git repository: /home/elfbob/claude-code/CLAUDE.md
  - Correct the Futures Strategies data boundary; verify separately because the parent directory is not a Git repository.

### Task 1: Pure Full-OHLC and Date Health Summary

**Files:**
- Modify: tests/test_cta_data_quality.py
- Modify: cta_gtja/data_quality.py:12-59

- [ ] **Step 1: Add failing full-OHLC health tests**

Extend the imports and add a synthetic row helper:

~~~python
from datetime import date
from decimal import Decimal

import pandas as pd

from cta_gtja.data_quality import (
    build_adjustment_audit,
    summarize_adjustment_quality,
    summarize_continuous_contract_quality,
    summarize_continuous_health,
)


def _health_row(
    symbol: str,
    trade_date: str,
    *,
    raw=10,
    ba=11,
    fa=12,
    daily_return=0.01,
    return_index=100,
):
    row = {
        "base_symbol": symbol,
        "trade_date": trade_date,
        "daily_return": daily_return,
        "return_index": return_index,
    }
    for lineage, value in (("raw", raw), ("ba", ba), ("fa", fa)):
        for field in ("open", "high", "low", "close"):
            row[f"{field}_{lineage}"] = Decimal(str(value))
    return row
~~~

Add these three tests after the existing adjustment tests:

~~~python
def test_full_ohlc_health_separates_incomplete_infinite_and_suspicious():
    zero_high = _health_row(
        "BR", "2026-01-07", daily_return=Decimal("-1.012815"),
        return_index=Decimal("-13.9470"),
    )
    zero_high["high_raw"] = Decimal("0")

    incomplete_and_infinite = _health_row(
        "BR", "2026-01-08", daily_return=None, return_index=Decimal("100"),
    )
    incomplete_and_infinite["low_raw"] = None
    incomplete_and_infinite["high_ba"] = Decimal("Infinity")

    health = summarize_continuous_health(
        pd.DataFrame([zero_high, incomplete_and_infinite])
    ).set_index("base_symbol")

    assert health.loc["BR", "raw_ohlc_nonpos"] == 1
    assert health.loc["BR", "raw_ohlc_incomplete"] == 1
    assert health.loc["BR", "raw_ohlc_infinite"] == 0
    assert health.loc["BR", "ba_ohlc_infinite"] == 1
    assert health.loc["BR", "suspicious_bar_count"] == 2
    assert health.loc["BR", "daily_return_invalid"] == 1
    assert health.loc["BR", "return_index_invalid"] == 1


def test_continuous_health_reports_lifetime_gaps_and_symbol_lag():
    rows = [
        _health_row("A", "2026-01-02"),
        _health_row("A", "2026-01-04"),
        _health_row("B", "2026-01-01"),
        _health_row("B", "2026-01-02"),
        _health_row("B", "2026-01-03"),
    ]

    health = summarize_continuous_health(pd.DataFrame(rows)).set_index("base_symbol")

    assert health.loc["A", "first_trade_date"] == date(2026, 1, 2)
    assert health.loc["A", "last_trade_date"] == date(2026, 1, 4)
    assert health.loc["A", "missing_trade_dates"] == 1
    assert health.loc["A", "lag_to_rule_max_days"] == 0
    assert health.loc["B", "missing_trade_dates"] == 0
    assert health.loc["B", "lag_to_rule_max_days"] == 1


def test_combined_quality_keeps_legacy_status_when_only_low_is_zero():
    row = _health_row("BR", "2026-01-07")
    row["low_raw"] = Decimal("0")

    report = summarize_continuous_contract_quality(
        pd.DataFrame([row])
    ).set_index("base_symbol")

    assert report.loc["BR", "status"] == "ok"
    assert report.loc["BR", "raw_nonpos"] == 0
    assert report.loc["BR", "raw_ohlc_nonpos"] == 1
    assert report.loc["BR", "suspicious_bar_count"] == 1
~~~

- [ ] **Step 2: Run the new tests and verify the red state**

Run:

~~~bash
.venv/bin/python -m pytest   tests/test_cta_data_quality.py::test_full_ohlc_health_separates_incomplete_infinite_and_suspicious   tests/test_cta_data_quality.py::test_continuous_health_reports_lifetime_gaps_and_symbol_lag   tests/test_cta_data_quality.py::test_combined_quality_keeps_legacy_status_when_only_low_is_zero -q
~~~

Expected: collection fails because summarize_continuous_health and summarize_continuous_contract_quality do not exist.

- [ ] **Step 3: Implement the pure health and merge helpers**

Add imports/constants after the future import in cta_gtja/data_quality.py:

~~~python
from datetime import date

import numpy as np
import pandas as pd

_LINEAGES = ("raw", "ba", "fa")
_OHLC_FIELDS = ("open", "high", "low", "close")
_HEALTH_COLUMNS = [
    "base_symbol",
    "raw_ohlc_nonpos",
    "raw_ohlc_incomplete",
    "raw_ohlc_infinite",
    "ba_ohlc_nonpos",
    "ba_ohlc_incomplete",
    "ba_ohlc_infinite",
    "fa_ohlc_nonpos",
    "fa_ohlc_incomplete",
    "fa_ohlc_infinite",
    "suspicious_bar_count",
    "daily_return_invalid",
    "return_index_invalid",
    "first_trade_date",
    "last_trade_date",
    "lag_to_rule_max_days",
    "missing_trade_dates",
]
~~~

Keep summarize_adjustment_quality unchanged. Add these functions immediately after it:

~~~python
def summarize_continuous_health(df: pd.DataFrame) -> pd.DataFrame:
    """Summarize full-OHLC and date health without changing lineage selection."""
    if df.empty:
        return pd.DataFrame(columns=_HEALTH_COLUMNS)

    work = df.copy()
    work["trade_date"] = pd.to_datetime(
        work["trade_date"], errors="coerce"
    ).dt.normalize()
    rule_calendar = pd.DatetimeIndex(
        work["trade_date"].dropna().unique()
    ).sort_values()
    rule_max = rule_calendar.max() if len(rule_calendar) else pd.NaT

    records = []
    for symbol, group in work.groupby("base_symbol", sort=True):
        group = group.copy()
        suspicious = np.zeros(len(group), dtype=bool)
        record: dict[str, object] = {"base_symbol": symbol}

        for lineage in _LINEAGES:
            columns = [f"{field}_{lineage}" for field in _OHLC_FIELDS]
            numeric = group[columns].apply(
                pd.to_numeric, errors="coerce"
            ).astype(float)
            values = numeric.to_numpy(dtype=float)

            incomplete = np.isnan(values).any(axis=1)
            infinite = np.isinf(values).any(axis=1)
            nonpos = ((values <= 0) & np.isfinite(values)).any(axis=1)

            record[f"{lineage}_ohlc_nonpos"] = int(nonpos.sum())
            record[f"{lineage}_ohlc_incomplete"] = int(incomplete.sum())
            record[f"{lineage}_ohlc_infinite"] = int(infinite.sum())
            suspicious |= nonpos | infinite

        daily_return = pd.to_numeric(
            group["daily_return"], errors="coerce"
        ).astype(float).to_numpy()
        return_index = pd.to_numeric(
            group["return_index"], errors="coerce"
        ).astype(float).to_numpy()

        daily_invalid = np.isinf(daily_return) | (
            np.isfinite(daily_return) & (daily_return <= -1)
        )
        index_invalid = np.isinf(return_index) | (
            np.isfinite(return_index) & (return_index <= 0)
        )

        symbol_dates = pd.DatetimeIndex(
            group["trade_date"].dropna().unique()
        ).sort_values()
        first = symbol_dates.min() if len(symbol_dates) else pd.NaT
        last = symbol_dates.max() if len(symbol_dates) else pd.NaT

        if pd.isna(first) or pd.isna(last) or pd.isna(rule_max):
            lag_days = None
            missing_dates = 0
        else:
            active_calendar = rule_calendar[
                (rule_calendar >= first) & (rule_calendar <= last)
            ]
            missing_dates = len(active_calendar.difference(symbol_dates))
            lag_days = int((rule_max - last).days)

        record.update(
            {
                "suspicious_bar_count": int(
                    group.loc[suspicious, "trade_date"].nunique()
                ),
                "daily_return_invalid": int(daily_invalid.sum()),
                "return_index_invalid": int(index_invalid.sum()),
                "first_trade_date": first.date() if not pd.isna(first) else None,
                "last_trade_date": last.date() if not pd.isna(last) else None,
                "lag_to_rule_max_days": lag_days,
                "missing_trade_dates": int(missing_dates),
            }
        )
        records.append(record)

    return pd.DataFrame.from_records(records, columns=_HEALTH_COLUMNS)


def summarize_continuous_contract_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Combine the compatible adjustment report with V2 health diagnostics."""
    adjustment = summarize_adjustment_quality(df)
    health = summarize_continuous_health(df)
    return adjustment.merge(
        health,
        on="base_symbol",
        how="outer",
        validate="one_to_one",
    )
~~~

- [ ] **Step 4: Run the data-quality tests and verify green**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -q
~~~

Expected: 7 passed.

- [ ] **Step 5: Verify PG-reader compatibility**

Run:

~~~bash
.venv/bin/python -m pytest   tests/test_cta_data_quality.py tests/test_cta_pg_source.py -q
~~~

Expected: 9 passed; the legacy open/close status and raw-fallback behavior remain unchanged.

- [ ] **Step 6: Commit Task 1**

~~~bash
git add cta_gtja/data_quality.py tests/test_cta_data_quality.py
git commit -m "feat: add CTA continuous health diagnostics"
~~~

### Task 2: Strict Policy as a Pure Function

**Files:**
- Modify: tests/test_cta_data_quality.py
- Modify: cta_gtja/data_quality.py

- [ ] **Step 1: Add the strict-policy test fixture and failing tests**

Replace the Task 1 data_quality import block with:

~~~python
from cta_gtja.data_quality import (
    build_adjustment_audit,
    strict_failure_reasons,
    summarize_adjustment_quality,
    summarize_continuous_contract_quality,
    summarize_continuous_health,
)
~~~

Then add:

~~~python
def _strict_report(**overrides) -> pd.DataFrame:
    row = {
        "base_symbol": "BR",
        "n_rows": 1,
        "raw_nonpos": 0,
        "ba_nonpos": 0,
        "fa_nonpos": 0,
        "recommended_adj": "fa",
        "status": "ok",
        "raw_ohlc_nonpos": 0,
        "raw_ohlc_incomplete": 0,
        "raw_ohlc_infinite": 0,
        "ba_ohlc_nonpos": 0,
        "ba_ohlc_incomplete": 0,
        "ba_ohlc_infinite": 0,
        "fa_ohlc_nonpos": 0,
        "fa_ohlc_incomplete": 0,
        "fa_ohlc_infinite": 0,
        "suspicious_bar_count": 0,
        "daily_return_invalid": 0,
        "return_index_invalid": 0,
        "last_trade_date": date(2026, 7, 10),
        "lag_to_rule_max_days": 0,
        "missing_trade_dates": 0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_strict_policy_accepts_healthy_report_at_ten_day_boundary():
    reasons = strict_failure_reasons(
        _strict_report(),
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == []


def test_strict_policy_ignores_incomplete_gaps_and_symbol_lag():
    report = _strict_report(
        raw_ohlc_incomplete=783,
        ba_ohlc_incomplete=783,
        fa_ohlc_incomplete=783,
        lag_to_rule_max_days=9724,
        missing_trade_dates=100,
    )

    reasons = strict_failure_reasons(
        report,
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == []


def test_strict_policy_rejects_corruption_impossible_values_and_staleness():
    report = _strict_report(
        status="fa_corrupt",
        raw_ohlc_nonpos=1,
        fa_ohlc_infinite=1,
        suspicious_bar_count=1,
        daily_return_invalid=1,
        return_index_invalid=1,
    )

    reasons = strict_failure_reasons(
        report,
        as_of=date(2026, 7, 22),
        max_lag_days=10,
    )

    assert reasons == [
        "adjustment corruption: BR",
        "raw OHLC invalid: BR",
        "fa OHLC invalid: BR",
        "daily_return invalid (<= -1 or infinite): BR",
        "return_index invalid (<= 0 or infinite): BR",
        "table stale: last_trade_date=2026-07-10 lag_days=12 max_lag_days=10",
    ]


def test_strict_policy_rejects_empty_report():
    reasons = strict_failure_reasons(
        pd.DataFrame(),
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == ["no symbols returned"]
~~~

- [ ] **Step 2: Run strict-policy tests and verify the red state**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -k strict_policy -q
~~~

Expected: collection fails because strict_failure_reasons does not exist.

- [ ] **Step 3: Implement deterministic strict evaluation**

Add the default and helpers after summarize_continuous_contract_quality:

~~~python
DEFAULT_MAX_LAG_DAYS = 10


def _symbols_matching(report: pd.DataFrame, mask: pd.Series) -> str:
    symbols = report.loc[mask, "base_symbol"].astype(str).unique()
    return ",".join(sorted(symbols))


def _positive_count(report: pd.DataFrame, column: str) -> pd.Series:
    values = report.get(column, pd.Series(0, index=report.index))
    return pd.to_numeric(values, errors="coerce").fillna(0) > 0


def strict_failure_reasons(
    report: pd.DataFrame,
    *,
    as_of: date,
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
) -> list[str]:
    """Return strict failures; diagnostic-only fields never enter this policy."""
    if max_lag_days < 0:
        raise ValueError("max_lag_days must be non-negative")
    if report.empty:
        return ["no symbols returned"]

    reasons: list[str] = []

    corrupt = report["status"].fillna("unknown") != "ok"
    if corrupt.any():
        reasons.append(
            f"adjustment corruption: {_symbols_matching(report, corrupt)}"
        )

    for lineage in _LINEAGES:
        invalid = _positive_count(
            report, f"{lineage}_ohlc_nonpos"
        ) | _positive_count(report, f"{lineage}_ohlc_infinite")
        if invalid.any():
            reasons.append(
                f"{lineage} OHLC invalid: {_symbols_matching(report, invalid)}"
            )

    daily_invalid = _positive_count(report, "daily_return_invalid")
    if daily_invalid.any():
        reasons.append(
            "daily_return invalid (<= -1 or infinite): "
            + _symbols_matching(report, daily_invalid)
        )

    index_invalid = _positive_count(report, "return_index_invalid")
    if index_invalid.any():
        reasons.append(
            "return_index invalid (<= 0 or infinite): "
            + _symbols_matching(report, index_invalid)
        )

    last_trade_date = pd.to_datetime(
        report["last_trade_date"], errors="coerce"
    ).max()
    if pd.isna(last_trade_date):
        reasons.append("table freshness unavailable: no valid last_trade_date")
    else:
        lag_days = int(
            (pd.Timestamp(as_of).normalize() - last_trade_date.normalize()).days
        )
        if lag_days > max_lag_days:
            reasons.append(
                "table stale: "
                f"last_trade_date={last_trade_date.date().isoformat()} "
                f"lag_days={lag_days} max_lag_days={max_lag_days}"
            )

    return reasons
~~~

- [ ] **Step 4: Run strict-policy and full data-quality tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -q
~~~

Expected: 11 passed.

- [ ] **Step 5: Commit Task 2**

~~~bash
git add cta_gtja/data_quality.py tests/test_cta_data_quality.py
git commit -m "feat: add strict CTA data quality policy"
~~~

### Task 3: Wire the Read-Only PG Scan and CLI

**Files:**
- Modify: tests/test_cta_data_quality.py
- Modify: cta_gtja/data_quality.py:111-169

- [ ] **Step 1: Add failing formatting and CLI behavior tests**

Add imports for sys, pytest, and the module:

~~~python
import sys

import pytest

import cta_gtja.data_quality as data_quality
from cta_gtja.data_quality import format_health_summary
~~~

Add:

~~~python
def test_main_default_reports_suspicious_bars_without_exiting(
    monkeypatch, capsys
):
    report = _strict_report(
        raw_ohlc_nonpos=1,
        raw_ohlc_incomplete=5,
        suspicious_bar_count=1,
        missing_trade_dates=2,
    )
    monkeypatch.setattr(
        data_quality,
        "scan_continuous_contract_quality",
        lambda **kwargs: report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_quality", "--rule-type", "standard"],
    )

    data_quality.main()

    output = capsys.readouterr().out
    assert "suspicious source bars" in output
    assert "any lineage flags the whole trade date" in output
    assert "incomplete OHLC lineage-rows: 5" in output


def test_main_strict_prints_reasons_and_exits_one(monkeypatch, capsys):
    report = _strict_report(
        raw_ohlc_nonpos=1,
        suspicious_bar_count=1,
        last_trade_date=date.today(),
    )
    monkeypatch.setattr(
        data_quality,
        "scan_continuous_contract_quality",
        lambda **kwargs: report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_quality", "--rule-type", "standard", "--strict"],
    )

    with pytest.raises(SystemExit) as exc:
        data_quality.main()

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "strict failures:" in output
    assert "raw OHLC invalid: BR" in output
~~~

- [ ] **Step 2: Run the CLI tests and verify the red state**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -k "main_" -q
~~~

Expected: tests fail because V2 formatting, SQL fields, and CLI flags are not wired.

- [ ] **Step 3: Add a pure health-summary formatter**

Add before main:

~~~python
def format_health_summary(
    report: pd.DataFrame,
    *,
    as_of: date,
) -> list[str]:
    if report.empty:
        return ["  health: no symbols"]

    last_trade_date = pd.to_datetime(
        report["last_trade_date"], errors="coerce"
    ).max()
    if pd.isna(last_trade_date):
        rule_max = "unknown"
        table_lag = "unknown"
    else:
        rule_max = last_trade_date.date().isoformat()
        table_lag = str(
            int(
                (
                    pd.Timestamp(as_of).normalize()
                    - last_trade_date.normalize()
                ).days
            )
        )

    suspicious = int(
        pd.to_numeric(
            report["suspicious_bar_count"], errors="coerce"
        ).fillna(0).sum()
    )
    incomplete = sum(
        int(
            pd.to_numeric(
                report[f"{lineage}_ohlc_incomplete"], errors="coerce"
            ).fillna(0).sum()
        )
        for lineage in _LINEAGES
    )
    lagging = int(
        (
            pd.to_numeric(
                report["lag_to_rule_max_days"], errors="coerce"
            ).fillna(0)
            > 0
        ).sum()
    )
    missing = int(
        pd.to_numeric(
            report["missing_trade_dates"], errors="coerce"
        ).fillna(0).sum()
    )

    return [
        f"  rule max date: {rule_max}   table lag days: {table_lag}",
        f"  suspicious source bars: {suspicious}",
        f"  incomplete OHLC lineage-rows: {incomplete}",
        f"  lagging symbols: {lagging}   inferred missing dates: {missing}",
    ]
~~~

- [ ] **Step 4: Expand the read-only SQL and return the combined report**

Replace the SQL and return statement in scan_continuous_contract_quality:

~~~python
    sql = """
        SELECT
            base_symbol,
            trade_date,
            open_raw, high_raw, low_raw, close_raw,
            open_ba, high_ba, low_ba, close_ba,
            open_fa, high_fa, low_fa, close_fa,
            daily_return,
            return_index
        FROM public.continuous_contract_ohlc
        WHERE rule_type = %(rule_type)s
        ORDER BY base_symbol, trade_date
    """
~~~

After the existing read_sql_query call, return:

~~~python
    return summarize_continuous_contract_quality(df)
~~~

Update its docstring to state that it returns the compatible adjustment report merged with V2 diagnostics.

- [ ] **Step 5: Add CLI flags, reporting, and strict exit**

Add arguments:

~~~python
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on actionable corruption or table staleness",
    )
    parser.add_argument(
        "--max-lag-days",
        type=int,
        default=DEFAULT_MAX_LAG_DAYS,
        help="maximum table lag in calendar days for --strict (default: 10)",
    )
~~~

Immediately after parse_args, reject negative thresholds:

~~~python
    if args.max_lag_days < 0:
        parser.error("--max-lag-days must be non-negative")
~~~

Use one as-of value for output and policy:

~~~python
    as_of = date.today()
~~~

After the existing status breakdown, print:

~~~python
    for line in format_health_summary(report, as_of=as_of):
        print(line)

    suspicious = report[
        pd.to_numeric(
            report["suspicious_bar_count"], errors="coerce"
        ).fillna(0) > 0
    ]
    if not suspicious.empty:
        columns = [
            "base_symbol",
            "suspicious_bar_count",
            "raw_ohlc_nonpos",
            "ba_ohlc_nonpos",
            "fa_ohlc_nonpos",
            "daily_return_invalid",
            "return_index_invalid",
        ]
        print(
            "\nsuspicious source bars "
            "(any lineage flags the whole trade date):"
        )
        print(suspicious[columns].to_string(index=False))
~~~

Keep CSV writing before the strict exit so an explicitly requested artifact is retained. Append:

~~~python
    if args.strict:
        reasons = strict_failure_reasons(
            report,
            as_of=as_of,
            max_lag_days=args.max_lag_days,
        )
        if reasons:
            print("\nstrict failures:")
            for reason in reasons:
                print(f"  - {reason}")
            raise SystemExit(1)
~~~

- [ ] **Step 6: Run CLI-focused and complete unit tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cta_data_quality.py -q
~~~

Expected: 13 passed.

Run:

~~~bash
.venv/bin/python -m pytest -q
~~~

Expected: 26 passed.

- [ ] **Step 7: Commit Task 3**

~~~bash
git add cta_gtja/data_quality.py tests/test_cta_data_quality.py
git commit -m "feat: expose strict CTA quality scan"
~~~

### Task 4: Correct Operational Documentation

**Files:**
- Modify: README.md:19-21
- Modify: docs/operations/cta-strategy-replication.md:73-97
- Modify: docs/plans/2026-05-31-cta-strategy-replication-plan.md:1-8
- Modify outside repository: /home/elfbob/claude-code/CLAUDE.md:514-517

- [ ] **Step 1: Correct README data status**

Replace the stale warning with:

~~~markdown
⚠️ 截至 2026-07-13 的已知停摆（见 `market-monitor/DATABASE_INVENTORY.md`）：`futures_daily`
与 `continuous_contract_ohlc` 的 `standard`/`nanhua` 两套规则均冻结在 2026-04-29，连续合约已追平
分合约行情，但整个 EOD 日更链仍停摆。做 Carry 历史回测前需先恢复上游日更。
~~~

Preserve the existing Markdown code formatting around table names when editing the file.

- [ ] **Step 2: Add the quality-scan runbook section**

Append after the guarded-smoke audit paragraph:

~~~~markdown
## 连续合约数据质量扫描

普通扫描只读数据库、完整报告问题并返回 0：

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua
~~~

调度器或 CI 使用 `--strict`；默认允许表级最新日期落后 10 个自然日，长假或特殊场景可显式覆盖：

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard --strict
.venv/bin/python -m cta_gtja.data_quality   --rule-type nanhua --strict --max-lag-days 12
~~~

`--strict` 仅阻断全 OHLC 非正/无限值、不可能的 `daily_return`/`return_index`、调整线损坏、
空结果和表级过期。NULL/NaN OHLC、单品种落后以及推断缺失交易日仅诊断，不单独阻断，
因为当前没有权威品种生命周期和交易日历。

`raw`/`ba`/`fa` 都派生自同一根源 bar；任一 lineage 出现非正或无限 OHLC 时，该交易日整根 bar
均视为可疑，即使策略选用的 `fa`/`ba` 价格仍为正。修复仍应在上游分合约数据或连续合约生成器完成。
~~~~

- [ ] **Step 3: Add a dated status note to the historical plan**

Insert after the migration note:

~~~markdown
> **状态更新（2026-07-13）**：乘法复权改造和 2026-03-06 至 2026-04-29 的连续合约回填已经完成，
> `standard`/`nanhua` 均追平 `futures_daily` 至 2026-04-29；整个 EOD 链仍停在该日。量价 guarded
> 路径可运行，当前推进 Data Quality V2；基本面标准化与 WSD 回填不在本阶段范围。
~~~

Do not change the historical task checkboxes.

- [ ] **Step 4: Correct the parent CLAUDE.md status**

Replace the two stale data-status lines in /home/elfbob/claude-code/CLAUDE.md with:

~~~markdown
  已归档 `_archive/2026H1_root_scripts/continuous/`）。⚠️ 2026-07-13 时点：`futures_daily` 与
  `continuous_contract_ohlc` 的 `standard`/`nanhua` 两套规则均冻在 04-29（连续合约已追平分合约行情，
  但 EOD 日更链仍停摆；见 market-monitor `DATABASE_INVENTORY.md`）。
~~~

Preserve the existing inline-code formatting when editing. This file is outside every Git repository, so do not attempt to include it in the futures_strategies commit.

- [ ] **Step 5: Verify documentation facts and formatting**

Run:

~~~bash
rg -n "2026-04-29|strict|10 个自然日|整根 bar|Data Quality V2"   README.md docs/operations/cta-strategy-replication.md   docs/plans/2026-05-31-cta-strategy-replication-plan.md
~~~

Expected: each project document contains the new status or policy.

Run:

~~~bash
sed -n '512,520p' ../CLAUDE.md
~~~

Expected: both futures_daily and continuous_contract_ohlc show 04-29.

Run:

~~~bash
git diff --check
~~~

Expected: exit 0.

- [ ] **Step 6: Commit repository documentation**

~~~bash
git add README.md   docs/operations/cta-strategy-replication.md   docs/plans/2026-05-31-cta-strategy-replication-plan.md
git commit -m "docs: document CTA quality scan operations"
~~~

### Task 5: Final Verification Against Unit and Live Data

**Files:**
- Verify only; do not modify database or generate backfill data.

- [ ] **Step 1: Run the complete local suite**

~~~bash
.venv/bin/python -m pytest -q
~~~

Expected: 26 passed and 0 failed.

- [ ] **Step 2: Run default scans**

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard
~~~

Expected: exit 0; 79 symbols; BR appears under suspicious source bars because raw low is zero; historical incomplete rows and symbol lag are reported diagnostically.

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua
~~~

Expected: exit 0; 79 symbols; BR remains adjustment-corrupt and reports daily_return <= -1 plus return_index <= 0.

- [ ] **Step 3: Run strict scans and verify expected red reasons**

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard --strict
~~~

Expected: exit 1 for table staleness and the BR raw full-OHLC defect. Incomplete bars and per-symbol lag must not appear under strict failures.

~~~bash
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua --strict
~~~

Expected: exit 1 for table staleness, BR adjustment/full-OHLC defects, daily_return <= -1, and return_index <= 0. Incomplete bars and per-symbol lag must not appear under strict failures.

- [ ] **Step 4: Verify the configurable freshness boundary without changing data**

Run with a large temporary threshold:

~~~bash
.venv/bin/python -m cta_gtja.data_quality   --rule-type standard --strict --max-lag-days 100
~~~

Expected: still exit 1 because BR is suspicious; the table-stale reason is absent. This isolates the threshold policy from data corruption.

- [ ] **Step 5: Verify repository state and commit history**

~~~bash
git status --short --branch
git log --oneline -6
~~~

Expected: no uncommitted files in futures_strategies; Task 1-4 commits are present. The parent CLAUDE.md change is outside this Git repository and is verified by Task 4 rather than git status.
