# 夜盘竞价 K 线归位 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让经验夜盘起点取自真实成交、并把集合竞价撮合根归位到它开启的时段起点，从而使
DCE 2019-12-26 的经验边界与公告原文 `22:30—23:00` 吻合，解除待审批次 A 的技术阻塞。

**Architecture:** 在 `build_session_boundary_query` 末尾**追加**三个带 `FILTER` 的聚合列
（首笔成交分钟、第二笔成交分钟、首笔成交根是否 `high = low`）；`classify_session_boundary`
改用这三列推夜盘起点，并在四个条件同时成立时把起点前移一分钟；`night_last` 与全部日盘边界
不动；归位与「有 K 无量」两类事件进采集审计报告。

**Tech Stack:** Python 3.13 / psycopg2 / pandas / pytest；TimescaleDB hypertable
`public.futures_minute`（6.6 亿行、264 压缩 chunk）。

**设计文档：** `docs/superpowers/specs/2026-08-19-carry-minute-auction-attribution-design.md`

---

## ⚠️ 动手前必读的三个陷阱

1. **新列只能追加在 SELECT 末尾。** `cta_carry/minute_pg_source.py:_validate_boundary_frame`
   用**硬编码位置下标**：`boundary_indexes = range(7, 15)`、`observed_rows = row[15]`、
   `row[7:15]`、`(*row[:15], int(observed_rows))`。把新列插在中间会让现有校验整体错位。
2. **两个同名常量不是一回事。**
   `cta_carry/minute_pg_source.py:_BOUNDARY_COLUMNS` = 边界查询的完整列清单（要扩）；
   `scripts/carry/capture_minute_sessions.py:BOUNDARY_COLUMNS` = 要求「恰好落在一个权威时隙」
   的时间戳清单（**不要扩**——`night_traded_first` 可能是 22:29，它本来就不是时隙）。
3. **查这张表的三条硬规**（见记忆 `futures-minute-ingestion`）：join 键必须是
   `futures_minute` 上的裸 `m.symbol`；时间边界写字面量；新查询先 `EXPLAIN`。
   Task 6 会复验这三条。

---

## Task 1: 边界查询追加三个成交列

**Files:**
- Modify: `cta_carry/minute_pg_source.py:68-85`（`_BOUNDARY_COLUMNS`）
- Modify: `cta_carry/minute_pg_source.py:458-543`（`build_session_boundary_query`）
- Test: `tests/test_carry_minute_pg_source.py`

**Step 1: 写失败测试**

在 `tests/test_carry_minute_pg_source.py` 中
`test_session_boundary_query_is_grouped_bounded_and_keeps_symbol_bare` 之后追加：

```python
def test_session_boundary_query_exposes_traded_night_columns():
    lower = datetime(2024, 1, 1, 20, 0, tzinfo=SHANGHAI)
    upper = datetime(2024, 2, 1, 15, 1, tzinfo=SHANGHAI)

    query = build_session_boundary_query(lower=lower, upper=upper).as_string(None)

    assert "AS night_traded_first" in query
    assert "AS night_traded_second" in query
    assert "AS night_traded_first_flat" in query
    assert "m.volume > 0" in query
    # 三条硬规不得因新列退化
    assert "m.symbol = c.minute_symbol" in query
    assert "regexp_replace(m.symbol" not in query
    assert "'2024-01-01T20:00:00+08:00'" in query


def test_boundary_columns_append_traded_columns_after_observed_rows():
    from cta_carry.minute_pg_source import _BOUNDARY_COLUMNS

    assert _BOUNDARY_COLUMNS[15] == "observed_rows"
    assert _BOUNDARY_COLUMNS[16:] == (
        "night_traded_first",
        "night_traded_second",
        "night_traded_first_flat",
    )
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_pg_source.py -k traded -q`
Expected: FAIL（`AS night_traded_first` not in query）

**Step 3: 实现**

