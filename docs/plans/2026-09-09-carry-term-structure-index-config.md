# Carry Term-Structure Index Configuration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the daily Carry engine run the CITIC term-structure index configuration (near/far leg, rank-linear weights, no stops, OI-value liquidity pool, product exclusions, deferred execution on missing opens) with every default bit-identical to today's baseline.

**Architecture:** Five opt-in `CarryConfig` fields plus one CLI-only exclusion list. Each field defaults to current behaviour, so the golden fixture test (`tests/test_carry_backtest.py::test_shared_decision_extraction_preserves_all_daily_result_tables`) passes without regenerating the pickle. Signal ranking stays in `signals.py`, sizing in `decision.py`, execution policy in `backtest.py`. Design: `docs/plans/2026-09-09-carry-term-structure-index-config-design.md`.

**Tech Stack:** Python 3.13, pandas, pytest. Run tests with `/home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest <file> -q -p no:cacheprovider` from the worktree root.

**Invariants to keep in mind:**
- New `CarryConfig` fields must have behaviour-preserving defaults and a same-name CLI flag with `default=None` (`__main__.py::_config_from_args` reflects over dataclass fields).
- Do not add columns to `signals`, `curve_selection` (audit) or `data_quality` frames on the default path; the golden test compares them `check_exact=True`.
- `data.py::_valid_ohlc` already drops bars whose open is null, so a "missing open" reaches the engine as a contract absent from that day's price map.
- Every test assertion is a hand-computed literal.

---

### Task 1: Config fields

**Files:**
- Modify: `cta_carry/config.py`
- Test: `tests/test_carry_config.py`

**Step 1: Write the failing tests**

```python
def test_index_configuration_fields_default_to_baseline_behaviour() -> None:
    config = CarryConfig()

    assert config.near_leg == "main"
    assert config.weighting == "risk_budget"
    assert config.stop_loss_enabled is True
    assert config.liquidity_measure == "turnover"
    assert config.missing_open_policy == "abort"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("near_leg", "far"),
        ("weighting", "equal"),
        ("liquidity_measure", "oi"),
        ("missing_open_policy", "fill"),
        ("stop_loss_enabled", 1),
    ],
)
def test_index_configuration_fields_reject_unknown_values(field, value) -> None:
    with pytest.raises(ValueError, match=field):
        CarryConfig(**{field: value})
```

**Step 2: Run to verify failure** — `pytest tests/test_carry_config.py -q` → FAIL (`unexpected keyword` / attribute missing).

**Step 3: Implement** in `config.py`: add the constants

```python
_NEAR_LEGS = frozenset({"main", "near_dominant"})
_WEIGHTINGS = frozenset({"risk_budget", "rank_linear"})
_LIQUIDITY_MEASURES = frozenset({"turnover", "open_interest_value"})
_MISSING_OPEN_POLICIES = frozenset({"abort", "defer"})
```

and the fields (after `equal_weight_capital`, before `prewarm_calendar_days`), each with a comment stating the default reproduces the baseline:

```python
    near_leg: str = "main"
    weighting: str = "risk_budget"
    stop_loss_enabled: bool = True
    liquidity_measure: str = "turnover"
    missing_open_policy: str = "abort"
```

Validation in `__post_init__` mirrors `secondary_selection` (`"near_leg must be one of ..."`), and `type(self.stop_loss_enabled) is not bool` → `ValueError("stop_loss_enabled must be a bool")`.

**Step 4: Run** `pytest tests/test_carry_config.py -q` → PASS. **Step 5: Commit** `feat(carry): config fields for the term-structure index configuration`.

---

### Task 2: CLI flags and `--exclude-products`

**Files:**
- Modify: `cta_carry/__main__.py` (`build_parser`, `_runtime_config`, `main`)
- Modify: `cta_carry/pg_source.py` (`load_public_carry_data`, `_contract_query`)
- Test: `tests/test_carry_report_cli.py`, `tests/test_carry_pg_source.py`

