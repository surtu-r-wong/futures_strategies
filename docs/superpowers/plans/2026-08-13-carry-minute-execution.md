# Carry Minute Execution Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minute execution mode to `cta_carry` that preserves daily Carry signals and risk formulas while executing daily targets at the next tradable five-minute VWAP and applying three-tranche chandelier stops on 15-minute bars.

**Architecture:** Extract the execution-independent daily research and target-planning seam from the existing daily engine, then add focused session, aggregation, PostgreSQL, event-accounting, and minute-backtest modules. Keep `--execution daily` byte-for-byte compatible at the result-table level; minute mode streams one target-trade-date month at a time from `public.futures_minute`, uses an explicit versioned session calendar, and produces auditable execution/stop/data-quality tables.

**Tech Stack:** Python 3.11+, pandas, NumPy, psycopg2, PostgreSQL 15, TimescaleDB 2.23, pytest, openpyxl, matplotlib.

---

## Scope and file map

This is one integrated subsystem: every new module serves the same minute event loop and no module is independently user-facing.

**Create**

- `cta_carry/decision.py` — daily curve/ATR/signal build plus execution-independent signal transition and raw-target planning.
- `cta_carry/minute_sessions.py` — versioned session-rule loading, trade-date mapping, trading-minute slots, five-minute windows, and 15-minute bucket boundaries.
- `cta_carry/minute_bars.py` — minute-row validation, zero-volume-safe OHLC aggregation, multiplier resolution, and VWAP.
- `cta_carry/minute_pg_source.py` — bounded monthly TimescaleDB access, candidate temp table, safe plan inspection, and contract metadata reads.
- `cta_carry/minute_account.py` — order/fill records and deterministic piecewise formal/shadow account ledgers.
- `cta_carry/minute_backtest.py` — monthly orchestration, intraday stop state machine, warmup, outputs, and provenance counters.
- `config/carry_minute_sessions.csv` — generated and reviewed product/effective-date session rules.
- `scripts/carry/capture_minute_sessions.py` — deterministic rule capture and verification command.
- `scripts/carry/compare_execution.py` — paired-workbook comparison table used by the final research run.
- `tests/fixtures/carry_daily_stateful_baseline.pkl` — pre-refactor deterministic result tables used to prove daily compatibility.
- `tests/test_carry_decision.py`
- `tests/test_carry_minute_sessions.py`
- `tests/test_carry_minute_bars.py`
- `tests/test_carry_minute_pg_source.py`
- `tests/test_carry_minute_account.py`
- `tests/test_carry_minute_backtest.py`
- `tests/test_carry_compare_execution.py`
- `docs/research/2026-08-13-carry-minute-execution-report.md` — generated only after both paired runs finish.

**Modify**

- `cta_carry/backtest.py` — delegate daily research/target planning to the shared seam; retain daily accounting and daily stop policy.
- `cta_carry/__main__.py` — add `--execution daily|minute`, reject minute+files, construct the minute source, and catch structured minute failures.
- `cta_carry/report.py` — conditionally publish the three minute audit sheets and validate the correct sheet list.
- `cta_carry/__init__.py` — export the minute backtester and structured minute error.
- `tests/test_carry_backtest.py` — lock daily result-table regression after extraction.
- `tests/test_carry_report_cli.py` — CLI mode selection, report sheets, provenance, and error exits.
- `README.md`
- `docs/operations/carry-daily-research.md`

## Required execution discipline

- Work in an isolated worktree when implementation begins.
- Invoke `test-driven-development` before Task 1 and keep every task red → green.
- Run one database integration or full-history process at a time.
- Do not run an unbounded aggregate or symbol-expression join against `futures_minute`.
- Before any completion claim, invoke `verification-before-completion` and run the commands in Task 12.

### Task 1: Extract the shared daily decision seam without changing daily output

**Files:**
- Create: `cta_carry/decision.py`
- Create: `tests/test_carry_decision.py`
- Create: `tests/fixtures/carry_daily_stateful_baseline.pkl`
- Modify: `cta_carry/backtest.py:439-604`
- Modify: `tests/test_carry_backtest.py:413-555`

- [ ] **Step 1: Record the fresh daily baseline**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_backtest.py tests/test_carry_risk.py tests/test_carry_signals.py -q
```

Expected: all selected tests pass. Save the test count in the execution notes; do not edit code if the baseline is red. Before changing implementation code, capture the existing deterministic stateful result:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from pathlib import Path
import runpy

import pandas as pd

namespace = runpy.run_path("tests/test_carry_backtest.py")
result = namespace["_run_stateful"](periods=24, start_index=12)
tables = (
    "daily_returns",
    "positions",
    "trades",
    "signals",
    "curve_selection",
    "data_quality",
    "run_config",
)
target = Path("tests/fixtures/carry_daily_stateful_baseline.pkl")
target.parent.mkdir(parents=True, exist_ok=True)
pd.to_pickle({"metrics": result.metrics, **{name: getattr(result, name) for name in tables}}, target)
loaded = pd.read_pickle(target)
assert loaded["metrics"] == result.metrics
print(f"captured={target} tables={len(tables)}")
PY
```

Expected: `captured=tests/fixtures/carry_daily_stateful_baseline.pkl tables=7`. Do not regenerate this fixture after editing `cta_carry` code.

- [ ] **Step 2: Write failing shared-seam tests**

Create `tests/test_carry_decision.py` with these tests:

```python
from datetime import date

import pandas as pd

from cta_carry.decision import build_daily_research, plan_signal_targets
from cta_carry.risk import PositionState
from tests.carry_fixtures import make_carry_panel, small_config


def _signal(direction=1, contract="A2405.SHF", strength=1.0):
    return pd.DataFrame(
        [
            {
                "trade_date": date(2024, 1, 2),
                "product": "A",
                "effective_direction": direction,
                "main_contract": contract,
                "strength": strength,
                "main_close": 100.0,
                "atr": 2.0,
            }
        ]
    )


def test_build_daily_research_returns_aligned_curve_atr_and_signals():
    data = make_carry_panel(periods=24)
    research = build_daily_research(data.prices, small_config())
    assert not research.curve_result.curve.empty
    assert not research.contract_atr.empty
    assert research.signal_result.signal_ready_date is not None
    assert set(research.contract_atr["trade_date"]) <= set(data.prices["trade_date"])


def test_signal_target_planning_preserves_a_post_stop_tranche_count():
    config = small_config()
    states = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=2,
            highest_high=110.0,
        )
    }
    plan = plan_signal_targets(
        states,
        _signal(),
        config,
        previous_states=states,
        reason_hints={"A": "stop_1"},
    )
    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "stop_1"}
    assert plan.raw_weights["A2405.SHF"] > 0.0


def test_direction_reversal_takes_precedence_over_stop_reason():
    config = small_config()
    previous = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=2,
        )
    }
    plan = plan_signal_targets(
        previous,
        _signal(direction=-1),
        config,
        previous_states=previous,
        reason_hints={"A": "stop_1"},
    )
    assert plan.states["A"].direction == -1
    assert plan.states["A"].tranches_remaining == config.stop_tranches
    assert plan.reasons == {"A": "direction_reversal"}
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_decision.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'cta_carry.decision'`.

- [ ] **Step 4: Implement the shared seam**

Create `cta_carry/decision.py` with these public objects and move the current validated logic into them:

```python
from dataclasses import dataclass
import math

import pandas as pd

from .config import CarryConfig
from .curve import CurveResult, build_curve
from .risk import (
    PositionState,
    apply_equal_weight_capital,
    compute_contract_atr,
    raw_target_weight,
    transition_signal,
)
from .signals import SignalResult, build_signals


@dataclass(frozen=True)
class DailyResearch:
    curve_result: CurveResult
    contract_atr: pd.DataFrame
    signal_result: SignalResult


@dataclass(frozen=True)
class TargetPlan:
    states: dict[str, PositionState]
    raw_weights: dict[str, float]
    reasons: dict[str, str]


class SignalInputError(RuntimeError):
    def __init__(
        self,
        *,
        trade_date,
        product,
        contract,
        check,
        reason,
        value=None,
    ) -> None:
        self.trade_date = trade_date
        self.product = product
        self.contract = contract
        self.check = check
        self.reason = reason
        self.value = value
        super().__init__(
            f"{trade_date} {product} {contract} {check}: {reason}; value={value!r}"
        )


def _valid_positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def build_daily_research(
    prices: pd.DataFrame,
    config: CarryConfig,
) -> DailyResearch:
    curve_result = build_curve(prices, config)
    contract_atr = compute_contract_atr(prices, config)
    main_atr = contract_atr.loc[:, ["trade_date", "contract", "atr"]].rename(
        columns={"contract": "main_contract"}
    )
    curve_with_atr = curve_result.curve.merge(
        main_atr,
        on=["trade_date", "main_contract"],
        how="left",
        validate="one_to_one",
    )
    return DailyResearch(
        curve_result=curve_result,
        contract_atr=contract_atr,
        signal_result=build_signals(curve_with_atr, config),
    )
```

Implement `plan_signal_targets(states, signal_rows, config, *, previous_states=None, reason_hints=None)` by moving the signal-transition and raw-sizing half of current `_close_plan`. Preserve this reason precedence exactly:

```python
if old_direction != 0 and after.direction == -old_direction:
    reason = "direction_reversal"
elif product in reason_hints:
    reason = reason_hints[product]
elif before.direction != 0 and after.direction == 0:
    reason = "signal_exit"
elif before.direction == 0 and after.direction != 0:
    reason = "entry"
elif before.direction == after.direction != 0 and before.contract != after.contract:
    reason = "roll"
else:
    reason = "rebalance"
```

The function must raise the moved `SignalInputError` with the same fields/messages as the current code and return the fully populated plan:

```python
return TargetPlan(
    states=next_states,
    raw_weights=apply_equal_weight_capital(raw_weights, config),
    reasons=reasons,
)
```

- [ ] **Step 5: Retain the daily stop wrapper**

In `cta_carry/backtest.py`:

- import `DailyResearch`, `SignalInputError`, `TargetPlan`, `build_daily_research`, and `plan_signal_targets`;
- alias `ClosePlan = TargetPlan` so existing private-test imports remain valid;
- replace `_curve_with_atr` with a call to `build_daily_research`;
- keep `_close_plan` as the daily execution policy: apply at most one daily chandelier stop per product, build `reason_hints`, then call the shared planner explicitly:

```python
return plan_signal_targets(
    post_stop_states,
    signal_rows,
    config,
    previous_states=states,
    reason_hints=reason_hints,
)
```

Do not change the daily event loop, ledger boundaries, output columns, or exception exports.

- [ ] **Step 6: Add a full result-table regression**

Extend `tests/test_carry_backtest.py`:

```python
def test_shared_decision_extraction_preserves_all_daily_result_tables():
    baseline = pd.read_pickle("tests/fixtures/carry_daily_stateful_baseline.pkl")
    rerun = _run_stateful(periods=24, start_index=12)
    for name in (
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "data_quality",
        "run_config",
    ):
        pd.testing.assert_frame_equal(
            baseline[name],
            getattr(rerun, name),
            check_exact=True,
        )
    assert baseline["metrics"] == rerun.metrics
```

- [ ] **Step 7: Run focused and full Carry tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_decision.py tests/test_carry_backtest.py tests/test_carry_risk.py tests/test_carry_signals.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add cta_carry/decision.py cta_carry/backtest.py tests/test_carry_decision.py tests/test_carry_backtest.py tests/fixtures/carry_daily_stateful_baseline.pkl
git commit -m "refactor(carry): extract shared daily decision seam"
```

### Task 2: Build the versioned trading-minute clock

**Files:**
- Create: `cta_carry/minute_sessions.py`
- Create: `tests/test_carry_minute_sessions.py`

- [ ] **Step 1: Write failing clock tests**

Create tests for Friday→Monday night mapping, recess skipping, sparse observations, and 15-minute segment boundaries:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

from cta_carry.minute_sessions import (
    SessionRule,
    SessionSegment,
    build_trading_slots,
    next_slots,
    fifteen_minute_buckets,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def test_monday_night_and_after_midnight_slots_follow_friday_session():
    rule = SessionRule(
        exchange="SHFE",
        product="AU",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(
            SessionSegment(-180, 150),
            SessionSegment(540, 615),
            SessionSegment(630, 690),
            SessionSegment(810, 900),
        ),
        version="commodity-v1",
    )
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )
    assert slots[0] == _dt(2024, 1, 5, 21, 0)
    assert _dt(2024, 1, 6, 0, 0) in slots
    assert _dt(2024, 1, 6, 2, 29) in slots
    assert _dt(2024, 1, 8, 0, 0) not in slots
    assert slots[-1] == _dt(2024, 1, 8, 14, 59)


def test_five_trading_minutes_skip_the_morning_recess():
    rule = SessionRule.day_only("DCE", "JD", version="commodity-v1")
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )
    window = next_slots(slots, _dt(2024, 1, 8, 10, 13), count=5)
    assert window == (
        _dt(2024, 1, 8, 10, 13),
        _dt(2024, 1, 8, 10, 14),
        _dt(2024, 1, 8, 10, 30),
        _dt(2024, 1, 8, 10, 31),
        _dt(2024, 1, 8, 10, 32),
    )


def test_fifteen_minute_buckets_never_cross_a_recess():
    rule = SessionRule.day_only("CZCE", "AP", version="commodity-v1")
    slots = build_trading_slots(
        trade_date=date(2024, 1, 8),
        previous_trade_date=date(2024, 1, 5),
        rule=rule,
    )
    buckets = fifteen_minute_buckets(slots, rule)
    assert all(len(bucket) == 15 for bucket in buckets)
    assert not any(
        _dt(2024, 1, 8, 10, 14) in bucket
        and _dt(2024, 1, 8, 10, 30) in bucket
        for bucket in buckets
    )
```

- [ ] **Step 2: Run to verify red**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q
```

Expected: collection fails because `cta_carry.minute_sessions` does not exist.

- [ ] **Step 3: Implement immutable rule and slot types**

Use logical minute offsets on the trade-date session clock. Night offset `-180` starts at 21:00 on `previous_trade_date`; night offsets `0..150` map to midnight onward on `previous_trade_date + 1 calendar day`; day offsets `540..900` map to `trade_date`. This distinction is essential across weekends: Monday's after-midnight night session is Saturday, not Monday.

```python
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_RULES_VERSION = "commodity-v1"
DAY_SEGMENTS = ((540, 615), (630, 690), (810, 900))