`_BOUNDARY_COLUMNS` 末尾追加三项：

```python
_BOUNDARY_COLUMNS = (
    # ... 既有 16 项不动，observed_rows 仍是第 16 个（下标 15）...
    "observed_rows",
    "night_traded_first",
    "night_traded_second",
    "night_traded_first_flat",
)
```

`build_session_boundary_query` 中 `count(m.bar_time) AS observed_rows,` 之后、
`FROM _carry_minute_candidates c` 之前插入（注意 `observed_rows` 后要补逗号）：

```sql
            (array_agg(m.bar_time ORDER BY m.bar_time) FILTER (
                WHERE m.volume > 0
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                  ) AT TIME ZONE 'Asia/Shanghai'
            ))[1] AS night_traded_first,
            (array_agg(m.bar_time ORDER BY m.bar_time) FILTER (
                WHERE m.volume > 0
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                  ) AT TIME ZONE 'Asia/Shanghai'
            ))[2] AS night_traded_second,
            (array_agg(m.high ORDER BY m.bar_time) FILTER (
                WHERE m.volume > 0
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                  ) AT TIME ZONE 'Asia/Shanghai'
            ))[1] = (array_agg(m.low ORDER BY m.bar_time) FILTER (
                WHERE m.volume > 0
                  AND m.bar_time < (
                    c.trade_date::timestamp + TIME '09:00'
                  ) AT TIME ZONE 'Asia/Shanghai'
            ))[1] AS night_traded_first_flat
```

夜盘窗口谓词与既有 `night_first` 完全一致，只多一个 `m.volume > 0`。
无成交时 `array_agg(...) FILTER` 返回 NULL，取下标得 NULL，`flat` 亦为 NULL。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_pg_source.py -q`
Expected: PASS

**Step 5: 提交**

```bash
git add cta_carry/minute_pg_source.py tests/test_carry_minute_pg_source.py
git commit -m "feat(carry): expose traded-night columns on the session boundary query"
```

---

## Task 2: 加宽边界帧校验

**Files:**
- Modify: `cta_carry/minute_pg_source.py:1145-1236`（`_validate_boundary_frame`）
- Test: `tests/test_carry_minute_pg_source.py`

**Step 1: 写失败测试**

```python
def test_boundary_frame_rejects_naive_traded_night_timestamps():
    # 构造一行合法边界，再把 night_traded_first 换成 naive datetime
    # （复用该文件中既有的 _boundary_row / candidates 构造助手；
    #   若无则照既有 test_validate_boundary_frame_* 用例的写法构造）
    ...
    with pytest.raises(MinuteDataError, match="aware datetimes"):
        _validate_boundary_frame([row], [candidate])


def test_boundary_frame_rejects_non_boolean_flat_flag():
    ...
    with pytest.raises(MinuteDataError, match="night_traded_first_flat"):
        _validate_boundary_frame([row], [candidate])


def test_boundary_frame_keeps_traded_columns_on_the_frame():
    ...
    frame = _validate_boundary_frame([row], [candidate])
    assert frame.loc[0, "night_traded_first"] is not None
    assert bool(frame.loc[0, "night_traded_first_flat"]) is True
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_pg_source.py -k boundary_frame -q`
Expected: FAIL

**Step 3: 实现**

在模块级加下标常量（避免再出现魔数）：

```python
_TRADED_FIRST_INDEX = _BOUNDARY_COLUMNS.index("night_traded_first")
_TRADED_SECOND_INDEX = _BOUNDARY_COLUMNS.index("night_traded_second")
_TRADED_FLAT_INDEX = _BOUNDARY_COLUMNS.index("night_traded_first_flat")
```

在 `_validate_boundary_frame` 的既有 `for column_index in boundary_indexes:` 循环之后追加：

```python
        for column_index in (_TRADED_FIRST_INDEX, _TRADED_SECOND_INDEX):
            value = row[column_index]
            if value is not None and not _is_aware(value):
                raise MinuteDataError(
                    trade_date=identity[0],
                    product=identity[1],
                    contract=identity[2],
                    check="session_boundaries",
                    reason="observed session boundaries must be aware datetimes",
                    context={
                        "row": row_number,
                        "column": _BOUNDARY_COLUMNS[column_index],
                    },
                )
        flat = row[_TRADED_FLAT_INDEX]
        if flat is not None and not isinstance(flat, bool):
            raise MinuteDataError(
                trade_date=identity[0],
                product=identity[1],
                contract=identity[2],
                check="session_boundaries",
                reason="night_traded_first_flat must be boolean or null",
                context={"row": row_number, "value": repr(flat)},
            )
