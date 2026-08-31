# 国信连续信号（商品期货）复刻 —— 摘要与实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复刻国信证券《CTA 系列专题之五：基于连续信号的商品期货交易策略》（研报日期 2023-06-20，公众号版 2023-06-27，本地 PDF 29 页），并给出研报样本之外的检验。

**Architecture:** 新建顶层包 `cta_continuous/`，与 `cta_gtja` / `cta_carry` / `index_open_momentum` 平级。**分钟机器不重造**：15 分钟 K 线、5 分钟 VWAP、合约乘数解析、62 品种夜盘规则全部复用 `common/minute/` 与 `config/carry_minute_sessions.csv`。本包只提供成交额宇宙、后复权连续价、EMA/TNR/U2P 信号层、组合回测与报告。

**Tech Stack:** Python 3.13 / pandas / pyarrow / pytest；PG `public` schema（`futures_minute` + `futures_daily`）；`common.config` / `common.db` / `common.minute`。

---

## 为什么这份计划的口径部分这么长

研报正文里**所有公式都是图片**，`pdftotext` 一个都取不到；而正文叙述与 §5.1 汇总框**在两处互相矛盾**。
下面 §1 是从公式图逐条抠出并验算过的口径，§2 是矛盾处的裁决。实施时以这两节为准，不要回头读 PDF 正文摘要。

---

## §1 研报口径逐条转写

### 1.1 投资标的

- 过去半年中**日均成交额超过 50 亿元**的商品期货全品种的**主力合约**。
- 主力合约 = **成交量与持仓量均达到最大**的合约；**主力不可逆**，一经切换不再切回。
- （研报同时讨论了股指/国债交割日前强制换月，本策略是商品，不适用。）

### 1.2 信号链（每 15 分钟计算一次）

短/长 EMA 均线、`Lev_ATR`、`ΔTNR`、`U2P` 四者同时计算。

**趋势噪音比**（TNR, Trend to Noise Ratio）：

```
TNR_{t,N} = |Close_t − Close_{t−N}| / Σ_{i=t−N+1}^{t} |Close_i − Close_{i−1}|
```

分子是「位移」，分母是「路程」。无噪音时 TNR=1，噪音越大 TNR 越接近 0。

**噪音变化趋势**：

```
ΔTNR_{t,k} = TNR_t − (Σ_{i=0}^{k−1} TNR_{t−i}) / k        k = 3
```

注意求和**含 TNR_t 自己**，所以 `ΔTNR = (2/3)·TNR_t − (1/3)·(TNR_{t−1} + TNR_{t−2})`。

**海龟杠杆**（附录二）：

```
TR      = max[(high − low), |high − preclose|, |low − preclose|]
ATR     = TR 的 n 期移动平均
Pos_ATR = 0.5% / ATR
Pos     = 1 / Close
Lev_ATR = Pos_ATR / Pos = 0.005 × Close / ATR
```

**涨跌概率与连续信号**（§3.2，全文最关键、正文完全没有文字描述的一段）：

```
UpProb_{t0}   = 0.5,  DownProb_{t0} = 0.5

UpProb_t   = UpProb_{t−1}   + 0.5 × (DownProb_{t−1} × Long_t − UpProb_{t−1}   × Short_t)
DownProb_t = DownProb_{t−1} + 0.5 × (UpProb_{t−1}   × Short_t − DownProb_{t−1} × Long_t)

U2P_t = UpProb_t − DownProb_t
```

任意时刻 `UpProb_t + DownProb_t = 1`。`Long_t` / `Short_t` 是 0/1 指示变量。
未触发任何一侧时两个概率保持不变（图 13 的横向段）。

**图 13 的验收字面量**（已复算逐点相符，实现必须重现）：

| 时刻 | 事件 | UpProb | DownProb |
|---|---|---|---|
| 起点 | — | 50.00% | 50.00% |
| T | 触发多仓 | 75.00% | 25.00% |
| T+1 | 再次触发多仓 | 87.50% | 12.50% |
| T+2 | 触发空仓 | 43.75% | 56.25% |
| T+3 | 再次触发空仓 | 21.88% | 78.12% |

**开仓条件**（§5.1，方向已按 §2 裁决改正）：

- 多头：短均线在长均线上方 **且二者距离扩大** 且 `Lev_ATR > 1` 且 `ΔTNR > 0` 且 `U2P > 0.2`
- 空头：长均线在短均线上方 **且二者距离扩大** 且 `Lev_ATR > 1` 且 `ΔTNR > 0` 且 `U2P < −0.2`
- 任一条不满足 → **空仓**（§3.1：「即使此时传统信号为 1，我们依然平仓操作」）

**信号值**：

```
Signal_t = +UpProb_t    （多头）
Signal_t = −DownProb_t  （空头）
Signal_t = 0            （空仓）
Position_i = Lev_i × Signal_i
```

因 `|U2P| > 0.2` 且两概率和为 1，多头时 `Signal ∈ (0.6, 1]`，空头时 `∈ [−1, −0.6)`。

### 1.3 杠杆

```
Mul_vol = 15% / Vol        Vol = 过去一年策略收益的已实现波动率，每月末回看重算
Lev     = Lev_ATR × Mul_vol，绝对值超过 4 截断为 4
```

### 1.4 资金、成交与成本

- 满足开仓条件的品种**等权分配资金**。
- 成交价 = 信号触发后 **5 分钟 VWAP** = `Σ Amount_i / (Σ Vol_i × Multi)`。
- 成本：手续费 0.3%% + 冲击 1%% = **1.3 个基点**（研报表 8 另测 1.8/2.5%%）。