@dataclass(frozen=True, order=True)
class SessionSegment:
    start_minute: int
    end_minute: int

    def __post_init__(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("session segment end must be after start")
        if self.start_minute < -180 or self.end_minute > 900:
            raise ValueError("session segment is outside the commodity clock")


@dataclass(frozen=True)
class SessionRule:
    exchange: str
    product: str
    effective_start: date
    effective_end: date | None
    segments: tuple[SessionSegment, ...]
    version: str

    @classmethod
    def day_only(cls, exchange, product, *, version):
        return cls(
            exchange=exchange,
            product=product,
            effective_start=date(2000, 1, 1),
            effective_end=None,
            segments=tuple(SessionSegment(*item) for item in DAY_SEGMENTS),
            version=version,
        )
```

Implement:

- `load_session_rules(path: Path) -> tuple[SessionRule, ...]` with exact CSV columns `exchange,product,effective_start,effective_end,night_end,version`;
- `resolve_session_rule(rules, exchange, product, trade_date)` requiring exactly one matching row;
- `build_trading_slots(trade_date, previous_trade_date, rule)` mapping negative night offsets to the evening of `previous_trade_date`, nonnegative night offsets below 540 to `previous_trade_date + timedelta(days=1)`, and day offsets from 540 onward to `trade_date`;
- translate CSV `night_end` values exactly: `none` adds no night segment, `23:00` adds `SessionSegment(-180, -60)`, `01:00` adds `SessionSegment(-180, 60)`, and `02:30` adds `SessionSegment(-180, 150)`;
- `next_slots(slots, start, count)` using `bisect_left`, requiring exactly `count` remaining slots;
- `fifteen_minute_buckets(slots, rule)` by splitting slots per `SessionSegment` and requiring every segment length to be divisible by 15.

- [ ] **Step 4: Add failure tests**

Test overlapping effective ranges, an unmapped product-date, duplicate slots, a segment not divisible by 15, and a five-minute request past the final slot. Each error must include exchange, product, trade date, and check name.

- [ ] **Step 5: Run tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add cta_carry/minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): add versioned trading-minute clock"
```

### Task 3: Add minute aggregation, multiplier inference, and VWAP

**Files:**
- Create: `cta_carry/minute_bars.py`
- Create: `tests/test_carry_minute_bars.py`

- [ ] **Step 1: Write failing aggregation tests**

Create deterministic rows where zero-volume carried prices would invent an extreme:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cta_carry.minute_bars import (
    MinuteDataError,
    aggregate_fifteen_minute_bar,
    infer_contract_multiplier,
    five_minute_vwap,
)


TZ = ZoneInfo("Asia/Shanghai")


def _rows(prices, volumes, multiplier=10):
    start = datetime(2024, 1, 8, 9, 0, tzinfo=TZ)
    records = []
    for index, (price, volume) in enumerate(zip(prices, volumes, strict=True)):
        records.append(
            {
                "bar_time": start + timedelta(minutes=index),
                "symbol": "RB2405",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "amount": price * volume * multiplier,
            }
        )
    return pd.DataFrame(records)


def test_zero_volume_prices_do_not_enter_fifteen_minute_extremes():
    frame = _rows([100.0, 1.0, 101.0], [2.0, 0.0, 3.0])
    bar = aggregate_fifteen_minute_bar(
        frame,
        slots=tuple(frame["bar_time"]),
        contract="RB2405",
    )
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 101.0, 100.0, 101.0)
    assert bar.volume == 5.0


def test_vwap_uses_amount_volume_and_multiplier():
    frame = _rows([100.0, 102.0, 104.0, 106.0, 108.0], [1, 2, 3, 4, 5])
    result = five_minute_vwap(
        frame,
        slots=tuple(frame["bar_time"]),
        contract="RB2405",
        multiplier=10,
    )
    expected = sum(frame["amount"]) / sum(frame["volume"]) / 10
    assert result.price == pytest.approx(expected)
    assert result.volume == 15.0


def test_multiplier_is_uniquely_inferred_from_price_ranges():
    frame = _rows([100.0] * 60, [1.0] * 60, multiplier=10)
    frame["trade_date"] = frame["bar_time"].dt.date
    frame.loc[20:39, "trade_date"] = frame.loc[20:39, "trade_date"].map(
        lambda value: value + timedelta(days=1)
    )
    frame.loc[40:59, "trade_date"] = frame.loc[40:59, "trade_date"].map(
        lambda value: value + timedelta(days=2)
    )
    result = infer_contract_multiplier(frame, contract="RB2405")
    assert result.multiplier == 10
    assert result.source == "inferred"
    assert result.sample_rows == 60


def test_zero_volume_execution_window_is_a_hard_failure():
    frame = _rows([100.0] * 5, [0.0] * 5)
    with pytest.raises(MinuteDataError, match="execution_vwap"):
        five_minute_vwap(
            frame,
            slots=tuple(frame["bar_time"]),
            contract="RB2405",
            multiplier=10,
        )
```

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_bars.py -q
```

Expected: collection fails because `cta_carry.minute_bars` does not exist.

- [ ] **Step 3: Implement structured values and errors**

Define frozen `FifteenMinuteBar`, `VwapFill`, and `MultiplierResolution` dataclasses. Define `MinuteDataError` with attributes `trade_date`, `timestamp`, `product`, `contract`, `check`, `reason`, and `context`; include all non-null attributes in the message.

Implement `aggregate_fifteen_minute_bar` so it:

- reindexes rows to the supplied clock slots without forward fill;
- uses only `volume > 0` rows for OHLC and close;
- returns a `no_trade=True` bar with null OHLC when no positive-volume row exists;
- rejects duplicate `bar_time`, rows outside the slots, nonfinite positive-volume OHLC, and negative volume/amount.

- [ ] **Step 4: Implement the exact multiplier rule**

```python
def _epsilon(low: float, high: float) -> float:
    return 1e-6 * max(1.0, abs(low), abs(high))


def _candidate_pass_rate(sample, multiplier: int) -> float:
    price = sample["amount"] / sample["volume"] / multiplier
    tolerance = sample.apply(
        lambda row: _epsilon(float(row.low), float(row.high)),
        axis=1,
    )
    passed = (
        price.ge(sample["low"] - tolerance)
        & price.le(sample["high"] + tolerance)
    )
    return float(passed.mean())
```

`infer_contract_multiplier` must sort by `(symbol, bar_time)`, take 60 deterministic evenly spaced nonzero rows across at least three dates, fall back to all rows only when fewer than 60 exist, reject fewer than 10 rows or fewer than two dates, enumerate integers 1–10,000, require pass rate `>= 0.99`, and accept exactly one candidate.

`validate_metadata_multiplier` applies the same sample/pass-rate check to a positive integer metadata value and returns source `metadata`.

- [ ] **Step 5: Implement five-minute VWAP**

`five_minute_vwap` must require exactly five supplied clock slots, sum positive-volume rows only, reject zero total volume/amount, compute `sum(amount)/sum(volume)/multiplier`, and enforce:

```python
epsilon = 1e-6 * max(1.0, abs(low), abs(high))
if not low - epsilon <= price <= high + epsilon:
    raise MinuteDataError(
        contract=contract,
        check="execution_vwap",
        reason="VWAP is outside the traded price range",
        context={"vwap": price, "low": low, "high": high},
    )
```

- [ ] **Step 6: Add edge-case tests and run**

