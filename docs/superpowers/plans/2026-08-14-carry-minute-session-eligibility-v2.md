# Carry Minute Session Eligibility v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Task 5 by generating a fail-closed, versioned commodity session-rule asset whose audited product-days cover the default Carry minute engine, including prewarm and first-out-of-pool exits.

**Architecture:** Keep empirical boundary capture separate from authority: daily liquidity creates a conservative `T/T-1/T-2` product-day envelope, minute PostgreSQL supplies observed boundaries, and strict versioned CSVs authorize genuine `none` sessions or documented liquidity-history gaps. Rules are emitted only for contiguous audited global trading days, then reloaded and expanded back to exactly the audited key set before atomic publication.

**Tech Stack:** Python 3.11+, pandas, psycopg2, PostgreSQL 15, TimescaleDB 2.23, pytest, CSV, SHA-256.

---

## Scope and file map

**Create**

- `cta_carry/session_authority.py` — strict authority CSV loaders, hashes, calendar-date mapping, and bidirectional empirical/authority checks.
- `config/carry_minute_no_night_dates.csv` — exchange-wide no-night target trading dates.
- `config/carry_minute_day_only_regimes.csv` — product day-only effective ranges.
- `config/carry_liquidity_history_exceptions.csv` — documented long-suspension history gaps; header-only unless a real trigger is reviewed.
- `tests/test_carry_session_authority.py` — strict schema, date semantics, matching, version, overlap, and hash tests.
- `scripts/carry/audit_fu_minute_history.sql` — reproducible read-only FU history and daily/minute consistency queries.
- `docs/research/2026-08-14-carry-minute-session-authority-sources.md` — primary-source register and row-count audit.
- `docs/research/2026-08-14-carry-minute-session-capture-audit.md` — commands, configuration, hashes, yearly coverage, ambiguity rounds, and FU evidence.

**Modify**

- `scripts/carry/capture_minute_sessions.py` — default eligibility, dense envelope, authority matching, coverage counters, inventory report, and atomic publication.
- `cta_carry/minute_sessions.py` — `23:30` mapping and asset coverage preflight.
- `cta_carry/minute_pg_source.py` — representative-candidate context and boundary failure classification.
- `cta_carry/pg_source.py` — batch product-history-start query used by the prewarm guard.
- `tests/test_carry_decision.py` — pin first missing signal to a real next-session exit target.
- `tests/test_carry_minute_sessions.py` — envelope, `23:30`, authority, gap, counters, rollback, and coverage tests.
- `tests/test_carry_minute_pg_source.py` — grouped-boundary candidate-role errors.
- `tests/test_carry_pg_source.py` — product history-start query.
- `docs/superpowers/plans/2026-08-13-carry-minute-execution.md` — mark its old Task 5 steps as replaced by this plan.

## Execution constraints and expected manual cost

- Run only one database integration or capture process at a time.
- Keep monthly boundary batches, bare `m.symbol = c.minute_symbol`, literal physical time bounds, and the existing transaction resource limits.
- Authority research may be delegated by exchange in parallel; the primary agent alone validates and merges the three CSV assets.
- Budget the authority pass for roughly 300–450 exchange/date rows, product day-only regimes covering the pre-night-market years, and the 2020 suspension. Expect two or three full ambiguity-inventory rounds before `ambiguous=0`; never fix one row and immediately rerun the full history.

### Task 1: Pin the causal exit and capture-coverage invariants

**Files:**
- Modify: `tests/test_carry_decision.py`
- Modify: `cta_carry/minute_sessions.py`
- Modify: `tests/test_carry_minute_sessions.py`

- [ ] **Step 1: Write the real decision-layer exit test**

Add to `tests/test_carry_decision.py`:

```python
def test_first_missing_signal_creates_a_signal_exit_target():
    config = small_config()
    active = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=config.stop_tranches,
        )
    }
    empty_signals = pd.DataFrame(columns=_signal().columns)

    plan = plan_signal_targets(active, empty_signals, config)

    assert plan.states["A"].direction == 0
    assert plan.states["A"].contract is None
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}
```

Add a failing session coverage test:

```python
def test_capture_coverage_includes_the_entire_backtest_prewarm():
    validate_capture_coverage(
        capture_start=date(2011, 1, 1),
        backtest_start=date(2013, 1, 4),
        prewarm_calendar_days=730,
    )
    with pytest.raises(SessionClockError, match="session_asset_prewarm_coverage"):
        validate_capture_coverage(
            capture_start=date(2011, 1, 6),
            backtest_start=date(2013, 1, 4),
            prewarm_calendar_days=730,
        )
```

- [ ] **Step 2: Run both tests red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_carry_decision.py::test_first_missing_signal_creates_a_signal_exit_target \
  tests/test_carry_minute_sessions.py::test_capture_coverage_includes_the_entire_backtest_prewarm -q
```

Expected: the decision test passes against the real state machine; the coverage test fails because `validate_capture_coverage` is absent.

- [ ] **Step 3: Implement the exact coverage preflight**

Add to `cta_carry/minute_sessions.py`:

```python
SESSION_RULES_CAPTURE_START = date(2011, 1, 1)

def validate_capture_coverage(
    *,
    capture_start: date,
    backtest_start: date,
    prewarm_calendar_days: int,
) -> date:
    required = backtest_start - timedelta(days=prewarm_calendar_days)
    if capture_start > required:
        raise SessionClockError(
            exchange="*",
            product="*",
            trade_date=backtest_start,
            check="session_asset_prewarm_coverage",
            reason=(
                "session asset begins after the minute-state prewarm; "
                f"capture_start={capture_start.isoformat()}; "
                f"required_start={required.isoformat()}"
            ),
        )
    return required
```

Import `timedelta`; treat `SESSION_RULES_CAPTURE_START` as versioned repository
asset metadata. Task 6 must reject publishing the repository asset when CLI `--start`
differs from this constant. Run the two tests again and expect both to pass.

- [ ] **Step 4: Commit the invariants**

```bash
git add cta_carry/minute_sessions.py tests/test_carry_decision.py tests/test_carry_minute_sessions.py
git commit -m "test(carry): pin minute eligibility coverage invariants"
```

### Task 2: Build strict authority assets before capture logic

**Files:**
- Create: `cta_carry/session_authority.py`
- Create: `tests/test_carry_session_authority.py`
- Create: `config/carry_minute_no_night_dates.csv`
- Create: `config/carry_minute_day_only_regimes.csv`
- Create: `config/carry_liquidity_history_exceptions.csv`

- [ ] **Step 1: Write failing schema and date-semantics tests**

The tests must construct temporary CSVs and assert:

```python
def test_notice_evening_maps_to_the_next_target_trade_date():
    calendar = (date(2024, 2, 8), date(2024, 2, 19), date(2024, 2, 20))
    row = NoNightDate(
        version="commodity-v1",
        exchange="SHFE",
        trade_date=date(2024, 2, 19),
        reason="holiday notice_evening=2024-02-08",
        source_url="https://www.shfe.com.cn/",
    )
    validate_no_night_calendar((row,), calendar)
    bad = replace(row, trade_date=date(2024, 2, 8))
    with pytest.raises(SessionAuthorityError, match="notice_target_trade_date"):
        validate_no_night_calendar((bad,), calendar)
```

Also test exact headers, empty required values, invalid dates, version mismatch, duplicate no-night keys, overlapping day-only/history-exception intervals, a header-only exception asset, and 64-character lowercase SHA-256 output.

- [ ] **Step 2: Run authority tests red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_session_authority.py -q
```

Expected: FAIL because the module and three assets do not exist.

- [ ] **Step 3: Implement strict immutable records and loaders**

Use these public interfaces in `cta_carry/session_authority.py`:

```python
AUTHORITY_VERSION = SESSION_RULES_VERSION

@dataclass(frozen=True)
class NoNightDate:
    version: str
    exchange: str
    trade_date: date
    reason: str
    source_url: str

@dataclass(frozen=True)
class EffectiveAuthorityRange:
    version: str
    exchange: str
    product: str
    effective_start: date
    effective_end: date | None
    reason: str
    source_url: str

@dataclass(frozen=True)
class SessionAuthority:
    no_night_dates: tuple[NoNightDate, ...]
    day_only_regimes: tuple[EffectiveAuthorityRange, ...]
    liquidity_history_exceptions: tuple[EffectiveAuthorityRange, ...]
    sha256_by_asset: Mapping[str, str]

def load_session_authority(
    *, no_night_path: Path, day_only_path: Path, history_exception_path: Path
) -> SessionAuthority:
    paths = {
        "no_night": no_night_path,
        "day_only": day_only_path,
        "history_exception": history_exception_path,
    }
    return SessionAuthority(
        no_night_dates=load_no_night_dates(no_night_path),
        day_only_regimes=load_authority_ranges(day_only_path),
        liquidity_history_exceptions=load_authority_ranges(
            history_exception_path
        ),
        sha256_by_asset={
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()
        },
    )

def validate_no_night_calendar(
    rows: Sequence[NoNightDate], global_calendar: Sequence[date]
) -> None:
    ordered = tuple(sorted(set(global_calendar)))
    for row in rows:
        tokens = NOTICE_EVENING.findall(row.reason)
        if len(tokens) != 1:
            raise SessionAuthorityError(check="notice_evening_token", row=row)
        evening = date.fromisoformat(tokens[0])
        target = next((day for day in ordered if day > evening), None)
        if target != row.trade_date:
            raise SessionAuthorityError(
                check="notice_target_trade_date",
                row=row,
                context={"expected_trade_date": target},
            )

def authorize_night_observation(
    authority: SessionAuthority, *, exchange: str, product: str,
    trade_date: date, observed_night_end: str
) -> None:
    regimes = matching_ranges(
        authority.day_only_regimes, exchange, product, trade_date
    )
    halts = matching_no_night_dates(
        authority.no_night_dates, exchange, trade_date
    )
    if observed_night_end == "none" and len(regimes) == 1:
        return
    if observed_night_end == "none" and not regimes and len(halts) == 1:
        return
    if observed_night_end != "none" and not regimes and not halts:
        return
    raise SessionAuthorityError(
        check="night_authority_conflict", trade_date=trade_date
    )
```

In the same module, implement `load_no_night_dates`, `load_authority_ranges`,
`matching_ranges`, and `matching_no_night_dates` with the exact header, required
field, date, version, unique-key, and non-overlap rules already enumerated in Step 1.
`SessionAuthorityError` must retain stable `check`, row identity, reason, and context;
matching helpers return deterministic tuples and reject multiplicity before matching.

- [ ] **Step 4: Create header-only assets and turn tests green**

Create the exact headers:

```csv
version,exchange,trade_date,reason,source_url
```

```csv
version,exchange,product,effective_start,effective_end,reason,source_url
```

