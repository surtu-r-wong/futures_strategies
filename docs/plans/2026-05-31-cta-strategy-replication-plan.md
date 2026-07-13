# CTA 因子组合策略复刻工作计划

> **迁移注**（2026-07-11）：本文档随 CTA 剥离迁自 stock_selector；文中 `cta/`、`tests/test_cta_*`、`docs/superpowers/*` 等路径为当时原仓路径（历史记录，不改写），现行代码对应本仓 `cta_gtja/`、`tests/`、`docs/plans|specs/`。

> **状态更新（2026-07-13）**：乘法复权改造和 2026-03-06 至 2026-04-29 的连续合约回填已经完成，
> `standard`/`nanhua` 均追平 `futures_daily` 至 2026-04-29；整个 EOD 链仍停在该日。量价 guarded
> 路径可运行，当前推进 Data Quality V2；基本面标准化与 WSD 回填不在本阶段范围。

日期：2026-05-31

## 目标

基于当前项目与 PostgreSQL `public` schema 中已有期货数据，复刻《CTA因子组合系列集合产品介绍》中的 CTA 因子组合策略：

- `medium_equal_weight`：CTA 因子组合（中波等权），目标波动率 8%，最大杠杆 2.5 倍。
- `high_composite`：CTA 因子组合 2 号（高波复合），60% 等权组合 + 40% 因子轮动，目标波动率 12%，最大杠杆 3.5 倍，单因子权重上限 50%。

## 架构决策

当前不单独拆出新仓库。

短期保留在本仓库顶层 `cta/` 包中开发，原因：

- 可复用现有 `config/settings.yaml`、PostgreSQL 连接封装、测试环境和输出目录。
- CTA 数据目前仍在同一个数据库 `public` schema，直接接入成本最低。
- 策略口径、基本面回填表、因子覆盖率和回测验收尚未稳定，过早拆仓库会增加迁移成本。

中期拆分触发条件：

- CTA 策略已能稳定从 `public` 或独立商品数据 schema 读取数据并完成回测。
- CTA 有独立配置、测试、文档、回填脚本和日常更新任务。
- 与股票策略只共享 DB/Wind/日历等基础设施，不共享业务代码。

满足后建议迁移到独立项目：

```text
/home/elfbob/claude-code/cta_selector
```

## 当前数据盘点

`public` schema 已确认：

| 表 | 状态 | 用途 |
|---|---|---|
| `continuous_contract_ohlc` | 388,942 行，1995-04-17 至 2026-03-05 | CTA 主行情表 |
| `continuous_dominant_daily` | 415,277 行，1995-04-17 至 2026-03-05 | 主力合约映射 |
| `futures_daily` | 2,069,715 行，1995-04-17 至 2026-04-29 | 单合约日线，连续合约回填来源 |
| `futures_contract_info` | 2025-12-22 至 2026-05-29 | 合约参数、保证金、手续费 |
| `spot_prices` | 仅 `M`，2015-01-05 至 2025-10-17 | 现货价 |
| `inventory` | 仅 `M`，2020-01-02 至 2025-10-16 | 库存 |
| `commodities_spot_prices` | 2000-01-06 至 2021-01-13 | 老现货/仓单数据 |
| `spreads` | 空表 | 暂不可用 |
| `indicators` | 空表 | 暂不可用 |

已知缺口：

- `continuous_contract_ohlc` 比 `futures_daily` 滞后约 55-58 天，`BR` 滞后约 82 天。
- 基差因子当前只有 `M` 有新表现货数据；老表可补 2021-01-13 以前多品种历史。
- 库存因子当前只有 `M`。
- 仓单数据存在于 `commodities_spot_prices.attribute='仓单'`，但尚未标准化接入。
- 利润因子暂无明确数据表或字段。

## 已完成

- [x] 新增 `cta/` 顶层包，避免混入股票 `stock_selector/` 包。
- [x] 新增 CTA 数据合同：`CTADataSet`。
- [x] 新增 CSV/Parquet 离线数据源。
- [x] 新增 PostgreSQL `public` schema 数据源：`cta.pg_source.load_public_cta_data`。
- [x] 默认读取 `public.continuous_contract_ohlc`。
- [x] 默认排除金融期货：`IF/IC/IH/IM/T/TF/TL/TS`。
- [x] 新增 6 个 CTA 因子骨架：
  - `basis`
  - `inventory`
  - `profit`
  - `long_rule_momentum`
  - `long_cross_momentum`
  - `price_volume_corr`
- [x] 新增两条产品策略：
  - `medium_equal_weight`
  - `high_composite`
- [x] 新增 CLI：`.venv/bin/python -m cta`。
- [x] 新增测试：`tests/test_cta_strategy.py`。
- [x] 用 public 实盘库短区间 smoke 跑通：
  - `medium_equal_weight`
  - `high_composite`

## 阶段 1：连续合约回填

目标：让价格类 CTA 因子可以跑到 `futures_daily` 的最新日期。

任务：

- [ ] 梳理 `continuous_dominant_daily` 与 `continuous_contract_ohlc` 的生成逻辑。
- [ ] 确认 `rule_type='standard'` 的换月规则。
- [ ] 用 `futures_daily` 回填 `continuous_dominant_daily`：
  - 起点：2026-03-06
  - 终点：2026-04-29
- [ ] 用 `futures_daily` 回填 `continuous_contract_ohlc`：
  - 起点：2026-03-06
  - 终点：2026-04-29
- [ ] 针对 `BR` 单独确认滞后原因。
- [ ] 增加覆盖率检查脚本，输出每个品种：
  - `futures_daily_max_date`
  - `continuous_max_date`
  - `days_lag`
  - `missing_trade_dates`

验收：

