# Futures Strategies Roadmap

更新日期：2026-08-18

本页是当前交付状态的唯一摘要；历史设计和实施细节保留在 `docs/plans/`、
`docs/specs/` 和 `docs/superpowers/`。

## 已交付

- `cta_gtja`：三因子价格/量价 guarded 路径、连续合约复权选择与 Data Quality V2。
- `cta_carry`：分合约 Carry 日线研究版、账户对账、报告与 CLI。
- 两条 `public-pg` 路径默认排除股指和国债期货；仅 `cta_gtja` 保留 `--include-financial`
  作为历史诊断例外，`cta_carry` 无此开关；文件源由输入数据负责边界。

## 实验，不是默认策略变更

- Carry 趋势滞后：`trend_band_atr=0`、`trend_confirm_days=1` 时逐点复现基线。
- `secondary_selection=second_by_oi`：研报次主力口径对照；默认仍为 `strictly_later`。
- `equal_weight_capital`：已实测为负面结果；默认关闭。

## 六因子基本面（2026-08-18 打通）

全窗口 build 已上线并被 CTA 端到端消费：
`conservative-20260818T060911Z-622e88840fa3`，catalog `v1`，2016-01-04..2026-04-29，
82,943 行，audit 零失败。三个春节后无数据的日期-指标切片由评审文件
`known-absent-v1.yaml` 显式豁免，并记录在 build 行上。

三组对照（同窗口、同九品种、2498 个交易日、1bp 成本）：

| 组 | 年化 | 波动 | 夏普 | 最大回撤 | 换手 |
|---|---|---|---|---|---|
| 六因子 medium_equal_weight | 3.65% | 8.22% | 0.444 | 15.9% | 0.204 |
| 六因子 high_composite | 4.09% | 13.0% | 0.315 | 39.9% | 0.380 |
| price-volume medium_equal_weight | 1.11% | 8.88% | 0.124 | 25.2% | 0.094 |
| price-volume high_composite | −0.46% | 13.5% | −0.034 | 45.5% | 0.183 |

基本面因子在两种组合方式下都优于纯量价对照。**这是单窗口结果，无样本外切分、
无成本敏感性测试；六因子换手是对照组的两倍多，夏普差对成本的稳健性未测。
是研究结论，不是可交易结论。**

细节与证据：market-monitor
`docs/operations/commodity-fundamentals-full-window-build-20260818.md`。

## 当前重点：基本面数据维护（用户手动推进）

下一步不是改策略，是换更好的数据源。当前每条序列、实测发布频率与断档、
现有选择的薄弱处、以及替换一条要付出的代价，见 market-monitor
`docs/data/commodity-fundamentals/current-series-inventory-20260818.md`。
最突出的几处：PP 库存只有 2023-07 起 156 个观测、TA 库存是「库存天数」而非吨量、
CU 库存只覆盖上海保税区、AL/CU/RB 现货是周频而其余六个是日频、RB 与 BU 完全没有
利润序列、所有利润公式都是零固定成本的裸价差。

替换后需重跑上面三组对照并与本页数字比较。

## 外部阻塞

- `futures_daily` 与 `continuous_contract_ohlc` 的 EOD 日更仍止于 2026-04-29；
  builder 的交易日历取自该表，所以任何 build 的上界都在那里。

## 仓库边界

- 本仓拥有商品期货与**股指期货**策略、只读数据适配、回测、质量闸门和研究报告。
- Wind 抽取、writer、数据库 DDL、标准数据构建属于 `market-monitor`。
- ~~股指期货类策略属于股票生态，不在本仓新增。~~ **2026-08-25 用户裁决推翻**：股指期货类
  策略改归本仓。依据是三条实测事实——① 分钟表 `public.futures_minute`（661,966,168 行，
  IF 2010-04-16 起无缺口）是本仓 2026-08-13 建的；② 15 分钟执行引擎（版本化时段、
  5 分钟 VWAP、吊灯止损三档减仓、影子账户波动率反馈）在本仓 `feature/carry-minute-execution`
  上正在建，与股指日内策略重合约八成；③ stock_selector 的策略七层布局是股票形状，
  其统一平台 `asset_class` 为 `equity | fund` 闭合枚举、设计 §4 Non-Goals 点名不收 CTA。
  首个落地对象 = 国信开盘动量（`docs/superpowers/plans/2026-07-09-guosen-open-momentum.md`，未开工）。
- ⚠️ 边界改动只挪**策略归属**，不挪数据责任：`cta_gtja` / `cta_carry` 两条 `public-pg` 路径
  默认排除股指与国债期货这条不变（`FINANCIAL_FUTURES` 常量原样保留）。
- 任何新策略都需要单独设计与批准；“期货归本仓”不是具体策略的实施授权。

## 股指期货：国信开盘动量（在建，26/48）

计划 = `docs/superpowers/plans/2026-07-09-guosen-open-momentum.md`（2026-08-25 从 stock_selector
迁入并按实测重写）。

**已完工**：Task 0（分钟层抽层）、Task 3~6（开盘信号 / ATR 与止损 / 持仓路径 / 杠杆组合，
全部由确定性合成数据覆盖并逐条变异验证）。
**下一步**：Task 1 → 2 → 7 → 8，闸门已全部解除。

**分钟层抽层已落地（2026-08-26，裁决方案 A）**：`feature/carry-minute-execution` 于 `1b5610b`
merge 后，`minute_{sessions,bars,pg_source,account}` 已从 `cta_carry/` 抽到 `common/minute/`
（`9a4ea9b` 净搬 + `74b4e3d` 破商品口径耦合）。

⚠️ 抽层的真正内容不是搬文件：`SESSION_RULES_VERSION` / `DAY_SEGMENTS` 是模块级常量，
`SessionRule` 硬拒非 `commodity-v1`，loader 给 5,292 条规则统统盖同一份日盘段。现由
`SessionRuleset` 值对象承载市场特性并由调用方传入；日盘段是**按生效日排序的日程**
（CFFEX 2016-01-01 缩短过日盘），`SessionSegment` 的边界也从写死的 900 改成结构性上下界
（15:15 = 第 915 分钟，原来根本构造不出 2016 前的股指日盘）。

Carry 侧逐点不变已量化：全套件 1065 passed（1054 + 新增 11）；真实
`config/carry_minute_sessions.csv` 在抽层前后加载出的 5,292 条规则 sha256 相同，
185 个 product-day 的分钟槽与 15 分钟桶逐点一致。新增用例变异验证 8/8。

📌 `minute_backtest.py` / `session_authority.py` / `decision.py` **仍留在 `cta_carry/`**：
前者是 Carry 策略语义，后两者是商品交易所公告资产。`EquityDepletedError` 因反向依赖被迫
一并移到 `common/errors.py`，`cta_carry.backtest` 原地再导出，类身份不变。
