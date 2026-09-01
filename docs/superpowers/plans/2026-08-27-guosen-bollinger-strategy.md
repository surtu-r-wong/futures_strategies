# Guosen Bollinger Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the 15-minute Guosen Bollinger commodity strategy as an independent CLI and report, including OI sizing, fixed take-profit bands, monthly product selection, 10% volatility targeting, and out-of-sample evaluation.

**Architecture:** cta_bollinger turns the shared panel bundle into per-product indicator paths and deterministic trade-state paths. A shadow ledger runs every product without the portfolio volatility multiplier; monthly selection reads only completed shadow history. A separate portfolio ledger applies fixed-universe weights and the monthly 10% volatility multiplier to selected target positions.

**Tech Stack:** Python 3.13, pandas, NumPy, common.commodity, common.minute.account, common.leverage, openpyxl/xlsxwriter, matplotlib, pytest.

---

**Dependency:** Complete 2026-08-27-guosen-commodity-shared-core.md first.

**Test fixtures:** Register tests/commodity_fixtures.py in panel-facing test
modules. Define score_factory locally as a function returning ProductScore;
define result fixtures by constructing the BacktestResult declared in Task 4
with explicit empty DataFrames for sheets not under test. Do not refer to a
fixture name until its constructor and required columns appear in that test
file. Build bundle and shadow_results fixtures from the four-table
bundle_frames fixture introduced by shared-core Task 6; do not read an
undeclared fixture directory.

### Task 1: Implement Bollinger and OI indicators

**Files:**
- Create: cta_bollinger/__init__.py
- Create: cta_bollinger/indicators.py
- Create: tests/test_bollinger_indicators.py

- [ ] **Step 1: Write failing tests**

    import numpy as np
    import pytest
    from cta_bollinger.indicators import bands, oi_multiplier

    def test_bands_use_one_window_for_mean_and_population_std():
        result = bands([1.0, 2.0, 3.0], length=3, beta=1.5, ddof=0)
        assert result.middle[-1] == pytest.approx(2.0)
        assert result.std[-1] == pytest.approx(np.std([1.0, 2.0, 3.0], ddof=0))
        assert result.upper[-1] == pytest.approx(
            2.0 + 1.5 * np.std([1.0, 2.0, 3.0], ddof=0)
        )

    def test_oi_multiplier_is_full_only_when_short_is_strictly_above_long():
        assert oi_multiplier(short_oi=101.0, long_oi=100.0) == 1.0
        assert oi_multiplier(short_oi=100.0, long_oi=100.0) == 0.5
        assert oi_multiplier(short_oi=99.0, long_oi=100.0) == 0.5

    def test_indicator_history_is_missing_until_the_full_window():
        result = bands([1.0, 2.0], length=3, beta=1.5, ddof=0)
        assert np.isnan(result.middle).all()

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_indicators.py -q

Expected: FAIL because cta_bollinger is absent.

- [ ] **Step 3: Implement**

    @dataclass(frozen=True, slots=True)
    class BandPath:
        middle: np.ndarray
        std: np.ndarray
        upper: np.ndarray
        lower: np.ndarray

    def bands(closes, *, length=300, beta=1.5, ddof=0):
        values = np.asarray(closes, dtype="float64")
        if values.ndim != 1:
            raise ValueError("bollinger_closes: expected one dimension")
        if type(length) is not int or length < 2:
            raise ValueError("bollinger_length: expected integer >= 2")
        if ddof not in (0, 1):
            raise ValueError("bollinger_ddof: expected 0 or 1")
        frame = pd.Series(values)
        middle = frame.rolling(length, min_periods=length).mean().to_numpy()
        std = frame.rolling(length, min_periods=length).std(ddof=ddof).to_numpy()
        return BandPath(
            middle=middle,
            std=std,
            upper=middle + beta * std,
            lower=middle - beta * std,
        )

    def oi_multiplier(*, short_oi, long_oi):
        if not all(math.isfinite(float(v)) for v in (short_oi, long_oi)):
            raise ValueError("bollinger_oi: both averages must be finite")
        return 1.0 if short_oi > long_oi else 0.5

Add rolling_oi(open_interest, short=150, long=300), using full-window simple means and the same one-dimensional validation.

- [ ] **Step 4: Run tests and commit**

Run: .venv/bin/python -m pytest tests/test_bollinger_indicators.py -q

Expected: PASS.

    git add cta_bollinger tests/test_bollinger_indicators.py
    git commit -m "feat: add Bollinger and open-interest indicators"

### Task 2: Implement the trade state machine

**Files:**
- Create: cta_bollinger/signals.py
- Create: tests/test_bollinger_signals.py