- [ ] 商品期货连续行情最新日期与 `futures_daily` 对齐。
- [ ] `daily_return` 与连续价格收益误差在可解释范围内。
- [ ] 换月日 `is_rolling`、`old_contract`、`new_contract`、`roll_weight` 正常。

## 阶段 2：量价类策略验收

目标：先不依赖基本面，完成可稳定运行的价格/量价 CTA。

任务：

- [ ] 固化可交易 universe：
  - 上市满 1 年。
  - 近 20/60 日成交额或成交量过滤。
  - 默认排除金融期货。
- [ ] 验收 `long_rule_momentum`。
- [ ] 验收 `long_cross_momentum`。
- [ ] 验收 `price_volume_corr`。
- [ ] 运行 `medium_equal_weight` 的量价子集版本。
- [ ] 运行 `high_composite` 的量价子集版本。
- [ ] 输出回测报告：
  - 净值
  - 年度收益
  - 月度收益
  - 最大回撤
  - 滚动夏普
  - 因子贡献
  - 换手与成本

验收：

- [ ] 2019-01-01 至 2025-09-30 可跑通。
- [ ] 2016-01-01 至 2025-09-30 对有足够历史的品种可跑通。
- [ ] 不出现大面积空仓或异常杠杆。

## 阶段 3：基本面表标准化

目标：为基差、库存、仓单、利润因子建立稳定输入。

建议新增标准表：

```text
commodity_product_mapping
commodity_spot_daily
commodity_inventory_daily
commodity_warehouse_receipt_daily
commodity_profit_formula
commodity_profit_daily
```

任务：

- [ ] 建立 `commodity_product_mapping`：
  - `product_code`
  - `base_symbol`
  - `product_name`
  - `source_name`
  - `active`
- [ ] 将 `spot_prices` 标准化迁入 `commodity_spot_daily`。
- [ ] 将 `inventory` 标准化迁入 `commodity_inventory_daily`。
- [ ] 将 `commodities_spot_prices.attribute='仓单'` 标准化迁入 `commodity_warehouse_receipt_daily`。
- [ ] 评估 `commodities_spot_prices.attribute='现货'` 是否可迁入 `commodity_spot_daily`。
- [ ] 为利润因子设计 `commodity_profit_formula`。
- [ ] 为首批品种导入或派生 `commodity_profit_daily`。

首批品种：

```text
M, RB, CU, AL, TA, PP, MA, BU, RU, AU, AG
```

验收：

- [ ] 每张标准表有唯一键与幂等 upsert 规则。
- [ ] 每条数据保留 source/source_payload 或可追溯来源。
- [ ] 每个基本面因子可输出日期级覆盖率。

## 阶段 4：基本面因子接入

目标：复刻 PDF 中完整 6 因子。

任务：

- [ ] `basis` 改读标准现货表：
  - `basis_rate = (spot - futures_close) / futures_close`
- [ ] `inventory` 改读标准库存表。
- [ ] 新增 `warehouse_receipt` 因子，作为扩展因子或库存补充。
- [ ] `profit` 改读标准利润表。
- [ ] 给每个基本面因子增加覆盖率输出。
- [ ] 覆盖率低于阈值时自动降权或剔除该因子当日信号。

验收：

- [ ] 基差、库存、利润因子不再只依赖 `M`。
- [ ] 因子缺失不会污染组合权重。
- [ ] `factor_returns` 可区分量价类与基本面类。

## 阶段 5：产品策略对齐

目标：对齐参考 PDF 的两条产品策略。

任务：

- [ ] 中波等权：
  - 6 因子等权。
  - 目标波动 8%。
  - 最大杠杆 2.5。
- [ ] 高波复合：
  - 60% 等权组合。
  - 40% 因子轮动。
  - 单因子最大权重 50%。
  - 目标波动 12%。
  - 最大杠杆 3.5。
- [ ] 输出产品级报告：
  - 历史净值
  - 年/月度收益表
  - 回撤
  - 因子贡献
  - 滚动夏普
  - 持仓周期/换手
  - 与大类资产相关性

验收：

- [ ] 两条策略均可一键运行。
- [ ] 输出 Excel 与 PNG 文件。
- [ ] 结果中包含 `metrics`、`period_returns`、`weights`、`factor_allocations`、`factor_returns`。

## 阶段 6：日常任务与监控

目标：让 CTA 策略进入可维护状态。

任务：

- [ ] 新增连续合约每日更新任务。
- [ ] 新增基本面数据回填/更新任务。
- [ ] 新增 CTA 数据 sanity check：
  - 最新日期滞后。
  - 单品种缺失。
  - 异常价格跳变。
  - 因子覆盖率。
- [ ] 新增 CTA 策略 smoke：
  - 短区间快速跑。
  - 输出关键指标。
- [ ] 在 `README` 或 runbook 中加入 CTA 运行入口。

验收：

- [ ] 每日更新后能自动发现缺口。
- [ ] 策略运行前能阻止明显不可信数据。
- [ ] 回测输出稳定可复现。

## 阶段 7：拆分项目评估

目标：决定是否迁移到独立 `cta_selector`。

评估条件：

- [ ] CTA 连续合约与基本面数据已稳定。
- [ ] CTA 策略与股票策略没有业务代码耦合。
- [ ] CTA 有独立配置、测试、文档、运行入口。
- [ ] CTA 需要独立部署或独立调度。

若全部满足，迁移为：

```text
/home/elfbob/claude-code/cta_selector
```

否则继续保持当前仓库顶层 `cta/` 包。

## 下一步建议

优先执行：

1. 阶段 1：补齐连续合约到 `futures_daily` 最新日期。
2. 阶段 2：先完成量价三因子的全品种策略验收。
3. 阶段 3：开始标准化仓单和现货表，先补 `M` 以外的核心品种。