Add tests for duplicate timestamps, negative amount, only 9 multiplier samples, two accepted candidates, bad metadata, missing slot rows that do not compress the clock, and VWAP outside range.

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_bars.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add cta_carry/minute_bars.py tests/test_carry_minute_bars.py
git commit -m "feat(carry): aggregate minute bars and VWAP safely"
```

### Task 4: Implement safe monthly PostgreSQL access

**Files:**
- Create: `cta_carry/minute_pg_source.py`
- Create: `tests/test_carry_minute_pg_source.py`

- [ ] **Step 1: Write failing SQL-shape tests**

Test that the hypertable join key is a bare column, physical bounds are rendered as SQL literals, and all planner settings are transaction-local:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from cta_carry.minute_pg_source import (
    MinuteCandidate,
    build_minute_batch_query,
    czce_minute_symbol,
)


def test_czce_three_digit_month_maps_on_the_small_side():
    assert czce_minute_symbol("AP605.CZC", date(2025, 12, 1)) == "AP2605"
    assert czce_minute_symbol("TA1701.CZC", date(2016, 9, 1)) == "TA1701"


def test_batch_query_keeps_minute_symbol_bare_and_has_literal_bounds():
    tz = ZoneInfo("Asia/Shanghai")
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=tz)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=tz)
    query = build_minute_batch_query(lower=lower, upper=upper).as_string(None)
    assert "m.symbol = c.minute_symbol" in query
    assert "m.bar_time >= '2024-01-01T20:00:00+08:00'" in query
    assert "m.bar_time < '2024-02-01T15:01:00+08:00'" in query
    assert "regexp_replace(m.symbol" not in query
    assert "CASE WHEN m.exchange" not in query
```

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_pg_source.py -q
```

Expected: collection fails because `cta_carry.minute_pg_source` does not exist.

- [ ] **Step 3: Implement candidate and code mapping**

Define `MinuteCandidate(trade_date, product, daily_contract, minute_symbol, exchange, window_start, window_end)`. Map daily suffixes `SHF/DCE/CZC/INE/GFE` to minute exchanges `SHFE/DCE/CZCE/INE/GFEX`. Implement the Stage 3 CZCE rule:

```python
k = (delivery_year_digit - trade_date.year % 10 + 10) % 10
if k > 3:
    raise MinuteDataError(
        trade_date=trade_date,
        contract=contract,
        check="czce_contract_mapping",
        reason="delivery year is more than three years after trade date",
    )
```

Return bare month symbols without daily suffixes for every venue.

- [ ] **Step 4: Implement the batch source**

Implement `PublicMinuteSource.iter_month(candidate_frame, lower, upper)`:

1. open the existing configured PostgreSQL connection;
2. execute exactly these local settings;
3. create `_carry_minute_candidates` with a primary key on `(minute_symbol, trade_date)`;
4. insert candidates using `psycopg2.extras.execute_values`;
5. run `EXPLAIN (FORMAT JSON)` on the same bounded select;
6. reject a plan with more than three referenced Timescale chunks or any node whose estimated rows are `>= 10_000_000`;
7. stream `fetchmany(100_000)` frames ordered by `bar_time, symbol`;
8. rollback/drop temp state on every failure.

The select must have this shape:

```sql
SELECT c.trade_date, c.product, c.daily_contract,
       m.bar_time, m.symbol, m.exchange,
       m.open, m.high, m.low, m.close,
       m.volume, m.amount, m.open_interest
FROM _carry_minute_candidates c
JOIN public.futures_minute m
  ON m.symbol = c.minute_symbol
 AND m.bar_time >= c.window_start
 AND m.bar_time <  c.window_end
WHERE m.bar_time >= TIMESTAMPTZ '2024-01-01T20:00:00+08:00'
  AND m.bar_time <  TIMESTAMPTZ '2024-02-01T15:01:00+08:00'
ORDER BY m.bar_time, m.symbol
```

The timestamps above are rendered examples, not constants. `build_minute_batch_query` must render each batch's actual aware `lower` and `upper` values as SQL literals so Timescale can prune physical chunks; do not pass those two bounds as bind parameters.

Use:

```sql
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL work_mem = '32MB';
SET LOCAL statement_timeout = '300s';
SET LOCAL enable_hashjoin = off;
SET LOCAL enable_mergejoin = off;
```

- [ ] **Step 5: Add mocked connection tests**

Use fake cursor/connection objects to assert:

- all five `SET LOCAL` statements execute before `EXPLAIN`;
- unsafe plans raise `MinuteDataError(check="minute_query_plan")`;
- chunks are yielded in deterministic order;
- a cursor exception triggers rollback;
- `futures_contract_info` metadata selects the latest row on or before the trade date and rejects conflicting same-date multipliers.

- [ ] **Step 6: Run tests**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_pg_source.py tests/test_carry_pg_source.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add cta_carry/minute_pg_source.py tests/test_carry_minute_pg_source.py
git commit -m "feat(carry): stream bounded minute batches from PostgreSQL"
```

### Task 5: Capture and validate the versioned session-rule asset
> **Authoritative replacement (2026-08-14):** Do not execute the legacy
> Steps 1–5 below. Execute every checked step in
> [`2026-08-14-carry-minute-session-eligibility-v2.md`](2026-08-14-carry-minute-session-eligibility-v2.md)
> Tasks 1–8 instead. The legacy text remains only to preserve the original plan
> history.

- [ ] Complete the authoritative Task 5 subplan with unit, database, authority-source,
  asset-hash, and `ambiguous=0` evidence before starting Task 6.


**Files:**
- Create: `scripts/carry/capture_minute_sessions.py`
- Create: `config/carry_minute_sessions.csv`
- Modify: `cta_carry/minute_pg_source.py`
- Modify: `tests/test_carry_minute_sessions.py`
- Modify: `tests/test_carry_minute_pg_source.py`

- [ ] **Step 1: Add a failing asset-loader test**

```python
def test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products():
    rules = load_session_rules(Path("config/carry_minute_sessions.csv"))
    assert rules
    for product in ("RB", "AU", "SC", "AP", "JD"):
        assert any(rule.product == product for rule in rules)
```

In `tests/test_carry_minute_pg_source.py`, import `build_session_boundary_query` and add:

```python
def test_session_boundary_query_is_grouped_bounded_and_keeps_symbol_bare():
    tz = ZoneInfo("Asia/Shanghai")
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=tz)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=tz)
    query = build_session_boundary_query(lower=lower, upper=upper).as_string(None)
    assert "LEFT JOIN public.futures_minute m" in query
    assert "m.symbol = c.minute_symbol" in query
    assert "GROUP BY c.trade_date, c.product, c.daily_contract" in query
    assert "min(m.bar_time)" in query.lower()
    assert "max(m.bar_time)" in query.lower()
    assert "'2024-01-01T20:00:00+08:00'" in query
    assert "'2024-02-01T15:01:00+08:00'" in query
```

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py::test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products \
  tests/test_carry_minute_pg_source.py::test_session_boundary_query_is_grouped_bounded_and_keeps_symbol_bare -q
```

Expected: FAIL because the CSV and `build_session_boundary_query` do not exist yet.

- [ ] **Step 2: Implement the deterministic capture command**

The script must:

- load daily data and select one highest-OI concrete contract per product-trade-date;
- map all symbols on the small side;
- query every eligible highest-OI product-trade-date through a boundary-only `PublicMinuteSource.iter_session_boundaries` path, batched by calendar month; use a left join so no-bar candidates survive for explicit rejection, and return only the first/last night and standard day-segment observations rather than streaming full intraday bars;
- extend the Task 4 connection fake with three candidate trade dates and assert `iter_session_boundaries` returns exactly one grouped audit row per candidate in deterministic order, rejecting a missing or multiply classified day;
- classify observed night end into exactly `none`, `23:00`, `01:00`, or `02:30`;
- require the standard three day segments;
- collapse consecutive trade dates with identical schedules into exact effective-date ranges so an intra-month historical rule change cannot be skipped;
- write sorted CSV columns `exchange,product,effective_start,effective_end,night_end,version`;
- re-read the CSV and validate every audited timestamp maps to exactly one slot;
- count every audited product-trade-date in `checked_days`;
- print `print(f"products={len(products)} rules={len(rules)} checked_days={checked_days} ambiguous={len(ambiguous)}")`.

Use `--output`, `--start`, `--end`, `--settings`, and `--use-test` arguments. Write through a sibling temporary file and `os.replace` only after validation.

- [ ] **Step 3: Run capture against the real read-only source**

Run after market close:

```bash
PYTHONPATH=. .venv/bin/python scripts/carry/capture_minute_sessions.py \
  --start 2011-01-01 \
  --end 2026-04-29 \
  --output config/carry_minute_sessions.csv
