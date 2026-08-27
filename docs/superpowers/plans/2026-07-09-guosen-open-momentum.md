# 国信开盘动量（股指期货）复刻 —— 摘要与实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复刻国信证券《基于开盘动量效应的股指期货交易策略》（研报日期 2021-05-13，本地 PDF 27 页）。

**Architecture:** 新建顶层包 `index_open_momentum/`，与 `cta_gtja` / `cta_carry` 平级，是本仓第一个股指期货包。**分钟数据层不重造** —— 复用本仓已建成的分钟机器（见下「本仓已有的机器」），本策略只提供开盘信号层、反向信号止损、隔夜规则和自己的报告。

**Tech Stack:** Python 3.13 / pandas / pytest；PG `public` schema（`futures_minute` + `futures_contract_info` + `continuous_contract_ohlc` + `trading_calendar`）；`common.config` / `common.db`。

---

## 迁移记录（2026-08-25）

本计划 2026-07-09 写于 `stock_selector`，写完当日起**从未开工**（该仓 `cta/open_momentum/` 从未存在，`grep open_momentum` 只命中计划文档自己）。2026-08-25 用户裁决**迁入本仓**，同时推翻 2026-07-12「股指期货类归股票生态、留 stock_selector」那条边界裁决。

裁决依据（当日实测，非推测）：

1. 原文「Current Local Data Assessment」整段是错的 —— 它只查了 `public.market_data_minute`，漏掉了 `public.futures_minute`；而后者正是**本仓** 2026-08-13 建的（`docs/plans/2026-08-12-futures-minute-ingestion-design.md`，661,626,782 行，入库脚本在 `scripts/futures_minute/`）。
2. 本策略需要的分钟机器 —— 版本化交易时段、交易时钟分钟槽、5 分钟 VWAP + 合约乘数解析、15 分钟吊灯止损三档减仓、影子账户波动率反馈 —— **本仓正在建**，与本策略重合约八成。留在 stock_selector 等于平行再造一遍。
3. stock_selector 的策略七层布局是股票形状（BaseFactor / 股票池过滤 / universe 注入 / 月度 picks Excel），日内期货策略哪一层都套不进；且其统一策略平台的 `asset_class` 是 `equity | fund` 闭合枚举，设计 §4 Non-Goals 直接写明不收 CTA。

同步改动的边界口径：本仓 `CLAUDE.md` / `README.md` / `docs/ROADMAP.md`，以及 `stock_selector/docs/strategies-layout.md`。

**语言约定**：下文「Strategy Summary」与 Task 3~6 保留原英文（论文参数的逐条转写，重译有引入误差的风险）；2026-08-25 新增或改写的段落用中文。

---
## Strategy Summary

Source report:

- `【国信金工】基于开盘动量效应的股指期货交易策略.pdf`
- Original report date in the extracted text: 2021-05-13.
- The PDF copy inspected locally has 27 pages.

Tradable universe:

- Paper universe: IF, IC, IH dominant stock-index futures contracts.
- Paper combines the three contracts with equal capital allocation.
- IM is not part of the paper universe because it listed after the paper.

Core signal:

- Build 15-minute K bars.
- Use the first 3 bars after the market opens, corresponding to the first 45 minutes.
- Long signal: the first 3 bars' `open`, `low`, and `close` are each strictly increasing.
- Short signal: the first 3 bars' `open`, `high`, and `close` are each strictly decreasing.
- If neither condition holds, do not trade that contract that day.

Entry and execution:

- Enter after the opening signal is confirmed.
- Paper execution price is VWAP over the 5 minutes after the signal.
- Exact paper execution requires minute-level turnover or amount. If only `last_price` and cumulative `volume` exist, the backtest must label execution as an approximation.

Intraday stop and scale-down:

- Reverse-signal stop:
  - Long position: 5 consecutive 15-minute bars with strictly decreasing highs.
  - Short position: 3 consecutive 15-minute bars with strictly increasing lows.
- Chandelier stop:
  - Long position: stop when close breaks below the best high since entry by `2.5 * ATR`.
  - Short position: stop when close breaks above the best low since entry by `2.0 * ATR`.
- Each bar can generate at most one scale-down event even if both stop families trigger.
- Each scale-down event reduces the initial position by one third.
- The third scale-down event flattens the position.

Overnight rule:

- If the opening signal is long and the position has not been fully flattened intraday, carry the remaining long position overnight and close at the next trading day's open.
- If the opening signal is short, close the remaining position on the same trading day's close.
- If there is no opening signal, keep zero position.

Leverage:

- ATR is computed on 15-minute K bars with a 16-bar window.
- True Range is `max(high - low, abs(high - previous_close), abs(low - previous_close))`.
- ATR capital management targets 1 ATR movement as 0.5% of total capital.
- The equivalent leverage ratio is `Lev_ATR = 0.005 * close / ATR`, clipped to 4.
- Realized-volatility multiplier is recomputed monthly from the past one year of strategy returns per contract family.
- The target annualized volatility is 15%.
- Final leverage is `clip(Lev_ATR * 0.15 / realized_vol, 0, 4)`.

Cost:

- Paper cost is `1.3%%`, made of `0.3%%` commission and `1.0%%` impact cost.
- In decimal return terms this is `0.00013` per one-way traded notional.

Paper headline result:

- Sample starts in June 2011.
- Fee-after annualized return: 25.79%.
- Sharpe ratio: 1.77.
- Maximum drawdown: 7.66%.
- Calmar ratio: 3.37.

## 本地数据实测（2026-08-25，全部经 PG 现场查询；取代原「Current Local Data Assessment」）

### 分钟数据：`public.futures_minute` —— 够，而且完整

TimescaleDB hypertable，264 chunk，**661,966,168 行**。列 = `bar_time`(timestamptz，bar open) / `symbol` / `exchange` / `open` / `high` / `low` / `close` / `volume` / `amount` / `open_interest`，PK `(symbol, bar_time)`。`volume` 是**每根 bar 的量**，不是日内累计。

