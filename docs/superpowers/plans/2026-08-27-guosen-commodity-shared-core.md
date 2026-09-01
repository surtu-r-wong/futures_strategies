# Guosen Commodity Shared Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the commodity-market machinery already built for cta_continuous into a stable shared package, then extend its cached panel for Bollinger and Dow without changing continuous-signal behavior.

**Architecture:** common/commodity owns liquidity universes, commodity dominant contracts, back adjustment, reusable indicators, the cached 15-minute panel, selection scores, allocation, and report primitives. cta_continuous keeps compatibility re-exports. A versioned panel bundle stores bars, universes, dominant choices, roll fills, and input hashes once for all strategies.

**Tech Stack:** Python 3.13, pandas, NumPy, fastparquet, psycopg2, matplotlib, pytest, common.minute, common.leverage.

---

**Dependency:** Complete this plan before either strategy implementation plan.

### Task 1: Capture the behavior-preservation baseline

**Files:**
- Read: cta_continuous/universe.py
- Read: cta_continuous/continuous.py
- Read: cta_continuous/indicators.py
- Read: cta_continuous/panel.py
- Create: docs/research/2026-08-27-commodity-core-migration-baseline.md

- [ ] **Step 1: Verify the starting tree and focused tests**

Run:

    git status --short
    .venv/bin/python -m pytest tests/test_continuous_universe.py tests/test_continuous_roll.py tests/test_continuous_indicators.py tests/test_continuous_panel.py -q

Expected: no unexpected edits and zero failures.

- [ ] **Step 2: Produce and record a deterministic panel digest**

Run:

    .venv/bin/python scripts/continuous/2026-08-27-panel-smoke.py
    cp output/continuous/panel_smoke.parquet /tmp/commodity_core_before.parquet
    .venv/bin/python -c "import hashlib,pandas as pd; f=pd.read_parquet('/tmp/commodity_core_before.parquet').sort_values(['product','slot_end']).reset_index(drop=True); print(len(f), hashlib.sha256(f.to_csv(index=False).encode()).hexdigest())"
    git rev-parse HEAD

Expected: a positive row count, one SHA-256 digest, and the baseline commit. Put all three in the research note.

- [ ] **Step 3: Commit**

    git add docs/research/2026-08-27-commodity-core-migration-baseline.md
    git commit -m "docs: capture commodity core migration baseline"

### Task 2: Move the liquidity universe behind a compatibility surface

**Files:**
- Create: common/commodity/__init__.py
- Move: cta_continuous/universe.py -> common/commodity/universe.py
- Create: cta_continuous/universe.py
- Create: tests/test_commodity_universe.py
- Modify: tests/test_continuous_universe.py

- [ ] **Step 1: Write the failing compatibility test**

    from common.commodity import universe as shared
    from cta_continuous import universe as legacy

    def test_continuous_universe_is_a_compatibility_surface():
        assert legacy.universe_for_month is shared.universe_for_month
        assert legacy.product_daily_turnover is shared.product_daily_turnover
        assert legacy.canonical_contract is shared.canonical_contract
        assert legacy.TURNOVER_THRESHOLD == 5e9

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_universe.py -q

Expected: FAIL because common.commodity does not exist.

- [ ] **Step 3: Move the implementation and create the wrapper**

Run:

    mkdir -p common/commodity
    git mv cta_continuous/universe.py common/commodity/universe.py

Create common/commodity/__init__.py:

    """Shared commodity-futures replication machinery."""

Create cta_continuous/universe.py:

    """Compatibility exports for the continuous-signal strategy."""

    from common.commodity.universe import (
        FINANCIAL_FUTURES,
        LOOKBACK_MONTHS,
        TURNOVER_THRESHOLD,
        canonical_contract,
        product_daily_turnover,
        universe_for_month,
    )

    __all__ = [
        "FINANCIAL_FUTURES", "LOOKBACK_MONTHS", "TURNOVER_THRESHOLD",
        "canonical_contract", "product_daily_turnover", "universe_for_month",
    ]

Move all behavioral tests to tests/test_commodity_universe.py and leave only the identity assertions in tests/test_continuous_universe.py.

- [ ] **Step 4: Run tests**

Run: .venv/bin/python -m pytest tests/test_commodity_universe.py tests/test_continuous_universe.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add common/commodity cta_continuous/universe.py tests/test_commodity_universe.py tests/test_continuous_universe.py
    git commit -m "refactor: share the commodity liquidity universe"

### Task 3: Split commodity dominant selection from adjustment

