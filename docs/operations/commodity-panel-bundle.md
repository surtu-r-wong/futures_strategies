# Shared commodity panel bundle operations

Date: 2026-08-28

The shared builder queries daily and minute PostgreSQL data once and publishes
a versioned, content-addressed bundle for commodity strategies. It does not run
a strategy or produce a spreadsheet/report. Bollinger and Dow consumers and
their report exporters remain future work.

## Build command and settings safety

From the repository root:

```bash
PYTHONPATH=. .venv/bin/python scripts/commodity/build_panel.py \
  --start 2023-01-03 \
  --end 2023-01-31 \
  --output-dir output/commodity-panel-202301 \
  --settings /absolute/path/to/ignored/config/settings.yaml
```

The command above assumes a normal checkout with an executable
`.venv/bin/python`. A linked worktree normally has no local `.venv`; verify and
reuse the main-checkout interpreter and settings in place:

```bash
test -x /home/elfbob/claude-code/futures_strategies/.venv/bin/python
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  scripts/commodity/build_panel.py \
  --start 2023-01-03 \
  --end 2023-01-31 \
  --output-dir output/commodity-panel-202301 \
  --settings /home/elfbob/claude-code/futures_strategies/config/settings.yaml
```

`scripts/continuous/build_panel.py` is a compatibility entry point to the same
builder. It also accepts legacy `--out` and `YYYY-MM` bounds, but ISO dates and
`--output-dir` are preferred. Add `--use-test` only when the requested build is
intentionally against the configured test database profile.

`config/settings.yaml` is ignored because it contains a plaintext PostgreSQL
credential. Never copy it into a linked worktree, commit it, print it, or put
its contents on the command line. If a linked worktree has no ignored settings,
pass the absolute path of the existing main-checkout file with `--settings`.

The committed session authority currently ends on **2026-01-30**. An explicit
end later than that fails. The legacy `--end 2026-01` spelling alone clamps to
2026-01-30 because it denotes the same authority month; an explicit
`--end 2026-01-31` still fails.

## Bundle contract

`manifest.json` declares `bundle_version: 1` and exactly four Parquet tables:

| Table | Primary key | Columns |
|---|---|---|
| `bars.parquet` | `trade_date, product, slot_end` | `product, contract, trade_date, slot_end, open, high, low, close, volume, open_interest, no_trade, adj_factor, continuity_segment, fill_time, fill_price, fill_pending, fill_unpriceable, pricing_basis, multiplier` |
| `universes.parquet` | `month_start, product` | `month_start, product` |
| `dominants.parquet` | `trade_date, product` | `trade_date, product, contract, oi, volume, selected_from, adj_factor` |
| `roll_fills.parquet` | `trade_date, product` | `trade_date, product, old_contract, new_contract, fill_time, old_price, new_price, old_pricing_basis, new_pricing_basis` |

Every manifest table entry records the exact filename and SHA-256 of the
Parquet bytes. `read_bundle()` validates the manifest version and inventory,
all four file hashes before decoding any Parquet, each exact schema/dtype and
primary key, canonical contract identities, and the cross-table dominant/roll
relationships. Missing, modified, extra-schema or internally inconsistent
artifacts fail closed.

### Price and fill semantics

- `bars` stores raw concrete-contract OHLC and carries a positive
  `adj_factor`. A signal consumer applies that factor to OHLC exactly once;
  it must not pre-adjust the cached table and then adjust again.
- `fill_price`, `roll_fills.old_price`, and `roll_fills.new_price` are raw
  executable-contract prices and are never multiplied by `adj_factor`.
- A roll request fetches both old and new concrete contracts over the next
  session's first five authoritative minute slots. Either leg missing or
  unpriceable aborts the build; the builder does not synthesize a spread or a
  replacement price.
- Exchanges without an override use `amount_vwap`. CZCE is explicitly
  overridden to `ohlc_typical` because its stored `amount` is synthesized from
  an integer price and cannot recover an exact VWAP. The chosen basis is stored
  on every bar/fill.
- `open_interest` comes from the chronologically last positive-volume minute
  in the 15-minute bar, but only when that minute's OI is finite. Otherwise it
  is null; the builder does not fall back to an earlier traded minute.
  `fill_time` is the last slot of the subsequent five-minute execution window,
  including a cross-session resolution when applicable.

## Manifest provenance and secrecy

The manifest has only four top-level keys: `bundle_version`, `inputs`,
`provenance`, and `tables`.

`inputs` records the requested dates, daily history start and relation, minute
relation, daily row count and content digest, candidate-context digest, exact
minute request/content digests, multiplier-resolution digest, and effective
configuration digest. `provenance` records Git HEAD, a hash of production
Python bytes, session/pricing authority filenames and hashes, the reliable
session end, dominant-selection rule, database profile, five-minute roll
window, and minute query/row/candidate counts. `tables` carries the four
published file hashes.

