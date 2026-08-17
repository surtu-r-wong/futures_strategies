# Carry Minute Session Exceptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unpublished no-night-only authority model with strict exchange-date session exceptions and make final Carry minute-session rules support an authoritative variable night start.

**Architecture:** Keep clock parsing in `minute_sessions`, authority ownership in `session_authority`, and empirical classification/publication in `capture_minute_sessions`. Convert every observed night to an exact `(night_start, night_end)` pair, reconcile it bidirectionally against one immutable authority snapshot, then collapse and atomically publish only adjacent audited product-days with identical pairs.

**Tech Stack:** Python 3.11+, dataclasses, pandas, CSV, pytest, Ruff, PostgreSQL-backed capture fakes.

---

## Scope and file map

**Create**

- `config/carry_minute_session_exceptions.csv` — header-only strict authority asset until reviewed facts are available.
- `docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md` — implementation evidence and remaining external gates.

**Delete**

- `config/carry_minute_no_night_dates.csv` — unpublished superseded authority schema.

**Modify**

- `cta_carry/minute_sessions.py` — validated night labels/offsets and final rule loader with `night_start`.
- `cta_carry/session_authority.py` — `SessionException`, strict loader/hash snapshot, matching and bidirectional authorization.
- `scripts/carry/capture_minute_sessions.py` — empirical start/end classification, exception consumption, collapse, replay and provenance.
- `tests/test_carry_minute_sessions.py` — clock, capture, collapse, publication and orchestration tests.
- `tests/test_carry_session_authority.py` — new authority schema, matching, hashes and authorization tests.
- `docs/research/2026-08-14-carry-minute-session-authority-sources.md` — asset terminology and explicit pending DCE evidence.
- `docs/research/2026-08-14-carry-minute-execution-handoff.md` — approved schema and implementation checkpoint.

## Required discipline and stop boundary

- Work only in `/home/elfbob/claude-code/futures_strategies/.worktrees/carry-minute-execution` on `feature/carry-minute-execution`.
- Invoke `test-driven-development` before Task 1; every production behavior change must be preceded by a failing test.
- Do not populate the DCE 2019-12-26 exception until its primary or equivalent authoritative record is fetched and marked reviewed.
- Do not synthesize, replace or fill the missing AL1803 2018-01-02 minute rows.
- Do not generate `config/carry_minute_sessions.csv` in this plan. Formal generation still requires full capture with `ambiguous=0`.
- Do not preserve compatibility aliases for `NoNightDate`, `no_night_dates` or their loaders.
- Run at most one database integration or capture process at a time. This plan requires no real database process.

### Task 1: Parse variable night intervals in the final session-rule loader

**Files:**

- Modify: `cta_carry/minute_sessions.py:17-254`
- Modify: `tests/test_carry_minute_sessions.py:230-310,884-960`

- [ ] **Step 1: Write failing interval and DCE slot tests**

Update the session CSV helper in `tests/test_carry_minute_sessions.py` to write the new exact header and add:

```python
@pytest.mark.parametrize(
    ("night_start", "night_end", "expected"),
    [
        ("none", "none", None),
        ("21:00", "23:00", SessionSegment(-180, -60)),
        ("21:00", "23:30", SessionSegment(-180, -30)),
        ("21:00", "01:00", SessionSegment(-180, 60)),
        ("21:00", "02:30", SessionSegment(-180, 150)),
        ("22:30", "23:00", SessionSegment(-90, -60)),
    ],
)
def test_csv_night_intervals_translate_exactly(
    tmp_path, night_start, night_end, expected
):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        f"DCE,I,2019-12-26,2019-12-26,{night_start},{night_end},commodity-v1\n",
        encoding="utf-8",
    )
    rule = load_session_rules(path)[0]
    night = tuple(segment for segment in rule.segments if segment.end_minute <= 150)
    assert night == (() if expected is None else (expected,))


def test_delayed_dce_rule_starts_at_2230_on_the_previous_trade_date(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        "DCE,I,2019-12-26,2019-12-26,22:30,23:00,commodity-v1\n",
        encoding="utf-8",
    )
    rule = load_session_rules(path)[0]
    slots = build_trading_slots(date(2019, 12, 26), date(2019, 12, 25), rule)
    assert slots[0] == _dt(2019, 12, 25, 22, 30)
    assert slots[29] == _dt(2019, 12, 25, 22, 59)
    assert _dt(2019, 12, 25, 21, 0) not in slots
```