### 1.5 后复权与时间对齐（附录一）

```
AdjFactor_i = AdjFactor_{i−1} × Close_{i−1,old} / Close_{i−1,new}
```

基期因子 = 1，研报基期 2010-01-01。新主力的 O/H/L/C 全部乘当期因子。

跨品种对齐：**非交易时段行情置空、信号延续上一交易时段、策略收益率置零**；合约未上市时行情置空、不许发信号、收益率置空。

### 1.6 研报自报业绩（复刻对照基准）

| 口径 | 年化 | 夏普 | Calmar | 最大回撤 | 波动 |
|---|---|---|---|---|---|
| EMA 均线穿越（基线） | 13.06% | 1.03 | 0.93 | 13.98% | — |
| + 开仓时点信号强弱 | 14.87% | 1.38 | 1.55 | 9.59% | 10.76% |
| + 信号持续度 | 12.80% | 1.45 | 1.45 | 8.80% | 8.85% |
| + 已实现波动调整（**最终策略**） | **23.1%** | **1.71** | **1.67** | — | 13.48% |

样本 2011-06 → 2023-05-31，平均杠杆 2.5 倍，2022 年 +17.67%。

---

## §2 口径裁决表（研报沉默或自相矛盾处）

**每一条都必须进保真度报告。** 编号被实现与测试引用。

| # | 歧义 | 裁决 | 依据 |
|---|---|---|---|
| D1 | ATR 算在日线还是 15 分钟线 | **15 分钟线**，n=20 | 实测四张不同年代的活跃合约：日线 ATR 下 `Lev_ATR>1` 通过率 **0.0%**（中位数 0.16~0.35），策略永不开仓；15 分钟 ATR 下通过率 74%~100%、中位数 1.24~1.74、从不触及 4 倍上限。附录二「衡量每日的波动幅度」是沿用旧文措辞 |
| D2 | 多头是短均线在上还是长均线在上 | **短均线在长均线上方为多头** | §2.1 有完整推导 + 图 5/7/8 图例；§5.1 汇总框写反。反向由 `--ma-orientation reversed` 并跑对照 |
| D3 | 噪音闸是 ΔTNR>0 还是 <0 | **ΔTNR > 0** | §3.1 结论句与表 4 实证（ΔTNR>0 单笔均值 0.29% vs ΔTNR<0 的 0.14% vs 不加闸 0.25%）；§5.1 汇总框写反。反向由 `--tnr-sign negative` 并跑对照 |
| D4 | 递推里的 `Long_t`/`Short_t` 含不含 U2P 闸 | **不含**：只含均线方向 + 距离扩大 + `Lev_ATR>1` + ΔTNR 闸 | 含则自指（U2P 依赖 Long，Long 依赖 U2P），无解。这是被迫的，不是选的 |
| D5 | 触发是穿越「事件」还是「状态」 | **状态**，逐 bar 判 | 图 13 有连续两根「再次触发多仓条件」；穿越事件不可能连续两根都发生 |
| D6 | 闸门不过时是平仓还是持有到反向 | **平仓（空仓）** | §3.1：「当开仓杠杆率 Lev_ATR<1 …… 即使此时传统信号为 1，我们依然平仓操作」 |
| D7 | ΔTNR 用「与 3 日前比」还是「与近 3 期均值比」 | **近 k 期均值**（公式图） | §3.1 正文说「当日与 3 日前」，与公式图不符；公式图更精确。正文版由 `--dtnr-mode lag` 作为变体 |
| D8 | 策略起始日 | **约 2012-01**（非研报 2011-06） | 商品分钟档案自 2011-01-04 起（SHFE 铜之前无分钟数据），`Mul_vol` 需 252 个交易日的策略收益垫底 |
| D9 | EMA 跨度、TNR 的 N、ATR 的 n | **预注册网格，只在样本内选点** | 研报全部未给。样本外数字对选中点与全网格都报 |
| D10 | 宇宙刷新节拍 | **每月末**，用截止上一自然月末的 6 个月 | 研报只说「过去半年」。逐日重算会让品种在 50 亿门槛上抖动、制造纯噪音换手；研报另一个动件（`Mul_vol`）明写按月 |
| D11 | 主力「双最大」不成立时 | **不切换，沿用上一主力** | 研报只给了双最大规则与不可逆约束，没写双最大不成立的情形 |
| D12 | EMA 递推约定 | `alpha = 2/(span+1)`，`adjust=False` | 研报只写「移动平均（EMA）算法」 |
| D13 | 无成交 bar 的 TR / TNR | **只在有成交的 bar 上取值**，无成交 bar 不入 ATR/TNR 窗口 | `volume=0` 的空 K 线带的是结转价不是成交价（见表 COMMENT 与 [[futures-minute-ingestion]]） |
| D14 | 信号落在时段最后一根 bar 时的成交窗口 | 顺延到该品种**下一交易时段的前 5 分钟** | 研报未写。与 `index_open_momentum` 的 `fill_window` 姿势一致 |
| D15 | 「日均成交额」的分母 | 窗口内**全市场观测到的交易日数**，品种缺席那天按 0 计 | 按品种自己的观测天数取平均，会让刚挂牌、只交易三天但每天 200 亿的品种被算成「日均 200 亿」入池 |
| D16 | D11 沿用的旧主力已退市/从日线池消失 | **该品种暂不交易**，直到新的双最大合约出现；恢复时，若新旧历史的有效收盘日期区间严格分离，则新建 `continuity_segment` 且复权因子重置为 1；若日期区间重叠，则用判定日前最近一个新旧合约都有有效收盘的日期衔接复权 | 继续输出已消失的旧合约会伪造 `oi=volume=0` 的主力日且没有分钟行情；强行改选持仓量或成交量单项最大又违反研报“双最大”。对严格分离的历史不伪造重叠，而是显式记录断代；日期区间重叠却没有共同有效收盘，或任一侧没有有效收盘时，仍硬失败，不取 1.0 顶替 |