**Step 1: Failing tests**

`tests/test_carry_pg_source.py`:
```python
def test_contract_query_unions_user_exclusions_with_financials():
    _, params = _contract_query(
        query_start=date(2022, 1, 1),
        end=date(2024, 1, 1),
        products=None,
        excluded_products=["cu", " AL", "cu"],
    )

    assert params["excluded_products"] == sorted(FINANCIAL_FUTURES | {"AL", "CU"})
```

`tests/test_carry_report_cli.py`:
```python
def test_cli_index_flags_override_config_and_exclusions_parse():
    args = build_parser().parse_args(
        [
            "--start", "2020-01-01", "--end", "2020-12-31",
            "--near-leg", "near_dominant", "--weighting", "rank_linear",
            "--no-stop-loss", "--liquidity-measure", "open_interest_value",
            "--missing-open-policy", "defer", "--exclude-products", "cu, al,cu",
        ]
    )

    config = _config_from_args(args)
    assert config.near_leg == "near_dominant"
    assert config.weighting == "rank_linear"
    assert config.stop_loss_enabled is False
    assert config.liquidity_measure == "open_interest_value"
    assert config.missing_open_policy == "defer"
    assert _parse_products(args.exclude_products) == ["AL", "CU"]
```

**Step 3: Implement**
- Parser: `--near-leg` choices `["main","near_dominant"]`; `--weighting` choices `["risk_budget","rank_linear"]`; `--no-stop-loss` (`dest="stop_loss_enabled", action="store_false", default=None`); `--liquidity-measure` choices; `--missing-open-policy` choices; `--exclude-products` (help "comma-separated product codes to drop before the pool").
- `main`: `excluded = _parse_products(args.exclude_products)`; pass to `load_public_carry_data(..., excluded_products=excluded)`; for `--source files`, drop those products from `data.prices` via `data.slice(products=...)`? `slice` only accepts an include list — instead filter: `data = CarryDataSet(prices=data.prices.loc[~data.prices["product"].isin(excluded)], data_quality=data.data_quality)` when `excluded`.
- `_runtime_config(..., excluded_products)`: add row `{"key": "excluded_products", "value": ",".join(excluded) if excluded else "NONE"}`.
- `pg_source._contract_query(..., excluded_products=None)`: `params["excluded_products"] = sorted(FINANCIAL_FUTURES | normalized_exclusions)`.

**Step 4: Run** both test files → PASS (the two parity tests must still pass). **Step 5: Commit** `feat(carry): CLI switches for the index configuration and a product exclusion list`.

---

### Task 3: Open-interest-value liquidity measure

**Files:**
- Modify: `cta_carry/curve.py::aggregate_product_liquidity`
- Test: `tests/test_carry_curve.py`

**Step 1: Failing test**

```python
def test_open_interest_value_pool_infers_multiplier_from_turnover_history() -> None:
    dates = pd.bdate_range("2024-01-02", periods=3).date.tolist()
    # turnover / (volume * close) = 1_000 / (100 * 10) = 1.0 on day 1;
    # day 2 ratio 3.0 -> expanding median 2.0; day 3 ratio 1.0 -> median 1.0.
    ratios = (1.0, 3.0, 1.0)
    prices = _prices(
        [
            _bar(
                trade_date,
                contract,
                close=10.0,
                volume=100.0,
                oi=oi,
                turnover=1_000.0 * ratio,
            )
            for trade_date, ratio in zip(dates, ratios)
            for contract, oi in (("RB2405.SHF", 300.0), ("RB2410.SHF", 200.0))
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
        liquidity_measure="open_interest_value",
    )

    liquidity = aggregate_product_liquidity(prices, config)

    # sum(oi * close * multiplier): 500 * 10 * {1.0, 2.0, 1.0}
    assert liquidity["product_turnover"].tolist() == [5_000.0, 10_000.0, 5_000.0]
    assert liquidity["liquidity_mean"].tolist()[1:] == [5_000.0, 10_000.0]
```

