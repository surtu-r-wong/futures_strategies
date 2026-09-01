# Guosen Dow Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Guosen 15-minute Dow-theory commodity strategy as an independent CLI and report, including cumulative MACD trend segments, turning-point invalidation, Dow-extreme resonance, monthly product selection, 15% volatility targeting, and out-of-sample evaluation.

**Architecture:** cta_dow separates the scale-invariant indicator path from a deterministic segment/extreme state machine. A latched paper-default signal enters only on a fresh close breakout and holds until the preliminary trend changes or turning-point validity fails; a literal every-bar gate is a registered sensitivity variant. Shadow product ledgers feed causal monthly selection, while the actual portfolio uses active-position weights and one monthly volatility multiplier.

**Tech Stack:** Python 3.13, pandas, NumPy, common.commodity, common.minute.account, common.leverage, openpyxl/xlsxwriter, matplotlib, pytest.

---

**Dependency:** Complete 2026-08-27-guosen-commodity-shared-core.md first. This plan is independent of the Bollinger implementation.

**Test fixtures:** Register tests/commodity_fixtures.py in panel-facing test
modules. Define score_factory locally as a function returning ProductScore;
define result fixtures by constructing the BacktestResult declared in Task 5
with explicit empty DataFrames for sheets not under test. Do not refer to a
fixture name until its constructor and required columns appear in that test
file. Build bundle and shadow_results fixtures from the four-table
bundle_frames fixture introduced by shared-core Task 6; do not read an
undeclared fixture directory.

### Task 1: Implement cumulative MACD and preliminary trend

**Files:**
- Create: cta_dow/__init__.py
- Create: cta_dow/indicators.py
- Create: tests/test_dow_indicators.py

- [ ] **Step 1: Write failing formula and threshold tests**

    import numpy as np
    from cta_dow.indicators import Trend, cumulative_macd, preliminary_trend

    def test_cumulative_macd_resets_only_when_diff_changes_sign():
        got = cumulative_macd([1.0, 2.0, 0.0, -1.0, -2.0, 1.0])
        assert got.tolist() == [1.0, 3.0, 3.0, 2.0, 0.0, 1.0]

    def test_threshold_gap_keeps_the_previous_trend():
        got = preliminary_trend(
            cumulative=[0.5, 1.1, 0.2, -0.4, -1.2, -0.2],
            atr=[1.0] * 6,
        )
        assert got == (
            Trend.NEUTRAL, Trend.UP, Trend.UP,
            Trend.UP, Trend.DOWN, Trend.DOWN,
        )

    def test_macd_path_uses_ema_twelve_twenty_six_and_nine():
        path = macd_path(np.arange(40.0))
        expected = ema(np.arange(40.0), span=12) - ema(
            np.arange(40.0), span=26
        )
        assert np.allclose(path.macd, expected)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_indicators.py -q

Expected: FAIL because cta_dow is absent.

- [ ] **Step 3: Implement**

    class Trend(StrEnum):
        NEUTRAL = "neutral"
        UP = "up"
        DOWN = "down"

    @dataclass(frozen=True, slots=True)
    class MacdPath:
        macd: np.ndarray
        signal_line: np.ndarray
        diff: np.ndarray
        cumulative: np.ndarray

    def cumulative_macd(diff):
        values = np.asarray(diff, dtype="float64")
        if values.ndim != 1:
            raise ValueError("dow_diff: expected one dimension")
        if not len(values):
            return values.copy()
        out = np.empty_like(values)
        out[0] = values[0]
        for index in range(1, len(values)):
            out[index] = (
                values[index]
                if values[index] * values[index - 1] < 0.0
                else out[index - 1] + values[index]
            )
        return out

    def macd_path(closes):
        fast = ema(closes, span=12)
        slow = ema(closes, span=26)
        macd = fast - slow
        signal = ema(macd, span=9)
        diff = macd - signal
        return MacdPath(macd, signal, diff, cumulative_macd(diff))

preliminary_trend validates equal finite arrays. It starts NEUTRAL, changes to UP on cumulative >= atr, changes to DOWN on cumulative <= -atr, and otherwise carries the previous state.

- [ ] **Step 4: Add NaN and equality tests, run, and commit**

