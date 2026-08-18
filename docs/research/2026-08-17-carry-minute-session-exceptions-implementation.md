# Carry 分钟 session exception schema 实施记录

日期：2026-08-18
分支：`feature/carry-minute-execution`
工作树：`/home/elfbob/claude-code/futures_strategies/.worktrees/carry-minute-execution`
资产版本：`commodity-v1`（未发布正式资产，因此不做版本迁移）

本文只记录 **schema 与校验代码** 的实施证据。**没有任何权威例外行被提交**，
也不声称任何一轮 capture 达到 `ambiguous=0`。

## 交付范围

按 `docs/superpowers/plans/2026-08-17-carry-minute-session-exceptions.md`
（设计 `320e9dc`，计划 `803b7f7`）实施 Task 1–5。

| 提交 | 内容 |
|---|---|
| `594b2d5` | `feat(carry): support variable minute night sessions` |
| `84ebb83` | `feat(carry): authorize exact minute session exceptions` |
| `b121707` | `feat(carry): classify authoritative night intervals` |
| `585a6f1` | `feat(carry): publish exact minute night intervals` |
| `d8e6575` | `style(carry): format session exception migration files`（纯 `ruff format`，无行为改动） |

## 新的 schema、哈希名与资产状态

### 会话规则资产表头

```text
exchange,product,effective_start,effective_end,night_start,night_end,version
```

`night_start` / `night_end` 都是精确 `HH:MM` 标签，或成对的 `none`。
翻译由 `cta_carry/minute_sessions.py` 的三个公共函数独占：

- `night_label_to_offset(value)` — `HH:MM` → 交易日分钟偏移；要求 5 字符、
  分钟为 15 的整数倍、且落在 `[21:00, 24:00) ∪ [00:00, 02:30]`。
- `night_offset_to_label(value)` — 反向；要求偏移在 `[-180, 150]` 且为 15 的整数倍。
- `parse_night_interval(night_start, night_end)` — 成对翻译为一个
  `SessionSegment`，或在双 `none` 时返回 `None`；单侧 `none`、非法标签、
  `start >= end` 一律 `ValueError`，消息带 `session_rule_time` 前缀。

`22:30/23:00` 翻译为 `SessionSegment(-90, -60)`，即目标交易日前一自然日
22:30–22:59 共 30 个交易分钟。

### 权威例外资产表头

```text
exchange,version,trade_date,night_start,night_end,reason,source_url
```

唯一键 `(version, exchange, trade_date)`。`trade_date` 是该夜盘例外所归属的
**目标交易日**，不是公告中的前夕自然日；`reason` 仍必须含且只含一个
`notice_evening=YYYY-MM-DD` token，并由全局交易日历映射校验（延迟开盘也适用）。

### 哈希清单键

```text
{"session_exception", "day_only", "history_exception"}
```

采集日志行相应变为：

```text
session_authority version=commodity-v1 session_exception_sha256=<digest> day_only_sha256=<digest> history_exception_sha256=<digest>
```

### 授权语义

`authorize_night_observation` 现在同时接收 `observed_night_start` 与
`observed_night_end`，并返回被消费的 `SessionException`（或 `None`）：

- 有 day-only 区间且无例外 → 只接受 `("none", "none")`。
- 有例外且无 day-only 区间 → 只接受与例外**逐字符相等**的区间对，并返回该例外。
- 两者同时命中同一产品日 → `night_authority_conflict`（互斥，不再允许并存）。
- 两者皆无 → 只接受 `21:00` 起、且终点属于 `{23:00, 23:30, 01:00, 02:30}` 的常规夜盘。

已加载但在本轮没有被任何**已审计产品日**消费的例外，会产出一条
`AmbiguityRecord(product="*", check="session_exception_unconsumed")`，
从而阻断发布。这条规则同时覆盖"该交易所在该日根本没有被审计产品"的情形。

### 经验分类不做任何取整

`classify_session_boundary` 返回 `tuple[str, str]`：要求 `night_first` 落在
`previous_trade_date`、`night_last + 1 分钟` 不晚于次自然日 02:30，然后按
`HH:MM` 格式化并交给 `parse_night_interval`。不从产品预期排期反推更早的开盘，
也不把时间戳吸附到 15 分钟网格 —— 22:31 这样的非网格开盘会以指名
`night_first` 的 `SessionCaptureError` 失败，而不是被静默修正成 22:30。

### 折叠与回放

折叠只在 `(night_start, night_end)` **成对相等** 且日期在全局日历上相邻时延续；
任一标签变化即断开。`_expected_night_interval` 取代 `_expected_night_end`，
回放不一致时抛出含 `session_rule_replay`、交易日、品种、期望对与实际对的
`SessionCaptureError`，且暂存文件在任何回放/哈希/回调失败路径上都被删除、
旧资产字节保持不变。

## 旧契约已完全移除

```console
$ ls -1 config/
carry_liquidity_history_exceptions.csv
carry_minute_day_only_regimes.csv
carry_minute_session_exceptions.csv
settings.example.yaml

$ test -e config/carry_minute_no_night_dates.csv && echo PRESENT || echo absent
absent

$ cat config/carry_minute_session_exceptions.csv
exchange,version,trade_date,night_start,night_end,reason,source_url

$ wc -l < config/carry_minute_session_exceptions.csv
1
```

