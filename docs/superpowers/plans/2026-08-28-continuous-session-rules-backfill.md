# 连续策略时段资产补采实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为国信连续信号产出一份覆盖其完整宇宙的时段资产，并消除「跑到第 N 个月才发现覆盖不全」这一失效模式，全程不改变 Carry 与股指线的任何行为。

**Architecture:** 把 `_build_default_liquidity_audit` 拆成「宇宙无关核心 + Carry 包装」，池子从外部注入；新增连续驱动用 `cta_continuous` 的同一批函数算键集；先普查后权威采集，边界观测落盘复用；面板侧加一道开跑前的覆盖闸，它同时是补采的验收标准。

**Tech Stack:** Python 3.12/3.13, pandas, pytest, PostgreSQL(TimescaleDB), SSH/WSL2。

**设计文档:** `docs/superpowers/specs/2026-08-28-continuous-session-rules-backfill-design.md`

---

## File map

- Modify `common/minute/sessions.py`: 抽出 `matching_session_rules`，`resolve_session_rule` 改用它（行为不变）。
- Modify `cta_continuous/panel.py`: 抽出 `_context_plan`，新增 `required_session_keys` 与 `require_session_coverage`。
- Modify `scripts/continuous/build_panel.py`: 月循环之前调覆盖闸；资产路径改指新 CSV。
- Modify `scripts/continuous/2026-08-27-panel-smoke.py`: 资产路径改指新 CSV。
- Modify `scripts/carry/capture_minute_sessions.py`: 拆核心、`_capture_and_publish_outcome` 收 audit builder、边界观测缓存。
- Create `scripts/continuous/capture_sessions.py`: 连续采集驱动 + 普查模式。
- Modify `tests/test_continuous_panel.py`: 覆盖闸单测 + 接线测试。
- Modify `tests/test_carry_minute_sessions.py`: 核心拆分后的等价性测试。
- Create `tests/test_continuous_session_capture.py`: 驱动键集一致性、普查记账、缓存陈旧拒绝。

---

## Task 1: 面板侧覆盖闸

**这一任务独立于补采，先落地。** 它把「跑到第 5 个月才崩」变成「开跑前一次报全」。

**Files:**
- Modify: `common/minute/sessions.py`
- Modify: `cta_continuous/panel.py`
- Modify: `scripts/continuous/build_panel.py`
- Modify: `tests/test_continuous_panel.py`

- [ ] **Step 1: 抽出共用谓词，写失败测试**

在 `tests/test_common_minute_sessions.py` 加：

```python
def test_matching_session_rules_returns_every_rule_covering_the_day():
    rules = (_rule(start=date(2024, 1, 1), end=date(2024, 6, 30)),
             _rule(start=date(2024, 7, 1), end=None))
    assert len(matching_session_rules(rules, "SHFE", "AU", date(2024, 3, 1))) == 1
    assert matching_session_rules(rules, "SHFE", "AU", date(2023, 1, 1)) == ()
```

- [ ] **Step 2: 跑，确认 RED**（`ImportError: cannot import name 'matching_session_rules'`）

```bash
.venv/bin/python -m pytest tests/test_common_minute_sessions.py -k matching_session_rules -q
```

- [ ] **Step 3: 实现，并让 `resolve_session_rule` 改用它**

在 `common/minute/sessions.py` `resolve_session_rule` 之前插入：

```python
def matching_session_rules(
    rules: Sequence[SessionRule], exchange: str, product: str, trade_date: date
) -> tuple[SessionRule, ...]:
    """覆盖该品种日的全部规则。`resolve_session_rule` 与覆盖闸共用同一谓词。"""
    return tuple(
        rule
        for rule in rules
        if rule.exchange == exchange
        and rule.product == product
        and rule.effective_start <= trade_date
        and (rule.effective_end is None or trade_date <= rule.effective_end)
    )
```

`resolve_session_rule` 体内改为 `matches = matching_session_rules(rules, exchange, product, trade_date)`，其余一字不动。加进 `__all__`（若该模块有）。

- [ ] **Step 4: 跑全量，确认 GREEN 且股指线不受影响**

```bash
.venv/bin/python -m pytest -q
```
预期：与基线同为 1349 passed（本步不新增业务测试，只 +1）。