- [ ] **Step 1: Write failing entry, exit, and frozen-state tests**

    from cta_bollinger.signals import Position, State, step

    def flat():
        return State(position=Position.FLAT, take_profit=None, oi_scale=0.0)

    def test_long_entry_needs_a_fresh_upper_cross():
        action = step(
            flat(),
            previous_close=100.0, previous_middle=100.0, previous_upper=101.0,
            previous_lower=99.0, close=102.0, middle=100.5,
            upper=101.5, lower=99.5, std=1.0, oi_scale=1.0,
        )
        assert action.state.position is Position.LONG
        assert action.state.take_profit == 108.5
        assert action.reason == "upper_cross"

    def test_take_profit_and_oi_scale_are_frozen_after_entry():
        state = State(Position.LONG, take_profit=108.5, oi_scale=0.5)
        action = step(
            state,
            previous_close=103.0, previous_middle=101.0, previous_upper=102.0,
            previous_lower=100.0, close=104.0, middle=102.0,
            upper=103.0, lower=101.0, std=4.0, oi_scale=1.0,
        )
        assert action.state == state

    def test_long_exits_on_middle_cross_or_fixed_take_profit():
        state = State(Position.LONG, take_profit=108.5, oi_scale=0.5)
        middle_exit = step(
            state,
            previous_close=102.0, previous_middle=101.0, previous_upper=103.0,
            previous_lower=99.0, close=100.0, middle=100.5,
            upper=102.0, lower=99.0, std=1.0, oi_scale=1.0,
        )
        assert middle_exit.state.position is Position.FLAT
        assert middle_exit.reason == "middle_cross"

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_signals.py -q

Expected: FAIL because signals.py is absent.

- [ ] **Step 3: Implement the complete public state types**

    class Position(StrEnum):
        FLAT = "flat"
        LONG = "long"
        SHORT = "short"

    @dataclass(frozen=True, slots=True)
    class State:
        position: Position
        take_profit: float | None
        oi_scale: float

    @dataclass(frozen=True, slots=True)
    class Action:
        state: State
        target_direction: int
        changed: bool
        reason: str

step validates all supplied indicator values. While flat it enters only on a cross using each bar's own boundary: previous_close <= previous_upper and close > upper for long; previous_close >= previous_lower and close < lower for short. Long take profit is middle + 8 * std at entry; short is middle - 8 * std. While positioned it ignores new entry crosses, freezes take_profit and oi_scale, and exits at the first middle cross or take-profit reach. A flat result always clears take_profit and oi_scale.

- [ ] **Step 4: Add symmetry and no-reentry tests, run, and commit**

Add tests that mirror every long case for short and prove a price remaining outside the upper band after take-profit does not create a new cross.

Run: .venv/bin/python -m pytest tests/test_bollinger_signals.py -q

Expected: PASS.

    git add cta_bollinger/signals.py tests/test_bollinger_signals.py
    git commit -m "feat: add the Bollinger trade state machine"

### Task 3: Build product shadow paths and monthly selection

**Files:**
- Create: cta_bollinger/shadow.py
- Create: cta_bollinger/selection.py
- Create: tests/test_bollinger_shadow.py
- Create: tests/test_bollinger_selection.py

- [ ] **Step 1: Write failing causal-path tests**

    from cta_bollinger.selection import eligible_products
    from cta_bollinger.shadow import run_shadow_product

    def test_shadow_uses_adjusted_prices_for_signals_and_raw_fill_for_returns(
        bollinger_panel,
    ):
        result = run_shadow_product(bollinger_panel, product="RB", ddof=0)
        assert result.signals.iloc[0]["signal_close"] == (
            bollinger_panel.iloc[0]["close"] * bollinger_panel.iloc[0]["adj_factor"]
        )
        assert result.trades.iloc[0]["entry_price"] == bollinger_panel.iloc[0]["fill_price"]

    def test_selector_rejects_only_when_both_risk_metrics_are_negative(score_factory):
        scores = {
            "RB": score_factory(trade_count=5, sharpe=-1.0, calmar=0.1),
            "CU": score_factory(trade_count=5, sharpe=-1.0, calmar=-0.1),
            "AL": score_factory(trade_count=4, sharpe=2.0, calmar=2.0),
        }
        assert eligible_products(scores) == ("RB",)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_shadow.py tests/test_bollinger_selection.py -q

Expected: FAIL because both modules are absent.

- [ ] **Step 3: Implement shadow outputs**

    @dataclass(frozen=True, slots=True)
    class ShadowResult:
        product: str
        signals: pd.DataFrame
        trades: pd.DataFrame
        daily: pd.DataFrame