**预注册网格（D9）**，样本内 = `≤ 2023-05-31`，选点后样本外只报不选：

- EMA `(short, long)` ∈ `{(12, 26), (10, 60), (20, 120)}`
- TNR `N` ∈ `{10, 20, 40}`
- ATR `n` = 20（固定）、`k` = 3（研报给定）

共 9 个参数点。另在基线参数点 `(12,26,N=20)` 上跑 D2/D3 的 3 个反向组合作对照，合计 12 次回测。
**网格不得事后扩张**——扩了就在计划里改并说明原因。

---

## §3 本仓已有的机器（不要重造）

| 已有 | 位置 | 本策略怎么用 |
|---|---|---|
| 62 品种夜盘规则（2011-01-04 → 2026-01-30，5,292 条） | `config/carry_minute_sessions.csv` | 直接加载；本策略的宇宙基本被它覆盖 |
| 商品交易时钟 / 分钟槽 / 15 分钟桶 | `common/minute/sessions.py`（`COMMODITY_V1`） | `build_trading_slots` / `fifteen_minute_buckets` |
| 15 分钟 K 线聚合（含无成交 bar 判定） | `common/minute/bars.py::aggregate_fifteen_minute_bar` | Task 3 |
| 5 分钟 VWAP + 合约乘数推断 + 郑商所计价基准 | `common/minute/bars.py::five_minute_vwap` / `infer_contract_multiplier` | Task 3 |
| 分钟批量查询（chunk exclusion + 计划审计） | `common/minute/pg_source.py::PublicMinuteSource` | Task 3 |
| 附录二杠杆（海龟 + 15% 目标波动 + 4 倍截断 + 按月重算） | `index_open_momentum/leverage.py` | Task 0 上提到 `common/` 后直接用 |
| 逐日主力选择 + 与连续合约对账 | `index_open_momentum/pg_source.py::choose_dominant` | Task 0 上提，Task 2 加「双最大 + 不可逆」 |
| 金融期货排除集 | `cta_gtja/pg_source.py::FINANCIAL_FUTURES` | Task 1 |
| 事件账户 | `common/minute/account.py::EventAccount` | Task 6 |

---

## §4 已知边界与缺口（先写下来，不要跑到最后才发现）

1. **样本外止于 2026-01-30**，不是分钟表末端 2026-08-05。`config/carry_minute_sessions.csv`
   有 57 个品种的规则在 2026-01-30 截止，`resolve_session_rule` 越界硬失败。延长该资产是
   交易所公告维护工作，**不在本计划内**；CLI 必须拒绝越界窗口而不是悄悄截断。
2. **`futures_daily` 有已知缺陷**：2015–2017 郑商所 2,565 对重复记录（成交额会被重复计入，
   Task 1 必须按 `(symbol, trade_date)` 去重）、2026-03 缺 6 个交易日的内部空洞。
3. **`futures_daily` EOD 日更止于 2026-04-29**，但本计划的上界由第 1 条决定，不受它约束。
4. **郑商所分钟 `amount` 是合成的**，算不出成交价；`five_minute_vwap` 的 `pricing_basis`
   对郑商所须走 `ohlc_typical`（见 `config/carry_minute_pricing_basis.csv` 与
   [[czce-minute-turnover-is-synthetic]]）。
5. **长跑一律上 WSL2**：本机到库是 DERP 中继、抖动会半开断连（[[db-path-is-derp-relayed]]）；
   后台长跑必须 `setsid` 脱离进程组（[[long-jobs-need-setsid]]）。
6. **绝不对 `futures_minute` 做全表聚合**（[[debian-pg-oom-guardrails]]）：时间边界写字面量、
   join 键用裸列、新查询先 `EXPLAIN`、一律加 `statement_timeout`。

---

## §5 任务

基线：`master` 上 `.venv/bin/python -m pytest -q` = **1252 passed**。每个 Task 结束都要跑全量，不是只跑新用例。

**变异验证是硬要求**：新增用例写完后，手工改坏被测逻辑确认用例变红，再改回。
改动前先 `find . -name __pycache__ -prune -exec rm -rf {} +`——等长变异 + 同秒 mtime 会让
`.pyc` 不失效，假报「测试没抓住」（[[mutation-runs-need-pycache-clear]]）。

---

### Task 0：把附录二杠杆与主力选择上提到 `common/`

本策略与 `index_open_momentum` 用的是**同一篇研报系列的同一个附录二**。第二个消费者出现了，才搬。

**Files:**
- Create: `common/leverage.py`, `common/dominant.py`
- Modify: `index_open_momentum/leverage.py`（改为薄再导出）, `index_open_momentum/pg_source.py`
- Test: `tests/test_common_leverage.py`, `tests/test_common_dominant.py`

**Step 1: 先固化「搬之前」的真实产物**