```

并把归一化行改为保留新列：

```python
        normalized_rows.append(
            (
                *row[:15],
                int(observed_rows),
                row[_TRADED_FIRST_INDEX],
                row[_TRADED_SECOND_INDEX],
                flat,
            )
        )
```

`observed_rows == 0 and all(value is None for value in row[7:15])` 这段判定**不动**
（新列不参与「候选完全无分钟」的判定）。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_pg_source.py -q`
Expected: PASS

**Step 5: 提交**

```bash
git add cta_carry/minute_pg_source.py tests/test_carry_minute_pg_source.py
git commit -m "feat(carry): validate the traded-night columns on the boundary frame"
```

---

## Task 3: 分类器改用成交起点 + 竞价归位

**Files:**
- Modify: `scripts/carry/capture_minute_sessions.py:780-845`（`classify_session_boundary`）
- Modify: `scripts/carry/capture_minute_sessions.py:1372-1404`（`validate_audited_boundaries` 的比较）
- Test: `tests/test_carry_minute_sessions.py`

**Step 1: 先改测试助手，再写失败测试**

把 `tests/test_carry_minute_sessions.py:944` 的 `_captured_boundary` 扩成能表达成交列
（默认＝首笔成交就在时段起点、非竞价形态，等价于今天的行为）：

```python
def _captured_boundary(
    *,
    night_end,
    night_start="21:00",
    traded_first=None,
    traded_second=None,
    traded_flat=False,
):
    trade_date = date(2024, 1, 8)
    previous = date(2024, 1, 5)
    values = {
        # ... 既有 identity 与六个日盘字段不动 ...
    }
    if night_end == "none":
        values.update(
            night_first=None,
            night_last=None,
            night_traded_first=None,
            night_traded_second=None,
            night_traded_first_flat=None,
        )
    else:
        first = _night_instant(previous, night_start)
        values.update(
            night_first=first,
            night_last=_night_instant(previous, night_end) - timedelta(minutes=1),
            night_traded_first=first if traded_first is None else traded_first,
            night_traded_second=(
                first + timedelta(minutes=1) if traded_second is None else traded_second
            ),
            night_traded_first_flat=traded_flat,
        )
    return values
```

把该文件里 6 处 `classify_session_boundary(row) == (...)` 改成比较
`_interval(classify_session_boundary(row))`，并加一个助手：

```python
def _interval(observation):
    return (observation.night_start, observation.night_end)
```

新增七个用例：