run_shadow_product sorts one product by slot_end, drops no_trade rows from indicator history, multiplies OHLC by adj_factor only for signals and ATR ratios, advances State on every traded bar, and sends target changes to a one-product EventAccount at fill_time with raw fill_price. Target magnitude is direction * oi_scale * atr_leverage(real_close, adjusted_atr / adj_factor). It applies 1.3 cost_bps and emits exit-dated trades and daily net returns. Roll rows use bundle roll_fills to rebalance old and new contract in one EventAccount call.

- [ ] **Step 4: Implement the policy**

    def eligible_products(scores):
        selected = []
        for product, score in sorted(scores.items()):
            if score.trade_count < 5:
                continue
            if score.sharpe < 0.0 and score.calmar < 0.0:
                continue
            selected.append(product)
        return tuple(selected)

The caller supplies trailing_scores restricted to dates before the new month; selection.py does not recompute or widen that window.

- [ ] **Step 5: Run tests and commit**

Run: .venv/bin/python -m pytest tests/test_bollinger_shadow.py tests/test_bollinger_selection.py -q

Expected: PASS.

    git add cta_bollinger/shadow.py cta_bollinger/selection.py tests/test_bollinger_shadow.py tests/test_bollinger_selection.py
    git commit -m "feat: add Bollinger shadow selection"

### Task 4: Implement the selected portfolio backtest

**Files:**
- Create: cta_bollinger/backtest.py
- Create: tests/test_bollinger_backtest.py

- [ ] **Step 1: Write failing capital and volatility tests**

    from cta_bollinger.backtest import run_backtest

    def test_selected_but_flat_product_keeps_its_half_in_cash(bundle, shadow_results):
        result = run_backtest(
            bundle=bundle,
            shadows=shadow_results,
            realized_vol_min_observations=2,
        )
        row = result.positions.query("product == 'RB'").iloc[0]
        assert row["universe_weight"] == 0.5

    def test_monthly_multiplier_targets_ten_percent(bundle, shadow_results):
        result = run_backtest(
            bundle=bundle,
            shadows=shadow_results,
            realized_vol_min_observations=2,
        )
        assert result.positions["target_annual_vol"].dropna().eq(0.10).all()

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_backtest.py -q

Expected: FAIL because backtest.py is absent.

- [ ] **Step 3: Implement result and event loop**

    @dataclass(frozen=True, slots=True)
    class BacktestResult:
        daily: pd.DataFrame
        positions: pd.DataFrame
        trades: pd.DataFrame
        signals: pd.DataFrame
        selection: pd.DataFrame
        data_quality: pd.DataFrame

For each month, intersect the shared liquidity universe with eligible_products(trailing_scores(...)). Use fixed_universe_weights for directions. Compute one monthly realized volatility from the previous 252 daily returns of the pre-volatility selected portfolio; use final_leverage with target_annual_vol=0.10 and cap 4. Rebalance the actual EventAccount only at fill_time, month-entry alignment, strategy exits, and roll fills. Reject any requested target with null fill_price, fill_time, multiplier, OI, or adjustment factor.

The pre-volatility return series is an explicit parallel ledger using selected weights, OI scale, and ATR leverage but no Mul_vol. This prevents a zero-position warmup loop and matches the paper's order: dynamic strategy first, realized-volatility adjustment second.

- [ ] **Step 4: Add causality, roll, and month-boundary tests**

Prove that changing March data does not alter February selection or multiplier; an exiting product is closed at the first March fill; an entering product aligns to its existing shadow position; and one roll event contains executions for both old and new contracts.

Run: .venv/bin/python -m pytest tests/test_bollinger_backtest.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add cta_bollinger/backtest.py tests/test_bollinger_backtest.py
    git commit -m "feat: backtest the selected Bollinger portfolio"

### Task 5: Add independent reports

**Files:**
- Create: cta_bollinger/report.py
- Create: tests/test_bollinger_report.py

- [ ] **Step 1: Write failing workbook-contract tests**

    from cta_bollinger.report import FIDELITY_RULE_IDS, write_outputs

    def test_fidelity_contains_every_registered_rule():
        assert FIDELITY_RULE_IDS == (
            "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
            "B1", "B2", "B3", "B4",
        )

    def test_workbook_has_the_required_sheets(tmp_path, result):
        paths = write_outputs(result, output_prefix=tmp_path / "bollinger")
        book = pd.ExcelFile(paths.xlsx)
        assert book.sheet_names == [
            "metrics", "daily_returns", "positions", "trades", "signals",
            "universe", "selection", "dominant_rolls", "data_quality",
            "fidelity", "run_config",
        ]

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_report.py -q

Expected: FAIL because report.py is absent.

- [ ] **Step 3: Implement outputs**