在动任何代码前，跑一次股指侧端到端并留存产物哈希。**「测试还绿」不算逐点不变**
（[[shared-layer-extraction-lessons]]）。

```bash
.venv/bin/python -m index_open_momentum --start 2024-01-02 --end 2024-03-29 \
  --output-prefix output/_preflight/index_before
```

⚠️ **三个产物里只有两个能比字节**。`audit.json` 与 `.png` 可复现；`.xlsx` 不行 ——
openpyxl 把写入时刻塞进 `docProps/core.xml`，同一份数字连跑两次字节也不同。所以
xlsx 必须**按内容**比：逐 sheet 读成 DataFrame、转规范化 CSV 再 sha256。
先跑一次**对照**（不改代码，只换输出前缀）确认哪些产物本来就可复现，否则事后的
diff 无法解释。

- [ ] 对照跑已确认可复现范围
- [ ] 产物哈希已留存

**Step 2: 净搬 `leverage.py`**

`common/leverage.py` = 现 `index_open_momentum/leverage.py` 的全部内容，**去掉** `PAPER_FAMILIES`
与 `equal_capital_weights` 的 `universe` 默认值（商品宇宙逐月变化，默认值在这里是错的）：

```python
def equal_capital_weights(
    active_families: Sequence[str], *, universe: Sequence[str]
) -> dict[str, float]:
```

`index_open_momentum/leverage.py` 缩成：

```python
"""国信开盘动量的杠杆层：附录二的通用部分已上提到 `common.leverage`。"""

from __future__ import annotations

from collections.abc import Sequence

from common.leverage import (  # noqa: F401  （原地再导出，调用方 import 路径不变）
    ATR_CAPITAL_FRACTION,
    MAX_LEVERAGE,
    TARGET_ANNUAL_VOL,
    TRADING_DAYS_PER_YEAR,
    atr_leverage,
    final_leverage,
    monthly_realized_volatility,
    realized_volatility,
)
from common.leverage import equal_capital_weights as _equal_capital_weights

#: 忠实口径的品种集合。IM 2022-07-22 才挂牌，晚于研报（2021-05-13），不在内。
PAPER_FAMILIES = ("IF", "IC", "IH")


def equal_capital_weights(
    active_families: Sequence[str], *, universe: Sequence[str] = PAPER_FAMILIES
) -> dict[str, float]:
    """当日有信号的品种之间等分资金；股指侧默认锁在忠实口径的三品种上。"""
    return _equal_capital_weights(active_families, universe=universe)
```

- [x] 搬完

**Step 3: 净搬 `choose_dominant`**

`common/dominant.py` 收 `DominantChoice`、`_product_of`（改名 `product_of`，公开）、
`daily_stats_from_minutes`、`choose_dominant`、`reconcile_dominant`、`DOMINANT_SELECTION_LAG`。
**`is_concrete_index_contract` 留在股指侧**——那是 CFFEX 合成码的判据，不是通用规则。
`index_open_momentum/pg_source.py` 原地再导出。

- [x] 搬完

**Step 4: 全量测试 + 逐点不变验证**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
.venv/bin/python -m pytest -q                      # 期望 1252 passed
.venv/bin/python -m index_open_momentum --start 2024-01-02 --end 2024-03-29 \
  --output-prefix output/_preflight/index_after
```

`audit.json` / `.png` 比字节，`.xlsx` 比内容摘要。

- [x] 1252 passed
- [x] 产物逐点相同（audit.json 与 png 字节相同、xlsx 内容摘要相同）

**Step 5: 为 `common/` 的新公开面补用例**

`tests/test_common_leverage.py` 至少钉住：`equal_capital_weights` 现在**要求**显式 `universe`
（不传应 `TypeError`）；`atr_leverage(atr=0)` 取 0 而非上限。

- [ ] 用例通过变异验证

**Step 6: Commit**

```bash
git add common/leverage.py common/dominant.py index_open_momentum/ tests/
git commit -m "refactor: lift the appendix-2 leverage and dominant choice to common/"
```

---

### Task 1：成交额宇宙筛选

**Files:**
- Create: `cta_continuous/__init__.py`, `cta_continuous/universe.py`
- Test: `tests/test_continuous_universe.py`

**Step 1: 写失败的测试**

⚠️ **去重键不是 `(symbol, trade_date)`**（写计划时想错了，实测更正）：孪生记录是同一张
合约的 **3 位与 4 位两种交割码**，`symbol` 本来就不同。必须先用
`common.minute.pg_source.minute_contract_identity` 把交割码归一到四位再去重。

```python
def test_universe_sums_all_contracts_of_a_product():
    """成交额是品种级的，要把该品种全部合约加起来，不是只看主力。"""


def test_universe_deduplicates_the_czce_three_and_four_digit_twins():
    """2015-2017 郑商所有 2,565 对孪生记录；不去重会把成交额算成两倍。"""


def test_universe_excludes_financial_futures():
    """IF/IC/IH/IM/T/TF/TL/TS 不是商品。"""


def test_universe_window_stops_at_previous_month_end():
    """D10：截止上一自然月末，当月数据一律不许进窗口——否则是前视。"""
```

**Step 2: 运行，确认失败**

```bash
.venv/bin/python -m pytest tests/test_continuous_universe.py -v
```

**Step 3: 实现 `universe.py`**

对外两个函数：

```python
def product_daily_turnover(daily: pd.DataFrame) -> pd.DataFrame:
    """`(symbol, trade_date, turnover)` → `(product, trade_date, turnover)`，先按
    `(symbol, trade_date)` 去重再按品种求和。"""


