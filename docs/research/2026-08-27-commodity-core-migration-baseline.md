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