- [ ] **Step 2: Run the new tests red**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py::test_csv_night_intervals_translate_exactly \
  tests/test_carry_minute_sessions.py::test_delayed_dce_rule_starts_at_2230_on_the_previous_trade_date -q
```

Expected: both fail because `_SESSION_RULE_COLUMNS` has no `night_start` and the loader still assumes `-180`.

- [ ] **Step 3: Implement one strict clock conversion seam**

In `cta_carry/minute_sessions.py`, replace `_NIGHT_SEGMENTS` with these public module functions so authority and capture code reuse the same rules:

```python
def night_label_to_offset(value: str) -> int:
    if type(value) is not str or len(value) != 5 or value[2] != ":":
        raise ValueError(f"session_rule_time: invalid night label {value!r}")
    try:
        hour = int(value[:2])
        minute = int(value[3:])
    except ValueError as exc:
        raise ValueError(
            f"session_rule_time: invalid night label {value!r}"
        ) from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or minute % 15:
        raise ValueError(f"session_rule_time: invalid night label {value!r}")
    clock_minute = hour * 60 + minute
    if 21 * 60 <= clock_minute < 24 * 60:
        return clock_minute - 24 * 60
    if 0 <= clock_minute <= 150:
        return clock_minute
    raise ValueError(f"session_rule_time: night label outside commodity clock {value!r}")


def night_offset_to_label(value: int) -> str:
    if type(value) is not int or value < -180 or value > 150 or value % 15:
        raise ValueError(f"session_rule_time: invalid night offset {value!r}")
    clock_minute = value + 24 * 60 if value < 0 else value
    return f"{clock_minute // 60:02d}:{clock_minute % 60:02d}"


def parse_night_interval(
    night_start: str,
    night_end: str,
) -> SessionSegment | None:
    if night_start == night_end == "none":
        return None
    if "none" in {night_start, night_end}:
        raise ValueError("session_rule_time: night_start and night_end must both be none")
    start = night_label_to_offset(night_start)
    end = night_label_to_offset(night_end)
    if start >= end:
        raise ValueError("session_rule_time: night_start must precede night_end")
    return SessionSegment(start, end)
```

Change `_SESSION_RULE_COLUMNS` to:

```python
_SESSION_RULE_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_start",
    "night_end",
    "version",
)
```

Require both new fields, call `parse_night_interval`, and wrap its `ValueError` with row/field context while retaining the `session_rule_time` prefix. Prepend the returned segment to the unchanged day segments when it is not `None`.

- [ ] **Step 4: Add failure tests for every invalid interval class**

Add this parameterized test:

```python
@pytest.mark.parametrize(
    ("night_start", "night_end"),
    [
        ("none", "23:00"),
        ("21:00", "none"),
        ("21:07", "23:00"),
        ("20:45", "23:00"),
        ("21:00", "02:45"),
        ("23:00", "22:30"),
        ("22:30", "22:30"),
        ("9:00", "23:00"),
    ],
)
def test_csv_rejects_invalid_night_intervals(tmp_path, night_start, night_end):
    path = tmp_path / "sessions.csv"
    path.write_text(
        "exchange,product,effective_start,effective_end,night_start,night_end,version\n"
        f"DCE,I,2019-12-26,2019-12-26,{night_start},{night_end},commodity-v1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="session_rule_time"):
        load_session_rules(path)
```

- [ ] **Step 5: Run the complete clock suite green**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures. The excluded repository-asset test remains blocked only because `config/carry_minute_sessions.csv` is intentionally absent and is exercised explicitly in Task 5.

- [ ] **Step 6: Commit the final-rule clock change**

```bash
git add cta_carry/minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): support variable minute night sessions"
```

### Task 2: Replace no-night authority with strict session exceptions

**Files:**

- Modify: `cta_carry/session_authority.py:20-679`
- Modify: `tests/test_carry_session_authority.py:1-530`
- Create: `config/carry_minute_session_exceptions.csv`
- Delete: `config/carry_minute_no_night_dates.csv`

- [ ] **Step 1: Write failing schema and immutability tests**

Replace the old imports/helpers in `tests/test_carry_session_authority.py` and add:

```python
SESSION_EXCEPTION_HEADER = (
    "exchange,version,trade_date,night_start,night_end,reason,source_url\n"
)