- [ ] **Step 5: 提交**

```bash
git commit -am "refactor: share the session rule predicate"
```

- [ ] **Step 6: 抽出 `_context_plan`，写覆盖闸的失败测试**

在 `tests/test_continuous_panel.py` 加三态测试：

```python
def test_require_session_coverage_reports_every_uncovered_product_day(tmp_path):
    choices = (_choice(date(2024, 1, 2), "AU", "AU2406.SHF"),
               _choice(date(2024, 1, 3), "AU", "AU2406.SHF"))
    manifest = tmp_path / "gaps.csv"
    with pytest.raises(SessionClockError, match="session_coverage_incomplete"):
        require_session_coverage(
            choices=choices,
            products_by_month={date(2024, 1, 1): ("AU",)},
            rules=(),
            manifest_path=manifest,
        )
    rows = manifest.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2          # 表头 + 1 个需要规则的品种日（首日无前一日，跳过）


def test_require_session_coverage_also_rejects_ambiguous_days(tmp_path): ...
def test_require_session_coverage_is_silent_when_every_day_is_covered(tmp_path): ...
```

- [ ] **Step 7: 跑，确认 RED**

- [ ] **Step 8: 实现**

在 `cta_continuous/panel.py`，把 `build_contexts` 循环里「决定 previous 与身份」的部分抽成共用函数，**`build_contexts` 改用它**（两者从此不可能分叉）：

```python
def _context_plan(choices):
    """(choice, previous, product, minute_symbol, exchange)。

    `build_contexts` 与 `require_session_coverage` 共用，保证「闸检查的」与
    「实际索取的」是同一个键集。
    """
    ordered = sorted(choices, key=lambda c: (c.product, c.trade_date))
    previous_by_product: dict[str, date] = {}
    plan = []
    for choice in ordered:
        predecessor = previous_by_product.get(choice.product)
        selected_from = getattr(choice, "selected_from", None)
        previous = (
            selected_from
            if type(selected_from) is date and selected_from < choice.trade_date
            else predecessor
        )
        previous_by_product[choice.product] = choice.trade_date
        if previous is None:
            continue
        product, minute_symbol, exchange = minute_contract_identity(
            choice.contract, choice.trade_date
        )
        plan.append((choice, previous, product, minute_symbol, exchange))
    return tuple(plan)


def required_session_keys(choices):
    """`build_contexts` 会向 `resolve_session_rule` 索取的键，逐点一致。"""
    return tuple(
        (exchange, product, choice.trade_date)
        for choice, _previous, product, _symbol, exchange in _context_plan(choices)
    )


def require_session_coverage(*, choices, products_by_month, rules, manifest_path=None):
    """开跑前一次性核对全区间时段规则覆盖；缺就带完整清单硬失败。

    ⚠️ 必须在任何分钟查询之前调用。逐月展开而不是只看当月 —— 覆盖不全要一次报全，
    不能跑到第 N 个月才发现（本函数的存在正是因为吃过这个亏）。
    """
    rows = []
    for month, products in sorted(products_by_month.items()):
        pool = set(products)
        eligible = tuple(c for c in choices if c.product in pool)
        for exchange, product, trade_date in required_session_keys(
            context_choices_for_month(eligible, month_start=month)
        ):
            found = len(matching_session_rules(rules, exchange, product, trade_date))
            if found != 1:
                rows.append((month, exchange, product, trade_date, found))
    if not rows:
        return
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["month", "exchange", "product", "trade_date", "found"])
            for month, exchange, product, trade_date, found in rows:
                writer.writerow(
                    [f"{month:%Y-%m}", exchange, product, trade_date.isoformat(), found]
                )
    products_hit = len({(row[1], row[2]) for row in rows})
    by_year = Counter(row[3].year for row in rows)
    raise SessionClockError(
        exchange=rows[0][1],
        product=rows[0][2],
        trade_date=rows[0][3],
        check="session_coverage_incomplete",
        reason=(
            f"{len(rows)} product-days lack exactly one session rule across "
            f"{products_hit} products; by year "
            + " ".join(f"{year}:{count}" for year, count in sorted(by_year.items()))
            + (f"; manifest={manifest_path}" if manifest_path else "")
        ),
    )
```