| 品种 | 合约数 | 覆盖 | 行数 |
|---|---|---|---|
| IF | 199 | 2010-04-16 ~ 2026-08-11 | 3,885,490 |
| IC | 139 | 2015-04-16 ~ 2026-08-11 | 2,650,482 |
| IH | 139 | 2015-04-16 ~ 2026-08-11 | 2,647,092 |
| IM | 52 | 2022-07-22 ~ 2026-08-11 | 940,766 |

三个起点分别精确等于各自挂牌日。**逐年交易日数无缺口**：IF 2011~2025 每年 242~245 天；IC/IH 2015 年 177 天、IM 2022 年 110 天均等于挂牌后的剩余交易日；2026 年到 08-11 共 146 天。

**精确 VWAP 可算**（两端各验一次，IF 合约乘数 300）：

- `IF1106` 2011-06-01 09:15 bar：`1,086,437,100 / (1208 × 300) = 2997.9` ∈ `[2996.0, 2998.8]` ✓
- `IF2608` 2026-08-05 09:30 bar：`1,845,753,360 / (1349 × 300) = 4560.5` ∈ `[4542.8, 4569.0]` ✓

⇒ 论文的「信号后 5 分钟 VWAP 成交」**能精确复现**。原计划为绕开数据缺陷设计的「近似成交模式 + approximate 旗」**整套作废**。

**独立佐证（本仓 `docs/research/2026-08-25-czce-minute-turnover-is-synthetic.md`）**：该文用
「5 分钟 VWAP 是否落在同窗口 `[low, high]` 内」逐年逐品种系统性测过分钟 `amount` 的真伪，
结论 = **郑商所全部 27 个品种的分钟 `amount` 是合成的**（`成交量 × 某整数价 × 乘数`，
逐年中位通过率仅 32~40%），而**其它交易所（含 CFFEX）逐年中位通过率 100.0%**。
⇒ IF/IC/IH/IM 的 `amount` 是真成交额，上面两次手算不是孤证。

⚠️ 但这条同时说明「分钟 `amount` 可信」**不是全局事实，是按交易所的事实**。Task 2 的
VWAP 价域校验闸**不得因为"本策略只做股指"而省略** —— 它是这类供应商缺陷的唯一探针。

### 三条必须处理的时间结构

1. **开盘时刻跨期变过**：2011 / 2015 首根 bar = `09:15`，2016 起 = `09:30`（实测 `IF1106` / `IF1506` vs `IF1606` / `IF2608`）。论文样本自 2011-06 起，横跨 2016-01-01 那次交易时段调整 ⇒「开盘后前 3 根 15 分钟 bar」必须按**当期实际开盘时刻**锚定，不得写死 09:30。
2. **收盘尾段缺 15 分钟**：两个年代最后一根 bar 都是 `14:59`。2016 前 IF 官方交易至 15:15，该尾段本库没有。不影响开盘信号，影响 2011~2015 的「日内收盘平仓」成交价口径，必须在保真度报告中显式披露。
3. **午休边界干净**：11:29 → 13:00，无跨休时段脏 bar。

### 新鲜度与主力合约

- `futures_minute` 最新 bar = **2026-08-11**；近 10 天增量只有 I / RB / SC / SI / T 几个非中金所品种。回测不受影响；将来要出实盘信号须先补这条尾巴。
- 主力合约映射：`public.continuous_contract_ohlc` 有 `base_symbol` / `contract_used` / `is_rolling` / 换月字段，IF/IC/IH/IM 全有，**但只到 2026-04-29**（上游冻结）。本计划取「用 `futures_minute` 自身的 `open_interest` / `volume` 自定主力」（可跑到 08-11），并在 2026-04-29 前的重叠区与 `contract_used` 对账。

### `public.market_data_minute` —— 不要用

Wind 实时快照表：中金所只有 IC / IM（2026-05-13 起），**无 IF / IH**；列里没有 `amount` 或 turnover，`volume` 是日内累计，`last_price` 无 close / OI ⇒ **精确 VWAP 算不出来**。两张表的语义差异，本仓入库设计决策 #1 已有记载。

---

## 本仓已有的机器（分支 `feature/carry-minute-execution`，2026-08-25 仍在演进）

设计 = `docs/superpowers/specs/2026-08-13-carry-minute-execution-design.md`；实现在分支 `feature/carry-minute-execution`（124 commit / 77 文件 / +35,911 行，worktree 在 `.worktrees/carry-minute-execution`，当日仍有未提交改动）。

> **2026-08-26 更新**：分支已 merge（master `1b5610b`）；其中四个模块已按 Task 0 裁决 (A)
> 抽到 `common/minute/`（commit `9a4ea9b` 净搬 + `74b4e3d` 破商品口径耦合）。下表已按新路径写。

| 模块 | 行数 | 对本策略的价值 |
|---|---|---|
| `common/minute/sessions.py` | 928 | 版本化交易时段 + 交易时钟分钟槽。~~商品口径写死在模块常量上~~ ⇒ **已解耦**：市场特性移到 `SessionRuleset` 值对象，股指注册一个 `cffex-v1` 即可，见 Task 1 |
| `common/minute/bars.py` | 1,073 | 分钟 bar 校验、确定性聚合、合约乘数反推。docstring 已中性（"contract minute bars"） |
| `common/minute/pg_source.py` | 2,084 | 有界 PG 访问：按月分批、临时表驱动裸列嵌套循环探针、事务级 `SET LOCAL` 保护 |
| `common/minute/account.py` | 454 | 逐事件计价账户；正式账户与未缩放影子账户并行 |
| `cta_carry/minute_backtest.py` | 2,291 | 15 分钟吊灯止损三档减仓、VWAP 成交窗口、日末冲突合并 |
| `cta_carry/session_authority.py` | 982 | 以交易所公告为准的时段权威源 |
| `config/carry_minute_sessions.csv` | 5,293 行 | 时段规则数据（商品） |