def _session_exception(**overrides) -> SessionException:
    values = {
        "exchange": "DCE",
        "version": SESSION_RULES_VERSION,
        "trade_date": date(2019, 12, 26),
        "night_start": "22:30",
        "night_end": "23:00",
        "reason": "delayed night open notice_evening=2019-12-25",
        "source_url": "https://www.dce.com.cn/notice/6202113",
    }
    values.update(overrides)
    return SessionException(**values)


def test_session_exception_is_immutable_and_uses_the_rule_version():
    row = _session_exception()
    assert row.version == AUTHORITY_VERSION == SESSION_RULES_VERSION
    with pytest.raises(FrozenInstanceError):
        row.night_start = "21:00"


def test_session_exception_loader_reads_exact_schema(tmp_path):
    path = _write(
        tmp_path / "exceptions.csv",
        SESSION_EXCEPTION_HEADER
        + "DCE,commodity-v1,2019-12-26,22:30,23:00,delayed night open notice_evening=2019-12-25,https://www.dce.com.cn/notice/6202113\n",
    )
    assert load_session_exceptions(path) == (_session_exception(),)
```

- [ ] **Step 2: Run authority tests red**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py -q
```

Expected: collection fails because `SessionException` and `load_session_exceptions` do not exist.

- [ ] **Step 3: Implement the new immutable authority contract**

In `cta_carry/session_authority.py`, define the exact column order:

```python
_SESSION_EXCEPTION_COLUMNS = (
    "exchange",
    "version",
    "trade_date",
    "night_start",
    "night_end",
    "reason",
    "source_url",
)
```

Replace `NoNightDate` with:

```python
@dataclass(frozen=True, order=True)
class SessionException:
    exchange: str
    version: str
    trade_date: date
    night_start: str
    night_end: str
    reason: str
    source_url: str

    def __post_init__(self) -> None:
        identity = {
            "version": self.version,
            "exchange": self.exchange,
            "trade_date": self.trade_date,
        }
        if self.version != AUTHORITY_VERSION:
            raise SessionAuthorityError(
                check="authority_record_version",
                reason=f"expected {AUTHORITY_VERSION!r}",
                row_identity=identity,
            )
        for field in ("exchange", "reason", "source_url"):
            _validate_required_record_text(
                record_kind="session exception",
                field=field,
                value=getattr(self, field),
                identity=identity,
            )
        if type(self.trade_date) is not date:
            raise SessionAuthorityError(
                check="authority_record_date",
                reason="session exception trade_date must be an actual date",
                row_identity=identity,
            )
        try:
            parse_night_interval(self.night_start, self.night_end)
        except ValueError as exc:
            raise SessionAuthorityError(
                check="authority_csv_time",
                reason=str(exc),
                row_identity=identity,
                context={
                    "night_start": self.night_start,
                    "night_end": self.night_end,
                },
            ) from exc
```

Rename the loader/matcher functions exactly:

```python
def load_session_exceptions(path: Path) -> tuple[SessionException, ...]:
    return _load_session_exceptions_payload(path, _read_asset_bytes(path))


def matching_session_exceptions(
    rows: Iterable[SessionException], exchange: str, trade_date: date
) -> tuple[SessionException, ...]:
    # Preserve the current validated query and at-most-one cardinality behavior.
```

The payload loader must parse the exact header, validate the date and interval, reject duplicate `(version, exchange, trade_date)` keys, and sort by version/exchange/date/start/end/reason/source.

Change `SessionAuthority` and its constructor to:

```python
@dataclass(frozen=True)
class SessionAuthority:
    session_exceptions: tuple[SessionException, ...]
    day_only_regimes: tuple[EffectiveAuthorityRange, ...]
    liquidity_history_exceptions: tuple[EffectiveAuthorityRange, ...]
    sha256_by_asset: Mapping[str, str]


def load_session_authority(
    *,
    session_exception_path: Path,
    day_only_path: Path,
    history_exception_path: Path,
) -> SessionAuthority:
    paths = {
        "session_exception": session_exception_path,
        "day_only": day_only_path,
        "history_exception": history_exception_path,
    }
```

Read each file once, parse that byte snapshot, and hash the same payload exactly as the current implementation does.

- [ ] **Step 4: Implement exact bidirectional interval authorization**

Change `authorize_night_observation` to accept both labels and return the matched exception when one is consumed:

```python
_NORMAL_NIGHT_ENDS = frozenset({"23:00", "23:30", "01:00", "02:30"})


def authorize_night_observation(
    authority: SessionAuthority,
    *,
    exchange: str,
    product: str,
    trade_date: date,
    observed_night_start: str,
    observed_night_end: str,
) -> SessionException | None:
    _validate_match_query(
        exchange=exchange, product=product, trade_date=trade_date
    )
    try:
        parse_night_interval(observed_night_start, observed_night_end)
    except ValueError as exc:
        raise SessionAuthorityError(
            check="night_observation_value",
            reason=str(exc),
            row_identity={
                "exchange": exchange,
                "product": product,
                "trade_date": trade_date.isoformat(),
            },
            context={
                "observed_night_start": observed_night_start,
                "observed_night_end": observed_night_end,
            },
        ) from exc
    regimes = matching_ranges(
        authority.day_only_regimes, exchange, product, trade_date
    )
    exceptions = matching_session_exceptions(
        authority.session_exceptions, exchange, trade_date
    )
    observed = (observed_night_start, observed_night_end)
    if regimes:
        if observed == ("none", "none") and not exceptions:
            return None
    elif exceptions:
        expected = (exceptions[0].night_start, exceptions[0].night_end)
        if observed == expected:
            return exceptions[0]
    elif observed_night_start == "21:00" and observed_night_end in _NORMAL_NIGHT_ENDS:
        return None
    raise SessionAuthorityError(
        check="night_authority_conflict",
        reason="observed night interval conflicts with repository authority",
        row_identity={
            "exchange": exchange,
            "product": product,
            "trade_date": trade_date.isoformat(),
        },
        context={
            "observed_night_start": observed_night_start,
            "observed_night_end": observed_night_end,
            "day_only_matches": len(regimes),
            "session_exception_matches": len(exceptions),
        },
    )
```

- [ ] **Step 5: Migrate calendar validation and add conflict tests**

Rename `validate_no_night_calendar` to `validate_session_exception_calendar` and preserve the exact `notice_evening=YYYY-MM-DD` to next-global-trade-date rule for every exception, including delayed opens.

Add tests for:

```python
def test_delayed_open_requires_an_exact_exception():
    exception = _session_exception()
    authority = _authority(session_exceptions=(exception,))
    values = {
        "exchange": "DCE",
        "product": "I",
        "trade_date": date(2019, 12, 26),
        "observed_night_start": "22:30",
        "observed_night_end": "23:00",
    }
    assert authorize_night_observation(authority, **values) == exception
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(_authority(), **values)
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(session_exceptions=(exception,)),
            **{**values, "observed_night_start": "21:00"},
        )


def test_day_only_and_session_exception_cannot_both_authorize_one_product_day():
    with pytest.raises(SessionAuthorityError, match="night_authority_conflict"):
        authorize_night_observation(
            _authority(
                session_exceptions=(
                    _session_exception(night_start="none", night_end="none"),
                ),
                day_only_regimes=(
                    _range_row(
                        exchange="DCE",
                        product="I",
                        effective_start=date(2019, 12, 26),
                        effective_end=date(2019, 12, 26),
                    ),
                ),
            ),
            exchange="DCE",
            product="I",
            trade_date=date(2019, 12, 26),
            observed_night_start="none",
            observed_night_end="none",
        )
```

Retain tests for malformed headers, empty required cells, invalid dates/version, duplicate keys, one-read hash binding and immutable hash mappings, updated to the new names and fields.

- [ ] **Step 6: Replace the repository authority asset**

Create `config/carry_minute_session_exceptions.csv` with exactly:

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
```

Delete `config/carry_minute_no_night_dates.csv`. Do not add DCE or holiday rows in this task.

- [ ] **Step 7: Run authority and clock tests green**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures. The known formal-session-asset gate is deliberately excluded until Task 5 verifies it separately.

- [ ] **Step 8: Commit the authority migration**

```bash
git add cta_carry/session_authority.py tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py config/carry_minute_session_exceptions.csv
git add -u config/carry_minute_no_night_dates.csv
git commit -m "feat(carry): authorize exact minute session exceptions"
```

### Task 3: Classify empirical night starts and require complete exception consumption

**Files:**

- Modify: `scripts/carry/capture_minute_sessions.py:55-94,746-880`
- Modify: `tests/test_carry_minute_sessions.py:884-1050,1650-1865`

The test snippets below live in `tests/test_carry_minute_sessions.py`. Update that module's existing local authority helpers to construct `SessionException` and `SessionAuthority(session_exceptions=...)`; do not import private helpers from `tests/test_carry_session_authority.py`.

- [ ] **Step 1: Write failing delayed-boundary tests**

Extend `_captured_boundary` to accept `night_start`; derive `night_first` from it and keep `night_last` as the minute before `night_end`. Add:

```python
def test_capture_classifies_dce_delayed_open_as_an_exact_pair():
    row = _captured_boundary(night_start="22:30", night_end="23:00")
    assert classify_session_boundary(row) == ("22:30", "23:00")


def test_capture_rejects_a_non_grid_night_start():
    row = _captured_boundary(night_start="22:30", night_end="23:00")
    row["night_first"] = row["night_first"] + timedelta(minutes=1)
    with pytest.raises(SessionCaptureError, match="night_first"):
        classify_session_boundary(row)
```

- [ ] **Step 2: Run the boundary tests red**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py::test_capture_classifies_dce_delayed_open_as_an_exact_pair \
  tests/test_carry_minute_sessions.py::test_capture_rejects_a_non_grid_night_start -q
```

Expected: the first fails because classification still requires 21:00 and returns only `night_end`.

- [ ] **Step 3: Generalize empirical classification without rounding**

Change `classify_session_boundary(row)` to return `tuple[str, str]`. Keep the exact three standard day-segment checks. For a night session:

1. Require both `night_first` and `night_last` or neither.
2. Require aware datetimes.
3. Require `night_first` on `previous_trade_date` and `night_last + one minute` no later than 02:30 on the following calendar day.
4. Format `night_first` and `night_last + timedelta(minutes=1)` as `HH:MM`.
5. Call `parse_night_interval`; convert its `ValueError` to `SessionCaptureError` identifying the failed boundary.
6. Return `("none", "none")` only when both night boundaries are missing.

Do not infer an earlier start from the expected product schedule and do not round timestamps to a 15-minute grid.