def universe_for_month(
    turnover: pd.DataFrame, *, month_start: date, threshold: float = 5e9,
    lookback_months: int = 6,
) -> tuple[str, ...]:
    """`month_start` 当月可交易的品种。窗口 = `month_start` 之前 6 个自然月。"""
```

**Step 4: 测试通过 + 变异验证**

**Step 5: 拿真实数据出一张宇宙轨迹表**

```bash
.venv/bin/python scripts/continuous/2026-08-27-universe-trace.py
```

**实测（2026-08-27）**：169 个月，品种数 **14（2012-01）→ 59（2026-01）**，中位数 36。
每月进出合计的分布：**0 个变动 88 个月、1 个 55 个月、2 个 18 个月、3 个 6 个月、4 个 1 个月**，
唯一的 14 是首月建池。宇宙抖动极小 —— 按月重算这条裁决（D10）达到了它的目的，
后面 §6 风险 ① 的换手主要来自闸门而非宇宙。
`product_daily_turnover` 的「孪生记录成交额不相等就报错」在全历史上**从未触发**，
反过来证实了上游那 2,565 对确实逐字段相同。

- [x] 轨迹表已核对

**Step 6: Commit**

---

### Task 2：主力合约与后复权连续价

**Files:**
- Create: `cta_continuous/continuous.py`
- Test: `tests/test_continuous_roll.py`

**Step 1: 写失败的测试**

```python
def test_dominant_requires_both_volume_and_oi_max():
    """研报：成交量与持仓量**均**达到最大才算主力。"""


def test_dominant_keeps_previous_when_no_contract_is_both_max():
    """D11：双最大不成立时不切换。"""


def test_dominant_never_rolls_backwards():
    """研报：主力不可逆。交割月只许非递减。"""


def test_adjust_factor_chains_across_two_rolls():
    """AdjFactor_i = AdjFactor_{i-1} × Close_old / Close_new，基期 1。
    手算两次展期的字面量，不许用被测代码生成期望值。"""


def test_adjusted_returns_have_no_roll_gap():
    """展期当日用复权价算出的收益率 = 同一合约的真实收益率。"""
```

**Step 2~4: 红 → 实现 → 绿 + 变异验证**

**Step 5: 拿真实数据体检「双最大」的严苛程度**

「双最大」比通用规则（持仓量优先、成交量决平）严得多，要先确认它不会让主力卡在
一张陈旧合约上。**实测 2018–2024（995,365 行日线）**：

| 品种 | 有主力的交易日 | 展期次数 | 通用规则展期 | 两法一致率 |
|---|---|---|---|---|
| RB | 1698 | 21 | 21 | 97.1% |
| CU | 1694 | 82 | 82 | 87.2% |
| M | 1698 | 21 | 21 | 91.5% |
| TA | 1698 | 21 | 21 | 97.5% |
| AU | 1698 | 23 | 24 | 85.9% |

约 1,700 个交易日里几乎每天都有主力，无卡死。CU 的 82 次 ≈ 每年 11.7 次是铜的
月度合约节奏，不是抖动。

- [x] 双最大不会卡死

**Step 6: Commit**

---

### Task 3：15 分钟面板与成交价缓存（重活，WSL2）

面板建一次、落 parquet，之后 12 次回测都不再过网。

**Files:**
- Create: `cta_continuous/panel.py`, `scripts/continuous/build_panel.sh`
- Test: `tests/test_continuous_panel.py`

面板每行 = 一个 `(product, slot_end)`：

| 列 | 含义 |
|---|---|
| `product` / `contract` / `trade_date` / `slot_end` | 主键与身份 |
| `open/high/low/close/volume` | 15 分钟 K 线（`volume=0` 的分钟行**已滤掉**） |
| `no_trade` | 该 15 分钟无成交 |
| `adj_factor` | 当期后复权因子 |
| `continuity_segment` | 品种内连续段；遇到可证明的市场断代时递增 |
| `fill_price` | **下** 5 分钟 VWAP（D14：跨时段时取下一时段前 5 分钟） |
| `multiplier` / `pricing_basis` | 乘数与计价基准（郑商所走 `ohlc_typical`） |

**Step 1: 写失败的测试**（用假的分钟帧，不连库）

```python
def test_panel_drops_zero_volume_minutes_before_aggregating():
    """空 K 线带结转价，直接聚合会造出没成交过的极值。"""


def test_fill_price_is_the_next_five_minutes_not_the_current_bar():
    """成交价是信号触发**之后**的 5 分钟，用当根 bar 自己就是前视。"""


def test_fill_price_rolls_into_next_session_at_session_end():
    """D14。"""


def test_czce_uses_ohlc_typical_pricing_basis():
    """郑商所 amount 是合成的，按 amount 算 VWAP 会得到假成交价。"""
```

**Step 2~4: 红 → 实现 → 绿 + 变异验证**

**Step 5: 小窗口真跑一次（本机，验证接线）**

```bash
.venv/bin/python -m cta_continuous.panel --start 2023-01-01 --end 2023-03-31 \
  --out output/continuous/panel_smoke.parquet
```

查询必须满足 §4 第 6 条。落盘前 `EXPLAIN` 一条代表性查询，确认只进少数 chunk。

- [ ] 小窗口跑通，`EXPLAIN` 显示 chunk exclusion 生效

**Step 6: 全历史长跑（WSL2）**

```bash
ssh -p 2223 ghls@100.120.152.1 "cd ~/futures_strategies && setsid nohup \
  .venv/bin/python -m cta_continuous.panel --start 2011-01-01 --end 2026-01-30 \
  --out output/continuous/panel_full.parquet > logs/panel_full.log 2>&1 &"