新资产**只有表头，零数据行**。

全仓库对 `NoNightDate` / `load_no_night_dates` / `matching_no_night_dates` /
`validate_no_night_calendar` / `carry_minute_no_night_dates` / `NO_NIGHT_PATH`
的引用只剩下断言其不存在的那条回归测试本身：

```console
$ grep -rn "NoNightDate\|load_no_night_dates\|matching_no_night_dates\|validate_no_night_calendar\|carry_minute_no_night_dates\|NO_NIGHT_PATH" \
    --include='*.py' --include='*.csv' . | grep -v '\.venv' | grep -v __pycache__ | grep -v '^\./docs/'
tests/test_carry_session_authority.py:600:    old_path = repository / "config/carry_minute_no_night_dates.csv"
tests/test_carry_session_authority.py:616:        "NoNightDate",
tests/test_carry_session_authority.py:617:        "load_no_night_dates",
tests/test_carry_session_authority.py:618:        "matching_no_night_dates",
tests/test_carry_session_authority.py:619:        "validate_no_night_calendar",
```

## 测试证据

全部命令都从工作树根目录执行。

### 仓库契约回归（计划 Task 5 Step 2）

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py::test_repository_uses_only_the_session_exception_authority_contract -q
```

```text
1 passed
```

### 聚焦 schema 回归（计划 Task 5 Step 4）

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_session_authority.py \
  tests/test_carry_minute_sessions.py \
  tests/test_carry_minute_pg_source.py \
  tests/test_carry_minute_backtest.py \
  tests/test_carry_report_cli.py -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

```text
431 passed, 1 deselected in 233.04s (0:03:53)
```

### 完整套件（计划 Task 5 Step 5）

先跑当前可执行的套件（排除已知缺失的正式会话资产闸门）：

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q \
  -k "not test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products"
```

```text
835 passed, 1 deselected, 2 warnings in 286.11s (0:04:46)
```

再跑完整套件，不做任何排除：

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q
```

```text
FAILED tests/test_carry_minute_sessions.py::test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products
1 failed, 835 passed, 2 warnings in 287.36s (0:04:47)
```

**完整套件不通过。** 唯一失败就是缺失 `config/carry_minute_sessions.csv` 这一个
已知外部闸门，其失败原因为 `FileNotFoundError`；正式资产未生成前它必然红，
不得以任何方式弱化或跳过该测试来"转绿"。

### 静态与仓库检查（计划 Task 5 Step 6）

```console
$ ruff check cta_carry tests scripts/carry
All checks passed!

$ ruff format --check cta_carry/minute_sessions.py cta_carry/session_authority.py \
    scripts/carry/capture_minute_sessions.py tests/test_carry_minute_sessions.py \
    tests/test_carry_session_authority.py
5 files already formatted

$ git diff --check
(no output)
```

注：`cta_carry/session_authority.py`、`scripts/carry/capture_minute_sessions.py`、
`tests/test_carry_minute_sessions.py`、`tests/test_carry_session_authority.py`
在本计划开始前（`803b7f7`）就已不合 `ruff format`，与本次改动无关。为让本步
真正通过、又不把无关格式改动混进功能提交，格式化单独落在 `d8e6575`；该提交
只含 `ruff format` 输出，无任何行为改动。

## 仍未解除的两个外部闸门

schema 就绪不等于权威就绪。正式 `config/carry_minute_sessions.csv` 的生成仍被
两项**本次无法在仓库内解决**的外部输入阻塞：

1. **DCE 2019-12-26 延迟开盘的权威原文。** 官方旧页
   `http://www.dce.com.cn/dalianshangpin/yw/fw/jystz/ywtz/6202113/index.html`
   当前受反爬保护、无法直读，来源登记状态维持 `pending_manual_fetch`、
   `rows_derived = 0`。本次实施**没有**提交
   `DCE,commodity-v1,2019-12-26,22:30,23:00,...` 这一行。取回并复核原文后，
   它必须与其他例外行在**同一个已复核批次**中写入。
2. **AL1803 2018-01-02 的原始未补值分钟数据。** 本地分钟库从
   2017-12-29 14:59 直接跳到 2018-01-02 21:00，缺整个日盘。需要从 MyQuant、
   Wind 或上期所授权档案取得原始 OHLCV、成交额与持仓量并审计连续性。
   禁止用日线合成、替代合约、伪造 `none`、删除审计键或硬编码绕过。

在两者都齐备之前，不启动全量 capture。届时才可串行运行既有采集命令、
要求 `ambiguous=0`、发布 `config/carry_minute_sessions.csv`、重跑完整套件，
并回到 Carry Minute Execution 计划的 Task 12 Step 2。

## 已复核节假日来源的地位

权威来源登记中所有 `reviewed_*` 的节前休市来源，本次**没有**派生任何行。
它们的地位是：**将来某个已复核批次**中可用于派生 `none,none` exception 行的
合格来源。本次只交付承载它们所需的 schema。

## 相关文档

- 设计：`docs/superpowers/specs/2026-08-17-carry-minute-session-exceptions-design.md`
- 计划：`docs/superpowers/plans/2026-08-17-carry-minute-session-exceptions.md`
- 权威来源登记：`docs/research/2026-08-14-carry-minute-session-authority-sources.md`
- 交接检查点：`docs/research/2026-08-14-carry-minute-execution-handoff.md`