**Files:**
- Create: common/commodity/dominant.py
- Create: common/commodity/continuous.py
- Replace: cta_continuous/continuous.py
- Create: tests/test_commodity_dominant.py
- Create: tests/test_commodity_adjustment.py
- Modify: tests/test_continuous_roll.py

- [ ] **Step 1: Write the failing public-surface test**

    from common.commodity.continuous import adjustment_factors, continuous_close
    from common.commodity.dominant import choose_dominant_commodity, delivery_month
    from cta_continuous import continuous as legacy

    def test_continuous_roll_exports_are_shared_objects():
        assert legacy.choose_dominant_commodity is choose_dominant_commodity
        assert legacy.adjustment_factors is adjustment_factors
        assert legacy.continuous_close is continuous_close
        assert legacy.delivery_month is delivery_month

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_continuous_roll.py::test_continuous_roll_exports_are_shared_objects -q

Expected: FAIL because the shared modules are absent.

- [ ] **Step 3: Move exact responsibilities**

Move DOMINANT_SELECTION_LAG, delivery_month, choose_dominant_commodity, and _both_max to common/commodity/dominant.py. Move adjustment_factors and continuous_close to common/commodity/continuous.py. Do not alter function bodies in this task.

Replace cta_continuous/continuous.py:

    """Compatibility exports for shared commodity roll logic."""

    from common.commodity.continuous import adjustment_factors, continuous_close
    from common.commodity.dominant import (
        DOMINANT_SELECTION_LAG,
        choose_dominant_commodity,
        delivery_month,
    )

    __all__ = [
        "DOMINANT_SELECTION_LAG", "adjustment_factors",
        "choose_dominant_commodity", "continuous_close", "delivery_month",
    ]

Split current behavioral tests between the two new test files; retain the identity test in tests/test_continuous_roll.py.

- [ ] **Step 4: Run tests**

Run: .venv/bin/python -m pytest tests/test_commodity_dominant.py tests/test_commodity_adjustment.py tests/test_continuous_roll.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add common/commodity cta_continuous/continuous.py tests/test_commodity_dominant.py tests/test_commodity_adjustment.py tests/test_continuous_roll.py
    git commit -m "refactor: split commodity dominant and adjustment logic"

### Task 4: Share indicators and parameterize target volatility

**Files:**
- Create: common/commodity/indicators.py
- Modify: cta_continuous/indicators.py
- Modify: common/leverage.py
- Create: tests/test_commodity_indicators.py
- Modify: tests/test_continuous_indicators.py
- Modify: tests/test_common_leverage.py

- [ ] **Step 1: Write failing tests**

    import pytest
    from common.commodity.indicators import atr_series, ema, true_range
    from common.leverage import final_leverage

    def test_final_leverage_accepts_a_ten_percent_target():
        got = final_leverage(
            close=100.0, atr=1.0, realized_vol=0.20, target_annual_vol=0.10
        )
        assert got == pytest.approx(0.25)

    def test_final_leverage_keeps_the_fifteen_percent_default():
        assert final_leverage(
            close=100.0, atr=1.0, realized_vol=0.20
        ) == pytest.approx(0.375)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_indicators.py tests/test_common_leverage.py -q

Expected: FAIL on missing module and keyword.

- [ ] **Step 3: Move functions and implement the keyword**

Move _as_float_array, ema, true_range, and atr_series unchanged into common/commodity/indicators.py. Re-export them from cta_continuous/indicators.py; keep gap_widening, tnr_series, and delta_tnr there.

Change final_leverage to this signature and calculation while preserving its current validation:

    def final_leverage(
        *,
        close: float,
        atr: float | None,
        realized_vol: float | None,
        target_annual_vol: float = TARGET_ANNUAL_VOL,
    ) -> float:
        if realized_vol is None:
            return 0.0
        if not math.isfinite(realized_vol) or realized_vol <= 0:
            raise ValueError(
                f"realized volatility must be finite and positive; got {realized_vol!r}"
            )
        if not math.isfinite(target_annual_vol) or target_annual_vol <= 0:
            raise ValueError(
                f"target annual volatility must be finite and positive; got {target_annual_vol!r}"
            )
        scaled = atr_leverage(close=close, atr=atr)
        scaled *= target_annual_vol / realized_vol
        return min(scaled, MAX_LEVERAGE)

- [ ] **Step 4: Run dependent tests**