Also assert the default measure is untouched by re-running the existing `test_liquidity_pool_uses_complete_shifted_product_history` (no change needed).

**Step 3: Implement**: at the top of `aggregate_product_liquidity`, build the per-row measure:

```python
    measure = prices.loc[:, ["product", "trade_date", "contract", "turnover"]].copy()
    if config.liquidity_measure == "open_interest_value":
        # No multiplier metadata on the daily path: infer it per product as the
        # expanding median of turnover / (volume * close) over rows that traded,
        # so the estimate at day t only uses bars up to t.
        ordered = prices.sort_values(["product", "trade_date", "contract"], kind="mergesort")
        traded = (ordered["volume"] > 0) & (ordered["turnover"] > 0)
        ratio = (ordered["turnover"] / (ordered["volume"] * ordered["close"])).where(traded)
        multiplier = ratio.groupby(ordered["product"], sort=False).transform(
            lambda values: values.expanding(min_periods=1).median()
        )
        measure = ordered.loc[:, ["product", "trade_date", "contract"]].copy()
        measure["turnover"] = ordered["oi"] * ordered["close"] * multiplier
```

then group `measure` (instead of `prices`) by `["product","trade_date"]` summing `turnover` with `min_count=1` so an all-NaN day stays NaN (`.sum(min_count=1)` via `agg`). Rest unchanged. Keep the column name `product_turnover` (schema stability; docstring notes it holds the configured measure).

**Step 4: Run** `pytest tests/test_carry_curve.py -q` → PASS. **Step 5: Commit** `feat(carry): open-interest-value liquidity pool with a multiplier inferred from turnover`.

---

### Task 4: Near-dominant leg

**Files:**
- Modify: `cta_carry/curve.py` (`_CURVE_COLUMNS`, `build_curve`)
- Test: `tests/test_carry_curve.py` (update `CURVE_COLUMNS` list: insert `"near_contract"` after `"secondary_contract"` and `"near_close"` after `"secondary_close"`)

**Step 1: Failing tests**

```python
def test_near_dominant_leg_picks_the_earlier_highest_oi_contract() -> None:
    dates = pd.bdate_range("2024-01-02", periods=1).date.tolist()
    contracts = (
        ("RB2401.SHF", 100.0, 999.0, 150.0),   # earlier, lower OI
        ("RB2403.SHF", 104.0, 100.0, 200.0),   # earlier, highest OI -> near
        ("RB2405.SHF", 110.0, 200.0, 300.0),   # main
        ("RB2409.SHF", 120.0, 300.0, 250.0),   # far
    )
    prices = _prices([_bar(dates[0], c, close=close, volume=v, oi=oi) for c, close, v, oi in contracts])
    config = CarryConfig(liquidity_window=1, liquidity_threshold=0.0, carry_window=1, near_leg="near_dominant")

    result = build_curve(prices, config)
    row = result.curve.iloc[0]

    assert row["main_contract"] == "RB2405.SHF"
    assert row["near_contract"] == "RB2403.SHF"
    assert row["secondary_contract"] == "RB2409.SHF"
    assert row["month_gap"] == 6                       # 2403 -> 2409
    # (near - far) / near / months * 12 = (104 - 120) / 104 / 6 * 12
    assert row["carry_raw"] == pytest.approx((104.0 - 120.0) / 104.0 / 6 * 12)
    audit = result.audit.set_index("contract")
    assert audit.loc["RB2403.SHF", "role"] == "near"
    assert audit.loc["RB2403.SHF", "reason"] == "earlier_highest_oi"


def test_near_dominant_leg_falls_back_to_the_main_contract() -> None:
    dates = pd.bdate_range("2024-01-02", periods=1).date.tolist()
    contracts = (("RB2405.SHF", 110.0, 200.0, 300.0), ("RB2409.SHF", 120.0, 300.0, 250.0))
    prices = _prices([_bar(dates[0], c, close=close, volume=v, oi=oi) for c, close, v, oi in contracts])
    config = CarryConfig(liquidity_window=1, liquidity_threshold=0.0, carry_window=1, near_leg="near_dominant")

    row = build_curve(prices, config).curve.iloc[0]

    assert row["near_contract"] == "RB2405.SHF"
    assert row["carry_raw"] == pytest.approx((110.0 - 120.0) / 110.0 / 4 * 12)


def test_default_leg_records_main_as_near_and_keeps_far_denominator() -> None:
    # reuse the contracts of test_curve_selects_later_secondary_after_oi_and_volume_ranking
    ...
    assert row["near_contract"] == row["main_contract"]
    assert row["carry_raw"] == pytest.approx((110.0 / 120.0 - 1.0) * 12 / 3)
```