`build_contexts` 改为遍历 `_context_plan(choices)`，其余逻辑不动。

- [ ] **Step 9: 跑，确认 GREEN**

- [ ] **Step 10: 写接线测试** —— 按 [[wiring-finds-what-unit-tests-cannot]]，写好没接线的闸比没有更糟

```python
def test_build_panel_checks_coverage_before_any_minute_query(monkeypatch):
    """闸必须在月循环之前调用；顺序错了这条测试要红。"""
    order = []
    monkeypatch.setattr(build_panel_module, "require_session_coverage",
                        lambda **kw: order.append("gate"))
    monkeypatch.setattr(build_panel_module, "PublicMinuteSource",
                        lambda **kw: order.append("source") or object())
    ...
    assert order[0] == "gate"
```

- [ ] **Step 11: 接线** —— 在 `scripts/continuous/build_panel.py` 的 `choices` / `products_by_month` 算完之后、`source = PublicMinuteSource(pg=pg)` **之前**插入：

```python
    require_session_coverage(
        choices=choices,
        products_by_month=products_by_month,
        rules=rules,
        manifest_path=args.out / "session_coverage_gaps.csv",
    )
```

- [ ] **Step 12: 全量测试 + 提交**

```bash
.venv/bin/python -m pytest -q
git commit -am "feat: gate the panel on session rule coverage"
```

- [ ] **Step 13: 变异验证** —— 按 [[mutation-runs-need-pycache-clear]]，先清 `__pycache__`

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```
把 `found != 1` 改成 `found > 1`（放过 found 0），确认覆盖闸测试变红；把接线那行删掉，确认接线测试变红。两处都红才算数，之后复原。

---

## Task 2: 拆出宇宙无关的采集核心

**Files:**
- Modify: `scripts/carry/capture_minute_sessions.py`
- Modify: `tests/test_carry_minute_sessions.py`

- [ ] **Step 1: 写失败测试** —— 注入一个与 Carry 流动性无关的池，核心应照单全收

```python
def test_build_audit_accepts_an_injected_pool():
    audit = capture_module.build_audit(
        _prices_fixture(),
        in_pool_source_keys=frozenset({("SHFE", "AU", date(2024, 1, 3))}),
        history_status_by_key={("SHFE", "AU", date(2024, 1, 2)): "lookback_complete"},
        start=date(2024, 1, 2),
        end=date(2024, 1, 5),
    )
    assert ("SHFE", "AU", date(2024, 1, 3)) in audit.key_sets.in_pool_keys
```

- [ ] **Step 2: 跑，确认 RED**
- [ ] **Step 3: 实现拆分**

新增 `build_audit(prices, *, in_pool_source_keys, history_status_by_key, start, end, config=None)`，
内含现在 `_build_default_liquidity_audit` 里**与口径无关**的部分：`_build_representative_index`、
`global_calendar`、`normalized_keys` 校验、`build_audit_key_sets`、`_select_audit_candidates_from_index`、
构造 `DefaultLiquidityAudit`。`config` 字段下游从未被读，设默认值即可。

`_build_default_liquidity_audit` **保留原名、原签名**，内部仍算 `aggregate_product_liquidity`、
推导 `in_pool_source_keys` 与 `history_status_by_key`（含 `must remain out of pool` 断言），
末尾改为 `return build_audit(...)`。

- [ ] **Step 4: 跑全量，确认既有采集测试全绿**（默认路径必须逐点不变）
- [ ] **Step 5: 提交** `refactor: separate the audit core from the Carry pool`

---

## Task 3: 发布路径接受 audit builder

- [ ] **Step 1: 写测试** —— 不传时必须仍走 Carry 包装；传了必须用传入的。
- [ ] **Step 2: RED → 实现** —— `_capture_and_publish_outcome(..., audit_builder=None)`，
      `None` 时用现有 Carry 包装。其余逻辑一字不动。
- [ ] **Step 3: 全量 + 提交** `feat: let the capture take an injected audit builder`

⚠️ 按 [[test-doubles-lag-source-signatures]]，本任务新增关键字参数，**必须跑全量**——
`tests/` 里的手写替身会被静默打断，定向测试全绿而全量红。

---

## Task 4: 连续采集驱动

**Files:** Create `scripts/continuous/capture_sessions.py`, `tests/test_continuous_session_capture.py`

- [ ] **Step 1: 写键集一致性测试**（本计划最重要的一条测试）

```python
def test_driver_demands_exactly_what_the_panel_demands():
    """采集键集必须与 build_panel 的 contexts 键集逐点相同 —— 设计 D4。"""
    keys = capture_keys(stats=_daily_fixture(), start=date(2011, 1, 1), end=date(2011, 3, 1))
    panel_keys = set()
    for month, products in _products_by_month(...).items():
        eligible = tuple(c for c in _choices if c.product in set(products))
        panel_keys |= set(required_session_keys(
            context_choices_for_month(eligible, month_start=month)))
    assert keys == panel_keys