The second header is used independently for both range assets. Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_session_authority.py -q
```

Expected: all authority tests pass.

- [ ] **Step 5: Commit authority infrastructure**

```bash
git add cta_carry/session_authority.py tests/test_carry_session_authority.py config/carry_minute_no_night_dates.csv config/carry_minute_day_only_regimes.csv config/carry_liquidity_history_exceptions.csv
git commit -m "feat(carry): validate minute session authority assets"
```

### Task 3: Start the primary-source register before the first full capture

**Files:**
- Create: `docs/research/2026-08-14-carry-minute-session-authority-sources.md`

- [ ] **Step 1: Seed the register with already verified primary evidence**

Create a table with exact columns `exchange`, `authority_kind`, `covered_dates_or_regime`, `source_url`, `rows_derived`, and `review_status`. Seed it with the SHFE FU-history links, the DCE May 2015 23:30 material, the CZCE 2019 23:30 material, and the CZCE 2018 holiday no-night notice from the approved v2 design. Mark only facts directly supported by each URL as reviewed.

- [ ] **Step 2: Collect sources by exchange in bounded parallel research**

Assign SHFE/INE, DCE, CZCE, and GFEX source searches independently. Accept only official exchange domains. Consolidate repeated holiday dates under batch notices, record every derived CSV row count, and reject a source whose effective date cannot be mapped unambiguously. The primary agent merges results only after checking URL scope and target-date conversion.

- [ ] **Step 3: Record the expected work volume**

The register must include counters for no-night rows, day-only ranges, 2020 suspension rows, sources, unresolved empirical dates, and duplicate derived keys. Before capture code proceeds, require duplicate derived keys to be zero and record the working estimate of 300–450 no-night rows plus the number of product regimes.

- [ ] **Step 4: Commit the source register**

```bash
git add docs/research/2026-08-14-carry-minute-session-authority-sources.md
git commit -m "docs(carry): register minute session authority sources"
```

### Task 4: Build the default liquidity envelope and history guard

**Files:**
- Modify: `cta_carry/pg_source.py`
- Modify: `scripts/carry/capture_minute_sessions.py`
- Modify: `tests/test_carry_pg_source.py`
- Modify: `tests/test_carry_minute_sessions.py`

- [ ] **Step 1: Write failing history-start and envelope tests**

Add a PostgreSQL query test requiring one grouped row per product and a bare symbol prefix expression. Add capture tests proving:

```python
def test_audit_envelope_emits_across_the_requested_start_boundary():
    calendar = [date(2023, 12, 28), date(2023, 12, 29), date(2024, 1, 2)]
    pool = {("SHFE", "RB", date(2023, 12, 28))}
    envelope = build_audit_key_sets(
        normalized_keys={("SHFE", "RB", date(2024, 1, 2))},
        in_pool_keys=pool,
        global_calendar=calendar,
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )
    assert envelope.audit_keys == frozenset({("SHFE", "RB", date(2024, 1, 2))})
```

Also cover `P(T)`, `P(T-1)`, `P(T-2)`, all-false, target day without a daily row, exact threshold, OI/volume/contract tie-breaks, default 730-day load start, per-product finite first mean, inception-short history, unauthorized old history, and an exactly matched documented suspension that remains out of pool.

- [ ] **Step 2: Run the focused tests red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_pg_source.py tests/test_carry_minute_sessions.py -q
```

Expected: new history-start and envelope tests fail.

- [ ] **Step 3: Add the batch history-start query**

Add to `cta_carry/pg_source.py`:

```python
def load_public_product_history_starts(*, config_path=None, use_test=False) -> pd.DataFrame:
    """Return product and first available daily date from public.futures_daily."""
```

Use `_PRODUCT_EXPRESSION`, exclude financial futures exactly as `load_public_carry_data` does, group by the bare product expression, and return deterministic `product,first_trade_date` rows.

- [ ] **Step 4: Implement the envelope from shared liquidity**

In `capture_minute_sessions.py`, instantiate exactly `config = CarryConfig()`, load from `start - 730 days`, call `aggregate_product_liquidity(data.prices, config)`, construct the global date union before slicing, and emit each pool key to `S`, `next(S)`, and `next(next(S))`. Preserve synthetic exit keys when target-day prices are absent; select the target-day highest-OI representative when present and otherwise the latest causal in-pool main contract.

Use immutable `normalized_keys`, `in_pool_keys`, `audit_universe_keys`, and `audit_keys`; assert the v2 subset relations before querying minutes. The history guard must classify only `finite`, `insufficient_since_inception`, `authorized_history_gap`, or raise `liquidity_history_incomplete`.

- [ ] **Step 5: Run focused tests green and commit**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_pg_source.py tests/test_carry_minute_sessions.py -q
git add cta_carry/pg_source.py scripts/carry/capture_minute_sessions.py tests/test_carry_pg_source.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): derive safe minute session audit envelope"
```

### Task 5: Add 23:30 and role-specific boundary failures

**Files:**
- Modify: `cta_carry/minute_sessions.py`
- Modify: `cta_carry/minute_pg_source.py`
- Modify: `scripts/carry/capture_minute_sessions.py`
- Modify: `tests/test_carry_minute_sessions.py`
- Modify: `tests/test_carry_minute_pg_source.py`

- [ ] **Step 1: Write failing 23:30 round-trip and error-role tests**

Require `23:29` to classify as `23:30`, CSV `23:30` to load as `SessionSegment(-180, -30)`, reverse mapping to return `23:30`, and 150 night minutes to divide into ten 15-minute buckets. For a zero-row representative boundary, assert:

```python
assert error.check == "session_representative_missing_minutes"
assert error.context == {
    "candidate_role": "session_representative",
    "causal_in_pool_date": date(2024, 1, 5),
    "selection_source": "causal_in_pool_main",
}
```

Reserve `dynamic_execution_leg_missing_minutes` for Task 8 and test that the capture path never emits it.

- [ ] **Step 2: Run the tests red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py tests/test_carry_minute_pg_source.py -q
```