Add tests proving equality triggers, negative or zero ATR is rejected, and no-trade bars must be removed by the caller rather than passed as NaN.

Run: .venv/bin/python -m pytest tests/test_dow_indicators.py -q

Expected: PASS.

    git add cta_dow tests/test_dow_indicators.py
    git commit -m "feat: add cumulative MACD trend indicators"

### Task 2: Implement preliminary segments and extreme history

**Files:**
- Create: cta_dow/state.py
- Create: tests/test_dow_state.py

- [ ] **Step 1: Write failing segment-history tests**

    from cta_dow.indicators import Trend
    from cta_dow.state import SegmentState, inspect_bar

    def prepared_up_state(*, segment_high, segment_low, last_down_lows):
        return SegmentState(
            trend=Trend.UP,
            segment_high=segment_high,
            segment_low=segment_low,
            last_up_highs=(),
            last_down_lows=last_down_lows,
        )

    def test_switch_closes_the_previous_segment_and_starts_with_current_bar():
        state = SegmentState.empty()
        state = inspect_bar(
            state, trend=Trend.UP, high=11.0, low=9.0, close=10.0
        ).next_state
        state = inspect_bar(
            state, trend=Trend.UP, high=12.0, low=8.0, close=11.0
        ).next_state
        state = inspect_bar(
            state, trend=Trend.DOWN, high=10.0, low=7.0, close=8.0
        ).next_state
        assert state.last_up_highs == (12.0,)
        assert state.segment_high == 10.0
        assert state.segment_low == 7.0

    def test_correction_uses_current_running_extreme_but_breakout_uses_prior_extreme():
        state = prepared_up_state(
            segment_high=12.0, segment_low=10.0,
            last_down_lows=(9.0, 8.0),
        )
        decision = inspect_bar(
            state, trend=Trend.UP, high=13.0, low=9.5, close=12.1
        )
        assert decision.turning_valid is True
        assert decision.dow_resonance is True
        assert decision.close_breakout is True
        assert decision.next_state.segment_high == 13.0

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_state.py -q

Expected: FAIL because state.py is absent.

- [ ] **Step 3: Implement state and ordered inspection**

    @dataclass(frozen=True, slots=True)
    class SegmentState:
        trend: Trend
        segment_high: float | None
        segment_low: float | None
        last_up_highs: tuple[float, ...]
        last_down_lows: tuple[float, ...]

        @classmethod
        def empty(cls):
            return cls(Trend.NEUTRAL, None, None, (), ())

    @dataclass(frozen=True, slots=True)
    class SegmentDecision:
        next_state: SegmentState
        turning_valid: bool
        dow_resonance: bool
        close_breakout: bool
        enough_history: bool
        trend_changed: bool

inspect_bar closes a non-neutral old segment only when the preliminary trend
changes; it prepends its high to last_up_highs or low to last_down_lows and
retains at most two values. It then initializes the new segment from the
current bar and returns close_breakout false. A neutral bar does not create a
segment.

When the trend is unchanged, inspect_bar first saves
segment_high/segment_low as the pre-bar breakout reference, then forms
candidate_high/candidate_low including the current bar. For UP: enough
history means two prior down lows; turning_valid is candidate_low >
last_down_lows[0]; resonance is last_down_lows[0] >
last_down_lows[1]; breakout is close >= pre-bar segment_high. DOWN is
symmetric with last_up_highs and close <= pre-bar segment_low.

- [ ] **Step 4: Add correction, equality, and symmetry tests**

Prove equality invalidates turning validity; equality satisfies close breakout; correction does not close or reclassify the preliminary segment; and DOWN mirrors UP.

Run: .venv/bin/python -m pytest tests/test_dow_state.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add cta_dow/state.py tests/test_dow_state.py
    git commit -m "feat: track Dow trend segments and extremes"

### Task 3: Implement latched and literal signal modes

**Files:**
- Create: cta_dow/signals.py
- Create: tests/test_dow_signals.py