```

- [ ] **Step 2: RED → 实现 `capture_keys`**，内部**调用** `product_daily_turnover` /
      `universe_for_month` / `choose_dominant_commodity` / `context_choices_for_month` /
      `required_session_keys`，不另写等价逻辑。
- [ ] **Step 3: 实现驱动主体** —— 用 `load_public_carry_data` 取日线，
      `history_status_by_key` 全部发 `"lookback_complete"`，并断言日线加载起点早于采集起点 6 个月。
      CLI：`--start --end --output --inventory-output --audit-report [--survey] [--boundary-cache]`。
- [ ] **Step 4: 全量 + 提交** `feat: capture sessions for the continuous universe`

---

## Task 5: 普查模式

- [ ] **Step 1: 写测试** —— 一行分类失败时：不抛、记进清单、**不产出规则 CSV**。
- [ ] **Step 2: RED → 实现**：逐行调**生产用的** `classify_session_boundary(row, day_session_absent=...)`，
      `day_session_absent` 取自 `absent_product_days`；捕获 `SessionCaptureError` 记账。
      `classify_authorized_boundaries` **不改**。普查是独立入口，走不到发布路径。
- [ ] **Step 3: 提交** `feat: survey product-days the session capture cannot classify`

---

## Task 6: 边界观测缓存

- [ ] **Step 1: 写测试** —— 往返一致；键集不匹配时**拒绝**而不是静默用旧的
      （沿用 `b19103b fix: reject stale continuous panel shards` 的姿态）。
- [ ] **Step 2: RED → 实现** —— `capture_session_boundaries` 的产物落 parquet，
      缓存内记录键集摘要；读取时校验，不符即拒。
- [ ] **Step 3: 提交** `feat: cache boundary observations for reuse`

---

## Task 7: 证明 Carry 逐点不变（需要数据库，走 WSL2）

- [ ] **Step 1: 在 b95a74f 建临时工作树**

```bash
git worktree add /tmp/carry-baseline b95a74f
```

- [ ] **Step 2: 同一分段区间各跑一次 Carry 采集**（改动前 / 改动后），
      输出到各自的临时路径，**不得指向 `config/carry_minute_sessions.csv`**。
- [ ] **Step 3: 比对 sha256** —— 规则 CSV、inventory、audit report **三份都必须逐字节相同**。
      按 [[shared-layer-extraction-lessons]]，「逐点不变」要拿真实资产比 sha256，不能靠读代码判断。
- [ ] **Step 4: 把三个 sha256 与所用区间写进** `docs/research/2026-08-28-carry-capture-invariance.md`
- [ ] **Step 5: 清理临时工作树** `git worktree remove /tmp/carry-baseline`

---

## Task 8: 全历史普查跑（WSL2，约 3 小时）—— ⛔ 跑完停下

- [ ] **Step 1: 同步代码到 WSL2**，确认 `settings.yaml` 就位
- [ ] **Step 2: `setsid` 脱离进程组启动普查**（按 [[long-jobs-need-setsid]]），日志落盘
- [ ] **Step 3: 判活体用 `ps -p <pid> -o etimes=` + 心跳日志，绝不用 `lstart=`**
- [ ] **Step 4: 跑完取回阻塞清单与边界缓存**
- [ ] **⛔ Step 5: 停下，把清单交用户逐条裁决。** 不得自行登记 `absent_product_days`，
      也不得自行放宽任何断言 —— 那是口径裁决。

---

## Task 9: 权威采集跑

- [ ] **Step 1: 落地用户裁决**（新增的 `absent_product_days` 行等），每条带证据与出处
- [ ] **Step 2: 读边界缓存跑权威采集**，产出 `config/continuous_minute_sessions.csv`
      + inventory + audit report
- [ ] **Step 3: 确认 `config/carry_minute_sessions.csv` 的 sha256 未变**
- [ ] **Step 4: 提交资产与审计产物**

---

## Task 10: 交叉验证（D3 的回报）

- [ ] **Step 1: 写比对脚本** —— 两份资产在 (交易所, 品种, 日期) 重叠部分逐品种日比对夜盘区间
- [ ] **Step 2: 产出重叠规模与一致率**，写进 `docs/research/2026-08-28-session-asset-cross-validation.md`
- [ ] **Step 3: 有分歧则逐条列名**，⛔ **交用户裁决，不自动取任何一方**

---

## Task 11: 切换面板资产并重跑全历史

- [ ] **Step 1: `build_panel.py` 与 `2026-08-27-panel-smoke.py` 的资产路径改指新 CSV**
- [ ] **Step 2: 全量测试 + 提交**
- [ ] **Step 3: 本地空跑覆盖闸**（不查分钟表，秒级）—— **必须静默通过**。
      这就是补采的验收：通过即证明「采集覆盖的」与「面板要求的」是同一集合。
- [ ] **Step 4: WSL2 上 `setsid` 重跑全历史面板**，逐月落盘、可续跑
- [ ] **Step 5: 跑完核对分片数与总 bar 数，写进研究笔记**

---

## 非目标

- 不改 `config/carry_minute_sessions.csv`、`cta_carry/`、`index_open_momentum/` 的行为。
- 不延长样本窗口（设计 D7：保持 2026-01-30）。
- 不把采集核心搬进 `common/`。
- 不自动裁决交叉验证分歧，不自动登记 `absent_product_days`。

---

## 实施记录（2026-08-28，Task 1–6 完成）

Task 1–6 已实现并提交，全量 **1378 passed**（基线 1349）。两处偏离原计划，都是接线时才暴露的：

**偏离一：Task 3 多做了一件事 —— 覆盖不变量改为消费者声明。**
`_capture_and_publish_outcome` 会校验「资产必须早于消费者首日 730 天」（Carry 的分钟状态机预热）。
连续面板从资产首日开始逐月造 bar，`capture_start == panel_start`，**无论参数怎么调都过不了**。
于是把 `validate_capture_request` 的两件事拆开：`repository_capture_start`（保护 Carry 资产）永远生效；
覆盖规则由调用方声明，默认仍是 Carry 那条。连续侧的规则是
`continuous_capture_coverage`：资产首日不得晚于面板首月（分段采集从 2018 起、面板从 2011 起，
仍会被正确拦下）。用户 2026-08-28 裁决走这条。

**偏离二：Task 5 的机制变了，范围缩小。**
原计划以为「分类失败会整跑中止」。实际上 `classify_authorized_boundaries` **本来就**逐行
catch `SessionCaptureError` 并记成 `AmbiguityRecord`，跑完一次报全并阻止发布 —— 这一类不需要写代码。
真正 fail-fast 的是 `common/minute/pg_source.py:1490` 的
`raise _missing_candidate_minutes(candidate)`（该品种日一根 K 线都没有），而采集是**按月批量**查的，
所以一个坏候选让整月中止。因此普查只做一件事：**按月跑，失败批二分定位缺数据的品种日，记账后继续**。
⚠️ 不能用 `tolerate_empty` —— 其文档串明写 "must never be set for the audited representative"，
打开后缺数据会被当成已授权缺席送进分类器。

**偏离三：二分必须限制在月内（2026-08-28 起跑前发现并修正）。**
`capture_session_boundaries` 内部本来就按月分组查询，所以最初写成「在全部 126,687 个候选上二分」
是错的：一次失败要把整份列表对半重查，每层重读约 n 行、深度约 log2(n) ⇒ **单个失败就是十几倍的
基础成本**，3 小时的活会变成几十小时。原先「只有失败批付 O(k·log n) 次查询」的说法只数了查询
**次数**、没算每次的**体量**。改为按月分组后，重查代价被限制在失败的那一个月，完好月份零额外
开销；同时按月输出心跳，长跑才判得了活体。

**缓存的实际收益要说准**：整份缓存，普查清单非空时不写。所以省下的是「普查 → 权威采集」这一跳；
一轮裁决之后重跑普查仍要重新观测。若裁决轮数多到难以忍受，再考虑按键集合并的部分缓存。

**已验证**
- 覆盖闸在 WSL2 真实数据上跑通：约 2–3 分钟报出 `1713 product-days ... across 39 products`
  并落下 1,713 行清单，产出目录里**没有任何分片**（证明它挡在第一次分钟查询之前）。
  数字与独立统计脚本逐个吻合。
- `config/carry_minute_sessions.csv` 未被任何本分支提交触及，
  sha256 = `e3aff444aba6b2f699a21051c1610da0c025461b81e9d649b1100b3690558c5e`。
- 22 处变异全部被测试抓住（覆盖闸 3、审计接缝 3、口径接缝 4、普查与缓存 5，其余为回归护栏）。

**Task 7 起未开工。** Task 7（Carry 逐字节不变的 sha256 证明）仍是必做项 ——
代码层已拆分过，测试全绿**不等于**真实资产逐字节相同。

---

## 实施记录（2026-08-28，Task 7–10 完成）

**Task 7 —— Carry 逐字节不变已证。** 见 `docs/research/2026-08-28-carry-capture-invariance.md`。
运行时证据覆盖改动的每一行；发布那一段本窗口够不到（局部窗口在设计上永远发布不了），改用静态证据。

**Task 8 —— 普查 `blocked=0`。** 126,771 个品种日全部观测得到，149 秒（远快于原估的 3 小时：
边界采集只取会话首末时间戳，返回集比全量 K 线小几个数量级）。**「缺分钟数据」这一类不存在**，
无可裁决。边界缓存 846 KB 写出，后续权威采集 `boundary_cache=hit`、没再查库。

**Task 9 —— 资产已发布。** `config/continuous_minute_sessions.csv`：5,299 条规则 / 63 品种 /
2011-01-04..2026-01-30，`ambiguous=0`，`publication_status=validated`，
sha256 `f8157ec3422d07a820b7451e6a29bf0929127c79d59dd27598cbf0f8e4e6d6c2`。

首跑被 58 条歧义挡下，压成 4 项（全部源于审计了 Carry 池从未覆盖的品种日）：
DCE EG 上夜盘前的日盘期、GFEX PT（广期所四品种全历史无夜盘）、INE 两个节后首日、
以及 SHFE AU 2019-12-26 那条本消费者合法用不上的例外。前三项由用户批准写入**批次 I** 授权行，
第四项改为**条件赦免**：声明审计范围后，范围**之外**未被消费的例外记入审计报告，
范围**之内**的仍然致命。

⚠️ `tests/test_carry_session_authority.py` 是一本**出处台账**，钉死授权表行数 ——
加授权行必须同时记账写明批次来由。（设计文档里「没有测试钉死行数」那句只对
`carry_minute_sessions.csv` 成立。）

**偏离四：`classify_authorized_boundaries` 有两个调用点。**
`_capture_and_publish_outcome` 里首次分类之后，sibling 二次判定会**再分类一次**。
给它加 `audit_keys` 时只改了第一处，于是有 sibling 的跑法（本次真实跑有 4 个 widening 事件）
把赦免整个丢掉、仍被那条 AU 例外卡住。修法不是补第二处，而是**把参数绑成一个闭包**让两处不可能分叉。
单测直接调该函数、永远够不到第二个调用点 —— 这一条只有真实跑能验。

**Task 10 —— 交叉验证 100.000000% 一致，零分歧。**
见 `docs/research/2026-08-28-session-asset-cross-validation.md`。重叠 175,134 个品种日全部相同，
既验证补采，也**反向验证了从未被独立复算过的 Carry 资产** —— 这正是 D3 选「整体重采」的回报。

**覆盖闸验收通过**：真实数据上静默放行、不落缺口清单，面板跑过了原先崩掉的 2011-05。