Run: .venv/bin/python -m pytest tests/test_commodity_indicators.py tests/test_continuous_indicators.py tests/test_common_leverage.py tests/test_index_open_momentum_leverage.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add common/commodity/indicators.py common/leverage.py cta_continuous/indicators.py tests/test_commodity_indicators.py tests/test_continuous_indicators.py tests/test_common_leverage.py
    git commit -m "refactor: share commodity indicators and volatility targets"

### Task 5: Move and extend the cached panel

**Files:**
- Move: cta_continuous/panel.py -> common/commodity/panel.py
- Create: cta_continuous/panel.py
- Create: tests/commodity_fixtures.py
- Create: tests/test_commodity_panel.py
- Modify: tests/test_continuous_panel.py

- [ ] **Step 1: Write failing schema tests**

Create tests/commodity_fixtures.py with a day-only SHFE SessionRule, its
timezone-aware slots/buckets, and a minute_frame fixture containing twenty
consecutive RB2405.SHF rows. Every row has finite OHLC, volume=1,
open_interest increasing from 100, amount=price*volume*10, and trade_date
matching the session. Register it from test_commodity_panel.py with:

    pytest_plugins = ("tests.commodity_fixtures",)

    import pandas as pd
    from common.commodity.panel import PANEL_COLUMNS, build_session_bars

    def test_panel_carries_oi_and_actual_fill_time(session, minute_frame):
        rows = build_session_bars(
            minute_frame,
            slots=session.slots,
            buckets=session.buckets,
            contract="RB2405.SHF",
            multiplier=10,
        )
        assert "open_interest" in PANEL_COLUMNS
        assert "fill_time" in PANEL_COLUMNS
        traded = minute_frame.loc[
            minute_frame["bar_time"].isin(session.buckets[0])
            & (minute_frame["volume"] > 0)
        ]
        assert rows[0]["open_interest"] == traded["open_interest"].iloc[-1]
        assert pd.Timestamp(rows[0]["fill_time"]) > pd.Timestamp(rows[0]["slot_end"])

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_panel.py -q

Expected: FAIL because shared panel and fields do not exist.

- [ ] **Step 3: Move and extend**

Run: git mv cta_continuous/panel.py common/commodity/panel.py

Add open_interest after volume and fill_time before fill_price in PANEL_COLUMNS. In build_session_bars use:

    traded_bucket = slot_frame(frame, bucket)
    traded_bucket = traded_bucket.loc[traded_bucket["volume"] > 0]
    open_interest = (
        None if traded_bucket.empty
        else float(traded_bucket["open_interest"].iloc[-1])
    )
    fill_time = None if pending else window[-1]

When resolve_pending_fill prices a prior final bar, also replace fill_time with the last slot of the next session opening window. Normalize open_interest as float and fill_time as timezone-aware datetime. Create cta_continuous/panel.py as explicit re-exports of the shared module's public names.

- [ ] **Step 4: Run migration and Parquet tests**

Run: .venv/bin/python -m pytest tests/test_commodity_panel.py tests/test_continuous_panel.py -q

Expected: PASS, including timezone-preserving Parquet round trip.

- [ ] **Step 5: Commit**

    git add common/commodity/panel.py cta_continuous/panel.py tests/commodity_fixtures.py tests/test_commodity_panel.py tests/test_continuous_panel.py
    git commit -m "refactor: share and extend the commodity minute panel"

### Task 6: Add a versioned panel bundle and roll fills

**Files:**
- Create: common/commodity/bundle.py
- Create: scripts/commodity/build_panel.py
- Modify: scripts/continuous/build_panel.py
- Modify: tests/commodity_fixtures.py
- Create: tests/test_commodity_bundle.py

- [ ] **Step 1: Write failing round-trip and tamper tests**

Extend tests/commodity_fixtures.py with bundle_frames: bars contains two
products, two dates, one contract roll, finite OI/fill fields, and both
amount_vwap and ohlc_typical pricing bases; universes contains the two month
members; dominants contains the old/new contract chain; roll_fills contains
both prices for the one roll. All four frames use exactly the production
column names declared in this plan.

    from common.commodity.bundle import PanelBundle, read_bundle, write_bundle

    def test_bundle_round_trip_preserves_all_tables(tmp_path, bundle_frames):
        write_bundle(tmp_path, **bundle_frames)
        loaded = read_bundle(tmp_path)
        assert isinstance(loaded, PanelBundle)
        assert loaded.bars.equals(bundle_frames["bars"])
        assert loaded.roll_fills.equals(bundle_frames["roll_fills"])

    def test_bundle_refuses_a_changed_table(tmp_path, bundle_frames):
        write_bundle(tmp_path, **bundle_frames)
        (tmp_path / "bars.parquet").write_bytes(b"tampered")
        with pytest.raises(ValueError, match="bundle_digest_mismatch"):
            read_bundle(tmp_path)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_bundle.py -q