- [ ] **Step 1: Write failing latch tests**

    from cta_dow.indicators import Trend
    from cta_dow.signals import Position, SignalMode, TradeState, decide

    def test_latched_mode_holds_after_the_entry_breakout():
        entered = decide(
            TradeState(Position.FLAT),
            trend=Trend.UP, trend_changed=False,
            turning_valid=True, dow_resonance=True, close_breakout=True,
            mode=SignalMode.LATCHED,
        )
        held = decide(
            entered.state,
            trend=Trend.UP, trend_changed=False,
            turning_valid=True, dow_resonance=True, close_breakout=False,
            mode=SignalMode.LATCHED,
        )
        assert entered.target_direction == 1
        assert held.target_direction == 1
        assert held.reason == "latched_hold"

    def test_turning_failure_flattens_without_reversing():
        result = decide(
            TradeState(Position.LONG),
            trend=Trend.UP, trend_changed=False,
            turning_valid=False, dow_resonance=True, close_breakout=False,
            mode=SignalMode.LATCHED,
        )
        assert result.target_direction == 0
        assert result.reason == "turning_invalid"

    def test_literal_mode_is_flat_when_the_breakout_gate_is_not_true():
        result = decide(
            TradeState(Position.LONG),
            trend=Trend.UP, trend_changed=False,
            turning_valid=True, dow_resonance=True, close_breakout=False,
            mode=SignalMode.LITERAL,
        )
        assert result.target_direction == 0

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_signals.py -q

Expected: FAIL because signals.py is absent.

- [ ] **Step 3: Implement complete types and precedence**

    class Position(StrEnum):
        FLAT = "flat"
        LONG = "long"
        SHORT = "short"

    class SignalMode(StrEnum):
        LATCHED = "latched"
        LITERAL = "literal"

    @dataclass(frozen=True, slots=True)
    class TradeState:
        position: Position

    @dataclass(frozen=True, slots=True)
    class SignalDecision:
        state: TradeState
        target_direction: int
        changed: bool
        reason: str

Decision precedence is: neutral or trend_changed closes an incompatible position; turning_invalid closes the same-direction position and never reverses; insufficient history or no resonance prevents entry; a flat state enters only on close_breakout; LATCHED holds a valid existing direction without another breakout; LITERAL requires all entry gates every bar. DOWN mirrors UP.

- [ ] **Step 4: Add full transition-table tests, run, and commit**

Cover flat/up, flat/down, long/up, long/down, short/down, short/up, both modes, and every rejection reason.

Run: .venv/bin/python -m pytest tests/test_dow_signals.py -q

Expected: PASS.

    git add cta_dow/signals.py tests/test_dow_signals.py
    git commit -m "feat: add latched and literal Dow signals"

### Task 4: Build shadow paths and monthly selection

**Files:**
- Create: cta_dow/shadow.py
- Create: cta_dow/selection.py
- Create: tests/test_dow_shadow.py
- Create: tests/test_dow_selection.py

- [ ] **Step 1: Write failing end-to-end path tests**

    from cta_dow.selection import eligible_products
    from cta_dow.shadow import run_shadow_product

    def test_shadow_preserves_segment_state_across_a_contract_roll(dow_panel):
        result = run_shadow_product(
            dow_panel, product="RB", signal_mode="latched"
        )
        around_roll = result.signals.query("roll_event").sort_values("slot_end")
        assert around_roll.iloc[0]["last_down_low_1"] == around_roll.iloc[1]["last_down_low_1"]

    def test_selector_needs_five_trades_and_nonnegative_return(score_factory):
        scores = {
            "RB": score_factory(trade_count=5, cumulative_return=0.0),
            "CU": score_factory(trade_count=5, cumulative_return=-0.001),
            "AL": score_factory(trade_count=4, cumulative_return=1.0),
        }
        assert eligible_products(scores) == ("RB",)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_shadow.py tests/test_dow_selection.py -q

Expected: FAIL because both modules are absent.

- [ ] **Step 3: Implement shadow outputs and ordered bar loop**

    @dataclass(frozen=True, slots=True)
    class ShadowResult:
        product: str
        signal_mode: str
        signals: pd.DataFrame
        trades: pd.DataFrame
        daily: pd.DataFrame

run_shadow_product filters no_trade bars from the indicator sequence,
multiplies OHLC by adj_factor, computes MACD and ATR20, and calls inspect_bar
exactly once per bar; its returned decision contains both the gates computed
against pre-bar extremes and the fully updated next_state. It then calls
decide. Target magnitude is direction * atr_leverage(real_close,
adjusted_atr / adj_factor). Fill and roll accounting use raw prices and
EventAccount at fill_time. It emits every gate and prior/current extreme into
signals so entry decisions can be reconstructed.