```

Expected: exit 0 and final line contains `ambiguous=0`. If ambiguity is nonzero, stop this task and inspect the named product/month; do not weaken the classifier or infer a rule silently.

- [ ] **Step 4: Review the generated asset**

Run:

```bash
rg -n ',(none|23:00|01:00|02:30),' config/carry_minute_sessions.csv
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q
```

Expected: every non-header CSV row matches one allowed night value; all session tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/carry/capture_minute_sessions.py config/carry_minute_sessions.csv cta_carry/minute_pg_source.py tests/test_carry_minute_sessions.py tests/test_carry_minute_pg_source.py
git commit -m "data(carry): capture versioned commodity sessions"
```

### Task 6: Implement deterministic piecewise formal and shadow ledgers

**Files:**
- Create: `cta_carry/minute_account.py`
- Create: `tests/test_carry_minute_account.py`

- [ ] **Step 1: Write failing account tests**

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from cta_carry.minute_account import EventAccount


TZ = ZoneInfo("Asia/Shanghai")


def _ts(hour, minute):
    return datetime(2024, 1, 8, hour, minute, tzinfo=TZ)


def test_piecewise_mark_rebalance_and_cost_identity():
    account = EventAccount(cost_bps=4.0)
    account.initialize({"RB2405.SHF": 100.0})
    first = account.rebalance(
        timestamp=_ts(9, 5),
        prices={"RB2405.SHF": 100.0},
        target_weights={"RB2405.SHF": 1.0},
        reason_by_contract={"RB2405.SHF": "entry"},
    )
    second = account.rebalance(
        timestamp=_ts(10, 35),
        prices={"RB2405.SHF": 110.0},
        target_weights={"RB2405.SHF": 2.0 / 3.0},
        reason_by_contract={"RB2405.SHF": "stop_1"},
    )
    close = account.mark_close(
        trade_date=date(2024, 1, 8),
        timestamp=_ts(15, 0),
        prices={"RB2405.SHF": 105.0},
    )
    daily = account.drain_daily_row(date(2024, 1, 8), "close")
    assert first.turnover == pytest.approx(1.0)
    assert second.gross_return == pytest.approx(0.10)
    assert second.turnover == pytest.approx(1.0 / 3.0)
    assert close.gross_return == pytest.approx((2.0 / 3.0) * (105.0 / 110.0 - 1.0))
    assert daily.net_return == pytest.approx(daily.gross_return - daily.cost)
    assert daily.cost >= 0.0
    assert account.equity > 0.0


def test_same_timestamp_contract_order_cannot_change_equity():
    left = EventAccount(cost_bps=4.0)
    right = EventAccount(cost_bps=4.0)
    prices = {"A": 100.0, "B": 200.0}
    left.initialize(prices)
    right.initialize(dict(reversed(tuple(prices.items()))))
    targets = {"A": 0.5, "B": -0.5}
    left.rebalance(_ts(9, 5), prices, targets, {"A": "entry", "B": "entry"})
    right.rebalance(
        _ts(9, 5),
        dict(reversed(tuple(prices.items()))),
        dict(reversed(tuple(targets.items()))),
        {"B": "entry", "A": "entry"},
    )
    assert left.equity == right.equity
```

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_account.py -q
```

Expected: collection fails because `cta_carry.minute_account` does not exist.

- [ ] **Step 3: Implement account records and identities**

Define frozen `ExecutionRecord` and `AccountEvent` dataclasses plus `EventAccount`. The account must:

- validate every nonzero held or changing contract has a finite positive price;
- mark all old weights from last price to the event price before applying targets;
- calculate gross contributions with `math.fsum` in sorted contract order;
- calculate one-way turnover over the contract union;
- compute each execution row's direct cost as `turnover * cost_bps / 10_000`;
- maintain parallel no-cost gross equity and after-cost net equity from the same event gross returns, advancing them by `1 + gross_return` and `1 + gross_return - direct_cost`, respectively;
- keep `account.equity` as the net-equity compatibility property and raise existing `EquityDepletedError` when net equity is nonpositive;
- retain gross/net opening equity for each trade date;
- store event rows without relying on dict insertion order;
- mark daily closes without turnover;
- expose `drain_daily_row(trade_date, boundary_type)` that aggregates turnover, closing net equity, and gross leverage;
- derive daily `gross_return` and `net_return` from their respective end/start equity ratios, then set daily `cost = gross_return - net_return` so the published identity remains exact under multiple compounded events;
- retain the direct event cost separately on every execution row for VWAP/turnover reconciliation.

Use the same methods for formal and shadow accounts; only target weights differ.

- [ ] **Step 4: Add validation tests**

Test missing mark prices, nonfinite weights, duplicate/nonmonotonic timestamps, close before the last event, negative cost, and equity depletion.

- [ ] **Step 5: Run tests**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_account.py tests/test_carry_backtest.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add cta_carry/minute_account.py tests/test_carry_minute_account.py
git commit -m "feat(carry): add piecewise minute account ledger"
```

### Task 7: Implement the intraday stop and order-merging state machine

**Files:**
- Create: `cta_carry/minute_backtest.py`
- Create: `tests/test_carry_minute_backtest.py`

- [ ] **Step 1: Write failing three-tranche and lock tests**

Use a small fake clock and directly exercise `IntradayStopMachine`:

```python
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from cta_carry.minute_backtest import IntradayStopMachine
from cta_carry.minute_bars import FifteenMinuteBar
from cta_carry.risk import PositionState
from tests.carry_fixtures import small_config


TZ = ZoneInfo("Asia/Shanghai")


def _bar(index, high, low, close):
    start = datetime(2024, 1, 8, 9, 0, tzinfo=TZ) + timedelta(minutes=15 * index)
    return FifteenMinuteBar(
        start=start,
        end=start + timedelta(minutes=15),
        contract="A2405.SHF",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        no_trade=False,
    )


def test_three_separate_bars_can_remove_all_tranches_in_one_day():
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = PositionState(
        direction=1,
        contract="A2405.SHF",
        tranches_remaining=3,
        highest_high=110.0,
    )
    for index in range(3):
        decision = machine.on_bar(
            trade_date=date(2024, 1, 8),
            product="A",
            state=state,
            bar=_bar(index, high=110.0, low=100.0, close=100.0),
            atr=5.0,
            next_fill_end=_bar(index + 1, 110.0, 100.0, 100.0).start,
        )
        assert decision.triggered
        state = decision.state
    assert state.direction == 0
    assert state.locked_direction == 1


def test_bar_overlapping_the_previous_fill_is_not_eligible():
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = PositionState(
        direction=1,
        contract="A2405.SHF",
        tranches_remaining=3,
        highest_high=110.0,
    )
    first = machine.on_bar(
        date(2024, 1, 8),
        "A",
        state,
        _bar(0, 110.0, 100.0, 100.0),
        5.0,
        _bar(1, 110.0, 100.0, 100.0).end,
    )
    skipped = machine.on_bar(
        date(2024, 1, 8),
        "A",
        first.state,
        _bar(1, 110.0, 100.0, 100.0),
        5.0,
        _bar(2, 110.0, 100.0, 100.0).end,
    )
    assert not skipped.eligible
```

- [ ] **Step 2: Verify red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_backtest.py -q
```

Expected: collection fails because `cta_carry.minute_backtest` does not exist.

- [ ] **Step 3: Implement stop decisions**

Define frozen `StopDecision` containing `eligible`, `triggered`, `state`, `stage`, `threshold`, `bar`, and `not_before`. `IntradayStopMachine` must:

- call existing `apply_chandelier` with the 15-minute traded OHLC and prior-day daily ATR;
- skip no-trade bars;
- require `bar.start >= not_before_by_product[product]`;
- on trigger set `not_before_by_product[product] = next_fill_end`;
- reset the gate on signal exit, reversal, or roll;
- never trigger more than `config.stop_tranches` times per product-trade-date.