Use split_metrics with in_sample_end=date(2021, 9, 30). Write the eleven sheets in the tested order. Plot equity, drawdown, and leverage with a vertical sample cutoff. Write audit JSON containing bundle manifest, commit, options, sheet row counts, query-plan summaries, fidelity variants, and SHA-256 digests of normalized sheet CSV content. Put the paper metrics 17.52%, 1.72, 8.27%, 2.12, and 10.16% beside replication metrics; never turn metric gaps into pass/fail tuning.

- [ ] **Step 4: Run tests and commit**

Run: .venv/bin/python -m pytest tests/test_bollinger_report.py -q

Expected: PASS.

    git add cta_bollinger/report.py tests/test_bollinger_report.py
    git commit -m "feat: report the Bollinger replication"

### Task 6: Add the CLI and ddof sensitivity run

**Files:**
- Create: cta_bollinger/__main__.py
- Create: tests/test_bollinger_cli.py
- Create: docs/operations/guosen-bollinger-runbook.md
- Modify: README.md
- Modify: docs/ROADMAP.md

- [ ] **Step 1: Write failing option tests**

    from cta_bollinger.__main__ import build_parser, resolve_options

    def test_paper_defaults(tmp_path):
        ns = build_parser().parse_args([
            "--panel-dir", str(tmp_path), "--start", "2012-01-04",
            "--end", "2026-01-30", "--output-prefix", "output/bollinger",
        ])
        options = resolve_options(ns)
        assert options.length == 300
        assert options.beta == 1.5
        assert options.ddof == 0
        assert options.target_vol == 0.10
        assert options.cost_bps == 1.3

    def test_cli_rejects_the_session_rule_overrun(tmp_path):
        ns = build_parser().parse_args([
            "--panel-dir", str(tmp_path), "--start", "2012-01-04",
            "--end", "2026-02-01", "--output-prefix", "output/bollinger",
        ])
        with pytest.raises(SystemExit, match="2026-01-30"):
            resolve_options(ns)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_bollinger_cli.py -q

Expected: FAIL because __main__.py is absent.

- [ ] **Step 3: Implement CLI**

Required arguments: --panel-dir, --start, --end, --output-prefix. Options: --ddof choices 0/1, --cost-bps default 1.3, --require-paper-faithful, and --run-ddof-sensitivity. Length, beta, OI windows, take-profit coefficient, target volatility, and sample cutoff remain constants rather than tuning flags.

main reads and verifies the bundle, checks requested coverage, runs shadows, backtest, and reports. --run-ddof-sensitivity writes a second prefix ending _ddof1 and marks it sensitivity_only in fidelity and audit JSON.

- [ ] **Step 4: Run CLI fixture and tests**

Run:

    .venv/bin/python -m pytest tests/test_bollinger_cli.py tests/test_bollinger_report.py -q
    .venv/bin/python -m cta_bollinger --panel-dir output/_preflight/commodity-panel --start 2023-01-03 --end 2023-01-31 --output-prefix output/_preflight/bollinger --require-paper-faithful --run-ddof-sensitivity

Expected: tests pass and two complete output triplets are created.

- [ ] **Step 5: Document and commit**

Document the fixed parameters, sample cutoff, bundle dependency, strict failures, and sensitivity-only interpretation.

    git add cta_bollinger/__main__.py tests/test_bollinger_cli.py docs/operations/guosen-bollinger-runbook.md README.md docs/ROADMAP.md
    git commit -m "feat: run the Guosen Bollinger replication"

### Task 7: Full-history acceptance

**Files:**
- Create: docs/research/2026-08-27-guosen-bollinger-replication.md

- [ ] **Step 1: Run all tests**

Run: .venv/bin/python -m pytest -q

Expected: zero failures.

- [ ] **Step 2: Run the registered full history**

Run:

    .venv/bin/python -m cta_bollinger --panel-dir output/commodity-panel-v1 --start 2012-01-04 --end 2026-01-30 --output-prefix output/guosen_bollinger --require-paper-faithful --run-ddof-sensitivity

Expected: default and sensitivity outputs complete without missing-fill or coverage errors.

- [ ] **Step 3: Record acceptance evidence**

Record bundle hash, commit, runtime, sample metrics through 2021-09-30, out-of-sample metrics from 2021-10-01, paper gaps, pricing-basis counts, selected-universe history, and ddof impact. Do not change parameters in response to gaps.

- [ ] **Step 4: Verify artifacts and commit**

Run:

    git diff --check
    .venv/bin/python -m pytest -q

Expected: no whitespace errors and zero failures.

    git add docs/research/2026-08-27-guosen-bollinger-replication.md
    git commit -m "docs: record the Bollinger replication results"