- [ ] **Step 4: Implement policy**

    def eligible_products(scores):
        return tuple(
            product
            for product, score in sorted(scores.items())
            if score.trade_count >= 5 and score.cumulative_return >= 0.0
        )

- [ ] **Step 5: Add future-data causality tests, run, and commit**

Append future bars to a fixture and prove all earlier signals, segments, trades, and daily returns are identical.

Run: .venv/bin/python -m pytest tests/test_dow_shadow.py tests/test_dow_selection.py -q

Expected: PASS.

    git add cta_dow/shadow.py cta_dow/selection.py tests/test_dow_shadow.py tests/test_dow_selection.py
    git commit -m "feat: add Dow shadow selection"

### Task 5: Implement the active-position portfolio

**Files:**
- Create: cta_dow/backtest.py
- Create: tests/test_dow_backtest.py

- [ ] **Step 1: Write failing active-weight and target tests**

    from cta_dow.backtest import run_backtest

    def test_only_current_positions_share_capital(bundle, shadow_results):
        result = run_backtest(
            bundle=bundle,
            shadows=shadow_results,
            realized_vol_min_observations=2,
        )
        row = result.positions.query("active_products == 2").iloc[0]
        assert row["base_weight_abs"] == 0.5

    def test_monthly_multiplier_targets_fifteen_percent(bundle, shadow_results):
        result = run_backtest(
            bundle=bundle,
            shadows=shadow_results,
            realized_vol_min_observations=2,
        )
        assert result.positions["target_annual_vol"].dropna().eq(0.15).all()

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_backtest.py -q

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

At each month start intersect the shared liquidity universe with eligible_products(trailing_scores(...)). Directions outside this selected set are zero. Pass selected directions to active_weights so only actual positions share capital. Maintain a pre-volatility parallel ledger with active weights and ATR leverage; compute Mul_vol from its previous 252 daily returns and call final_leverage with target_annual_vol=0.15. Apply resulting targets to the actual EventAccount at fill_time.

An exiting product closes at the first month fill; an entering product aligns to its current shadow state. One roll rebalance supplies old and new contract prices together and records two executions. Any target requiring an unpriceable fill fails with product, contract, trade_date, and slot_end.

- [ ] **Step 4: Add causality, month-boundary, and roll tests**

Prove future returns do not alter an earlier multiplier; no-position products are absent from the active denominator; selection exit closes; selection entry aligns; and roll preserves product direction and segment state.

Run: .venv/bin/python -m pytest tests/test_dow_backtest.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add cta_dow/backtest.py tests/test_dow_backtest.py
    git commit -m "feat: backtest the selected Dow portfolio"

### Task 6: Add reports and fidelity variants

**Files:**
- Create: cta_dow/report.py
- Create: tests/test_dow_report.py

- [ ] **Step 1: Write failing report-contract tests**

    from cta_dow.report import FIDELITY_RULE_IDS, write_outputs

    def test_fidelity_contains_every_registered_rule():
        assert FIDELITY_RULE_IDS == (
            "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
            "D1", "D2", "D3", "D4", "D5", "D6", "D7",
        )

    def test_workbook_has_required_sheets(tmp_path, result):
        paths = write_outputs(result, output_prefix=tmp_path / "dow")
        assert pd.ExcelFile(paths.xlsx).sheet_names == [
            "metrics", "daily_returns", "positions", "trades", "signals",
            "universe", "selection", "dominant_rolls", "data_quality",
            "fidelity", "run_config",
        ]

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_report.py -q

Expected: FAIL because report.py is absent.

- [ ] **Step 3: Implement outputs**

Use split_metrics with in_sample_end=date(2022, 7, 29). Write the eleven tested sheets, a PNG with equity/drawdown/leverage and the cutoff, and audit JSON containing bundle hash, commit, options, sheet summaries, plan summaries, fidelity mode, and normalized-content hashes.

Put paper metrics 21.74%, 1.42, 9.90%, 2.20, and 15.34% beside replication metrics. For literal mode set variant=literal_every_bar and status=sensitivity_only. Do not use metric gaps to choose between modes.