- [ ] **Step 4: Implement close-time net-order merging**

Add `merge_close_plan(post_stop_states, signal_rows, config)` that calls `plan_signal_targets` after applying the final-bar stop. Assert with tests:

- final stop + same direction keeps the reduced tranche count;
- final stop + zero signal creates one exit target;
- final stop + reversal creates one reverse target with three tranches;
- roll preserves tranche count and resets extremes;
- no pair of execution rows shares the same product/window with an intermediate stop target.

- [ ] **Step 5: Run tests**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_backtest.py tests/test_carry_risk.py tests/test_carry_decision.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add cta_carry/minute_backtest.py tests/test_carry_minute_backtest.py
git commit -m "feat(carry): add intraday chandelier state machine"
```

### Task 8: Orchestrate the complete minute backtester

**Files:**
- Modify: `cta_carry/minute_backtest.py`
- Modify: `tests/test_carry_minute_backtest.py`
- Modify: `cta_carry/__init__.py`

- [ ] **Step 1: Write a failing end-to-end fake-source test**

Build a `FakeMinuteSource` that returns five products, one night session and four day-only sessions, with 300 deterministic minute rows per product-trade-date. Use a small config (`vol_window=3`, `min_shadow_active_days=2`, small signal windows) and assert:

```python
def test_minute_backtester_produces_auditable_tables_and_feedback():
    data, source, rules, start, end = minute_backtest_fixture()
    result = CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=small_config(vol_window=3, min_shadow_active_days=2),
        start=start,
        end=end,
    ).run()
    assert not result.daily_returns.empty
    assert set(result.executions["reason"]) >= {"entry", "rebalance"}
    assert {
        "bar_start",
        "bar_end",
        "threshold",
        "tranches_before",
        "tranches_after",
        "execution_id",
    } <= set(result.intraday_stops)
    assert {
        "session_rules_version",
        "minute_query_rules_version",
        "accounting_clock",
    } <= set(result.run_config["key"])
    assert result.run_config.set_index("key").loc["accounting_clock", "value"] == (
        "piecewise_close_marked"
    )
```

Also prove the minute engine invokes the repository-asset prewarm preflight before
daily research or minute queries:

```python
def test_minute_backtester_rejects_a_session_asset_that_misses_prewarm(
    monkeypatch,
):
    data, source, rules, start, end = minute_backtest_fixture()
    monkeypatch.setattr(
        "cta_carry.minute_backtest.SESSION_RULES_CAPTURE_START", start
    )
    with pytest.raises(SessionClockError, match="session_asset_prewarm_coverage"):
        CarryMinuteBacktester(
            data=data, minute_source=source, session_rules=rules,
            config=small_config(), start=start, end=end,
        ).run()
```


- [ ] **Step 2: Verify red**

Run the named test. Expected: FAIL because `CarryMinuteBacktester` is not defined.

- [ ] **Step 3: Implement candidate-month preparation**
Before `build_daily_research`, call `validate_capture_coverage` with
`SESSION_RULES_CAPTURE_START`, the requested backtest `start`, and
`config.prewarm_calendar_days`. This makes a future prewarm/start change fail at
engine startup with `session_asset_prewarm_coverage`, rather than later as a resolver
zero-match.


`CarryMinuteBacktester.run()` first calls `build_daily_research`, then creates candidate rows from:

- every nonzero signal's main contract;
- every carried contract;
- both legs of a same-direction roll;
- every contract required for a daily close mark.

Tag every concrete candidate with exactly one role from `signal_main`, `carried`,
`roll_old`, `roll_new`, `exit`, or `close_mark`. A missing actual candidate raises
`MinuteDataError(check="dynamic_execution_leg_missing_minutes")` with that
`candidate_role`; it must never reuse
`session_representative_missing_minutes`. Convert the concrete candidate union to
product-day keys and require it to be a subset of the Task 5 `audit_keys` evidence
before the first monthly query.


For each target trade date, attach `previous_trade_date`, resolve the session rule, and use the rule's first/last slot for `window_start/window_end`. Group by target trade-date calendar month while preserving one-session overlap at boundaries.

- [ ] **Step 4: Implement the event loop in this fixed order**

For each target trade date:

1. execute the prior close's net target in each product's first five slots;
2. apply the fill to formal and shadow accounts at `fill_end`;
3. scan eligible 15-minute bars in absolute timestamp order;
4. on each stop, compute the following five-slot VWAP and apply one reduced target at `fill_end`;
5. at day close, mark both accounts to concrete `futures_daily.close`;
6. append the shadow net daily return and active flag to `ShadowVolWindow`;
7. build the daily signal plan, using the post-final-bar stop states;
8. use the just-completed shadow estimate to scale the next target;
9. append report rows only on/after `report_start_date`;
10. carry states, weights, last prices, pending plan, query counters, and the overlap de-duplication set into the next month.

At the first report day, require the same complete-window/active-days/positive-vol gates as `CarryBacktester`; otherwise raise `WarmupInsufficientError`.

- [ ] **Step 5: Define the result contract**

Extend `CarryBacktestResult` in `backtest.py` with default-empty frames after `metrics`:

```python
executions: pd.DataFrame = field(default_factory=pd.DataFrame)
intraday_stops: pd.DataFrame = field(default_factory=pd.DataFrame)
minute_data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
execution_mode: str = "daily"
```

Return `execution_mode="minute"` from the minute engine. Keep daily construction source-compatible by relying on defaults.

- [ ] **Step 6: Add accounting and lookahead tests**

Add tests proving:

- changing any minute row after a checkpoint cannot alter earlier executions/stops;
- `T` intraday stops use only the ATR labeled `T-1`;
- minute returns enter the next close's shadow estimate, not an earlier target;
- formal and shadow accounts share state/fills but use scaled/unscaled weights;
- month batching produces identical results to a single fake-source batch;
- duplicate overlap events are rejected or de-duplicated by stable event ID;
- the full-stop locked direction cannot re-enter until zero or reversal;
- result tables are deterministic across two runs.

- [ ] **Step 7: Run focused tests**

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_carry_minute_backtest.py \
  tests/test_carry_minute_account.py \
  tests/test_carry_minute_bars.py \
  tests/test_carry_minute_sessions.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Export and commit**

Export `CarryMinuteBacktester` and `MinuteDataError` from `cta_carry/__init__.py`.

```bash
git add cta_carry/backtest.py cta_carry/minute_backtest.py cta_carry/__init__.py tests/test_carry_minute_backtest.py
git commit -m "feat(carry): run minute VWAP and intraday-stop backtests"
```

### Task 9: Add conditional minute audit sheets

**Files:**
- Modify: `cta_carry/report.py:15-34,177-218,244-262`
- Modify: `tests/test_carry_report_cli.py:24-99`

- [ ] **Step 1: Write failing report tests**

```python
def test_minute_report_adds_three_audit_sheets(tmp_path):
    result = replace(
        _result(),
        execution_mode="minute",
        executions=pd.DataFrame([{"execution_id": "e1"}]),
        intraday_stops=pd.DataFrame([{"execution_id": "e1"}]),
        minute_data_quality=pd.DataFrame([{"check": "coverage"}]),
    )
    xlsx, _ = write_carry_outputs(result, tmp_path / "minute")
    with pd.ExcelFile(xlsx, engine="openpyxl") as workbook:
        assert workbook.sheet_names == [
            "metrics",
            "daily_returns",
            "positions",
            "trades",
            "signals",
            "curve_selection",
            "data_quality",
            "executions",
            "intraday_stops",
            "minute_data_quality",
            "run_config",
        ]