与本策略的对应关系：论文的 15 分钟吊灯止损（多头 `2.5×ATR`、空头 `2.0×ATR`）、三档减仓、每根 bar 最多一档、第三档平仓，与该设计 §7.2 逐条同构；ATR 杠杆 + 已实现波动率反馈 + 4 倍帽与其 §3.1 / §8.2 同构。本策略独有的是：开盘前 3 根 bar 定多空、反向信号止损（多头 5 根递减高点 / 空头 3 根递增低点）、隔夜规则，以及**股指无夜盘**（比商品少一层）。

**设计 §5.3 的 TimescaleDB 查询纪律对本策略同样适用、不可豁免**：按自然月分批；候选合约写临时表并驱动对 `m.symbol` **裸列**的嵌套循环索引探针；**禁止在 `m.symbol` 上使用 CASE / 正则 / 截断 / 大小写函数**；分钟表查询事务内 `SET LOCAL max_parallel_workers_per_gather=0` / `work_mem='32MB'` / `statement_timeout='300s'` / `enable_hashjoin=off` / `enable_mergejoin=off`；**任何新查询在全历史运行前都要保存 `EXPLAIN`，计划若包含对 264 个 chunk 的无界全扫描或预计扫描接近 6.6 亿行，验收失败。**

⚠️ `minute_backtest.py`（2,292 行）/ `session_authority.py`（982 行）/ `decision.py` **仍在 `cta_carry/`**：前者是 Carry 的策略语义，后两者是商品交易所公告资产。本策略不复用它们的实现，只复用其形状。

---
## Future Implementation Plan

> Task 0~2 与 Task 7~8 为 2026-08-25 按实测数据与本仓已有机器重写；Task 3~6 保留 2026-07-09 原文（纯策略逻辑，与数据源无关）。

### Task 0: 定分钟层归属与排期（**已裁决 —— 2026-08-25**）

**Files:**

- 无代码产出；结论已回填本文件与 `docs/ROADMAP.md`

- [x] **裁决 = 方案 (A)**：等 `feature/carry-minute-execution` merge 后，把 `minute_sessions` /
  `minute_bars` / `minute_pg_source` / `minute_account` 抽到 `common/minute/`，Carry 与本策略
  各接一个信号层。理由 = 那六个模块正在高频改动（124 commit，裁决当日仍在提交），
  现在接第二个消费者的冲突成本高于等待成本。被否的 (B) = 先只读依赖 `cta_carry.minute_*`。
- [x] **落地触发条件 = 该分支 merge 进 master。** ✅ 2026-08-26 18:18 merge（`1b5610b`）。 裁决当日实测状态：主计划
  （`docs/superpowers/plans/2026-08-13-carry-minute-execution.md`）13 个 Task 中 Task 1~11 的代码
  均已在分支上；Task 12（数据库冒烟 / 全量验证 / 文档）在做；Task 13（两个配对研究窗 +
  证据报告）未开始；会话权威批次 A/C/D/E 已过目写入资产，**G / G3 / H 仍待过目**。
  ⇒ **不是即将 merge，等待期以周计——不要空转轮询，也不要因为"等不及"改投 (B)。**
- [x] 抽层必须保证 Carry 侧 CLI 输出**逐点不变**（沿用其设计 §3.1 对日线 CLI 的同一条约束），
  以分支上现有的 Carry 分钟测试家族为回归闸
- [x] 结论已回填本文件与 `docs/ROADMAP.md`

**2026-08-26 完工记录（commit `9a4ea9b` + `74b4e3d`）**

抽层分两步落地，第一步净搬、第二步才动耦合：

1. `9a4ea9b` **净搬**：`minute_{sessions,bars,pg_source,account}` → `common/minute/{sessions,bars,pg_source,account}`，
   零行为改动，全套件仍 **1054 passed**。附带必须搬的一件：`EquityDepletedError` 原定义在
   `cta_carry/backtest.py`（**日线** Carry 引擎），而 `minute_account` 引用它 —— 不搬就成了
   `common/` 反向依赖 `cta_carry/`。已移到 `common/errors.py`，`cta_carry.backtest` 原地再导出，
   所有 raise/except 点看到的**类身份不变**。
2. `74b4e3d` **破口径耦合**。⚠️ 这一步是抽层的真正内容，此前被低估：搬到 `common/` 并不使它市场中立。
   `SESSION_RULES_VERSION` / `DAY_SEGMENTS` 是模块级常量，`SessionRule.__post_init__` 直接硬拒
   非 `commodity-v1` 的版本，`load_session_rules()` 给 5,292 条规则统统盖同一份日盘段（CSV 只带夜盘）。
   现引入 `SessionRuleset` 值对象承载市场特性（version / capture_start / day_segment_schedule /
   clock 边界 / allows_night），由调用方传入。

📌 **两条实地发现，直接改写了 Task 1 的形状**：

- **日盘段必须按日期分期，不能是一个 tuple**：CFFEX 2016-01-01 缩短过日盘。所以
  `SessionRuleset.day_segment_schedule` 是**按生效日排序的日程**，`load_session_rules` 按每行自己的
  `effective_start` 取段。商品是单条目日程（退化情形），股指两条目。
- **15:15 = 第 915 分钟，而 `SessionSegment` 原本拒绝 > 900**。⇒ 2016 前的股指日盘**根本构造不出来**。
  已把结构性边界抽成 `CLOCK_MIN_MINUTE`/`CLOCK_MAX_MINUTE`（-180 / 960），各市场自己更紧的窗口
  声明在 ruleset 上、建规则时强制。⚠️ 原来那条 900 的守卫**没有任何测试守着**（本次新增了）。

**"逐点不变"是量出来的，不是断言的**（两条独立证据）：

