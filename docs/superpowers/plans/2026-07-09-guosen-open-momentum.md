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

| 模块 | 行数 | 对本策略的价值 |
|---|---|---|
| `cta_carry/minute_sessions.py` | 756 | 版本化交易时段 + 交易时钟分钟槽。⚠️ `SESSION_RULES_VERSION = "commodity-v1"`，只含 CZCE/DCE/GFEX/INE/SHFE，**无 CFFEX**；`DAY_SEGMENTS` 是商品日盘段 ⇒ 股指须新起 `cffex-v1` |
| `cta_carry/minute_bars.py` | 1,018 | 分钟 bar 校验、确定性聚合、合约乘数反推。docstring 已中性（"contract minute bars"） |
| `cta_carry/minute_pg_source.py` | 1,794 | 有界 PG 访问：按月分批、临时表驱动裸列嵌套循环探针、事务级 `SET LOCAL` 保护 |
| `cta_carry/minute_account.py` | 454 | 逐事件计价账户；正式账户与未缩放影子账户并行 |
| `cta_carry/minute_backtest.py` | 2,291 | 15 分钟吊灯止损三档减仓、VWAP 成交窗口、日末冲突合并 |
| `cta_carry/session_authority.py` | 982 | 以交易所公告为准的时段权威源 |
| `config/carry_minute_sessions.csv` | 5,293 行 | 时段规则数据（商品） |

与本策略的对应关系：论文的 15 分钟吊灯止损（多头 `2.5×ATR`、空头 `2.0×ATR`）、三档减仓、每根 bar 最多一档、第三档平仓，与该设计 §7.2 逐条同构；ATR 杠杆 + 已实现波动率反馈 + 4 倍帽与其 §3.1 / §8.2 同构。本策略独有的是：开盘前 3 根 bar 定多空、反向信号止损（多头 5 根递减高点 / 空头 3 根递增低点）、隔夜规则，以及**股指无夜盘**（比商品少一层）。

**设计 §5.3 的 TimescaleDB 查询纪律对本策略同样适用、不可豁免**：按自然月分批；候选合约写临时表并驱动对 `m.symbol` **裸列**的嵌套循环索引探针；**禁止在 `m.symbol` 上使用 CASE / 正则 / 截断 / 大小写函数**；分钟表查询事务内 `SET LOCAL max_parallel_workers_per_gather=0` / `work_mem='32MB'` / `statement_timeout='300s'` / `enable_hashjoin=off` / `enable_mergejoin=off`；**任何新查询在全历史运行前都要保存 `EXPLAIN`，计划若包含对 264 个 chunk 的无界全扫描或预计扫描接近 6.6 亿行，验收失败。**

⚠️ 上述模块目前都在 `cta_carry/` 命名空间下、不在 `common/`，且分支在飞。抽层与排期见 Task 0。

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
- [ ] **落地触发条件 = 该分支 merge 进 master。** 裁决当日实测状态：主计划
  （`docs/superpowers/plans/2026-08-13-carry-minute-execution.md`）13 个 Task 中 Task 1~11 的代码
  均已在分支上；Task 12（数据库冒烟 / 全量验证 / 文档）在做；Task 13（两个配对研究窗 +
  证据报告）未开始；会话权威批次 A/C/D/E 已过目写入资产，**G / G3 / H 仍待过目**。
  ⇒ **不是即将 merge，等待期以周计——不要空转轮询，也不要因为"等不及"改投 (B)。**
- [ ] 抽层必须保证 Carry 侧 CLI 输出**逐点不变**（沿用其设计 §3.1 对日线 CLI 的同一条约束），
  以分支上现有的 Carry 分钟测试家族为回归闸
- [x] 结论已回填本文件与 `docs/ROADMAP.md`

**(A) 的排期后果 —— 哪些现在能做、哪些必须等：**

| Task | 是否被 (A) 阻塞 | 说明 |
|---|---|---|
| Task 1 CFFEX 时段版本 | **阻塞** | 目标文件是 `common/minute/sessions.py`，抽层前不存在 |
| Task 2 15 分钟 K 线 / VWAP | **阻塞** | 薄封装，依赖分钟层 |
| **Task 3~6** 开盘信号 / ATR 与止损 / 持仓路径 / 杠杆组合 | **不阻塞** | 纯策略逻辑 + 确定性合成数据，不碰分钟层，可先按 TDD 写完 |
| Task 7 PG 源与 CLI | **阻塞** | 依赖分钟层的有界访问 |
| Task 8 报告与 runbook | 部分 | 报告骨架可先写；数据要求段等 Task 1/2/7 |

⇒ 起手顺序：**Task 3 → 4 → 5 → 6**（全合成数据），期间等分支落地；merge 后做抽层，
再回来做 Task 1 → 2 → 7 → 8。

### Task 1: CFFEX 时段版本与数据质量闸

**Files:**

- Create: `index_open_momentum/sessions.py`（若 Task 0 选 (A)，改为扩展 `common/minute/sessions.py`）
- Create: `config/index_minute_sessions.csv`
- Test: `tests/test_index_open_momentum_sessions.py`

