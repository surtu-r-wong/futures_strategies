## Futures Strategies (`futures_strategies/`)

商品期货策略研究（券商研报复刻），2026-07-11 从 `stock_selector/cta/` 剥离成独立项目（迁移计划：
stock_selector `docs/superpowers/plans/2026-07-11-extract-cta-to-futures-strategies.md`）。

- 布局：`common/`（PG 连接 / settings 加载 / 净值指标，拷贝自 stock_selector 后独立演化）+
  `cta_gtja/`（国君 CTA：量价 guarded 路径可用；完整六因子等待标准基本面 build）+
  `cta_carry/`（国信 Carry 日线研究版已交付，实验参数默认保持基线兼容）。当前状态见
  `docs/ROADMAP.md`。
- 数据源：market_monitor `public` schema（`continuous_contract_ohlc` / `spot_prices` / `inventory`；
  Carry 还需 `futures_daily` 分合约行情）。上游 = data-collecter + 连续合约生成器（用户领域，
  已归档 `_archive/2026H1_root_scripts/continuous/`）。⚠️ 2026-07-13 时点：`futures_daily` 与
  `continuous_contract_ohlc` 的 `standard`/`nanhua` 两套规则均冻在 04-29（连续合约已追平分合约行情，
  但 EOD 日更链仍停摆；见 market-monitor `DATABASE_INVENTORY.md`）。
- 入口：`cd futures_strategies && .venv/bin/python -m cta_gtja --source public-pg ...`；
  测试 `.venv/bin/python -m pytest -q`。详见项目 `README.md` 与
  `docs/operations/cta-strategy-replication.md`。
- **商品期货**类新策略（Carry、Bollinger 通道、道氏……）一律落这里。
- **股指期货**类策略也落这里（**2026-08-25 用户裁决，推翻 2026-07-12 那条"归股票生态"**）：
  分钟表 `public.futures_minute`（661,966,168 行 / IF 2010-04-16 起无缺口 / 有 `amount` 可算精确
  VWAP）是本仓 2026-08-13 建的（`docs/plans/2026-08-12-futures-minute-ingestion-design.md`），
  15 分钟执行引擎在 `feature/carry-minute-execution` 上正在建，股指日内策略与之重合约八成。
  首个对象 = 国信开盘动量，计划 `docs/superpowers/plans/2026-07-09-guosen-open-momentum.md`（**未开工**）。
- ⚠️ 只挪策略归属，不挪数据责任：`cta_gtja` / `cta_carry` 的 `public-pg` 路径默认排除股指与
  国债期货（`FINANCIAL_FUTURES`）这条**不变**。
- ⚠️ 分钟数据一律用 `public.futures_minute`，**不要用 `public.market_data_minute`**（后者是 Wind
  实时快照：中金所只有 IC/IM、无 `amount`、`volume` 是日内累计 ⇒ 算不出 VWAP）。