def test_daily_report_keeps_exact_original_eight_sheets(tmp_path):
    xlsx, _ = write_carry_outputs(_result(), tmp_path / "daily")
    with pd.ExcelFile(xlsx, engine="openpyxl") as workbook:
        assert len(workbook.sheet_names) == 8
        assert "executions" not in workbook.sheet_names
```

- [ ] **Step 2: Verify red**

Run both named tests. Expected: minute test fails because only the original sheet list is written.

- [ ] **Step 3: Make sheet selection result-dependent**

Replace the fixed validation constant with:

```python
_DAILY_SHEET_NAMES = (
    "metrics",
    "daily_returns",
    "positions",
    "trades",
    "signals",
    "curve_selection",
    "data_quality",
    "run_config",
)
_MINUTE_AUDIT_SHEETS = (
    "executions",
    "intraday_stops",
    "minute_data_quality",
)
```

`_report_sheets(result)` inserts minute audit frames immediately before `run_config` only when `result.execution_mode == "minute"`. Pass the expected names into `_validate_workbook(path, expected_names)`; do not read a mutable global during validation.

- [ ] **Step 4: Test Excel bounds and empty audit frames**

Minute mode must still write all three sheets even if a frame has zero rows. Add a preflight test proving an oversized minute audit frame fails before opening the writer and names the correct sheet.

- [ ] **Step 5: Run and commit**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_report_cli.py -q
git add cta_carry/report.py tests/test_carry_report_cli.py
git commit -m "feat(carry): report minute executions and stops"
```

Expected: all report/CLI tests pass before commit.

### Task 10: Wire minute mode into the CLI and runtime provenance

**Files:**
- Modify: `cta_carry/__main__.py:25-238`
- Modify: `tests/test_carry_report_cli.py:229-480`

- [ ] **Step 1: Write failing parser and mode tests**

Add:

```python
def test_cli_execution_defaults_to_daily():
    args = build_parser().parse_args(["--start", "2024-01-01", "--end", "2024-02-01"])
    assert args.execution == "daily"


def test_minute_execution_rejects_file_source(tmp_path, capsys):
    code = main(
        [
            "--execution",
            "minute",
            "--source",
            "files",
            "--data-dir",
            str(tmp_path),
            "--start",
            "2024-01-01",
            "--end",
            "2024-02-01",
        ]
    )
    assert code == 2
    assert "--execution minute requires --source public-pg" in capsys.readouterr().err
```

Add a monkeypatched public-PG test asserting daily mode constructs `CarryBacktester`, minute mode constructs `CarryMinuteBacktester`, and neither constructs the other.

- [ ] **Step 2: Verify red**

Run the new tests. Expected: parser test fails because `--execution` is absent.

- [ ] **Step 3: Add parser validation and engine dispatch**

Add:

```python
parser.add_argument(
    "--execution",
    choices=["daily", "minute"],
    default="daily",
)
```

Validate minute+files before any data read. For minute mode:

- load the same daily prewarm dataset;
- load `config/carry_minute_sessions.csv`;
- construct `PublicMinuteSource` from the same settings/use-test selection;
- run `CarryMinuteBacktester`.

Catch `MinuteDataError` alongside existing structured research errors and return 2. Unexpected exceptions must continue to propagate.

- [ ] **Step 4: Extend runtime provenance**

`_runtime_config` receives `execution_mode` and adds it for every run. Minute engine run config must also contain:

```text
accounting_clock=piecewise_close_marked
minute_query_rules_version=timescale-bare-symbol-v1
session_rules_version=commodity-v1
multiplier_resolution_version=price-range-v1
minute_table_min
minute_table_max
minute_query_months
minute_rows
minute_candidate_contract_days
```

Store `minute_table_min` and `minute_table_max` as the exact ISO-8601 bounds returned by the metadata query. Store the other three as integer counters collected by `PublicMinuteSource`; tests must compare them with the source audit object instead of hard-coding values.

Continue lowercasing the `code_dirty` row exactly as today.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_report_cli.py tests/test_carry_pg_source.py tests/test_carry_minute_pg_source.py -q
git add cta_carry/__main__.py tests/test_carry_report_cli.py
git commit -m "feat(carry): expose minute execution in the CLI"
```

Expected: all selected tests pass before commit.

### Task 11: Add the paired-result comparison command

**Files:**
- Create: `scripts/carry/compare_execution.py`
- Create: `tests/test_carry_compare_execution.py`

- [ ] **Step 1: Write failing comparison tests**

Create two small workbooks with `metrics`, `daily_returns`, `executions`, and `intraday_stops`. Assert the command returns one row containing daily, minute, and delta values for gross/net annual return, Sharpe, max drawdown, turnover, cost, leverage, stop counts, and VWAP/open basis points.

Core assertion:

```python
def test_compare_execution_reports_minute_minus_daily(tmp_path):
    daily = write_fixture_workbook(tmp_path / "daily.xlsx", minute=False)
    minute = write_fixture_workbook(tmp_path / "minute.xlsx", minute=True)
    result = compare_workbooks(daily, minute, label="report_window")
    assert result.loc[0, "label"] == "report_window"
    assert result.loc[0, "net_ann_return_delta"] == pytest.approx(
        result.loc[0, "net_ann_return_minute"]
        - result.loc[0, "net_ann_return_daily"]
    )
    assert result.loc[0, "same_day_multi_stop_count"] == 1
```

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_compare_execution.py -q
```

Expected: collection fails because the comparison module does not exist.

- [ ] **Step 3: Implement comparison and CLI**

The command accepts repeated:

```text
--pair LABEL DAILY_XLSX MINUTE_XLSX
--output output/carry_minute_comparison.csv
```

Recompute metrics from `daily_returns` using `common.metrics.summarize`; never trust only the metrics sheet. Validate both workbooks have identical requested/report dates and research parameters other than execution/accounting metadata. Calculate:

- gross/net annual return and Sharpe;
- annual volatility, max drawdown, Calmar;
- annual turnover and total/annualized cost;
- average/max gross leverage;
- stop rows, triggered stops, same-day multi-stop product-days;
- volume-weighted `(vwap/open - 1) * 10_000` for daily target fills;
- the fraction of the prior 10 percentage-point gross gap explained by `gross_ann_return_delta / 0.10`.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_compare_execution.py -q
git add scripts/carry/compare_execution.py tests/test_carry_compare_execution.py
git commit -m "feat(carry): compare daily and minute execution runs"
```

Expected: all comparison tests pass before commit.

### Task 12: Run database smoke tests, full verification, and documentation

**Files:**
- Modify: `README.md:42-66`
- Modify: `docs/operations/carry-daily-research.md`
- Modify: any code/test files required by failures, limited to root-cause fixes.

- [ ] **Step 0: Close the explain-only and plan-summary audit gap with TDD**

Before the real smoke, add failing source and backtester tests for an immutable,
serializable `MinutePlanSummary`. `PublicMinuteSource.explain_month(...)` must reuse
the exact candidate canonicalization, temp table, transaction-local settings and
bounded SELECT text used by `iter_month`, but execute only `EXPLAIN (FORMAT JSON)`:
it must not open the named streaming cursor or execute the data SELECT. Refactor
the existing plan gate to return the summary while retaining every current hard
failure. Both explain-only calls and the EXPLAIN already executed by `iter_month`
must expose their summaries through an immutable source snapshot.

The minute backtester must append one deterministic `minute_query_plan` row per
actual monthly query to `minute_data_quality`, including physical lower/upper
bounds, candidate contract-day count, referenced chunk names, maximum estimated
rows and plan node types. Missing, malformed or count-mismatched source plan audit
must fail closed. Tests must cover the single-contract/five-date explain-only
smoke shape, unsafe-plan cleanup, absence of a data SELECT, and report-table
serialization before proceeding to Step 1.

- [ ] **Step 1: Explain the real bounded query before selecting rows**

Use a single active contract and five trade dates in 2020. Run the source's explain-only entry point with a 300-second timeout.

Expected:

- bare `m.symbol` index condition;
- at most three Timescale chunks named in the plan;
- no plan node with estimated rows `>= 10_000_000`;
- no sequential scan over the entire hypertable.

Save the plan summary into the smoke run's `minute_data_quality`.

- [ ] **Step 2: Run a real five-product smoke backtest**

Use five products because the existing signal engine intentionally requires at least five ready products for a legal cross-section; a single-product command cannot exercise execution.

```bash
PYTHONPATH=. .venv/bin/python -m cta_carry \
  --execution minute \
  --source public-pg \
  --products RB,HC,I,J,JM \
  --start 2020-01-02 \
  --end 2020-03-31 \
  --liquidity-window 5 \
  --liquidity-threshold 0 \
  --carry-window 2 \
  --momentum-window 2 \
  --atr-window 2 \
  --vol-window 5 \
  --min-shadow-active-days 2 \
  --prewarm-calendar-days 365 \
  --output-prefix output/carry_minute_smoke
