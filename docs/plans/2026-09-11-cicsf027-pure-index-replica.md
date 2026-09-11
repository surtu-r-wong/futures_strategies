# CICSF027 纯指数复刻 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按中信官方编制方式建一条独立的**纯指数**复刻路径，产出 2010-01-04 基点 1000 的
CICSF027 基差动量策略指数序列，并与库里的官方序列 `CICSF027.WI` 比相关，定位 0.39 的缺口在哪一条规则上。

**Architecture:** 新包 `citic_index/`，与 `cta_carry` / `cta_gtja` 平级、**不改生产出单路径**。
分五层：品种池 → 三腿选取与链收益 → 基差动量因子 → 日频秩权重 → 指数累计。
通用件（`forward_only_chain` / `build_leg_returns` / `rank_linear_weights`）从 `cta_carry` 导入复用，
不复制。每条已审出的偏差（①T1 腿 ②除相隔月数 ③日频 ④三个月门槛 ⑤37 品种池）做成**可开关的参数**，
这样最后一步能逐条量出各自值多少——一次只动一条，否则归因不出来。

**Tech Stack:** Python 3.13、pandas、psycopg2、pytest。数据源 `public.futures_daily`（分块取数落本地
CSV 后走离线路径）、官方序列 `stock_selector.index_daily`。

**依据：** 设计文档 `docs/plans/2026-09-11-cicsf027-pure-index-replica-design.md`（含规则摘录、
偏差审计表、37 品种清单、验收判据、三条待裁决）。动手前先读它。

---

## 环境前提（先做，别跳）

**这个仓有别的会话在同时作业**（见记忆 `concurrent-sessions-share-the-repo`）。本工作全程在
worktree 里做，不在主工作树改任何文件：

```bash
cd /home/elfbob/claude-code/futures_strategies
git worktree add .worktrees/citic-index -b feature/citic-index-replica master
cd .worktrees/citic-index
ln -s ../../.venv .venv          # 复用主树的虚拟环境
cp ../../config/settings.yaml config/settings.yaml 2>/dev/null || true
.venv/bin/python -m pytest -q    # 基线：记下通过数，后面每步都要对比
```

**取数纪律**：本机到库是 DERP 中继，**撑不住几分钟的单条连接**，但短查询 0.5 秒（记忆
`db-path-is-derp-relayed`）。所有取数一律分块 + 每块重试 + 落盘后逐月对账，绝不写一条几分钟的大查询。

---

## Task 1: 品种池

**Files:**
- Create: `citic_index/__init__.py`
- Create: `citic_index/universe.py`
- Test: `tests/test_citic_universe.py`

编制方式 3.2：过去 20 个交易日平均沉淀资金 ≥ 20 亿；上市时间 ≥ 三个月；37 个指定品种；
涨跌停与退市排除。沉淀资金 = Σ(持仓量 × 收盘 × 乘数)，乘数按品种日取
`成交额 ÷ (成交量 × 收盘)` 的当日中位数再做**逐日扩展中位数**（无前视）——与 `cta_carry` 同法。

**Step 1: 写失败的测试**

```python
# tests/test_citic_universe.py
from datetime import date
import pandas as pd
from citic_index.universe import NAMED_37, pool_membership

def test_named_universe_is_the_37_from_section_3_2():
    assert len(NAMED_37) == 37
    assert {"AL", "AU", "CU", "ZN", "NI", "AG"} <= NAMED_37   # 有色贵金属在名单里
    assert "SA" not in NAMED_37                                # 纯碱不在

def test_a_product_needs_three_months_of_listing():
    # M 每天沉淀资金 30 亿，远超门槛；上市日 2024-01-02。
    # 三个月 = 63 个交易日，所以第 63 个交易日才入池。
    days = pd.bdate_range("2024-01-02", periods=70)
    prices = pd.DataFrame({
        "trade_date": [d.date() for d in days],
        "product": "M", "contract": "M2405.DCE",
        "close": 3000.0, "volume": 1000.0, "oi": 100000.0,
        "turnover": 3000.0 * 1000.0 * 10.0,
    })
    pool = pool_membership(prices, liquidity_window=20, threshold=2e9, min_listing_days=63)
    first = pool.loc[pool["in_pool"], "trade_date"].min()
    assert first == days[62].date()
```

**Step 2: 跑，确认它因 `ModuleNotFoundError: citic_index` 而失败**

`.venv/bin/python -m pytest tests/test_citic_universe.py -q`