**Step 3: Implement** in `build_curve` after `secondary` is chosen:

```python
        near = main
        if config.near_leg == "near_dominant":
            earlier = ranked.loc[ranked["delivery_yyyymm"] < main["delivery_yyyymm"]]
            if not earlier.empty:
                near = earlier.iloc[0]
        if config.near_leg == "near_dominant":
            month_gap = _month_gap(near["delivery_yyyymm"], secondary["delivery_yyyymm"])
            # CITIC definition: (near - far) / near / months, annualised.
            carry_raw = (near["close"] - secondary["close"]) / near["close"] * 12.0 / month_gap
        else:
            month_gap = _month_gap(main["delivery_yyyymm"], secondary["delivery_yyyymm"], allow_earlier=second_by_oi)
            carry_raw = (main["close"] / secondary["close"] - 1.0) * 12.0 / month_gap
```

Add `"near_contract": near["contract"], "near_close": near["close"]` to the curve row; in the audit loop, before the `secondary` branch, `elif candidate.contract == near["contract"] and near["contract"] != main["contract"]: role, selected, reason = "near", True, "earlier_highest_oi"`. Extend `_CURVE_COLUMNS`.

Check `report.py::curve_selection_excel_view`: the `included_reasons` set becomes `{"highest_oi", "later_highest_oi", "earlier_highest_oi"}` so the near contract is not reported as an exclusion. Default path unaffected (no `near` rows).

**Step 4: Run** `pytest tests/test_carry_curve.py tests/test_carry_report_cli.py -q` → PASS. **Step 5: Commit** `feat(carry): near-dominant leg for the CITIC term-structure definition`.

---

### Task 5: Rank-linear weighting

**Files:**
- Modify: `cta_carry/signals.py` (new helper + branch in `build_signals`)
- Modify: `cta_carry/decision.py::plan_signal_targets`
- Test: `tests/test_carry_signals.py`, `tests/test_carry_decision.py`

**Step 1: Failing tests**

`tests/test_carry_signals.py`:
```python
def test_rank_linear_weights_are_centred_and_sum_to_zero() -> None:
    from cta_carry.signals import rank_linear_weights
    ready = pd.DataFrame({"product": list("ABCDE"), "carry_ma": [-0.3, 0.4, -0.1, 0.2, 0.05]})

    weights = rank_linear_weights(ready)

    # ascending ranks: A=1, C=2, E=3, D=4, B=5; centre 3; denominator 5*6/2 = 15
    assert weights.tolist() == pytest.approx([-2 / 15, 2 / 15, -1 / 15, 1 / 15, 0.0])


def test_rank_linear_signals_direct_every_ready_product_without_a_sign_gate() -> None:
    result = build_signals(
        _two_day_cross_section({"A": -0.3, "B": 0.4, "C": -0.1, "D": 0.2, "E": 0.05}),
        _config(weighting="rank_linear", trend_filter_enabled=False),
    )
    latest = _latest_by_product(result)

    assert [latest[p]["rank_direction"] for p in "ABCDE"] == [-1, 1, -1, 1, 0]
    assert [latest[p]["effective_direction"] for p in "ABCDE"] == [-1, 1, -1, 1, 0]
    assert latest["A"]["reason"] == "rank_linear"
```
(check `_two_day_cross_section` accepts a dict — read the helper first; adapt to its signature.)