Expected: failures for unsupported `23:30` and old generic boundary errors.

- [ ] **Step 3: Implement minimal mappings and structured context**

Add `"23:30": SessionSegment(-180, -30)` to the strict loader/classifier maps and the reverse `_expected_night_end` map. Extend `MinuteCandidate` with immutable `candidate_role`, `causal_in_pool_date`, and `selection_source`; pass these through the temporary table result identity or the in-memory candidate lookup, then raise the role-specific `MinuteDataError` for missing/zero rows.

- [ ] **Step 4: Run focused tests green and commit**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py tests/test_carry_minute_pg_source.py -q
git add cta_carry/minute_sessions.py cta_carry/minute_pg_source.py scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py tests/test_carry_minute_pg_source.py
git commit -m "feat(carry): support historical 2330 minute sessions"
```

### Task 6: Gate classification, collapse, observability, and publication

**Files:**
- Modify: `scripts/carry/capture_minute_sessions.py`
- Modify: `tests/test_carry_minute_sessions.py`

- [ ] **Step 1: Write failing authority and gap tests**

Test a normal weekend missing night without authority, an authorized exchange holiday, a day-only regime, an unauthorized continuous `none` run, authority saying `none` while complete night rows exist, `[23:00, none, 23:00]`, and same/different endpoint values separated by one unaudited global trading day. Both gap cases must emit two disjoint rules and `resolve_session_rule` must raise zero-match inside the gap.

- [ ] **Step 2: Write failing coverage and rollback tests**

Assert one coverage row for every requested year, six-decimal `in_pool_ratio` and `audited_ratio`, four independently derived key counts, `boundary_keys == audit_keys`, normalization excluded product-days, yearly unkeyable rows, unknown-date total, and publication refusal for any unkeyable row. Mock loader replay, reverse expansion, and `os.replace` to prove missing, duplicate, zero-row, ambiguity, hash mismatch, and reverse-key mismatch leave no final or partial asset.

- [ ] **Step 3: Run capture tests red**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q
```

- [ ] **Step 4: Implement the fail-closed publisher**

Classify empirical boundaries first, call `validate_no_night_calendar` once against
the global calendar, then call `authorize_night_observation` for every audited key.
Collapse only when exchange/product/night_end match and both dates are adjacent in
the global calendar and present in `audit_keys`. Add `--backtest-start`,
`--inventory-output`, and `--audit-report`; call `validate_capture_coverage`, and
when output is the repository CSV require `--start == SESSION_RULES_CAPTURE_START`.
Inventory/report writes may occur on ambiguity, but the output CSV changes only
after strict loader replay and exact reverse-key equality.

Print exact configuration, requested/load dates, authority versions and SHA-256 values, every yearly coverage row, unknown-date unkeyable total, every ambiguity in deterministic order, and the final `products/rules/checked_days/ambiguous` summary.

- [ ] **Step 5: Run all Task 5 unit tests green**

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_carry_decision.py \
  tests/test_carry_pg_source.py \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_minute_pg_source.py \
  tests/test_carry_session_authority.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the publisher**

```bash
git add scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): publish authority-checked session rules"
```

### Task 7: Populate authority data in batches and converge the real capture

**Files:**
- Modify: `config/carry_minute_no_night_dates.csv`
- Modify: `config/carry_minute_day_only_regimes.csv`
- Modify only if triggered and reviewed: `config/carry_liquidity_history_exceptions.csv`
- Create: `scripts/carry/audit_fu_minute_history.sql`
- Create: `docs/research/2026-08-14-carry-minute-session-capture-audit.md`
- Create: `config/carry_minute_sessions.csv`