**Step 3: 实现 `citic_index/universe.py`**

`NAMED_37` 抄设计文档 §4 的表（37 个代码）。`pool_membership(prices, *, liquidity_window,
threshold, min_listing_days, restrict_to_named=True, limit_locked=None)` 返回
`trade_date / product / liquidity_mean / listed_days / in_pool / reason`。
`restrict_to_named` 与 `limit_locked` 都是开关，最后一步要用它们做归因。

**Step 4: 跑通**

**Step 5: 变异验证**——把 `>= threshold` 改成 `> 0`，确认测试变红；改回。
（等长变异要先 `find . -name __pycache__ -exec rm -rf {} +`，否则 `.pyc` 不失效、假报"测试没抓住"，
见记忆 `mutation-runs-need-pycache-clear`。）

**Step 6: Commit** — `feat(citic): the 37-product pool with the three-month listing gate`

---

## Task 2: 三腿选取与链收益

**Files:**
- Create: `citic_index/legs.py`
- Test: `tests/test_citic_legs.py`

编制方式 3.5 第一步：**T1 = 交割月早于主力、持仓量最高的合约；若没有，就是主力本身**。
T2 = 交割月晚于主力、持仓量最高的合约。主力 = 持仓量最大。二者都 forward-only（3.4）。

⚠️ **这就是偏差 ①**：生产腿把 T1 取成了主力本身。这里必须取近月主力，并把
`t1_leg="main"` 留成开关，好在 Task 9 里量它值多少。

**Step 1: 写失败的测试（手算字面量）**

```python
# tests/test_citic_legs.py
import pandas as pd
from citic_index.legs import select_legs

def test_t1_is_the_highest_oi_contract_delivering_before_the_dominant():
    day = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-03-01").date()] * 3,
        "product": "M",
        "contract": ["M2403.DCE", "M2405.DCE", "M2409.DCE"],
        "delivery_yyyymm": [202403, 202405, 202409],
        "oi": [30000.0, 90000.0, 50000.0],     # 主力 = M2405
        "close": [3100.0, 3000.0, 2950.0],
    })
    legs = select_legs(day)
    row = legs.iloc[0]
    assert row["main_contract"] == "M2405.DCE"
    assert row["t1_contract"] == "M2403.DCE"    # 早于主力里 OI 最高的
    assert row["t2_contract"] == "M2409.DCE"    # 晚于主力里 OI 最高的
    assert row["month_gap"] == 6                # 202403 -> 202409

def test_t1_falls_back_to_the_dominant_when_nothing_delivers_earlier():
    day = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-03-01").date()] * 2,
        "product": "M", "contract": ["M2405.DCE", "M2409.DCE"],
        "delivery_yyyymm": [202405, 202409], "oi": [90000.0, 50000.0],
        "close": [3000.0, 2950.0],
    })
    row = select_legs(day).iloc[0]
    assert row["t1_contract"] == "M2405.DCE"
    assert row["month_gap"] == 4
```

**Step 2–4:** 跑失败 → 实现 `select_legs(frame, *, t1_leg="near_dominant")` → 跑通。
链收益直接复用：`from cta_carry.legreturns import forward_only_chain, build_leg_returns`。

**Step 5: 变异验证** —— 把 `< main_delivery` 改成 `<= main_delivery`，确认第一个测试变红。

**Step 6: Commit** — `feat(citic): CITIC 3.5 step 1 leg selection, T1 is the near dominant`

---

## Task 3: 基差动量因子

**Files:**
- Create: `citic_index/factor.py`
- Test: `tests/test_citic_factor.py`

```
BM_it = [ ∏(1+r^T1) − ∏(1+r^T2) ] ÷ 相隔月数
```

⚠️ **偏差 ②** 就在那个除法上，做成开关 `normalise_by_gap=True`。
⚠️ **偏差 ④**：历史门槛。中信只要求上市三个月，所以窗口不足时的处理必须**明确**：
`min_observations` 参数，复刻档设为「窗口内有多少算多少，但至少 `min_obs_floor` 天」，
生产腿那种 ≥90% 的严格档作为对照值。**绝不 `fillna(0)` 再滚动**——那是 09-10 已定案的补零缺陷
（设计文档 §1.2），会系统性虚增长回望期。

**Step 1: 写失败的测试（手算）**