Expected: FAIL because bundle.py is absent.

- [ ] **Step 3: Implement the bundle contract**

    @dataclass(frozen=True, slots=True)
    class PanelBundle:
        bars: pd.DataFrame
        universes: pd.DataFrame
        dominants: pd.DataFrame
        roll_fills: pd.DataFrame
        manifest: dict[str, object]

    BUNDLE_VERSION = 1
    TABLE_FILES = {
        "bars": "bars.parquet",
        "universes": "universes.parquet",
        "dominants": "dominants.parquet",
        "roll_fills": "roll_fills.parquet",
    }

write_bundle normalizes frames, writes each table, hashes file bytes with SHA-256, and atomically replaces manifest.json. read_bundle requires bundle_version 1 and verifies every digest before reading.

The roll_fills schema is:

    trade_date, product, old_contract, new_contract, fill_time,
    old_price, new_price, old_pricing_basis, new_pricing_basis

scripts/commodity/build_panel.py builds the four tables. On a dominant change it requests both concrete contracts for the next session's first five slots and fails if either price is unavailable. scripts/continuous/build_panel.py becomes a compatibility entry point calling the shared builder.

- [ ] **Step 4: Run tests and a small PG smoke**

Run:

    .venv/bin/python -m pytest tests/test_commodity_bundle.py tests/test_commodity_panel.py tests/test_continuous_panel.py -q
    .venv/bin/python scripts/commodity/build_panel.py --start 2023-01-03 --end 2023-01-31 --output-dir output/_preflight/commodity-panel

Expected: tests pass; manifest and four Parquet files exist; no requested fill is unpriceable.

- [ ] **Step 5: Commit**

    git add common/commodity/bundle.py scripts/commodity scripts/continuous/build_panel.py tests/test_commodity_bundle.py
    git commit -m "feat: cache a versioned commodity panel bundle"

### Task 7: Add monthly scores and allocation policies

**Files:**
- Create: common/commodity/selection.py
- Create: common/commodity/portfolio.py
- Create: tests/test_commodity_selection.py
- Create: tests/test_commodity_portfolio.py

- [ ] **Step 1: Write failing tests**

    from common.commodity.portfolio import active_weights, fixed_universe_weights
    from common.commodity.selection import trailing_scores

    def test_fixed_universe_keeps_inactive_capital_in_cash():
        assert fixed_universe_weights(
            {"RB": 1, "CU": 0}, universe=("RB", "CU")
        ) == {"RB": 0.5, "CU": 0.0}

    def test_active_weights_reallocate_only_across_positions():
        assert active_weights({"RB": 1, "CU": 0, "AL": -1}) == {
            "RB": 0.5, "CU": 0.0, "AL": -0.5,
        }

    def test_scores_stop_before_the_current_month(shadow_daily, shadow_trades):
        scores = trailing_scores(
            month_start=date(2024, 3, 1),
            daily=shadow_daily,
            trades=shadow_trades,
            observations=252,
        )
        assert scores["RB"].last_observation < date(2024, 3, 1)

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_selection.py tests/test_commodity_portfolio.py -q

Expected: FAIL because the modules are absent.

- [ ] **Step 3: Implement exact types and weights**

    @dataclass(frozen=True, slots=True)
    class ProductScore:
        product: str
        first_observation: date
        last_observation: date
        observations: int
        trade_count: int
        cumulative_return: float
        sharpe: float
        max_drawdown: float
        calmar: float

    def fixed_universe_weights(directions, *, universe):
        outsiders = set(directions) - set(universe)
        if outsiders:
            raise ValueError(f"allocation_outside_universe: {sorted(outsiders)}")
        unit = 0.0 if not universe else 1.0 / len(universe)
        return {
            product: float(directions.get(product, 0)) * unit
            for product in universe
        }

    def active_weights(directions):
        active = [p for p, side in directions.items() if side]
        unit = 0.0 if not active else 1.0 / len(active)
        return {
            product: float(side) * unit if side else 0.0
            for product, side in directions.items()
        }