```python
def test_night_start_ignores_padded_empty_bars():
    """补齐空 K 把 night_first 拉到 21:00，但首笔成交在 22:29 → 归位 22:30。"""
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    previous = date(2024, 1, 5)
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:30")
    row["night_traded_first_flat"] = True
    row["night_last"] = _night_instant(previous, "23:00") - timedelta(minutes=1)

    observation = classify_session_boundary(row)

    assert _interval(observation) == ("22:30", "23:00")
    assert observation.note == "night_auction_attributed"


def test_normal_night_does_not_trigger_attribution():
    row = _captured_boundary(night_end="23:00")
    observation = classify_session_boundary(row)
    assert _interval(observation) == ("21:00", "23:00")
    assert observation.note is None


def test_attribution_requires_the_next_minute_to_trade():
    """22:29 有成交、22:30 无成交、22:31 才有 → 不归位 → 网格闸拒绝。"""
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    previous = date(2024, 1, 5)
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:31")
    row["night_traded_first_flat"] = True
    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_attribution_requires_the_shifted_minute_on_the_grid():
    """21:05 → 21:06 仍不在网格 → 拒绝。"""
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    previous = date(2024, 1, 5)
    row["night_traded_first"] = _night_instant(previous, "21:05")
    row["night_traded_second"] = _night_instant(previous, "21:06")
    row["night_traded_first_flat"] = True
    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_attribution_requires_the_auction_signature():
    """22:29 有成交但有高低点 → 不是竞价根 → 不归位 → 拒绝。"""
    row = _captured_boundary(night_start="21:00", night_end="23:00")
    previous = date(2024, 1, 5)
    row["night_traded_first"] = _night_instant(previous, "22:29")
    row["night_traded_second"] = _night_instant(previous, "22:30")
    row["night_traded_first_flat"] = False
    with pytest.raises(SessionCaptureError, match="session_rule_time"):
        classify_session_boundary(row)


def test_padded_night_without_any_trade_is_classified_as_no_night():
    """有补齐 K、一笔未成交 → ("none","none") 并留下计数用的 note。"""
    row = _captured_boundary(night_end="23:00")
    row["night_traded_first"] = None
    row["night_traded_second"] = None
    row["night_traded_first_flat"] = None

    observation = classify_session_boundary(row)

    assert _interval(observation) == ("none", "none")
    assert observation.note == "night_untraded_padding"


def test_absent_night_bars_remain_no_night_without_a_note():
    observation = classify_session_boundary(_captured_boundary(night_end="none"))
    assert _interval(observation) == ("none", "none")
    assert observation.note is None
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_sessions.py -k "night_start or attribution or padded_night or absent_night" -q`
Expected: FAIL（`NightObservation` 未定义 / 返回的是二元组）

**Step 3: 实现**

在 `AmbiguityRecord` 附近加：

```python
@dataclass(frozen=True)
class NightObservation:
    night_start: str
    night_end: str
    note: str | None = None
```

`classify_session_boundary` 的夜盘段整体替换为（日盘六项校验在前，保持原样不动）：

```python
    night_first = row["night_first"]
    night_last = row["night_last"]
    traded_first = row["night_traded_first"]
    traded_second = row["night_traded_second"]
    traded_flat = row["night_traded_first_flat"]

    if _missing_boundary(night_first) and _missing_boundary(night_last):
        return NightObservation("none", "none")
    if _missing_boundary(night_first) or _missing_boundary(night_last):
        _boundary_error(
            row,
            "night_first" if _missing_boundary(night_first) else "night_last",
            None,
        )
    if _missing_boundary(traded_first):
        # 有补齐 K 但一笔未成交：与「日盘 only 品种被补了夜 K」在数据上不可区分，
        # 交给授权层裁决，并留下计数。
        return NightObservation("none", "none", "night_untraded_padding")

    for field, value in (
        ("night_traded_first", traded_first),
        ("night_last", night_last),
    ):
        if not isinstance(value, datetime) or value.tzinfo is None:
            _night_boundary_error(row, field, "requires an aware datetime")
    try:
        start = traded_first.astimezone(SHANGHAI)
        end = night_last.astimezone(SHANGHAI) + timedelta(minutes=1)
    except (TypeError, ValueError, OverflowError):
        _night_boundary_error(
            row, "night_traded_first", "could not be converted to the exchange clock"
        )

    note = None
    if start.minute % 15 and traded_flat is True and not _missing_boundary(traded_second):
        if not isinstance(traded_second, datetime) or traded_second.tzinfo is None:
            _night_boundary_error(
                row, "night_traded_second", "requires an aware datetime"
            )
        shifted = traded_second.astimezone(SHANGHAI)
        if shifted == start + timedelta(minutes=1) and shifted.minute % 15 == 0:
            start = shifted
            note = "night_auction_attributed"

    after_midnight = previous_trade_date.fromordinal(
        previous_trade_date.toordinal() + 1
    )
    if start.date() != previous_trade_date:
        _night_boundary_error(
            row,
            "night_traded_first",
            f"expected the previous trade date {previous_trade_date}; "
            f"got {start.date()}",
        )
    if end > _at(after_midnight, 2, 30):
        _night_boundary_error(
            row,
            "night_last",
            f"ends after the 02:30 commodity clock bound; got {end}",
        )
    labels = (f"{start:%H:%M}", f"{end:%H:%M}")
    for field, label in (("night_traded_first", labels[0]), ("night_last", labels[1])):
        try:
            night_label_to_offset(label)
        except ValueError as exc:
            _night_boundary_error(row, field, str(exc))
    try:
        parse_night_interval(*labels)
    except ValueError as exc:
        _night_boundary_error(row, "night_traded_first/night_last", str(exc))
    return NightObservation(labels[0], labels[1], note)
```