```python
# tests/test_citic_factor.py
import pandas as pd
from citic_index.factor import basis_momentum

def test_bm_is_the_product_difference_divided_by_the_month_gap():
    # T1 连涨 3 天各 +10%，T2 各 +0%，相隔 4 个月。
    # ∏(1.1)^3 = 1.331，∏(1.0)^3 = 1.0 ⇒ 差 0.331，÷4 = 0.08275
    frame = pd.DataFrame({
        "trade_date": pd.bdate_range("2024-01-01", periods=3).date,
        "product": "M",
        "t1_return": [0.10, 0.10, 0.10],
        "t2_return": [0.0, 0.0, 0.0],
        "month_gap": 4,
    })
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.08275, abs=1e-9)

def test_without_normalisation_it_is_the_raw_difference():
    ...  # 同一输入，normalise_by_gap=False ⇒ 0.331
```

**Step 2–4:** 跑失败 → 实现（`expm1(rolling(log1p).sum())` 之差，再除 `month_gap`）→ 跑通。

**Step 5: 变异验证** —— 去掉 `÷ month_gap`，确认第一个测试变红。

**Step 6: Commit** — `feat(citic): basis momentum normalised by the month gap`

---

## Task 4: 日频秩权重

**Files:**
- Create: `citic_index/weights.py`
- Test: `tests/test_citic_weights.py`

3.5 第二/三步。**复用** `from cta_carry.signals import rank_linear_weights`——它已逐字实现该公式
且有自己的测试。本模块只负责：逐日取截面、按 `basis_momentum` 升序、调用它、写回。

⚠️ **偏差 ③**：节拍。`cadence="daily"` 是复刻档，`"monthly"` 保留作对照。

**Step 1: 写失败的测试**

```python
def test_weights_are_recomputed_every_day_and_sum_to_zero():
    # N=5 的截面：Rank 1..5 ⇒ w = (r-3)/15 ⇒ [-2/15,-1/15,0,1/15,2/15]
    ...
    assert w.sum() == pytest.approx(0.0, abs=1e-15)
    assert w.iloc[-1] == pytest.approx(2/15)

def test_monthly_cadence_holds_the_struck_weights_inside_the_month():
    ...
```

**Step 2–6:** 同上循环；变异：把 `ascending=True` 改成 `False`，确认符号断言变红。
Commit — `feat(citic): daily cross-sectional rank weights`

---

## Task 5: 指数累计

**Files:**
- Create: `citic_index/index.py`
- Test: `tests/test_citic_index.py`

3.5 第四/五步：`R_{T+1} = Σ w_p r_p`；换月日 `r_i = w̄·r_new + (1−w̄)·r_old`；
`MoMI_{T+1} = MoMI_T × (1+R_{T+1})`；基日 2010-01-04 = 1000。

⚠️ **偏差 ⑦** 就是换月日那个混合，做成开关 `roll_blend=True`。

**Step 1: 写失败的测试（手算字面量）**

```python
def test_index_compounds_the_weighted_cross_section():
    # 两品种：w=[-0.5, +0.5]，r=[-0.02, +0.04] ⇒ R = 0.01+0.02 = 0.03
    # 1000 -> 1030
    ...
    assert series.iloc[1] == pytest.approx(1030.0)

def test_roll_day_blends_the_two_contracts_by_value_share():
    # w̄=0.25, r_new=0.08, r_old=0.04 ⇒ r = 0.25*0.08 + 0.75*0.04 = 0.05
    ...
```

**Step 2–6:** 同上；变异：把 `w̄·r_new + (1−w̄)·r_old` 写成 `r_new`，确认第二个测试变红。
Commit — `feat(citic): index accumulation from the base date`

---

## Task 6: 取数与离线包

**Files:**
- Create: `scripts/citic_index_fetch.py`
- Test: `tests/test_citic_data.py`

按日期分块（60 天一块）拉 37 个品种的 `public.futures_daily`（2009-01-01 起，给三个月上市门槛留出
前置），每块自己重试、每块新开短连接，拼起来落 `data/citic_index/prices.csv`。
**落盘后逐月 `COUNT(*)` 与库对账，不一致就报错退出**——不对账的本地包不许拿来出结论。
同时拉 `stock_selector.index_daily` 里 `CICSF027.WI` 与 `CICSF025.WI` 落 `official.csv`。

测试只测纯函数（分块边界、拼接去重、对账判据），不连库。

Commit — `feat(citic): chunked fetch with a month-by-month reconciliation gate`

---

## Task 7: CLI 与端到端

**Files:**
- Create: `citic_index/__main__.py`
- Test: `tests/test_citic_cli.py`