- [ ] **Step 1: Run ambiguity inventory round 1 without publication**

```bash
PYTHONPATH=. .venv/bin/python scripts/carry/capture_minute_sessions.py \
  --start 2011-01-01 --end 2026-04-29 \
  --backtest-start 2013-01-04 \
  --settings /home/elfbob/claude-code/futures_strategies/config/settings.yaml \
  --inventory-output /tmp/carry-minute-session-inventory-round1.csv \
  --audit-report /tmp/carry-minute-session-audit-round1.md \
  --output config/carry_minute_sessions.csv
```

Expected: one complete deterministic ambiguity inventory; a nonzero exit is normal in round 1, and `config/carry_minute_sessions.csv` must remain absent.

- [ ] **Step 2: Populate authority assets as one reviewed batch**

Group inventory `none` rows into exchange holiday dates, product day-only regimes, and unresolved observations. Convert every `notice_evening` through the global calendar; attach an official URL and reason to every row. Do not add a liquidity-history exception unless a real trigger has a suspension/resumption source and the reviewed interval exactly covers it. Update source-register row counts and require no duplicate keys.

- [ ] **Step 3: Run convergence rounds 2 and 3 only after batch review**

Repeat the round-1 command with round-specific `/tmp` names. Expected convergence is two or three total rounds. For each round, append configuration, hashes, yearly counters, ambiguity counts by reason, and the full command to the repository audit Markdown. If round 3 remains ambiguous, stop publication and investigate the grouped cause; do not loosen matching or add an unsourced row.

- [ ] **Step 4: Save reproducible FU evidence**

`scripts/carry/audit_fu_minute_history.sql` must contain read-only queries for overall minute coverage, FU daily/minute coverage by year, `FU1604` 2016-01-04 daily/minute OHLC-volume-amount consistency using multiplier 50, and the FU liquidity input range. Record the command and result summaries, including the 2010 prewarm input and maximum `4,299,422,549.9167`, in the capture audit Markdown.

- [ ] **Step 5: Require the successful final capture**

The final command must exit 0 with `ambiguous=0`, zero unkeyable counts, all subset/equality invariants true, loader replay successful, reverse keys exactly equal to `audit_keys`, and an atomically published `config/carry_minute_sessions.csv`. Preserve only reviewed authority assets and the final repository audit; `/tmp` round files remain disposable.

- [ ] **Step 6: Commit data and evidence**

```bash
git add config/carry_minute_no_night_dates.csv config/carry_minute_day_only_regimes.csv config/carry_liquidity_history_exceptions.csv config/carry_minute_sessions.csv scripts/carry/audit_fu_minute_history.sql docs/research/2026-08-14-carry-minute-session-authority-sources.md docs/research/2026-08-14-carry-minute-session-capture-audit.md
git commit -m "data(carry): capture authoritative commodity sessions"
```

### Task 8: Verify Task 5 and return to the master plan

**Files:**
- Modify: `docs/superpowers/plans/2026-08-13-carry-minute-execution.md`

- [ ] **Step 1: Run focused and full regression checks**

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_carry_config.py \
  tests/test_carry_data.py \
  tests/test_carry_curve.py \
  tests/test_carry_decision.py \
  tests/test_carry_pg_source.py \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_minute_pg_source.py \
  tests/test_carry_session_authority.py -q
PYTHONPATH=. .venv/bin/python -m pytest -q
```

Expected: both commands pass.

- [ ] **Step 2: Run static and repository checks**

```bash
ruff check cta_carry scripts/carry tests
git diff --check
git status --short
```

Expected: ruff and diff checks are clean; status shows no unintended files.

- [ ] **Step 3: Mark master Task 5 complete only with evidence**

Record the final test counts, database command exit, asset hashes, `ambiguous=0`, yearly counter invariant, and commit IDs in the master plan execution notes. Then resume master Task 6; do not treat the source register or an ambiguity inventory as Task 5 completion.