- [ ] **Step 4: Write failing authorization and unconsumed-exception tests**

Add:

```python
def test_authorized_delayed_open_is_classified_for_every_dce_product():
    day = date(2019, 12, 26)
    previous = date(2019, 12, 25)
    exception = _session_exception()
    boundaries = pd.DataFrame(
        [
            _observation(
                day, previous, night_start="22:30", night_end="23:00", product="I"
            ),
            _observation(
                day, previous, night_start="22:30", night_end="23:00", product="J"
            ),
        ]
    )
    classified, ambiguities = classify_authorized_boundaries(
        boundaries,
        _authority(session_exceptions=(exception,)),
        global_calendar=(day,),
    )
    assert ambiguities == ()
    assert set(classified["night_start"]) == {"22:30"}
    assert set(classified["night_end"]) == {"23:00"}


def test_loaded_but_unconsumed_exception_blocks_capture():
    day = date(2019, 12, 26)
    classified, ambiguities = classify_authorized_boundaries(
        pd.DataFrame(
            [
                _observation(
                    day,
                    date(2019, 12, 25),
                    night_start="21:00",
                    night_end="23:00",
                    exchange="SHFE",
                    product="RB",
                )
            ]
        ),
        _authority(session_exceptions=(_session_exception(),)),
        global_calendar=(day,),
    )
    assert classified.empty is False
    assert [(item.exchange, item.product, item.check) for item in ambiguities] == [
        ("DCE", "*", "session_exception_unconsumed")
    ]
```

- [ ] **Step 5: Track exact consumed exception keys**

Change the classifier signature to:

```python
def classify_authorized_boundaries(
    boundaries: pd.DataFrame,
    authority: SessionAuthority,
    *,
    global_calendar: Sequence[date],
) -> tuple[pd.DataFrame, tuple[AmbiguityRecord, ...]]:
```

For each row, call `authorize_night_observation` with both observed labels. Add a returned exception's `(exchange, trade_date)` key to `consumed_exception_keys`. Emit classified columns exactly:

```python
["exchange", "product", "trade_date", "night_start", "night_end"]
```

After processing all rows, calculate relevant exception keys whose `trade_date` is in the validated `global_calendar`. For every relevant key absent from `consumed_exception_keys`, append one deterministic `AmbiguityRecord(product="*", check="session_exception_unconsumed")`. This also catches an exception for an exchange with no audited product on that loaded trade date.

Rename the filtered calendar wrapper to:

```python
def validate_capture_session_exception_calendar(
    rows: Sequence[SessionException],
    global_calendar: Sequence[date],
) -> None:
```

It must filter to dates in the loaded calendar and call `validate_session_exception_calendar` exactly once.

- [ ] **Step 6: Update orchestration call sites and fakes**

In `_capture_and_publish_outcome`, replace `NO_NIGHT_PATH` with:

```python
SESSION_EXCEPTIONS_PATH = (
    REPOSITORY_ROOT / "config" / "carry_minute_session_exceptions.csv"
)
```

Load `session_exception_path`, validate the filtered calendar, and pass `audit.global_calendar` to `classify_authorized_boundaries`. Update test fakes to use `session_exceptions`, `session_exception` hashes and `night_start/night_end` pairs.

- [ ] **Step 7: Run classification and orchestration tests green**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures with the known absent formal-session-asset gate excluded.

- [ ] **Step 8: Commit empirical start/end classification**

```bash
git add scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): classify authoritative night intervals"
```

### Task 4: Collapse, replay and atomically publish start/end rules

**Files:**

- Modify: `scripts/carry/capture_minute_sessions.py:1027-1390`
- Modify: `tests/test_carry_minute_sessions.py:1890-2375`

- [ ] **Step 1: Write failing collapse-boundary tests**

Add:

```python
def test_collapse_breaks_when_only_night_start_changes():
    days = (date(2019, 12, 25), date(2019, 12, 26))
    classified = pd.DataFrame(
        [
            {
                "exchange": "DCE",
                "product": "I",
                "trade_date": days[0],
                "night_start": "21:00",
                "night_end": "23:00",
            },
            {
                "exchange": "DCE",
                "product": "I",
                "trade_date": days[1],
                "night_start": "22:30",
                "night_end": "23:00",
            },
        ]
    )
    keys = frozenset(("DCE", "I", day) for day in days)
    rules = collapse_session_rules(
        classified, global_calendar=days, audit_keys=keys
    )
    assert [(row["night_start"], row["night_end"]) for row in rules] == [
        ("21:00", "23:00"),
        ("22:30", "23:00"),
    ]
```

Extend the existing unaudited-gap test so equal start/end pairs on either side still produce disjoint rules.

- [ ] **Step 2: Run collapse tests red**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py -k "collapse and night_start" -q
```

Expected: failure because `collapse_session_rules` ignores `night_start`.

- [ ] **Step 3: Collapse exact interval pairs**

Change the required classified schema to include both fields. Validate every pair with `parse_night_interval`, including `none,none`. Within each exchange/product group, carry one `night_interval = (record.night_start, record.night_end)` and split when either label changes or the next date is not adjacent in the global calendar. Emit each rule row with both labels.

- [ ] **Step 4: Write failing stage/replay tests**

Extend the existing atomic publication test to assert the staged and installed header is:

```text
exchange,product,effective_start,effective_end,night_start,night_end,version
```

and that a `22:30/23:00` rule reloads to `SessionSegment(-90, -60)`. Add a replay test where the staged `night_start` is changed to `21:00`; `publish_session_rules` must raise `session_rule_replay` or the existing captured-rule-changed error and must preserve the old output bytes.

- [ ] **Step 5: Publish and replay exact interval pairs**

Update `CSV_COLUMNS`, `_stage_session_rules`, and the reverse helper:

```python
def _expected_night_interval(rule: SessionRule) -> tuple[str, str]:
    night_segments = tuple(
        segment
        for segment in rule.segments
        if segment.start_minute < 0 or segment.end_minute <= 150
    )
    if not night_segments:
        return "none", "none"
    if len(night_segments) != 1:
        raise SessionCaptureError(
            "session_rule_replay: expected at most one night segment"
        )
    segment = night_segments[0]
    return (
        night_offset_to_label(segment.start_minute),
        night_offset_to_label(segment.end_minute),
    )
```

`validate_audited_boundaries` must compare this exact pair to `classify_session_boundary(row)`. Convert a mismatch to a `SessionCaptureError` message containing `session_rule_replay`, the trade date, product, expected pair and actual pair.

- [ ] **Step 6: Rename authority hashes and diagnostics**

Change `_validate_authority_hashes` expected keys to:

```python
{"session_exception", "day_only", "history_exception"}
```

Change `_authority_line` to render:

```text
session_authority version=commodity-v1 session_exception_sha256=<digest> day_only_sha256=<digest> history_exception_sha256=<digest>
```

Update tests to prove a post-load mutation of the new exception file fails before staging, all three hash names are required, and temporary output is removed on every replay/hash/callback failure.

- [ ] **Step 7: Run publication tests green**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_session_authority.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures with the known absent formal-session-asset gate excluded.

- [ ] **Step 8: Commit collapse and publication**

```bash
git add scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): publish exact minute night intervals"
```

### Task 5: Remove the old contract, document evidence and verify the schema slice

**Files:**

- Modify: `tests/test_carry_session_authority.py:516-530`
- Modify: `tests/test_carry_minute_sessions.py:1640-2785`
- Modify: `docs/research/2026-08-14-carry-minute-session-authority-sources.md`
- Modify: `docs/research/2026-08-14-carry-minute-execution-handoff.md`
- Create: `docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`

- [ ] **Step 1: Add the repository-contract regression**

Replace the old header-only test with:

```python
def test_repository_uses_only_the_session_exception_authority_contract():
    repository = Path(__file__).resolve().parents[1]
    exception_path = repository / "config/carry_minute_session_exceptions.csv"
    old_path = repository / "config/carry_minute_no_night_dates.csv"
    assert exception_path.read_text(encoding="utf-8") == (
        "exchange,version,trade_date,night_start,night_end,reason,source_url\n"
    )
    assert not old_path.exists()
    import cta_carry.session_authority as module
    for name in (
        "NoNightDate",
        "load_no_night_dates",
        "matching_no_night_dates",
        "validate_no_night_calendar",
    ):
        assert not hasattr(module, name)