```bash
.venv/bin/python -m citic_index \
  --data-dir data/citic_index --start 2010-01-04 --end 2026-09-10 \
  --window 500 --cadence daily --t1-leg near_dominant --normalise-by-gap \
  --min-listing-days 63 --restrict-to-named \
  --output-prefix output/citic/cicsf027
```

产出 `<prefix>_index.csv`（trade_date / index_value / daily_return / n_products）与
`<prefix>.xlsx`（legs / factor / weights / index / run_config 五张表）。
`run_config` 必须记全部开关 + `code_version` + 数据行数——这是后面归因唯一的凭据。

Commit — `feat(citic): CLI for the pure index replica`

---

## Task 8: 与官方序列比较

**Files:**
- Create: `citic_index/compare.py`
- Test: `tests/test_citic_compare.py`

输出：日收益 pearson/spearman、净值相关、年化/夏普/回撤对照、**逐年收益两列并排**、
以及 `corr(replica_t, official_{t−k})` 的 **lag 扫描 k∈[−3,3]**。

⚠️ lag 扫描不是可选项：025 那次交叉验证里日相关只有 0.105，但 `corr(engine_t, replica_{t−1})=0.87`
——分歧是一天错位而不是逻辑差异（记忆 `carry-term-structure-index-status`）。**先扫 lag 再下结论。**

Commit — `feat(citic): compare a replica against the official series`

---

## Task 9: 控制臂 —— 同引擎跑 025

**Files:**
- Modify: `citic_index/factor.py`（加 `factor="term_structure"` 分支）
- Test: `tests/test_citic_term_structure.py`

先读 `/home/elfbob/exchange/20260903/中信期货期限结构策略指数编制方式.pdf`（**看原图，别只做文字提取**，
记忆 `continuous-spec-audit`），把它的 3.5 实现进来，跑出 CICSF025 复刻与 `CICSF025.WI` 比。

**这一步是闸**：025 已知可复刻到 9.37%/1.90 对 9.31%/1.89。
**若新引擎连 025 都对不上，027 的任何数字都不能归因给 027**，必须先修引擎。

Commit — `feat(citic): the term-structure control arm`

---

## Task 10: R 标定与偏差归因

**Files:**
- Create: `scripts/citic_027_attribution.py`
- Create: `docs/plans/2026-09-11-cicsf027-pure-index-replica-results.md`

**10a — R 标定**：在 {60, 120, 250, 500, 750} 上扫，按**与官方序列的相关**挑。
**报全表，不报选中点**（记忆 `report-sensitive-quantities-at-the-selected-point`）。

**10b — 逐条归因**：从"生产腿口径"出发，**一次只关掉一条偏差**，量相关的变化：

| 档 | ① T1 腿 | ② 除月数 | ③ 节拍 | ④ 门槛 | ⑤ 品种池 | 与官方相关 |
|---|---|---|---|---|---|---|
| 生产腿口径 | main | 否 | 月度 | ≥450/500 | 剔除 19 个 | 基线 |
| +① | near_dominant | 否 | 月度 | ≥450/500 | 同上 | |
| +①② | near_dominant | 是 | 月度 | 同上 | 同上 | |
| +①②③ | … | | 日频 | | | |
| +①②③④ | | | | 63 天 | | |
| **全部（复刻档）** | near_dominant | 是 | 日频 | 63 天 | 37 品种 | |

⚠️ **一次只动一条**。同时动多条，量到的是交互项，归因不出来。
⚠️ **别拿同一配置里更多的天数去佐证一个可疑的数**——那是循环（记忆
`window-length-contaminates-path-dependent-signals`）。要变的是配置本身。

**10c — 写结果文档**：把上表、R 全表、lag 扫描、逐年对照落进 results 文档，并回填设计文档 §7 的
三条待裁决。**若全部修正后相关仍上不去**，那是真结论，照实写——不要回头去调参数凑。

Commit — `docs(citic): what each rule deviation was worth`

---

## 收尾

1. `.venv/bin/python -m pytest -q` 全绿，且通过数 ≥ 基线。
2. `.venv/bin/python -m pytest --collect-only -q` 无 import 错。
3. 确认**没有碰**生产路径：`git diff master --stat -- cta_carry/ scripts/carry_tsindex_daily.sh` 应为空。
4. 回主树前先 `git log --oneline -3` 看 master 有没有被另一个会话推进；有就先看那些提交改了什么。
5. 用 superpowers:finishing-a-development-branch 决定合并方式。