trailing_scores takes the final 252 rows per product strictly before
month_start, compounds net_return, counts trades by exit_date in the same
window, and rejects duplicate (product, trade_date) keys. Call
common.metrics.summarize(returns, periods_per_year=252), map ann_return to
annual_return and ann_vol to annual_volatility, set cumulative_return to
cumulative_equity(returns).iloc[-1] - 1, and set Calmar to
ann_return / max_drawdown when max_drawdown is positive (otherwise NaN).

- [ ] **Step 4: Run tests**

Run: .venv/bin/python -m pytest tests/test_commodity_selection.py tests/test_commodity_portfolio.py tests/test_metrics.py -q

Expected: PASS.

- [ ] **Step 5: Commit**

    git add common/commodity/selection.py common/commodity/portfolio.py tests/test_commodity_selection.py tests/test_commodity_portfolio.py
    git commit -m "feat: add commodity selection and allocation primitives"

### Task 8: Add shared report primitives

**Files:**
- Create: common/commodity/reporting.py
- Create: tests/test_commodity_reporting.py

- [ ] **Step 1: Write failing tests**

    from common.commodity.reporting import fidelity_frame, split_metrics

    def test_metrics_split_without_overlapping_the_cutoff(daily_returns):
        rows = split_metrics(daily_returns, in_sample_end=date(2021, 9, 30))
        assert list(rows["period"]) == ["full", "in_sample", "out_of_sample"]
        assert rows.loc[
            rows["period"] == "out_of_sample", "start"
        ].item() > date(2021, 9, 30)

    def test_fidelity_rejects_duplicate_rule_ids():
        with pytest.raises(ValueError, match="fidelity_duplicate_rule"):
            row = {
                "rule_id": "F1",
                "paper_text": "主力合约",
                "implementation": "lag one session",
                "basis": "causality",
                "status": "assumption",
                "variant": "none",
                "impact": "reported",
            }
            fidelity_frame([row, row])

- [ ] **Step 2: Verify failure**

Run: .venv/bin/python -m pytest tests/test_commodity_reporting.py -q

Expected: FAIL because reporting.py is absent.

- [ ] **Step 3: Implement**

split_metrics validates unique sorted trade_date and calls
common.metrics.summarize(series, periods_per_year=252) for full, in_sample,
and out_of_sample. Map ann_return and ann_vol to the public column names and
compute Calmar as ann_return / max_drawdown when drawdown is positive. Return
period,start,end,annual_return,annual_volatility,sharpe,max_drawdown,calmar.

fidelity_frame requires rule_id,paper_text,implementation,basis,status,variant,impact; it rejects blank and duplicate rule IDs and sorts by rule_id.

- [ ] **Step 4: Run tests and commit**

Run: .venv/bin/python -m pytest tests/test_commodity_reporting.py tests/test_metrics.py -q

Expected: PASS.

    git add common/commodity/reporting.py tests/test_commodity_reporting.py
    git commit -m "feat: add shared commodity report primitives"

### Task 9: Verify equivalence and document operations

**Files:**
- Modify: README.md
- Modify: docs/ROADMAP.md
- Create: docs/operations/commodity-panel-bundle.md
- Modify: docs/research/2026-08-27-commodity-core-migration-baseline.md

- [ ] **Step 1: Run all tests**

Run: .venv/bin/python -m pytest -q

Expected: zero failures.

- [ ] **Step 2: Compare old columns against the baseline**

Run:

    .venv/bin/python scripts/continuous/2026-08-27-panel-smoke.py
    cp output/continuous/panel_smoke.parquet /tmp/commodity_core_after.parquet
    .venv/bin/python -c "import hashlib,pandas as pd; a=pd.read_parquet('/tmp/commodity_core_before.parquet'); b=pd.read_parquet('/tmp/commodity_core_after.parquet'); old=list(a.columns); assert a[old].equals(b[old]); print(len(b), hashlib.sha256(b[old].to_csv(index=False).encode()).hexdigest())"

Expected: old columns are content-identical and the digest matches Task 1. New open_interest and fill_time columns are intentionally outside the old-column comparison.

- [ ] **Step 3: Document the bundle**

Document four tables, manifest hashes, build command, 2026-01-30 session upper bound, CZCE typical-price basis, and the rule that any changed input digest or bundle version requires a full rebuild.

- [ ] **Step 4: Final verification and commit**

Run:

    git diff --check
    .venv/bin/python -m pytest -q

Expected: no whitespace errors and zero failures.

    git add README.md docs/ROADMAP.md docs/operations/commodity-panel-bundle.md docs/research/2026-08-27-commodity-core-migration-baseline.md
    git commit -m "docs: operate the shared commodity panel bundle"