- 全套件 **1065 passed**（1054 + 新增 11）
- 拿真实的 `config/carry_minute_sessions.csv`，用抽层**前**（`9a4ea9b`）与**后**的代码各跑一遍
  `load_session_rules`：**5,292 条规则 sha256 完全相同**；再对 185 个 product-day 比
  `build_trading_slots` 与 `fifteen_minute_buckets`，**逐点一致**。

新增 11 个用例经**变异验证 8/8**，按"打红集合 == 预期集合"验收。📌 其中一条用例是变异逼出来的：
日程顺序守卫写的是 `>=`，而原用例喂的是**乱序**日期（`>` 也能抓），所以把 `>=` 放宽成 `>`
当场漏网 —— 补了一个**同日重复**的用例才守住。

**(A) 的排期后果 —— 哪些现在能做、哪些必须等（已全部解除）：**

| Task | 是否被 (A) 阻塞 | 说明 |
|---|---|---|
| Task 1 CFFEX 时段版本 | ~~阻塞~~ **可开工** | 目标文件 `common/minute/sessions.py` 已就位 |
| Task 2 15 分钟 K 线 / VWAP | ~~阻塞~~ **可开工** | 薄封装，依赖分钟层 |
| **Task 3~6** 开盘信号 / ATR 与止损 / 持仓路径 / 杠杆组合 | **不阻塞** | 纯策略逻辑 + 确定性合成数据，不碰分钟层，可先按 TDD 写完 |
| Task 7 PG 源与 CLI | ~~阻塞~~ **可开工** | 依赖分钟层的有界访问 |
| Task 8 报告与 runbook | 部分 | 报告骨架可先写；数据要求段等 Task 1/2/7 |

⇒ 起手顺序：~~Task 3 → 4 → 5 → 6~~（已完工）→ ~~抽层~~（已完工 2026-08-26）→
**下一步 Task 1 → 2 → 7 → 8**。

### Task 1: CFFEX 时段版本与数据质量闸

**Files:**

- Modify: `common/minute/sessions.py`（注册 `CFFEX_V1` 到 `SESSION_RULESETS`）
- Create: `config/index_minute_sessions.csv`
- Test: `tests/test_index_open_momentum_sessions.py`

> **抽层后的形状（2026-08-26）**：`SessionRuleset` 已能表达本 Task 需要的一切，照着写即可 ——
>
> ```python
> CFFEX_V1 = SessionRuleset(
>     version="cffex-v1",
>     capture_start=date(2010, 4, 16),          # IF 挂牌日
>     day_segment_schedule=(
>         (date(2000, 1, 1), (SessionSegment(555, 690), SessionSegment(780, 915))),
>         (date(2016, 1, 1), (SessionSegment(570, 690), SessionSegment(780, 900))),
>     ),
>     clock_start_minute=540, clock_end_minute=915, allows_night=False,
> )
> ```
>
> 两条日程条目 + `allows_night=False` 的行为已在 `tests/test_common_minute_sessions.py` 用
> **同形状的合成 ruleset** 覆盖并变异验证过（那里叫 `test-two-era`），本 Task 只需换成真值
> 并与 `futures_minute` 实测首尾交叉校验。
>
> 📌 注册后 `SESSION_RULESETS` 会有两个成员。`test_an_unregistered_version_fails_and_names_what_is_registered` 已改用一个
> 永不会被注册的版本名，不会因为本 Task 而失守。

- [x] 新起 `cffex-v1` 时段版本：2016-01-01 前日盘 `09:15–11:30` / `13:00–15:15`，2016-01-01 起 `09:30–11:30` / `13:00–15:00`；**无夜盘**
- [x] 时段规则须与 `futures_minute` 实测首尾时间交叉校验并记录版本；任何时间戳映射不到唯一时段则**硬失败**，不靠"数已有行数"压缩时间
- [x] 把「本库 2016 前最后一根 bar 是 14:59、官方到 15:15」显式登记为已知缺口，进质量审计与保真度报告
- [x] 覆盖度闸：IF/IC/IH 在请求窗口内的逐年交易日数与 `public.trading_calendar` 对账，缺口超阈值即拒绝标注 paper-faithful
- [x] 测试：时段版本切换日、午休边界、无夜盘、映射失败硬失败、覆盖度闸拒绝路径

**2026-08-27 完工记录**

交付：`common/minute/sessions.py` 注册 `CFFEX_V1` / `config/index_minute_sessions.csv`
（7 行：IF/IC/IH 各两个年代 + IM 一个）/ `index_open_momentum/sessions.py`
（资产加载 + 已知缺口登记 + 时间戳映射硬失败 + 覆盖度闸）/
`tests/test_index_open_momentum_sessions.py`（42 用例）。

**全套件 1107 passed**（基线 1065 + 新增 42）。**变异验证 17/17**。
证据 = `docs/research/2026-08-27-cffex-session-crosscheck.md`。

📌 **三处与本 Task 原文不符，已按实测改**：

1. **Step 4 的字面写法不可执行** —— `public.trading_calendar` 只有
   `sfe / hkxe / comex / lme / nyse / tse / sgx`，**没有 CFFEX 列**。改用 `sfe`，
   并先证等价：2010–2025 逐年与 IF/IC/IH/IM 的日线行数**一个不差**。
   （2026 差 6 天 = 已知的 `futures_daily` 2026-03 空洞传导，不是口径不符。）
2. **Step 3/4 的代码没有归宿** —— Files 段只列了 ruleset、CSV、测试三样。已新建
   `index_open_momentum/sessions.py` 承载缺口登记与闸门；取数留给 Task 7
   （闸门本身是纯函数，按验收标准"先由确定性合成数据覆盖"）。
3. **阈值未定** —— Step 4 写"缺口超阈值"但没给阈值。取 `max_missing_days=0`
   fail-closed 为默认，可由调用方放宽；放宽只改 `paper_faithful` 一个判定，
   缺口本身照报不误。