同步改 `validate_audited_boundaries`（约 1385 行）：

```python
        observation = classify_session_boundary(row)
        actual_interval = (observation.night_start, observation.night_end)
```

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q`
Expected: PASS（若 `classify_authorized_boundaries` 内部仍解包二元组会红，Task 4 修）

**Step 5: 提交**

```bash
git add scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): derive the night start from traded minutes and attribute the auction bar"
```

---

## Task 4: 归位与补齐事件进审计

**Files:**
- Modify: `scripts/carry/capture_minute_sessions.py:849-936`（`classify_authorized_boundaries`）
- Modify: `scripts/carry/capture_minute_sessions.py:1651`、`1599`（采集流程拼 log_lines）
- Test: `tests/test_carry_minute_sessions.py`

**Step 1: 写失败测试**

```python
def test_classification_reports_auction_attribution_lines():
    # 用既有 classify_authorized_boundaries 用例的构造方式，喂一行 22:29→22:30 的观测，
    # 并提供对应的 DCE 权威例外行（22:30/23:00）
    rows, ambiguities, notes = capture_module.classify_authorized_boundaries(
        boundaries, authority, global_calendar=calendar
    )

    assert ambiguities == ()
    assert notes == (
        "night_auction_attributed=2019-12-26 DCE I",
    )
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_sessions.py -k attribution_lines -q`
Expected: FAIL（返回二元组，无法解包三个）

**Step 3: 实现**

`classify_authorized_boundaries` 内：把 `night_start, night_end = classify_session_boundary(row)`
改为

```python
            observation = classify_session_boundary(row)
            night_start, night_end = observation.night_start, observation.night_end
```

在 `try` 成功分支收集：

```python
        if observation.note is not None:
            notes.append(
                f"{observation.note}={row['trade_date'].isoformat()} "
                f"{row['exchange']} {row['product']}"
            )
```

返回三元组 `(frame, tuple(sorted(ambiguous)), tuple(notes))`（`notes` 已按行序，
行本身已排序，故天然确定）。

采集流程（约 1651 行）改为

```python
    classified, ambiguities, attribution_notes = classify_authorized_boundaries(
        boundaries, authority, global_calendar=audit.global_calendar
    )
    log_lines = (*log_lines, *attribution_notes)
```

（放在两处 `write_capture_diagnostics` 调用之前，两条路径都带上。）

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_sessions.py -q`
Expected: PASS

**Step 5: 提交**

```bash
git add scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py
git commit -m "feat(carry): report auction attribution and untraded padding in the capture audit"
```

---

## Task 5: 全量回归 + 格式

**Step 1: 聚焦回归**

Run: `.venv/bin/python -m pytest tests/test_carry_minute_sessions.py tests/test_carry_minute_pg_source.py tests/test_carry_session_authority.py -q`
Expected: 全绿

