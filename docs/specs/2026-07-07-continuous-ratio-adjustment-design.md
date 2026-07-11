# Continuous Ratio Adjustment Fix Design

> **迁移注**（2026-07-11）：本文档随 CTA 剥离迁自 stock_selector；文中 `cta/`、`tests/test_cta_*`、`docs/superpowers/*` 等路径为当时原仓路径（历史记录，不改写），现行代码对应本仓 `cta_gtja/`、`tests/`、`docs/plans|specs/`。

## Context

CTA backtests currently guard around corrupted adjusted prices in
`public.continuous_contract_ohlc`. The downstream audit finds 79 `standard`
symbols, with 13 affected by non-positive adjusted prices. Raw prices are clean,
but either `ba` or `fa` can become non-positive depending on the symbol.

The latest upstream continuous-contract scripts are not in the local archived
copy. They live on pi at:

- `pi:/home/pi/market-monitor/backend/continuous_generator.py`
- `pi:/home/pi/market-monitor/backend/continuous_generator_nh.py`
- runner: `pi:/home/pi/market-monitor/backend/continuous_daily_runner.py`

That directory is not a git repository. The pi scripts connect to
`100.75.102.44:5432/market_monitor`, while local `stock_selector` currently
connects to `100.65.111.79:5432/market_monitor`. The pi database was unavailable
during investigation, returning `database system is in recovery mode` and
`pg_isready: rejecting connections`.

## Root Cause

Both latest pi generators still compute adjusted prices by accumulating
absolute roll gaps and adding the cumulative factor to raw prices:

```python
adjusted_price = raw_price + cumulative_gap_factor
```

For long histories or structurally persistent contango/backwardation, the
cumulative absolute gap can exceed the raw price level. This creates invalid
non-positive adjusted prices. Examples observed in the downstream database:

- `I`: `close_raw=586`, `fa_factor=-588`, `close_fa=-2`.
- `CJ`: `close_raw=8840`, `ba_factor=-14910`, `close_ba=-6070`.

The immediate fix should address the generator, not only the CTA reader guard.

## Goals

- Replace absolute-gap adjusted prices with ratio-based adjusted prices in the
  pi continuous generators.
- Keep `raw` prices and roll event metadata intact.
- Add a hard post-generation validation that prevents writing non-positive
  adjusted open/close prices.
- Prove the fix first on known affected symbols, then run the full 79-symbol
  regeneration after the pi database is healthy.

## Non-Goals

- Do not change CTA factor logic in `stock_selector`.
- Do not rewrite the whole continuous-contract system.
- Do not run full database backfills while the pi database is in recovery mode.
- Do not rely on raw-price fallback as the final fix.

## Proposed Adjustment Algorithm

For each rollover, compute a price ratio instead of an absolute gap.

For a roll from old contract to new contract on `roll_date`:

```python
ratio = new_price / old_price
```

The price basis should match each generator's current intent:

- `continuous_generator.py` currently uses old close from the prior date and new
  close on the roll date. The first implementation should keep that timing to
  minimize scope.
- `continuous_generator_nh.py` currently uses same-day open prices. The first
  implementation should keep that timing to minimize scope.

Back-adjusted prices should scale historical prices into the latest contract
basis. For every future rollover after a row's date, multiply that row by
`new_price / old_price`:

```python
ba_factor = cumulative_multiplier
price_ba = price_raw * ba_factor
```

Forward-adjusted prices should scale later prices into the initial contract
basis. For every rollover at or before a row's date, multiply that row by
`old_price / new_price`:

```python
fa_factor = cumulative_multiplier
price_fa = price_raw * fa_factor
```

The existing `ba_factor` and `fa_factor` columns will change meaning from
additive offsets to multiplicative adjustment factors. This is acceptable only
because the adjusted price columns are the downstream contract; the rollout
must note the semantic change in the runbook. Synthetic roll tests must assert
continuity around both contango and backwardation rolls before the implementation
is accepted.

## Validation Rules

Before writing a symbol's generated rows to `continuous_contract_ohlc`, validate:

- `open_raw` and `close_raw` are positive.
- `open_ba`, `close_ba`, `open_fa`, and `close_fa` are positive.
- `daily_return` and `return_index` are finite.

If validation fails for a symbol, raise an error before database writes for that
symbol. The runner should report the symbol as failed instead of silently
persisting bad rows.

## Rollout Plan

1. Back up the pi files before editing:
   - `continuous_generator.py`
   - `continuous_generator_nh.py`
   - `continuous_daily_runner.py` if touched
2. Add focused synthetic tests or a local validation script for ratio adjustment.
3. Patch `continuous_generator.py` and `continuous_generator_nh.py`.
4. Document that `ba_factor` and `fa_factor` now store multipliers, not additive
   offsets.
5. When pi's database is healthy, regenerate a small affected set first:
   - `I`
   - `CJ`
   - `RU`
6. Run the downstream `cta.data_quality` scan against the target database.
7. If the affected set is clean, run all 79 symbols for `standard` and `nanhua`.
8. Re-run CTA guarded smoke and confirm the `data_quality` sheet reports clean
   adjusted lineages.

## Operational Constraints

- The pi backend directory is not version-controlled. Every remote edit must be
  preceded by timestamped file backups.
- The pi database may be unavailable until recovery completes. Database
  regeneration and validation must wait for `pg_isready` to report accepting
  connections.
- The local and pi database endpoints differ. Before any regeneration, confirm
  which database should be the source of truth for CTA.

## Acceptance Criteria

- Synthetic ratio-adjustment tests pass for contango and backwardation rolls.
- Affected-symbol regeneration does not produce any non-positive adjusted
  open/close prices.
- Full regeneration exits with zero failed symbols.
- Downstream quality scan reports no `fa_corrupt`, `ba_corrupt`, or
  `both_corrupt` statuses for the regenerated database.
- CTA price/volume smoke runs without raw fallback and writes a `data_quality`
  audit sheet.
