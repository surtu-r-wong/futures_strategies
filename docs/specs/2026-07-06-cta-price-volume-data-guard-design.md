# CTA Price/Volume Data Guard Design

> **迁移注**（2026-07-11）：本文档随 CTA 剥离迁自 stock_selector；文中 `cta/`、`tests/test_cta_*`、`docs/superpowers/*` 等路径为当时原仓路径（历史记录，不改写），现行代码对应本仓 `cta_gtja/`、`tests/`、`docs/plans|specs/`。

Date: 2026-07-06

## Context

The CTA module already has a working strategy layer in `cta/`, a PostgreSQL
reader for `public.continuous_contract_ohlc`, and synthetic tests for the six
factor-combo sleeves. Current verification:

- `tests/test_cta_strategy.py tests/test_cta_data_quality.py` pass.
- `python -m cta.data_quality` scans 79 `standard` rule symbols and flags 13
  symbols with corrupt adjusted price lineages.
- The documented upstream repair path `/home/elfbob/claude-code/continuous/`
  is not present in this workspace.

The next CTA slice is therefore a downstream guardrail: make price/volume CTA
backtests runnable and auditable inside this repository without claiming the
upstream continuous-contract generator is fixed.

## Goal

Make the CTA price/volume strategy path credible enough for research smoke
runs by preventing known corrupt adjusted prices from entering returns or
factor signals.

The usable first slice is:

- Read each symbol from the best available adjusted price lineage.
- Exclude symbols whose adjusted lineages are unusable by default.
- Allow raw-price fallback only through an explicit opt-in.
- Run price/volume-only CTA strategies without invoking sparse fundamental
  factors.
- Record the data-quality choices in output artifacts.

## Non-Goals

- Do not modify upstream continuous-contract generation.
- Do not create commodity fundamental standard tables.
- Do not claim full six-factor paper fidelity.
- Do not change stock-selection pipelines.
- Do not add scheduler jobs.

## Design

### Adjustment Quality Map

Extend the existing `cta.data_quality` logic with a small reusable helper that
builds a per-symbol adjustment map from `continuous_contract_ohlc`:

- `status`: `ok`, `fa_corrupt`, `ba_corrupt`, or `both_corrupt`.
- `recommended_adj`: `fa`, `ba`, or `raw`.
- non-positive row counts per lineage.
- row count per symbol.

The classification remains based on non-positive open or close values. Raw is
treated as a last-resort fallback, not a paper-faithful adjusted series.

### PostgreSQL Reader Policy

`cta.pg_source.load_public_cta_data` gets an adjustment policy:

- `adjustment_policy="recommended"` by default.
- `allow_raw_fallback=False` by default.

Under the default policy:

- Clean symbols use `fa`.
- `fa_corrupt` symbols use `ba`.
- `ba_corrupt` symbols use `fa`.
- `both_corrupt` symbols are excluded unless raw fallback is explicitly enabled.

The SQL reader may load all lineages and choose columns in pandas. That is
simple, auditable, and fast enough for CTA research-scale reads.

### Price/Volume Factor Set

Add a named factor subset for price/volume research:

- `long_rule_momentum`
- `long_cross_momentum`
- `price_volume_corr`

The CLI should expose this as a factor-set choice, for example:

```bash
.venv/bin/python -m cta \
  --source public-pg \
  --strategy both \
  --factor-set price_volume \
  --start 2019-01-01 \
  --end 2025-09-30
```

The existing default can remain the six-factor set. The price/volume set is the
recommended smoke path until basic commodity fundamentals are standardized.

### Audit Output

CTA result artifacts should include the adjustment decisions used by the run:

- total symbols requested.
- symbols retained.
- symbols excluded.
- selected lineage per retained symbol.
- corruption counts and status per symbol.
- whether raw fallback was allowed.
- factor set used.

For Excel output, add a `data_quality` sheet. For CLI logs, print a concise
summary before strategy metrics.

### Error Handling

The reader should fail closed when a requested symbol has no usable price
lineage under the selected policy and all requested symbols are excluded.

If only some symbols are excluded, the run may continue, but the audit must
record exclusions. This supports broad universe runs while keeping exact symbol
runs honest.

Raw fallback must be explicit. When enabled, outputs should mark the run as a
raw-fallback research run, because raw prices do not solve roll-adjusted return
continuity.

### Testing

Add focused tests for:

- per-symbol lineage selection from synthetic `open_fa/open_ba/open_raw` data.
- `both_corrupt` default exclusion.
- explicit raw fallback.
- CLI or strategy construction for `price_volume` factor set.
- output writer includes a `data_quality` sheet when audit data is provided.

Keep the existing CTA strategy and data-quality tests green.

## Acceptance Criteria

- Existing CTA tests still pass.
- New tests cover the adjustment policy and price/volume factor set.
- `python -m cta.data_quality` still reports the live corruption summary.
- A price/volume-only CTA command can run without using basis, inventory, or
  profit factors.
- Output artifacts include data-quality audit information.
- The runbook documents the recommended CTA smoke command and clearly labels it
  as a guarded price/volume research path, not a full six-factor replication.