- [ ] **Step 4: Run tests and commit**

Run: .venv/bin/python -m pytest tests/test_dow_report.py -q

Expected: PASS.

    git add cta_dow/report.py tests/test_dow_report.py
    git commit -m "feat: report the Dow replication"

### Task 7: Add CLI and literal sensitivity

**Files:**
- Create: cta_dow/__main__.py
- Create: tests/test_dow_cli.py
- Create: docs/operations/guosen-dow-runbook.md
- Modify: README.md
- Modify: docs/ROADMAP.md

- [ ] **Step 1: Write failing option tests**

    from cta_dow.__main__ import build_parser, resolve_options

    def test_paper_defaults(tmp_path):
        ns = build_parser().parse_args([
            "--panel-dir", str(tmp_path), "--start", "2012-01-04",
            "--end", "2026-01-30", "--output-prefix", "output/dow",
        ])
        options = resolve_options(ns)
        assert options.ema_spans == (12, 26, 9)
        assert options.atr_window == 20
        assert options.signal_mode == "latched"
        assert options.target_vol == 0.15
        assert options.cost_bps == 1.3

    def test_cli_rejects_the_session_rule_overrun(tmp_path):
        ns = build_parser().parse_args([
            "--panel-dir", str(tmp_path), "--start", "2012-01-04",
            "--end", "2026-02-01", "--output-prefix", "output/dow",
        ])
        with pytest.raises(SystemExit, match="2026-01-30"):
            resolve_options(ns)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_dow_cli.py -q

Expected: FAIL because __main__.py is absent.

- [ ] **Step 3: Implement CLI**

Required arguments: --panel-dir, --start, --end, --output-prefix. Options: --signal-mode choices latched/literal, --cost-bps default 1.3, --require-paper-faithful, and --run-literal-sensitivity. EMA spans, ATR window, target volatility, selection thresholds, and sample cutoff are fixed constants rather than tuning flags.

main verifies the bundle and requested coverage, runs product shadows, portfolio backtest, and outputs. --run-literal-sensitivity creates a second prefix ending _literal and marks it sensitivity_only.

- [ ] **Step 4: Run fixture CLI and tests**

Run:

    .venv/bin/python -m pytest tests/test_dow_cli.py tests/test_dow_report.py -q
    .venv/bin/python -m cta_dow --panel-dir output/_preflight/commodity-panel --start 2023-01-03 --end 2023-01-31 --output-prefix output/_preflight/dow --require-paper-faithful --run-literal-sensitivity

Expected: tests pass and two complete output triplets are created.

- [ ] **Step 5: Document and commit**

Document formulas, ordered extreme update, latch interpretation, fixed sample date, strict failures, bundle dependency, and sensitivity-only meaning.

    git add cta_dow/__main__.py tests/test_dow_cli.py docs/operations/guosen-dow-runbook.md README.md docs/ROADMAP.md
    git commit -m "feat: run the Guosen Dow replication"

### Task 8: Full-history acceptance

**Files:**
- Create: docs/research/2026-08-27-guosen-dow-replication.md

- [ ] **Step 1: Run all tests**

Run: .venv/bin/python -m pytest -q

Expected: zero failures.

- [ ] **Step 2: Run registered full history**

Run:

    .venv/bin/python -m cta_dow --panel-dir output/commodity-panel-v1 --start 2012-01-04 --end 2026-01-30 --output-prefix output/guosen_dow --require-paper-faithful --run-literal-sensitivity

Expected: default and literal outputs complete without missing-fill or coverage errors.

- [ ] **Step 3: Record acceptance evidence**

Record bundle hash, commit, runtime, sample metrics through 2022-07-29, out-of-sample metrics from 2022-08-01, paper gaps, pricing-basis counts, selected universe history, state rejection counts, and literal-mode impact. Do not change parameters or state rules in response to gaps.

- [ ] **Step 4: Verify and commit**

Run:

    git diff --check
    .venv/bin/python -m pytest -q

Expected: no whitespace errors and zero failures.

    git add docs/research/2026-08-27-guosen-dow-replication.md
    git commit -m "docs: record the Dow replication results"