`tests/test_carry_decision.py`:
```python
def test_rank_linear_sizing_ignores_atr_budget_and_uses_rank_weight_times_strength() -> None:
    config = CarryConfig(weighting="rank_linear", trend_filter_enabled=False)
    rows = pd.DataFrame([...five signal rows with input_ready=True, effective_direction from ranks, strength 1.0, atr 2.0, main_close 100...])

    plan = plan_signal_targets({}, rows, config)

    assert plan.raw_weights == pytest.approx({"A2405": -2 / 15, "B2405": 2 / 15, "C2405": -1 / 15, "D2405": 1 / 15})
```

**Step 3: Implement**

`signals.py`:
```python
def rank_linear_weights(ready: pd.DataFrame) -> pd.Series:
    """CITIC 3.5 step 3: w = (rank - (1+N)/2) / (N(1+N)/2), ascending carry_ma."""
    count = len(ready)
    rank = ready["carry_ma"].rank(method="first", ascending=True)
    return (rank - (1 + count) / 2) / (count * (1 + count) / 2)
```
(ties broken by product order: sort `ready` by `["carry_ma","product"]` before calling — `build_signals` already does.)

In `build_signals`, inside the daily loop after the `len(ready) < 5` check:
```python
        if config.weighting == "rank_linear":
            signals.loc[daily.index, "reason"] = "rank_linear"
            weights = rank_linear_weights(ready)
            signals.loc[ready.index, "rank_direction"] = np.sign(weights).astype(int)
        else:
            ... existing top/bottom selection ...
```
The trend-filter block below is unchanged (it reads `rank_direction`).

`decision.py::plan_signal_targets`: before the product loop,
```python
    rank_weights: dict[str, float] = {}
    if config.weighting == "rank_linear":
        ready = signal_rows.loc[signal_rows["input_ready"]].sort_values(["carry_ma", "product"], kind="mergesort")
        if len(ready) >= 5:
            rank_weights = dict(zip(ready["product"], rank_linear_weights(ready)))
```
and in the sizing branch:
```python
            if config.weighting == "rank_linear":
                raw_weights[after.contract] = float(rank_weights[product]) * float(signal.strength)
            else:
                ... existing raw_target_weight call ...
```
Keep the ATR guard before it (spec: ATR requirement retained). Import `rank_linear_weights` from `.signals`.

**Step 4: Run** both files → PASS. **Step 5: Commit** `feat(carry): rank-linear cross-sectional weights`.

---

### Task 6: Stop-loss switch

**Files:**
- Modify: `cta_carry/backtest.py::_close_plan`
- Test: `tests/test_carry_backtest.py`

**Step 1: Failing test** (model on `test_close_plan_emits_exact_stop_stage_reasons`):

```python
def test_close_plan_skips_chandelier_when_stop_loss_is_disabled() -> None:
    config = small_config(stop_loss_enabled=False)
    states = {"A": PositionState(direction=1, contract="A2405", tranches_remaining=3, highest_high=120.0)}
    bars = {"A2405": {"high": 100.0, "low": 90.0, "close": 90.0}}   # 120 - 2.5*2 = 115 > 90 -> would stop
    signals = pd.DataFrame([...one ready row for A with effective_direction=1, strength=1.0, main_close=90.0, atr=2.0...])

    plan = _close_plan(states, signals, bars, {"A2405": 2.0}, config)

    assert plan.states["A"].tranches_remaining == 3
    assert plan.reasons["A"] == "rebalance"
```

**Step 3: Implement**: `if config.stop_loss_enabled and before.direction != 0 and before.contract is not None:`.

