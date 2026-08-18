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

- 本仓拥有商品期货策略、只读数据适配、回测、质量闸门和研究报告。
- Wind 抽取、writer、数据库 DDL、标准数据构建属于 `market-monitor`。
- 股指期货类策略属于股票生态，不在本仓新增。
- 任何新策略都需要单独设计与批准；“商品期货归本仓”不是具体策略的实施授权。