- [ ] 新起 `cffex-v1` 时段版本：2016-01-01 前日盘 `09:15–11:30` / `13:00–15:15`，2016-01-01 起 `09:30–11:30` / `13:00–15:00`；**无夜盘**
- [ ] 时段规则须与 `futures_minute` 实测首尾时间交叉校验并记录版本；任何时间戳映射不到唯一时段则**硬失败**，不靠"数已有行数"压缩时间
- [ ] 把「本库 2016 前最后一根 bar 是 14:59、官方到 15:15」显式登记为已知缺口，进质量审计与保真度报告
- [ ] 覆盖度闸：IF/IC/IH 在请求窗口内的逐年交易日数与 `public.trading_calendar` 对账，缺口超阈值即拒绝标注 paper-faithful
- [ ] 测试：时段版本切换日、午休边界、无夜盘、映射失败硬失败、覆盖度闸拒绝路径

### Task 2: 15 分钟 K 线与 5 分钟 VWAP

**Files:**

- Create: `index_open_momentum/bars.py`（薄封装；聚合与乘数解析复用分钟层）
- Test: `tests/test_index_open_momentum_bars.py`

- [ ] 15 分钟 K 线按连续 15 个**交易时钟分钟槽**构造，绝不跨休市拼成一根
- [ ] 5 分钟成交窗口按五个交易时钟分钟槽计数；`vwap = sum(amount) / sum(volume) / contract_multiplier`，只汇总 `volume > 0` 的分钟
- [ ] VWAP 落在窗口 `[min(low) - eps, max(high) + eps]`（`eps = 1e-6 * max(1, |low|, |high|)`）之外则硬失败
- [ ] IF/IC/IH/IM 合约乘数优先取 `public.futures_contract_info`，缺失时按分钟层既有规则反推（确定性抽样、99% 价域通过率、唯一解，否则硬失败）；记录 `metadata` / `inferred` 来源
- [ ] 一根 15 分钟 K 线若无任何正成交量，记 `no_trade_bar`，不更新吊灯极值、不触发止损
- [ ] 一个必需的 5 分钟成交窗口若总成交量为零，**运行硬失败**，不顺延到更晚窗口
- [ ] 测试：跨午休不拼接、跨时段版本切换日、零成交 bar、乘数反推与元数据冲突、VWAP 越界硬失败

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

- [ ] Enter after the opening signal confirmation.
- [ ] Reduce the initial position by one third for each stop event.
- [ ] Flatten after the third stop event.
- [ ] Carry remaining long exposure overnight only for long opening signals that are not fully flattened.
- [ ] Flatten short exposure on same-day close.
- [ ] Record a trade ledger with entry, scale-downs, day-close exits, overnight exits, gross return, cost, and net return.

### Task 6: Add Leverage and Portfolio Combination

**Files:**

- Create: `index_open_momentum/leverage.py`
- Modify: `index_open_momentum/backtest.py`
- Test: `tests/test_index_open_momentum_leverage.py`

- [ ] Add ATR leverage: `clip(0.005 * close / ATR, 0, 4)`.
- [ ] Add monthly realized-volatility multiplier using one year of prior strategy returns.
- [ ] Combine IF, IC, and IH by equal capital across available active families.
- [ ] Keep IM excluded from the paper-faithful profile.
- [ ] Add tests for leverage clipping, zero ATR handling, missing realized-volatility history, and active-family equal weighting.

### Task 7: 数据源与 CLI

**Files:**

- Create: `index_open_momentum/pg_source.py`
- Create: `index_open_momentum/__main__.py`
- Test: `tests/test_index_open_momentum_pg_source.py`

- [ ] PG 读取器面向 `public.futures_minute`，遵守本文件「本仓已有的机器」段落转述的设计 §5.3 全部查询纪律（按月分批 / 临时表驱动 / 裸列探针 / `SET LOCAL`）
- [ ] 主力合约按 `open_interest` / `volume` 自定，并在 2026-04-29 前的重叠区与 `continuous_contract_ohlc.contract_used` 对账，不一致须报告而非静默取一边
- [ ] 每条新查询保存 `EXPLAIN` 产物；出现 264 chunk 无界全扫或预计扫描接近 6.6 亿行即验收失败
- [ ] CLI 选项：起止日期、品种、时段版本、输出前缀、是否强制 paper-faithful
- [ ] `amount` 缺失或必需的 5 分钟窗口零成交时**硬失败** —— 不降级为近似成交、不顺延窗口（原计划的 approximate-execution 旗已随数据源更正作废）

### Task 8: Add Reporting and Runbook

**Files:**

- Create: `index_open_momentum/report.py`
- Create: `docs/operations/guosen-open-momentum-runbook.md`
- Test: `tests/test_index_open_momentum_report.py`

- [ ] Write Excel outputs for metrics, daily returns, annual returns, trades, signals, leverage, and data-quality audit.
- [ ] Write a net-value chart.
- [ ] Document data requirements for paper-faithful replication.
- [ ] runbook 记录：数据源为 `public.futures_minute`（最新 bar 2026-08-11，实盘前需补尾）、时段版本 `cffex-v1`、2016 前 15:00–15:15 缺口的披露口径、以及 `EXPLAIN` 存档位置。

## Acceptance Criteria

- 策略逻辑先由确定性合成数据测试覆盖，再接生产数据（TDD）。
- 全程精确 VWAP，无近似成交模式；任一成交窗口不满足 Task 2 的校验即硬失败。
- IF/IC/IH 的覆盖度闸与时段映射全部通过后，才允许把结果标注为 paper-faithful；2016 前 `15:00–15:15` 缺口必须在报告中显式披露。
- 全历史运行之前，每条新查询都有存档的 `EXPLAIN`。
- 与论文口径对账：样本自 2011-06 起，费后年化 25.79% / Sharpe 1.77 / 最大回撤 7.66% / Calmar 3.37。差异逐项归因，**不调参逼近论文收益**。