**Step 4/5:** run, commit `feat(carry): switch to run without the chandelier stop`.

---

### Task 7: Deferred execution on missing opens

**Files:**
- Modify: `cta_carry/backtest.py` (`CarryBacktester.run`, new helpers `_split_marked_holdings`, `_defer_unpriced_targets`, `_execution_audit_row`)
- Test: `tests/test_carry_backtest.py`

**Behaviour (policy `defer`):**
1. Holdings return: for each held contract with a valid open today, `weight * (open_today / reference_open − 1)`; `reference_open` is the last valid open seen for that contract (persistent map, updated only with valid opens). Contracts without a valid open contribute 0 and produce one audit row.
2. Targets: contracts whose weight changes are grouped by product; if any contract in a product's change set has no valid open today, **all** of that product's target weights revert to the old weights (both raw and formal) and one audit row per product is written. Other products execute normally.
3. Audit rows: `object_type="execution"`, `object_id=contract or product`, `trade_date`, `check="open_price"`, `status="deferred"`, `action="carried"`, `reason` ∈ {`"held contract has no open; position carried at zero return"`, `"target contract has no open; product rebalance deferred"`}. Appended to `data_quality` only when at least one row exists.
4. Policy `abort` keeps every existing code path byte-for-byte (including `previous_open = current_open` replacement).

**Step 1: Failing tests** (use `make_carry_panel` and delete the open of one contract on one day by dropping that row — `data.py` would have dropped a null-open bar anyway, so remove the row from `data.prices`):

```python
def test_defer_policy_carries_a_held_contract_through_a_day_without_an_open() -> None:
    data = make_carry_panel()
    # find the first report day where product D is held (run the baseline first), then drop D's held contract bar on the next day
    ...
    result = CarryBacktester(data_with_gap, small_config(missing_open_policy="defer"), start=..., end=...).run()

    audit = result.data_quality
    deferred = audit.loc[audit["status"] == "deferred"]
    assert deferred["object_id"].tolist() == [held_contract]
    assert deferred["trade_date"].tolist() == [gap_date]
    # the position is carried: weight on gap_date equals weight the day before
    ...
    # two-day catch-up: gross return on the next day equals w * (open_next / open_before_gap - 1) for that contract
    ...


def test_abort_policy_still_raises_on_a_missing_open() -> None:
    with pytest.raises(ExecutionPriceError):
        CarryBacktester(data_with_gap, small_config(), start=..., end=...).run()


def test_defer_policy_holds_a_whole_product_when_one_leg_of_its_rebalance_has_no_open() -> None:
    # unit-level: _defer_unpriced_targets({"A2405": 0.2}, {"A2405": 0.0, "A2409": 0.2}, opens={"A2409": 100.0}, contract_products={...})
    assert target == {"A2405": 0.2}  and audit has one row for product A
```

**Step 3: Implement** — helpers:

```python
def _split_marked_holdings(weights, opens):
    marked, deferred = {}, []
    for contract, weight in weights.items():
        value = opens.get(contract, _MISSING)
        if value is _MISSING or not _is_valid_price(value):
            if _finite_float(weight, "weight") != 0.0:
                deferred.append(contract)
            continue
        marked[contract] = weight
    return marked, deferred


def _defer_unpriced_targets(old_weights, target_weights, opens, contract_products):
    """Return (adjusted targets, deferred products)."""
    changed_by_product = {}
    for contract in set(old_weights) | set(target_weights):
        if _finite_float(old_weights.get(contract, 0.0), "weight") != _finite_float(target_weights.get(contract, 0.0), "weight"):
            changed_by_product.setdefault(contract_products[contract], []).append(contract)
    adjusted = dict(target_weights)
    deferred = []
    for product, contracts in sorted(changed_by_product.items()):
        if all(_is_valid_price(opens.get(contract, _MISSING)) for contract in contracts):
            continue
        deferred.append(product)
        for contract in contracts:
            old = old_weights.get(contract, 0.0)
            if old == 0.0:
                adjusted.pop(contract, None)
            else:
                adjusted[contract] = old
    return adjusted, deferred
```