⚠️ **实测逼出的一条设计约束**：2016 前官方段长 **270** 分钟，而本库只有 **255** 根
（缺 15:00–15:14）。ruleset 记**官方**口径，那 15 分钟由 `CFFEX_ARCHIVE_GAPS`
登记为授权缺席 —— **不能**把 915 改成 900 去迁就档案，否则将来补上数据后，
15:00 之后真出现的 bar 反而会被当成非法数据拒掉。
不登记的话，任何"每个 slot 都应有 bar"的完整性校验会在 2016 前**每一个**交易日上硬失败。

⚠️ **给 Task 7 的实测约束**：同一条探针，时间边界从 join 列推导 ⇒ **264 个 chunk 全部
进计划**、planning 209 ms；改成 `TIMESTAMPTZ` 字面量 ⇒ **1 个 chunk**、planning 4.9 ms。
**chunk 排除只在边界是计划期常量时才发生** —— 按月分批的意义正在于让边界成为常量。

### Task 2: 15 分钟 K 线与 5 分钟 VWAP

**Files:**

- Create: `index_open_momentum/bars.py`（薄封装；聚合与乘数解析复用分钟层）
- Test: `tests/test_index_open_momentum_bars.py`

- [x] 15 分钟 K 线按连续 15 个**交易时钟分钟槽**构造，绝不跨休市拼成一根
- [x] 5 分钟成交窗口按五个交易时钟分钟槽计数；`vwap = sum(amount) / sum(volume) / contract_multiplier`，只汇总 `volume > 0` 的分钟
- [x] VWAP 落在窗口 `[min(low) - eps, max(high) + eps]`（`eps = 1e-6 * max(1, |low|, |high|)`）之外则硬失败
- [x] IF/IC/IH/IM 合约乘数优先取 `public.futures_contract_info`，缺失时按分钟层既有规则反推（确定性抽样、99% 价域通过率、唯一解，否则硬失败）；记录 `metadata` / `inferred` 来源
- [x] 一根 15 分钟 K 线若无任何正成交量，记 `no_trade_bar`，不更新吊灯极值、不触发止损
- [x] 一个必需的 5 分钟成交窗口若总成交量为零，**运行硬失败**，不顺延到更晚窗口
- [x] 测试：跨午休不拼接、跨时段版本切换日、零成交 bar、乘数反推与元数据冲突、VWAP 越界硬失败

### Task 3: Implement Opening Trend Signals

**Files:**

- Create: `index_open_momentum/signals.py`
- Test: `tests/test_index_open_momentum_signals.py`

- [x] Add strict long-signal detection from the first 3 bars: increasing `open`, `low`, and `close`.
- [x] Add strict short-signal detection from the first 3 bars: decreasing `open`, `high`, and `close`.
- [x] Return a neutral signal when there are fewer than 3 valid opening bars or either strict condition fails.
- [x] Add tests for long, short, neutral, ties, and missing opening bars.

**2026-08-25 完工记录**：8 个测试全绿。两条"写完就过"的断言（tie、第四根不影响判定）
已做变异验证 —— `<` 放宽成 `<=` 只打红 tie 那条、去掉 `[:3]` 切片只打红第四根那条，
两次都是精确一条，证明它们不是空断言。另外两处是实测驱动出来的：
① 空序列与两根序列原本会被判成多头（`all(())` 恒真），由测试当场抓出；
② 多头判据只读 open/low/close，一根只有 `high` 是 NaN 的坏 bar 会沿判据盲区溜过去
——因此 `Bar.is_valid()` 显式判有限性，不依赖"NaN 比较恒为假"这条巧合。

### Task 4: Implement ATR and Stop Events

**Files:**

- Create: `index_open_momentum/risk.py`
- Test: `tests/test_index_open_momentum_risk.py`

- [x] Compute True Range from 15-minute bars.
- [x] Compute 16-bar rolling ATR with previous close.
- [x] Add long reverse-stop detection using 5 strictly decreasing highs.
- [x] Add short reverse-stop detection using 3 strictly increasing lows.
- [x] Add long chandelier stop using `best_high_since_entry - 2.5 * ATR`.
- [x] Add short chandelier stop using `best_low_since_entry + 2.0 * ATR`.
- [x] Add tests proving one bar can count as only one scale-down event even when both stop families trigger.

**2026-08-25 完工记录**：27 个测试全绿，10 个变异**逐一按"打红集合等于预期集合"验收**（不是只看红不红）。

三条要留给后来者的：

1. ⚠️ **复刻假设：ATR 取 16 根的简单算术均值，不是 Wilder 平滑。** 研报只写"16 根窗口"，
   没写平滑方式。两者在同一窗口下数值不同，会移动吊灯止损的触发时点 —— 这是必须写进
   保真度报告的显式假设。同理，TR 跨隔夜时把跳空计入（前收取上一交易日末根），也是假设。
2. **ATR 窗口正好等于一个交易日**：股指日盘 09:30–11:30 + 13:00–15:00 = 240 分钟 = 16 根。
   ⇒ ATR 必须在**跨日连续**的 15 分钟序列上算，不能按 session 重置；否则建仓时（当日第 3 根）
   永远拿不到 ATR。这一条直接约束 Task 7 的取数窗口：每个交易日至少要多取前一交易日。
3. **无 ATR 时硬失败，不返回"未触发"**：窗口未满就判"不触发"，等于在风控最弱的时刻把风控
   关掉，而且外部现象与"行情很稳"完全一致。研报口径下没有 ATR 就不该建仓（既算不出吊灯
   阈值也算不出杠杆），所以缺 ATR 属于调用方的错，`_require_atr` 抛 `ValueError`。

⚠️ **变异 harness 的一个坑（第一次跑时真踩了）**：等长变异（`= 3` → `= 2`）在同一秒内写入时，
`.pyc` 的 `(mtime, size)` 校验不失效，pytest 会跑**上一轮**的字节码 —— 现象是几个互不相关的
变异报出同一批红测试，而 APPLIED 回执一切正常。**APPLIED 回执只证明文件变了，不证明测试
跑的是变后的代码。** 必须 `PYTHONDONTWRITEBYTECODE=1` + 每轮清 `__pycache__`，并断言打红
**集合**而非只看红不红。harness 存档：`scratchpad/mutate.py`（不入库，方法写在这里）。