```

Expected: exit 0, eleven workbook sheets, nonempty `executions`, no required-window error, and bounded query audit rows. If no natural stop occurs, that is acceptable because synthetic tests cover stop behavior.

- [ ] **Step 3: Manually reconcile one execution**

For the first nonzero execution row, query its five slots directly by bare symbol and literal time bounds. Verify:

```text
reported_vwap = sum(amount) / sum(volume) / reported_multiplier
reported_turnover = abs(new_weight - old_weight)
reported_cost = reported_turnover * cost_bps / 10000
```

Record the contract, window, and equality result in `docs/operations/carry-daily-research.md`.

- [ ] **Step 4: Run the complete automated suite**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

Expected: zero failures.

- [ ] **Step 5: Run static and repository checks**

```bash
.venv/bin/ruff check cta_carry tests scripts/carry
git diff --check
git status --short
```

Expected: ruff exit 0, no whitespace errors, and only intentional documentation/output changes.

- [ ] **Step 6: Update user documentation**

Document:

- `--execution daily|minute` and the daily default;
- minute mode's public-PG-only restriction;
- night-first five-minute execution;
- 15-minute, same-day three-tranche stop behavior;
- zero-volume filtering, session-rule asset, multiplier audit, hard failures;
- query resource rules and one-run-at-a-time requirement;
- the two exact research commands from Task 13.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/operations/carry-daily-research.md
git commit -m "docs(carry): document minute execution operations"
```

### Task 13: Run both paired research windows and write the evidence report

**Files:**
- Create: `docs/research/2026-08-13-carry-minute-execution-report.md`
- Generate: `output/carry_report_daily.xlsx`
- Generate: `output/carry_report_minute.xlsx`
- Generate: `output/carry_full_daily.xlsx`
- Generate: `output/carry_full_minute.xlsx`
- Generate: `output/carry_report_literal_daily.xlsx`
- Generate: `output/carry_report_literal_minute.xlsx`
- Generate: `output/carry_minute_comparison.csv`

- [ ] **Step 1: Confirm resources and clean code provenance**

Run:

```bash
free -g
git status --short
git rev-parse HEAD
```

Expected: enough available memory for one Carry process, empty status, and a recorded clean commit. Never run two histories concurrently.

- [ ] **Step 2: Run the report-window daily/minute pair**

```bash
PYTHONPATH=. .venv/bin/python -m cta_carry --execution daily --source public-pg \
  --start 2013-01-04 --end 2022-05-24 --cost-bps 1.3 \
  --output-prefix output/carry_report_daily
```

After it exits 0:

```bash
PYTHONPATH=. .venv/bin/python -m cta_carry --execution minute --source public-pg \
  --start 2013-01-04 --end 2022-05-24 --cost-bps 1.3 \
  --output-prefix output/carry_report_minute
```

Expected: both exit 0 and record the same requested/report dates and research parameters.

- [ ] **Step 3: Run the common-full-history pair**

```bash
PYTHONPATH=. .venv/bin/python -m cta_carry --execution daily --source public-pg \
  --start 2013-01-04 --end 2026-04-29 --cost-bps 4.0 \
  --output-prefix output/carry_full_daily
```

After it exits 0:

```bash
PYTHONPATH=. .venv/bin/python -m cta_carry --execution minute --source public-pg \
  --start 2013-01-04 --end 2026-04-29 --cost-bps 4.0 \
  --output-prefix output/carry_full_minute
```

Expected: both exit 0.

- [ ] **Step 4: Run the report-literal sensitivity pair**

Run daily and minute with:

```text
--start 2013-01-04
--end 2022-05-24
--cost-bps 1.3
--secondary-selection second_by_oi
--equal-weight-capital
```

Write to `output/carry_report_literal_daily` and `output/carry_report_literal_minute`. Expected: both exit 0; label them sensitivity, not baseline.

- [ ] **Step 5: Generate the comparison CSV**

```bash
PYTHONPATH=. .venv/bin/python scripts/carry/compare_execution.py \
  --pair report_window output/carry_report_daily.xlsx output/carry_report_minute.xlsx \
  --pair full_history output/carry_full_daily.xlsx output/carry_full_minute.xlsx \
  --pair report_literal output/carry_report_literal_daily.xlsx output/carry_report_literal_minute.xlsx \
  --output output/carry_minute_comparison.csv
```

Expected: three rows with no missing required metric.

- [ ] **Step 6: Write the research report**

The report must include:

- exact commands, commit, dirty flag, data coverage, and session/multiplier/query versions;
- a daily/minute table for all metrics required by design §12.2;
- execution VWAP versus daily open attribution;
- intraday stop frequency, same-day multi-tranche frequency, and stop PnL/cost attribution;
- minute gross-return delta divided by 10pp;
- separate default and report-literal conclusions;
- explicit statement whether minute execution explains the historical gap;
- limitations: no 2012 daily sample, no hand/guarantee/capacity/limit-queue modeling, and report ambiguity.

Do not claim the report numbers until every workbook passes provenance and ledger checks.

- [ ] **Step 7: Fresh final verification**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
.venv/bin/ruff check cta_carry tests scripts/carry
git diff --check
```

Expected: zero test/lint/whitespace failures.

- [ ] **Step 8: Commit the research record**

Do not commit generated `output/` files unless existing repository policy explicitly tracks them. Commit the report and any operation-doc updates:

```bash
git add docs/research/2026-08-13-carry-minute-execution-report.md docs/operations/carry-daily-research.md
git commit -m "docs(carry): report minute execution replay results"
```

## Plan self-review checklist

- **Design coverage:** Tasks 1–10 cover architecture, daily compatibility, sessions, CZCE mapping, bounded queries, zero-volume handling, multiplier/VWAP, minute stops, event accounting, outputs, hard failures, and provenance. Tasks 11–13 cover paired comparison, database audit, operations, both required windows, sensitivity, and the final evidence report.
- **Type consistency:** `DailyResearch`, `TargetPlan`, `SessionRule`, `FifteenMinuteBar`, `VwapFill`, `MultiplierResolution`, `MinuteCandidate`, `EventAccount`, `StopDecision`, `CarryMinuteBacktester`, and `MinuteDataError` are introduced before their downstream use.
- **No hidden database mutation:** all PostgreSQL operations are read-only except connection-local temp tables.
- **No execution ambiguity:** target fills become effective at five-minute window end; the next eligible stop bar starts at or after fill end; final-bar stop and next daily target merge into one net order.
- **No resource ambiguity:** one target-trade-date month at a time, literal physical bounds, bare minute symbol, nested loop, plan gate, 32MB work memory, no parallel gather, 300-second timeout.