```

判活体用 `ps -p 1 -o etimes=` + 心跳日志，**不用 `lstart=`**，**不用 `pgrep -f`**。

- [ ] 全历史面板已生成，行数与逐月分批日志相符

**Step 7: Commit**

---

### Task 4：指标层

**Files:**
- Create: `cta_continuous/indicators.py`
- Test: `tests/test_continuous_indicators.py`

EMA / ATR / TNR / U2P 必须按 `(product, continuity_segment)` 分组计算；
切段时所有递推与窗口状态归零，新段必须重新完成各自的预热窗口后才能产生信号，
不得跨市场断代携带任何指标状态。

**Step 1: 写失败的测试**

```python
def test_tnr_is_one_on_a_monotone_path():
    """无噪音时位移 = 路程，TNR = 1（研报图 11 左）。"""


def test_tnr_falls_as_the_path_wanders():
    """同样的起点终点、更长的路程 → 更小的 TNR（图 11 中/右）。"""


def test_delta_tnr_uses_the_mean_of_the_last_k_including_now():
    """D7：ΔTNR = (2/3)TNR_t − (1/3)(TNR_{t-1} + TNR_{t-2})。手算字面量。"""


def test_atr_skips_no_trade_bars():
    """D13。"""


def test_gap_widening_compares_absolute_distance_to_the_previous_bar():
    """『二者距离扩大』——距离是绝对值，扩大是与上一根比。"""
```

**Step 2~4: 红 → 实现 → 绿 + 变异验证**

**Step 5: Commit**

---

### Task 5：U2P 递推与开仓闸

**Files:**
- Create: `cta_continuous/signals.py`
- Test: `tests/test_continuous_signals.py`

**Step 1: 写失败的测试 —— 图 13 是硬验收**

```python
def test_up_down_prob_reproduces_figure_13():
    """研报图 13 的四步。期望值是研报印出来的数字，不是被测代码算的。"""
    result = up_down_prob(
        long_flags=[True, True, False, False],
        short_flags=[False, False, True, True],
    )
    assert [round(p, 4) for p in result.up] == [0.75, 0.875, 0.4375, 0.21875]
    assert [round(p, 4) for p in result.down] == [0.25, 0.125, 0.5625, 0.78125]


def test_probabilities_hold_still_when_neither_side_fires():
    """图 13 的横向段：两侧都没触发时概率不动。"""


def test_probabilities_always_sum_to_one():


def test_gate_flags_exclude_u2p():
    """D4：递推的输入闸里不许出现 U2P，否则自指。"""


def test_position_is_flat_when_any_gate_fails():
    """D6。"""


def test_long_signal_value_is_up_prob_and_short_is_negative_down_prob():
    """§5.1『信号调整』。"""
```

**Step 2~4: 红 → 实现 → 绿 + 变异验证**

**Step 5: Commit**

---

### Task 6：组合回测

**Files:**
- Create: `cta_continuous/backtest.py`
- Test: `tests/test_continuous_backtest.py`

要点：

- 全局 15 分钟槽推进；每个品种在自己非交易的槽上**延续信号、收益率置零**（§1.5）。
- 每槽在**当时满足开仓条件**的品种间等权，权重 × `Lev` × `Signal`。
- 成交在 `fill_price`（下 5 分钟 VWAP），成本按**成交名义额** 1.3bp。
- `Mul_vol` 每月末回看过去 252 个交易日的**策略自身**日收益；不足则该月不建仓（沿用
  `common.leverage.final_leverage` 的裁定）。
- 回测必须按 `(product, continuity_segment)` 隔离 EMA / ATR / TNR / U2P 与信号状态；
  新段重新预热完成前保持空仓，不得将旧段的指标、信号或仓位跨断代接续。
- 换手要**分解**：信号进出 / 宇宙进出 / 等权再平衡 / 展期，四类分开记（见 §6 风险 ①）。

**Step 1: 写失败的测试**

```python
def test_weights_are_equal_among_products_passing_the_gates_that_slot():


def test_a_product_outside_its_trading_session_earns_zero_not_nan():


def test_universe_exit_forces_a_close_at_the_next_fill_price():


def test_turnover_is_attributed_to_the_four_causes():
    """信号 / 宇宙 / 再平衡 / 展期，加总等于总换手。"""


def test_no_position_before_the_first_month_with_a_full_year_of_returns():
    """D8。"""


def test_a_new_continuity_segment_cannot_signal_before_renewed_warmup():
    """EMA / ATR / TNR / U2P 不得跨断代携带状态。"""


def test_costs_scale_linearly_with_traded_notional():
```

**Step 2~4: 红 → 实现 → 绿 + 变异验证**

**Step 5: Commit**

---

### Task 7：报告与保真度台账

**Files:**
- Create: `cta_continuous/report.py`
- Test: `tests/test_continuous_report.py`

必须输出、且**不能藏起来**的东西：

1. **样本内 / 样本外分段指标**（切点 2023-05-31）。样本外不许缺席，格式沿用
   `index_open_momentum/report.py::segment_metrics`。
2. **分年度表**，列对齐研报表 7（收益 / 最大回撤 / 夏普 / 波动 / Calmar / 月度胜率）。
3. **保真度台账**：D1~D15 逐条列出裁决与依据，外加 §4 的已知缺口。
4. **成本敏感性**：1.3 / 1.8 / 2.5 个基点三档，对齐研报表 8。
5. **换手四分解**与各自的成本贡献。
6. **平均杠杆时序**，与研报图 15 的「均值 2.5 倍」对照。

```python
def test_report_refuses_to_omit_the_out_of_sample_segment():