📌 这一轮还抓出一条测试名 overclaim：`test_the_two_reverse_stops_read_different_columns_and_different_lengths`
原先的多头用例全是 3 根 bar，**在长度守卫处就返回了，走不到"读哪一列"**——名字里的
"different columns" 对多头侧根本没验到。已补一组 5 根、low 严格递减而 high 恒定的 bar。

### Task 5: Simulate Intraday and Overnight Position Paths

**Files:**

- Create: `index_open_momentum/backtest.py`
- Test: `tests/test_index_open_momentum_backtest.py`

- [x] Enter after the opening signal confirmation.
- [x] Reduce the initial position by one third for each stop event.
- [x] Flatten after the third stop event.
- [x] Carry remaining long exposure overnight only for long opening signals that are not fully flattened.
- [x] Flatten short exposure on same-day close.
- [x] Record a trade ledger with entry, scale-downs, day-close exits, overnight exits, gross return, cost, and net return.

**2026-08-25 完工记录**：9 个测试全绿，8 个变异全部捕获（其中 3 个按"打红集合 == 预期集合"验收）。

**成交价是注入的**：`simulate_session(fill_price=...)`。生产上它是分钟层的"信号后 5 分钟
VWAP"（Task 2/7，被 Task 0 裁决 A 阻塞），测试里是确定性字典。这条缝使持仓路径逻辑
不必等分钟层就能验完 —— 也是 Task 3~6 能在 (A) 方案的等待期里做掉的原因。

⚠️ **一条复刻假设**：入场后的价格极值**从建仓那根之后的第一根起算**，开盘三根自身的
高低价不属于这笔持仓。研报没写极值从哪根起算。这条假设有实质后果，已用专门用例钉住
（第 0 根 high 抬到 4200：按错误口径第 3 根就会触发吊灯，按本口径全程不触发）。
Carry 的分钟执行设计 §7.2 用的是更严的 `bar_start >= fill_end`（跳过与成交窗重叠的
那根）—— 抽层（Task 0-A）之后应当对齐到那条，届时本假设作废，须重跑对照。

⚠️ **隔夜仓缺次日开盘价 → 硬失败**，不静默用当日收盘代替。多头未被打平就必须持隔夜，
拿收盘价冒充次日开盘价会把隔夜跳空的损益整段抹掉，而报表上看不出任何异常。

📌 变异跑出的一处**测试盲区**（补了才有人守）：第三档减完后若不 `break`，后续 bar 仍会
再触发 —— 但原有用例里第三次止损恰好发生在最后一根 bar 上，永远走不到那个分支。已补
`test_a_flat_position_takes_no_further_stops_for_the_rest_of_the_day`。

### Task 6: Add Leverage and Portfolio Combination

**Files:**

- Create: `index_open_momentum/leverage.py`
- Modify: `index_open_momentum/backtest.py`
- Test: `tests/test_index_open_momentum_leverage.py`

- [x] Add ATR leverage: `clip(0.005 * close / ATR, 0, 4)`.
- [x] Add monthly realized-volatility multiplier using one year of prior strategy returns.
- [x] Combine IF, IC, and IH by equal capital across available active families.
- [x] Keep IM excluded from the paper-faithful profile.
- [x] Add tests for leverage clipping, zero ATR handling, missing realized-volatility history, and active-family equal weighting.

**2026-08-25 完工记录**：14 个测试全绿，10 个变异**逐一按"打红集合 == 预期集合"验收**。

**两道截断的顺序有实质差别**，已单独钉住：`close=4000 / atr=2 / 已实现波动 60%` 时，
先截 `Lev_ATR` 得 1.0，不先截得 2.5。

三处对"研报沉默处"的显式裁定（都要进保真度报告，不是实现细节）：

1. **`ATR == 0` 取 0（当日不建仓），不是按字面把 `+inf` 截到 4。** 此时公式是除零、
   没有定义。按字面取上限，等于在跌停封死或数据坏掉的那天上满杠杆 —— 所有解读里最差的。
2. **不满一年策略收益取 0（不建仓），不是把乘数默认成 1。** 这条同时给研报的样本起点
   一个**可检验的解释**：IF 2010-04-16 挂牌、研报样本自 2011 年 6 月起，相隔约 14 个月，
   与"先攒够一年策略收益再开跑"一致。⚠️ 是假设不是原文，列为待检验项。
3. **等权分母是当日活跃品种数，不是恒定的 3。** 某品种当日无信号时资金给到其余品种，
   不空置。IM 不在忠实口径内（挂牌 2022-07-22，晚于研报 2021-05-13），传入直接报错。

⚠️ **8-25 补记：上面那个勾我先打早了。** "Add **monthly** realized-volatility multiplier"
里的**月节拍**当时没实现 —— 只有 `realized_volatility()` 这个纯函数。已补
`monthly_realized_volatility()`：窗口在**当月第一天之前**截断，一刀同时兑现两条性质
——① 月内保持不变（研报是 recomputed monthly，不是逐日重算；逐日会让杠杆天天微动，
换手与成本都不再是研报那条曲线）；② 当月自己的收益不参与（否则月中的杠杆会知道本月
前几天发生了什么，是前视）。四个变异全部按集合吻合。

⚠️ **窗口取"最近 252 个观测"而非"最近 365 个自然日"**，又一条显式假设（停市长假下
窗口长度稳定，代价是真实跨度略长于一年）。📌 这条假设第一次变异时**漏网**：原有
fixture 是连续自然日，252 个观测恰好只跨 252 天，两种口径选出同一批、差别不可见。
已补一个 3 天一个观测、跨 897 天的稀疏历史用例才守住 —— **写在 docstring 里的假设
不等于有人守，得有一个能分辨它的 fixture。**