```

- [ ] **Step 2: Run the repository-contract test**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py::test_repository_uses_only_the_session_exception_authority_contract -q
```

Expected: pass after Tasks 2-4; if any old alias survives, fail and remove that alias rather than weakening the test.

- [ ] **Step 3: Update source and handoff records without inventing authority**

In the authority-source register:

- rename no-night asset/count/hash references to session exception;
- retain reviewed holiday sources only as sources eligible to derive `none,none` rows in a later reviewed batch;
- keep DCE notice `6202113` as `pending_manual_fetch` and set derived repository rows to zero;
- state that no DCE 2019-12-26 exception is committed by this schema implementation.

In the handoff:

- record the design commit `320e9dc` and implementation commits from Tasks 1-4;
- state that schema code is complete but formal asset capture remains blocked by DCE evidence and AL1803 data;
- retain Task 12 Step 2 as not started.

Create `docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md` containing the exact test commands/results, new header/hash name, proof the old asset is absent, and the two remaining external gates. Do not include a fabricated DCE row or a claim of `ambiguous=0`.

- [ ] **Step 4: Run all focused schema regressions**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_minute_pg_source.py \
  tests/test_carry_minute_backtest.py \
  tests/test_carry_report_cli.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures. The excluded test remains blocked by the intentionally absent `config/carry_minute_sessions.csv` and is exercised by the full-suite command in Step 5. Any failure here must be root-caused and fixed before continuing.

- [ ] **Step 5: Run the complete suite with and without the known external gate**

Run the currently executable suite:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

Expected: zero failures.

Then run the full suite:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q
```

Expected: exactly one failure, the missing formal `config/carry_minute_sessions.csv` asset gate. Record the exact pass/fail counts; do not claim the full suite passes.

- [ ] **Step 6: Run static and repository checks**

```bash
/home/elfbob/miniconda3/bin/ruff check cta_carry tests scripts/carry
/home/elfbob/miniconda3/bin/ruff format --check \
  cta_carry/minute_sessions.py \
  cta_carry/session_authority.py \
  scripts/carry/capture_minute_sessions.py \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_session_authority.py
git diff --check
git status --short
```

Expected: Ruff and whitespace checks pass; status contains only intentional documentation changes before the final commit.

- [ ] **Step 7: Commit the implementation record**

```bash
git add \
  docs/research/2026-08-14-carry-minute-session-authority-sources.md \
  docs/research/2026-08-14-carry-minute-execution-handoff.md \
  docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md \
  tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py
git commit -m "docs(carry): record minute session exception migration"
```

## Post-plan external continuation

After this plan is complete, do not start full capture until both external inputs are available:

1. Fetch and archive the authoritative DCE delayed-open record; after review, add the exact `DCE,commodity-v1,2019-12-26,22:30,23:00,...` row in the same reviewed authority batch as the other exception rows.
2. Obtain raw, unfilled AL1803 2018-01-02 minute OHLCV, amount and open interest from MyQuant, Wind or an SHFE-authorized archive and audit its continuity.

Only then run the existing full capture command serially, require `ambiguous=0`, publish `config/carry_minute_sessions.csv`, rerun the complete suite, and resume Carry Minute Execution Plan Task 12 Step 2.