**Step 2: 全量（排除已知闸门）**

Run: `.venv/bin/python -m pytest -q --deselect tests/test_carry_minute_backtest.py`
（若该闸门名与计划起点不同，以 `docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`
记录的口径为准：全量不排除时应恰好 `1 failed`，唯一失败＝缺 `config/carry_minute_sessions.csv`。）
Expected: `0 failed`

**Step 3: 格式**

Run: `.venv/bin/ruff format --check . && .venv/bin/ruff check .`
Expected: 干净。**若发现 HEAD 既有漂移，单独开 style 提交**（记忆
`formatting-goes-in-its-own-commit`）。

**Step 4: 提交**（若有格式改动）

```bash
git add -A && git commit -m "style: ruff format"
```

---

## Task 6: 真实数据定点验证（不需要补数）

**Files:**
- Create: `docs/research/2026-08-19-carry-minute-auction-attribution-verification.md`

**Step 1: 写一次性验证脚本**（放 scratchpad，不入库）

对 2019-12-26（延迟夜）与 2019-12-25（正常夜）各构造五个 DCE 主力候选
（`I2005` / `J2005` / `M2005` / `P2005` / `Y2005`），调
`PublicMinuteSource.iter_session_boundaries(...)` 拿真实边界行，再喂
`classify_session_boundary`。

**Step 2: 判定**

- 2019-12-26 五行必须全部 `("22:30", "23:00")`，且 `note == "night_auction_attributed"`；
- 2019-12-25 五行必须全部 `("21:00", "23:00")`，且 `note is None`；
- `EXPLAIN` 复验三条硬规：`Index Cond` 里 `symbol = c.minute_symbol` 为裸列、
  chunk 剪枝后 ≤ 3 个、最大估计行 < 10,000,000、hypertable 侧无 `Seq Scan`
  （唯一 `Seq Scan` 只允许打在临时候选表上，口径见
  `docs/research/2026-08-18-carry-minute-task12-step1-explain-smoke.md`）。

**Step 3: 记录证据**

把上述输出（含实际 `EXPLAIN` 摘要）写进
`docs/research/2026-08-19-carry-minute-auction-attribution-verification.md`。

**Step 4: 提交**

```bash
git add docs/research/2026-08-19-carry-minute-auction-attribution-verification.md
git commit -m "docs(carry): verify auction attribution against the real 2019-12-26 night"
```

---

## Task 7: 更新待审批次与交接

**Files:**
- Modify: `docs/research/2026-08-18-carry-minute-session-exception-pending-batch.md`
- Modify: `docs/research/2026-08-18-carry-minute-handoff.md`

**Step 1:** 批次 A 的「⚠️ 现在写入会 fail-closed」结论改为**已解除**，指向本次设计与验证文档；
明确批次 A **仍需用户逐行过目**才允许写入 `config/carry_minute_session_exceptions.csv`
（用户 2026-08-18 口径，不变）。

**Step 2:** 交接文档「明天的推荐顺序」第 1 项标记完成，唯一外部阻塞收敛为
5 个缺失 product-day。

**Step 3: 提交**

```bash
git add docs/research/
git commit -m "docs(carry): close the auction-bar decision in the pending batch and handoff"
```

---

## 完成判据

- [ ] 三个新列在查询、帧校验与分类器上端到端可用
- [ ] 七个分类器用例全绿，且既有 6 处调用点已迁移
- [ ] 归位与补齐两类事件出现在采集审计报告
- [ ] 全量测试除既有 `config/carry_minute_sessions.csv` 闸门外 0 failed
- [ ] 真实 2019-12-26 五个 DCE 主力经验边界 == `("22:30","23:00")`，正常夜未受影响
- [ ] `EXPLAIN` 三条硬规复验通过
- [ ] **未写入任何 `config/*.csv` 权威资产**（批次 A 仍待用户逐行过目）