📌 顺带删掉一段死代码：`monthly_realized_volatility` 里原本又写了一遍长度守卫，而
`realized_volatility` 已经拦了（切片后不足照样返回 None）。同一条规则两个出口迟早分叉。

**已实现波动率用样本标准差（`ddof=1`）**，写死并由手算用例钉住 —— 本仓为 ddof 取舍
吃过一次亏（vol_basis 差 1.16%），这不是可有可无的细节。

📌 抓到并修掉一条自指断言：截断测试原先拿 `MAX_LEVERAGE` 当期望值，常量改了测试跟着改，
等于没有断言（变异"上限 4→8"当场漏网）。已全部换成手写字面量 `4.0`。

**2026-08-27 完工记录**

交付：`index_open_momentum/bars.py`（`IndexBar` / `build_index_bars` /
`resolve_index_multiplier` / `relative_excursion` / `index_execution_fill`）+
`tests/test_index_open_momentum_bars.py`（27 用例）。
另改 `backtest.py` 与 `signals.py` 以承载 no-trade 语义（见下）。
**变异验证 14/14**，证据 `docs/research/2026-08-27-cffex-session-crosscheck.md` §五~八。

📌 **三处与本 Task 原文不符，已按实测裁**：

1. **eps 取 1e-6 还是 1e-4** —— 本 Task 写 `1e-6`，共享层 `five_minute_vwap` 实际用
   `_fill_epsilon = 1e-4`（理由是**商品**涨停锁死 bar 的 turnover 取整残差）。
   实测 IF/IC/IH 主力四年 **2,139 个真实执行窗口，最大相对越界 = 0.0**（含一个涨停
   锁死窗口）⇒ 按本 Task 口径收紧到 **1e-6**。两条边界：**零宽窗口豁免紧闸**
   （只可能成交在这一个价上，越界只能是取整残差；但 `relative_excursion()` 仍如实报出），
   且**本层的闸只能更紧、越不过共享层的 1e-4**。
2. **`futures_contract_info` 是快照不是历史** —— 实测只覆盖 2025-12-22 起。所以
   「优先元数据、缺失才反推」在 2011–2025 **绝大部分走反推**。元数据在场也要过价域校验。
3. **no-trade 语义没有归宿** —— 本 Task 要求"不更新吊灯极值、不触发止损"，但那发生在
   Task 5 的 `simulate_session` 里。已把它的入参从 `Sequence[Bar]` 改为
   `Sequence[Bar | None]`，`None` 即"这根没成交"。**不用平行布尔数组**：那样调用方
   得伪造一根 `Bar` 占位，正是 `types.Bar` 反对的事，且两个序列还能对不齐。
   Task 3~6 的既有用例全部原样通过。

⚠️ ~~**实测：no-trade 是冷路径**~~ —— **该结论已于同日被端到端真跑推翻**：
2016 Q1 的 IF 主力 58 个 product-day 里有 **14 根**整桶零成交（`missing_slots=0`）。
先前那句依据的是 2012/2015/2018/2024 四年抽样，**2016 不在样本里**。
⇒ no-trade 是**常规路径**，`atr_series` 必须能穿过它（真跑当场炸在这里）。

⚠️ **新增复刻假设（第 9 条）**：研报没写无成交 bar 算不算数。取**「打断反向信号的
连续计数」**而非「透明跳过」—— 透明跳过是在断言"这段时间价格没动过"，比数据支持的更强。
同理，空头当日收盘平仓必须用**入场之后**有成交的 bar：退到入场那根本身是回看，
一根都没有则硬失败。

### Task 7: 数据源与 CLI

**Files:**

- Create: `index_open_momentum/pg_source.py`
- Create: `index_open_momentum/__main__.py`
- Test: `tests/test_index_open_momentum_pg_source.py`

- [x] PG 读取器面向 `public.futures_minute`，遵守本文件「本仓已有的机器」段落转述的设计 §5.3 全部查询纪律（按月分批 / 临时表驱动 / 裸列探针 / `SET LOCAL`）
- [x] 主力合约按 `open_interest` / `volume` 自定，并在 2026-04-29 前的重叠区与 `continuous_contract_ohlc.contract_used` 对账，不一致须报告而非静默取一边
- [x] 每条新查询保存 `EXPLAIN` 产物；出现 264 chunk 无界全扫或预计扫描接近 6.6 亿行即验收失败
- [x] CLI 选项：起止日期、品种、时段版本、输出前缀、是否强制 paper-faithful
- [x] `amount` 缺失或必需的 5 分钟窗口零成交时**硬失败** —— 不降级为近似成交、不顺延窗口（原计划的 approximate-execution 旗已随数据源更正作废）

**2026-08-27 部分完工记录（5 步完成 4 步）**

交付：`index_open_momentum/pg_source.py`（主力选取 / 对账 / candidate 构造 /
尾部通路）+ `index_open_momentum/__main__.py`（参数面与三道当场拒绝的闸）+
`tests/test_index_open_momentum_{pg_source,cli}.py`（30 + 18 用例）。
共享层补一处：`_DAILY_TO_MINUTE_EXCHANGE` 原本**没有 `CFE`** ⇒ `IF1606.CFE` 会被拒，
已补 `"CFE": "CFFEX"`。**变异验证 14/14**，证据见研究档 §九~十二。

⬜ **未完成的第 5 步 = "每条新查询保存 `EXPLAIN` 产物"的落盘。**
`PublicMinuteSource.plan_audit` 已经在内存里攒了 `MinutePlanSummary`，
真候选也已过闸实测（2016-06 与 2012-06 各引用 **2 个 chunk**，上限 3），
但**没有任何东西把它写到磁盘**。CLI 的 `main()` 目前校验完参数即停
（取数编排与报告尚未接线）。