def test_fidelity_ledger_lists_every_decision_from_d1_to_d15():
```

**Step 5: Commit**

---

### Task 8：CLI 与端到端

**Files:**
- Create: `cta_continuous/__main__.py`
- Test: `tests/test_continuous_cli.py`

```python
def test_cli_refuses_a_window_past_the_session_rule_horizon():
    """§4 第 1 条：越过 2026-01-30 必须报错退出，不许悄悄截断。"""


def test_cli_exposes_the_two_contradiction_switches():
    """--ma-orientation / --tnr-sign（D2/D3）。"""
```

**端到端：12 次回测**（9 个网格点 + 3 个反向对照），全部在 WSL2 上串行跑，
`free -g` 先看内存（[[run-heavy-jobs-serially]]）。

- [ ] 12 次跑完，报告产出
- [ ] 样本内选点、样本外只报不选

---

## §6 两个现在就点名的风险

1. **等权再平衡的换手**。品种进出宇宙、闸门开关都会全盘改权重。Carry 那条线的真凶正是
   过滤器抖动（53% 换手 / 年化 4.16% 成本，[[carry-cost-is-filter-whipsaw]]），这里 62 个
   品种只会更凶。所以 Task 6 要求换手**四分解**、Task 7 要求逐类报成本贡献——不是锦上添花，
   是这条线最可能的死因。
2. **研报样本恰好止于策略失效前**。本仓已经撞上两次（[[guosen-carry-paper-direction-is-wrong]]、
   [[index-open-momentum-out-of-sample-fails]]）。所以样本外分段从 Task 7 起就是**强制输出**，
   不是复刻成功之后的可选补充。

---

## 实施记录（2026-08-31）

**Task 6 / 7 / 8 代码全部交付**，全量 **1428 passed**（Task 5 收尾时是 1385）。
剩下的只有 Task 8 的端到端 12 次 —— 它等重建后的面板。

### 动手前先修掉的两个缺陷

1. **D16 的守卫错位一天**。主力选择滞后一个交易日，「旧主力是否仍可交易」却按
   **选择日**的合约池判。临期合约在 `selected_from` 那天还挂着、次日退市，于是面板
   建了上下文却一根 bar 都查不到 —— 全历史六例（AU1912 / AU2012 / AU2206 / LU2209 /
   FU2309 / FU2509）形态完全一致，每一例的 `selected_from` 恰是该合约在
   `futures_daily` 上的最后一个交易日。改为同时按 **trade_date 当日**的池判；
   整个品种当日无行时不判退市（`futures_daily` 有已知的整日空洞）。
   全历史主力 155,952 → **155,936**（−16；面板宇宙内的那 6 个只是其中一部分）。
   连续段 64、断代 1 不变。

2. **跨分片的成交价接力断了**。`build_panel` 的 `pending` 是跨月存活的，但
   `scripts/continuous/build_panel.py` **逐月各调一次**、每次从空字典开始。实测
   181 个分片里未补上的 `fill_pending` **恰好 6,259 个 = （品种 × 分片）组合数**，
   无一例外，即每个品种每个月最后一个交易日的收盘那根拿不到成交价（占品种日 4.94%，
   且系统性地钉在月末）。修法是把下个分片首日的上下文一并交进 `build_panel`，用
   `resolve_only_keys` 标出「只定价、不发 bar」。2024-02/03 两个月的实数据冒烟：
   `fill_pending` 57 → **0**，其余列（close / volume / adj_factor / continuity_segment /
   contract）**逐点相同**，`fill_price` 只新增 57 个、无丢失无变化。

   ⚠️ 这条只有**接线**才看得见：`build_panel` 自己的用例是单次调用跨两个月的，那样
   接力是通的。

### 三条新的口径裁决（已进保真度台账）

- **D17** `Mul_vol` 读**组合层影子收益**（只加 `Lev_ATR`、不乘 `Mul_vol`）。按字面读
  第一年 `Lev=0` ⇒ 收益恒 0 ⇒ 标准差 0 ⇒ `final_leverage` 对零波动硬失败，策略永远
  启动不了。被迫，不是选的。
- **D18** 展期算换手并在四分解里单列；换月当根的信号缩放一并计入 ROLL（一张合约一根
  bar 只能挂一个成因，而四类之和必须等于总换手）。
- **D19** 拿不到成交价就顺延到下一个有价的槽并记账；掉出宇宙是唯一例外（等不到）。
- **D20** 预热 = `max(ema_long, N + k − 1, atr_n)` 根**已成交** bar，按 `(品种, 连续段)` 各自计。

### 顺手修掉的一个共用层缺陷

`EventAccount.drain_daily_row` 在**零换手的日子**会硬失败：gross 与 net 走同一条相对
路径，`G/G₀−1` 与 `N/N₀−1` 在实数里恒等、浮点里差一两个 ulp，于是复利化成本是个
`−1.1e−16`。本策略闸门关着的日子远比 Carry 那条线多，所以这条一定会碰上。已夹取该档
噪音，真正为负的成本仍然硬失败；回归用例的价格序列是**搜出来的真实触发例**。

### 闸门几何（`docs/research/2026-08-31-continuous-gate-geometry.md`）

`ΔTNR > 0` 要求 TNR 严格上升，而 TNR 上界是 1 ⇒ 光滑趋势上位移恰等于路程、`TNR ≡ 1`、
`ΔTNR ≡ 0`，噪音闸**严格关闭**（线性 / 二次 / 几何三种趋势实测同为 0.0%，随机游走 49%）。
「距离扩大」则相反，趋势里几乎免费（99%~100%）—— 与动手前的判断相反。所以信号天然
断续，§6 风险 ① 的换手是**结构性**的，不是参数没调好。

### 剩余项

- Task 3 的 `- [ ] 全历史面板已生成`：旧面板已建成验收，但带上述缺陷 2；
  修正后的重建在 WSL2 跑（`output/continuous/panel_84d482d/`，日志
  `logs/panel_84d482d.log`，可续跑），届时要重跑一次验收对账。
- Task 8 的 12 次端到端：等重建。
- Task 0 的 `- [ ] 对照跑已确认可复现范围` / `- [ ] 产物哈希已留存` / `- [ ] 用例通过
  变异验证` 是更早会话的记账，本次未复核，**不代跑不代勾**。
- Task 3 的 `- [ ] 小窗口跑通，EXPLAIN 显示 chunk exclusion 生效`：今天的两月冒烟确实
  跑通了，但没看 `EXPLAIN`，所以不勾。

### ⚠️ WSL2 长跑会被静默收走

宿主没有交互登录时 H9 锚不起，最后一个 `wsl.exe` 退出后 8–15 秒整个 WSL 实例被拆重建
（`ps -p 1 -o etimes=` 归零、`/proc/uptime` 不变 —— 这对矛盾数字就是判据）。本次重建
第一轮就是这么死在第 20 个分片的。起长跑前先查
`(Get-Process wsl | Measure-Object).Count`，为 0 就自己从开发机挂一条
`ssh -p 2222 ... "wsl -d ubuntu2404 -e sleep 86000"`。

---

## 实施记录续（2026-08-31 下午）：第一次真数据回测的结论

**按计划当前口径，策略毛收益为正、净收益深度为负，差额全是交易成本。** 而且成本高到
研报自报业绩在**算术上不可能** —— 这本身就是口径读错了的证据。详见
`docs/research/2026-08-31-continuous-turnover-verdict.md`。

### 实测（旧面板，2015-01..2016-12，30 品种，322,106 根 bar，基线参数）

| | wide（D6 原样） | narrow（D21） |
|---|---:|---:|
| 毛收益（2 年） | +32.90% | +15.13% |
| **净收益（2 年）** | **−47.41%** | **−5.90%** |
| 累计成本 | 92.68% | 20.17% |
| 年化换手 | 3,682 倍 | 801 倍 |
| 夏普 | −2.79 | −0.32 |
| 最大回撤 | 48.4% | 15.7% |
| 换手四分解 | 信号 83% / 再平衡 17% | 信号 53% / **再平衡 46%** / 展期 1% |

全历史 wide 网格前三点（2011-01..2026-01，63 品种）：样本内夏普 −4.42 / −4.51 / −4.54，
样本外 −5.60 / −5.62 / −5.58。TNR 窗口取 10/20/40 几乎无差别 —— 成本压倒一切。

### 是哪道闸在抖（2015–2016，预热后 321,191 根 bar，按 bar 加权）

| 闸 | 通过率 |
|---|---:|
| 距离扩大 | 47.1% |
| `Lev_ATR > 1` | 76.8%（中位 1.42，**确认 D1**） |
| `ΔTNR > 0` | 48.6% |
| 三道同时 | 22.8% |

距离扩大与 ΔTNR 都是**每根 15 分钟 bar 上的近似抛硬币**，合取约 20% 的 bar 翻面 ⇒
方向每品种每天翻 5.2 次、79.2% 的 bar 空仓。

### D21（用户 2026-08-31 裁决：两条都跑）

D6 的依据只点名了一道闸：「当开仓杠杆率 **Lev_ATR<1** …… 即使此时传统信号为 1，
我们依然平仓操作」。它没说另外两道也会平仓，而抖得最凶的恰恰是它没点名的那两道。

- `--exit-gates wide` = D6 原样，**逐点不变**（旧面板两年探针，daily/segments/annual/
  turnover/leverage 五张表 sha 全等）。
- `--exit-gates narrow` = 只有 `Lev_ATR<1` 或均线状态反向才离场。

⚠️ D5（状态而非事件）**没有跟着改**：图 13 有连续两根「再次触发多仓条件」，穿越事件
不可能连续发生，所以递推的输入确实是状态。动的只是「离场用哪几道闸」。

### 一个还没裁的口径

narrow 下**剩余换手的 46% 来自等权再平衡与杠杆漂移**（每 15 分钟重算一次权重），
折合年化约 4.6% 成本。研报只说「等权分配资金」，没说多久重算一次权重。这是下一个
可能要裁的地方，本次未动。

### 另外补上的

- `--dtnr-mode lag`：D7 正文那一侧（「当日与 3 日前」），计划早已点名、之前没实现。
  滞后版预热要多一根（`TNR_t − TNR_{t−k}` 在第 k+1 个 TNR 上才有定义）。
- CLI 越界闸改为**按月比**：资产止于 2026-01-30，而 01-31 是周六，按自然月末比会把
  `--end 2026-01` 整个拒掉、白丢一个月样本外。