The effective configuration digest excludes credential-bearing keys/values.
The manifest writer also rejects secret-like keys, credential URLs, DSNs,
tokens and unsupported/non-finite values. **No username, password, DSN, API
token, settings contents, or source contents belong in the manifest**; only
safe metadata and cryptographic digests do.

## Atomic publication, checkpoints and recovery

Publication is serialized by an in-process lock plus the bundle's
`.bundle.lock`. The writer stages and fsyncs all four Parquet files and the
manifest in the target directory. `.incomplete-generation.json` journals the
generation and hashes before tables are renamed; `manifest.json` is renamed
last and is the commit point. A retry under the same inputs either removes an
uncommitted partial generation or recognizes the already committed manifest.
Unknown/symlinked/undeclared recovery files and digest disagreements fail
rather than being deleted or adopted.

Long panel extraction uses `OUTPUT_DIR/.panel-checkpoint`, guarded by an
adjacent file lock. Each completed month atomically records finalized bars, the
per-product pending fills carried into the next month, minute-query audit
state, and multiplier-resolution state. A retry with the same checkpoint key
resumes after the latest completed month. The key binds dates, database
profile, daily/context/factor content, safe effective configuration, and
production source revision. After successful bundle publication the builder
safely clears only its own declared checkpoint files.

Checkpoint resume is crash recovery, not a fresh source reproducibility check.
For completed months it restores saved Parquet rows and saved minute/multiplier
audit state; it does **not** re-query or revalidate mutable upstream minute
content or private multiplier evidence. If upstream data changes after the
crash, later months can be fetched at a different time and the final manifest
can represent a mixture of saved and new fetches. `panel_checkpoint_mismatch`
does not detect this completed-month drift.

For strict reproducibility after an interruption or a known source update, do
not resume that checkpoint. Preserve or discard the interrupted target and run
a full build from scratch into a fresh empty output/checkpoint directory.

Final bundle publication is a separate compatibility gate. When a committed
bundle already exists, newly assembled `inputs`/`provenance` that differ are
rejected with `bundle_input_mismatch`; an unsupported artifact version is
rejected with `bundle_version_unsupported`. These final checks do not
retroactively revalidate source evidence restored from completed checkpoint
months. Changed inputs, configuration, source, minute content, multiplier
resolution, or version therefore require a full build in a new directory. A
compatible repeat of a final bundle is a byte-identical no-op.

## Expected hard failures and current blockers

The builder deliberately refuses to continue on, among other cases:

- an end beyond session authority, an empty commodity/context set, or invalid
  daily/minute schema;
- missing pricing basis, non-positive/contradictory multiplier evidence, or a
  used contract/date without audited multiplier provenance;
- a dominant roll without both raw five-minute fill legs;
- missing/changed bundle files, unsupported versions, schema/key/relationship
  violations, unsafe manifest data, or ambiguous recovery state;
- a changed checkpoint-key input, or changed inputs/provenance against an
  existing committed bundle.

### Market breaks: resolved 2026-08-28

The live daily chain rolls from `FU1804.SHF` (last valid close 2018-03-30) to
`FU1901.SHF` (first valid close 2018-07-16). The contracts never traded on the
same day, so no adjustment ratio is observable. A full scan of the whole
history — 80 commodity products, 162,797 dominant choices — found **exactly one
such boundary**.

It is now handled as an explicit continuity segment rather than a hard failure
(design `2026-08-27-continuity-segment-boundaries-design.md`, fidelity rule
F10). When the two contracts' valid-close ranges are strictly disjoint **and**
the outgoing contract stopped trading when it lost dominance, the new contract
opens the next segment: `continuity_segment` increments and `adj_factor`
restarts at 1.0. Nothing is fabricated — no factor of 1.0 across the gap, no
single leg, no theoretical price.

The third condition matters. Without it, an ordinary missing roll close looks
identical to a break, and a successor whose closes are null over the overlap
would silently open a segment. In a normal roll the retiring contract keeps
trading for months after handing over; only a relaunched product goes quiet on
the day it does.

Still hard failures: overlapping ranges with no common valid close, and either
contract having no valid close at all. Those are missing data, not a market
break.

`continuity_segment` is fail-closed at the builder — defaulting it to 0 is
exactly how a break would get flattened into one continuous series. Consumers
must not carry any price-derived state across it; both replications re-warm
their indicators, reset their state machines, and close any open position at
the last bar of the outgoing segment.

The 2026-08-28 migration smoke passed the strict old-column compatibility gate:
all 16 baseline columns have exact dtype/value equality after deterministic
sorting, and both digests are `0d43a277...76687`. Full evidence is recorded in
`docs/research/2026-08-27-commodity-core-migration-baseline.md`.

Future strategy report exporters must apply the repository's spreadsheet
safety rules before writing user-controlled cell text. No spreadsheet exporter
is part of this bundle command today.