📌 **一个被我读错又更正的边界**（这条比代码要紧）：先前据
`max(trade_date)` 判断 `futures_daily` 的中金所数据"没有冻在 04-29"，**是错的**。
逐月看，IF 在 2026-05/06/07 **一行都没有**，2026-08 只有一天 4 行。
⇒ `max(trade_date)` 回答的是"最后一行在哪天"，不是"覆盖到哪天"。
取数上界必须**逐月数行**。四张表的实测区间见研究档 §十。

⚠️ **对账分歧全部落在换月窗口**，不是缺陷：自选用因果滞后规则，参照
（`continuous_contract_ohlc`）用混合滚动，交割周必然错开一两天，参照那边还会出现
`IF1606.CFE+IF1607.CFE` 这种混合值。按计划要求**只报告、不改选**。

### Task 8: Add Reporting and Runbook

**Files:**

- Create: `index_open_momentum/report.py`
- Create: `docs/operations/guosen-open-momentum-runbook.md`
- Test: `tests/test_index_open_momentum_report.py`

- [x] Write Excel outputs for metrics, daily returns, annual returns, trades, signals, leverage, and data-quality audit.
- [x] Write a net-value chart.
- [x] Document data requirements for paper-faithful replication.
- [x] runbook 记录：数据源为 `public.futures_minute`（最新 bar 2026-08-11，实盘前需补尾）、时段版本 `cffex-v1`、2016 前 15:00–15:15 缺口的披露口径、以及 `EXPLAIN` 存档位置。
- [x] **样本外一段单独出**：研报样本 2011-06 起、研报日期 2021-05-13 ⇒ 其样本止于 2021。报告必须把 **2021-05 之后**单独成段，与复刻区间并列呈现，不允许只给全样本合并数。

**2026-08-27 完工记录（Task 7 收尾 + Task 8）**

交付：`index_open_momentum/run.py`（回测编排）+ `report.py`（七张表 / 净值图 /
审计 JSON）+ `__main__.py` 接线 + `docs/operations/guosen-open-momentum-runbook.md`
+ 三个测试文件（run 28 / report 26 / cli 18）。**变异验证 run 13/13、report 10/10。**

**✅ 端到端真跑通过**（2016-01-04~2016-03-31，IF，生产库）：
58 个 product-day / 12 天有信号 / EXPLAIN 3 条各引用 **2 个 chunk**（上限 3）、
最大预计行数 92 万（上限 1000 万）/ 四个合约乘数全部由兜底**升级为价域校验过**
（`pass_rate=1.0`）/ VWAP 最大相对越界 **0.0**。产出三件齐全。

📌 **接线阶段炸出五个真缺陷**，每个都不是接线问题而是设计问题：

1. **`atr_series` 收到 `None` 直接崩** —— no-trade bar 是真实存在的（见 4）。
2. **`simulate_session` 没 ATR 也建仓**，然后在第一次判止损时被 `_require_atr` 炸掉。
   闸放在 **`run.py`**（杠杆归它），不放在 `simulate_session`（那会改掉 Task 5 的契约）。
3. **波动率反馈算在已加杠杆的收益上 ⇒ 整段回测恒为零**：第一年没历史 ⇒ 杠杆 0 ⇒
   收益全 0 ⇒ 波动率 0 ⇒ 杠杆仍 0，**永远启动不了**，而症状是"跑完了只是都不赚钱"。
   已改为**未缩放的影子收益**（只加 ATR 杠杆），即计划 §「本仓已有的机器」提到的
   "正式账户与未缩放影子账户并行"。
4. **换月后新主力头两天推不出乘数**（样本要跨 ≥3 个交易日，而新合约在我们取的
   数据里 0 天历史）。实测 2016-03-18 的 `IF1604` 即是。已加交易所乘数兜底
   （`source="metadata_unvalidated"`，样本一够**自动升级**为校验过）。
   15 年 × 3 品种约 540 个交易日会因此发不出成交，不是边角情形。
5. **止损可能落在当日最后一根**，而其后没有 5 分钟成交窗 ⇒ 无从计价。
   **新增复刻假设⑪：当日最后一根不判减仓**（剩余仓位本来就由日末规则处理）。

⚠️ **一条先前结论被真跑推翻**：「no-trade 是冷路径」作废 —— 2016 Q1 的 IF 主力
58 个 product-day 里有 **14 根**整桶零成交（`missing_slots=0`）。先前那句依据的是
2012/2015/2018/2024 四年抽样，2016 不在样本里。**抽样得出的"从未出现"只对抽到的
样本成立。**

⬜ **仍未接线**：尾部（2026-04-29 之后）的主力通路（名单取 `futures_contract_info`、
量取 `futures_minute`）。CLI 走到那条会显式报"尚未接线"，不静默降级。
⇒ 当前可跑区间 = **2010-04-16 ~ 2026-04-29**。

## Acceptance Criteria

- 策略逻辑先由确定性合成数据测试覆盖，再接生产数据（TDD）。
- 全程精确 VWAP，无近似成交模式；任一成交窗口不满足 Task 2 的校验即硬失败。
- IF/IC/IH 的覆盖度闸与时段映射全部通过后，才允许把结果标注为 paper-faithful；2016 前 `15:00–15:15` 缺口必须在报告中显式披露。
- 全历史运行之前，每条新查询都有存档的 `EXPLAIN`。
- 与论文口径对账：样本自 2011-06 起，费后年化 25.79% / Sharpe 1.77 / 最大回撤 7.66% / Calmar 3.37。差异逐项归因，**不调参逼近论文收益**。
- **样本外（2021-05 之后）必须单独成段呈现**。理由不是谨慎，是本仓已经撞过一次：国信 Carry
  那篇的样本**恰好止于策略失效前**（见记忆 `guosen-carry-paper-direction-is-wrong` /
  `carry-decay-four-ruled-out`）。同一家、同一类研报，先验上应当假设这里也一样，而不是等
  Task 8 全做完才发现。