Engine loop changes, guarded by `defer = self.config.missing_open_policy == "defer"`:
- keep `reference_open: dict[str, float] = {}`; when `defer`, holdings return uses `_split_marked_holdings(weights, current_open)` then `contract_gross_return(marked, reference_open, current_open, ...)`, audit each deferred contract; after the day, `reference_open.update({c: p for c, p in current_open.items() if _is_valid_price(p)})`. When not `defer`, existing code untouched (`previous_open = current_open`).
- when `defer`, replace the two `_validate_target_opens` calls by `_defer_unpriced_targets` for raw and formal (audit deferred products once, from the formal pass); then proceed with the adjusted targets. Note `weight_turnover` must use the adjusted targets so no cost is charged for deferred legs.
- `states`: unchanged (the plan already moved on); the deferred product's formal weight simply lags until priced.
- Result assembly: `data_quality = self.data.data_quality` if no execution rows else `pd.concat([...], ignore_index=True)`.

`_is_valid_price(value)`: finite and > 0.

**Step 4: Run** `pytest tests/test_carry_backtest.py -q` → PASS, including the golden test. **Step 5: Commit** `feat(carry): defer execution instead of aborting when a contract has no open`.

---

### Task 8: End-to-end run of the index configuration on the synthetic panel

**Files:**
- Test: `tests/test_carry_backtest.py`

```python
def test_index_configuration_runs_end_to_end_with_zero_sum_rank_weights() -> None:
    data = make_carry_panel(periods=40)
    config = small_config(
        near_leg="near_dominant", weighting="rank_linear", stop_loss_enabled=False,
        trend_filter_enabled=False, liquidity_measure="open_interest_value",
        missing_open_policy="defer", carry_window=2,
    )
    result = CarryBacktester(data, config, start=data.dates[20], end=data.dates[-1]).run()

    positions = result.positions
    by_day = positions.groupby("trade_date")["raw_weight"].sum()
    assert by_day.abs().max() < 1e-12            # rank weights sum to zero every day
    assert (positions["direction"] != 0).all()
    assert result.trades["reason"].isin({"entry", "rebalance", "roll", "signal_exit", "direction_reversal"}).all()   # no stop_* reasons
    assert result.run_config.set_index("key").loc["weighting", "value"] == "rank_linear"
```

Commit `test(carry): the index configuration runs end to end on the synthetic panel`.

---

### Task 9: Full suite, mutation checks, merge

1. `pytest -q -p no:cacheprovider` (whole repo, ~11 min locally; or ssh WSL2).
2. Mutation checks (each must turn a test red, then revert): flip the rank centre to `N/2`; make near-leg fall back to `secondary` instead of `main`; skip the `stop_loss_enabled` guard; return `target_weights` unchanged from `_defer_unpriced_targets`; use `.mean()` instead of `.median()` for the multiplier.
3. Update `docs/operations/carry-daily-research.md` with a new section "期限结构指数口径" listing the flags and the target command.
4. Merge `feature/carry-term-structure-index` into `master` (fast-forward or merge commit), remove the worktree.

---

### Task 10: Full-history run on WSL2 and cross-validation

1. Compute `--end` as the minimum of per-exchange `max(trade_date)` in `public.futures_daily` (2026-09-09: DCE 2026-09-01).
2. On WSL2 (`ssh -p 2223 ghls@100.120.152.1`, feed the script via stdin per the runbook), pull the branch, run the target command from the design doc §4 with `--start 2012-01-04`, under `setsid nohup`, log heartbeat.
3. Report: ann/sharpe/maxdd/turnover full and 2021-2026; count of `deferred` rows in `data_quality` by reason; daily-return correlation and segment stats against the research replica (`lever_test.py` output in the session scratchpad, same exclusion list, `p=90`).
