# Commodity core migration behavior-preservation baseline

Date: 2026-08-27

This note freezes the behavior of the existing `cta_continuous` commodity
machinery before it is moved behind `common/commodity`. The baseline commit is:

```text
97b66f81fe4b2a4af3e092eb2e0f6dcc0bd0b622
```

## Starting tree and focused tests

The assigned linked worktree was on `feature/guosen-bollinger-dow` and
`git status --short` produced no output before verification.

Command:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  -m pytest \
  tests/test_continuous_universe.py \
  tests/test_continuous_roll.py \
  tests/test_continuous_indicators.py \
  tests/test_continuous_panel.py -q
```

Result: **53 passed, 0 failed in 3.77 seconds**. Pytest reported four existing
fastparquet/NumPy timedelta deprecation warnings from the Parquet round-trip
test.

The reviewed behavior anchors were:

- `cta_continuous/universe.py`: six-calendar-month, prior-data-only turnover
  universe; market-day denominator; financial-futures exclusion; normalized
  CZCE contract aliases with disagreement detection.
- `cta_continuous/continuous.py`: one-trading-day-lagged, both-max and
  irreversible commodity dominant selection; explicit failure when a roll has
  no eligible shared close; chained back-adjustment factors.
- `cta_continuous/indicators.py`: EMA with `adjust=False` recursion, TNR and
  delta-TNR decisions, and ATR/TR over the caller-provided traded-bar series.
- `cta_continuous/panel.py`: 15-minute session bars, subsequent-five-trading-
  minute fills, cross-session pending-fill resolution, pricing-basis audit,
  per-product-day adjustment factors, and normalized Parquet-safe dtypes.

## Frozen observed smoke artifact

The requested direct command was run first:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  scripts/continuous/2026-08-27-panel-smoke.py
```

It could not authenticate because an ignored `config/settings.yaml` is not
materialized in the linked worktree; configuration therefore fell back to the
committed example and its placeholder password. Running the same `main()` with
the existing main-checkout settings injected in memory reached PostgreSQL, then
exposed a second pre-existing mismatch: the smoke script predates the required
`build_panel(..., adjustment_factor_by_key=...)` argument.

For this wiring-only smoke, the parent task owner approved a non-mutating
compatibility invocation. It used the existing ignored settings file in place
without copying or printing credentials and supplied an explicit factor of
`1.0` for every one of the 171 contexts, matching the historical
`build_session_bars` default:

```bash
mkdir -p output/continuous
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -u -c '
import importlib.util
from pathlib import Path

script = Path("scripts/continuous/2026-08-27-panel-smoke.py").resolve()
spec = importlib.util.spec_from_file_location("panel_smoke", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.resolve_settings_path = lambda: Path(
    "/home/elfbob/claude-code/futures_strategies/config/settings.yaml"
)
original = module.build_panel
module.build_panel = lambda **kwargs: original(
    **kwargs,
    adjustment_factor_by_key={key: 1.0 for key in kwargs["contexts"]},
)
raise SystemExit(module.main())
'
```

This completed with:

- 37,005 daily rows;
- 171 dominant choices across 5 contracts;
- 171 product-day contexts;
- 65,115 source minute rows across 3 monthly queries;
- 4,341 output panel rows;
- 1 final pending fill each for CU, RB, and TA, and 0 unpriceable fills;
- `amount_vwap` for CU/RB and `ohlc_typical` for TA.

The generated output remained ignored and uncommitted. It was frozen and
digested with:

```bash
cp output/continuous/panel_smoke.parquet /tmp/commodity_core_before.parquet
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -c \
  "import hashlib,pandas as pd; f=pd.read_parquet('/tmp/commodity_core_before.parquet').sort_values(['product','slot_end']).reset_index(drop=True); print(len(f), hashlib.sha256(f.to_csv(index=False).encode()).hexdigest())"
git rev-parse HEAD
```

For this fixed Parquet artifact, sorting by `product` and `slot_end`, resetting
the index, serializing with `to_csv(index=False)`, and hashing those bytes is a
deterministic procedure. The recorded value is therefore a frozen observation
of that artifact, not a claim that commit `97b66f8` can reproduce the upstream
panel by itself. The smoke read through ignored, machine-specific settings from
a mutable live PostgreSQL dataset; neither those settings nor the queried data
snapshot is versioned or immutable here. A future digest mismatch alone cannot
distinguish source-data drift from a behavior change introduced by the
migration.

Frozen observed baseline result:

```text
rows:   4341
sha256: 0d43a2771ddccca7a1b2f832fb919e4ffb452eea636bf3cae8372d0d6fb76687
commit: 97b66f81fe4b2a4af3e092eb2e0f6dcc0bd0b622
```

This digest is deliberately an **unadjusted panel-wiring baseline**: every
`adj_factor` is `1.0`. Production back-adjustment chaining remains covered by
the focused roll tests and is not represented by this smoke digest.

## After-migration verification (2026-08-28)

The initial pre-documentation suite completed with **1,546 passed, 0 failed
and 144 warnings in 170.77 seconds**. After the compatibility fix added five
tests, the final full suite at current HEAD completed with **1,551 passed,
0 failed and 150 warnings in 190.90 seconds**.

The historical smoke script remains stale in two known ways: an ignored
`config/settings.yaml` is not linked into this worktree, and the script does
not pass the now-required `adjustment_factor_by_key`. The same approved
non-mutating compatibility wrapper documented above was therefore reused. It
referenced the main checkout's existing ignored settings file in place,
printed no credentials, supplied factor `1.0` for all 171 contexts, and wrote
only the ignored output plus `/tmp/commodity_core_after.parquet`.

The rerun reproduced the operational counts:

- 37,005 daily rows, 171 dominant choices over 5 contracts, and 171 contexts;
- 65,115 minute rows over 3 bounded monthly queries;
- 4,341 panel rows;
- one final pending fill for each of CU, RB and TA, and zero unpriceable fills;
- `amount_vwap` for CU/RB and `ohlc_typical` for TA.

Both artifacts were sorted by `product,slot_end` with a reset index. The
baseline's column list was used verbatim for the comparison, with pandas'
exact frame assertion requiring equal dtypes and values. The only new columns
are `open_interest` and `fill_time`, and both were outside the old-column
comparison. The strict assertion passed:

```text
rows:                         4341
old columns:                  16
new columns:                  open_interest, fill_time
baseline old-column sha256:   0d43a2771ddccca7a1b2f832fb919e4ffb452eea636bf3cae8372d0d6fb76687
after old-column sha256:      0d43a2771ddccca7a1b2f832fb919e4ffb452eea636bf3cae8372d0d6fb76687
exact dtype/value equality:   true
```

The post-fix compatibility output retains the frozen legacy contract values:
`CU2303`, `CU2304`, `CU2305`, `RB2305`, and `TA2305`. The production bundle
continues to validate canonical contract relationships separately.

The production bundle builder has a separate known full-history blocker. The
observed FU chain transitions from `FU1804.SHF` (last valid close 2018-03-30)
to `FU1901.SHF` (first valid close 2018-07-16), so there is no common close
anchor for an adjustment ratio. Current code correctly raises
`roll_close_missing`; this note does not endorse inventing a 1.0 factor,
single-leg price, theoretical price, or any fabricated bridge.
