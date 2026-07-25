## Futures Strategies (`futures_strategies/`)

商品期货策略研究（券商研报复刻），2026-07-11 从 `stock_selector/cta/` 剥离成独立项目（迁移计划：
stock_selector `docs/superpowers/plans/2026-07-11-extract-cta-to-futures-strategies.md`）。

- 布局：`common/`（PG 连接 / settings 加载 / 净值指标，拷贝自 stock_selector 后独立演化）+
  `cta_gtja/`（国君六因子 CTA，原 `stock_selector/cta/`）；下一个策略 = 国信《基于 Carry 的商品期货交易策略》。
- 数据源：market_monitor `public` schema（`continuous_contract_ohlc` / `spot_prices` / `inventory`；
  Carry 还需 `futures_daily` 分合约行情）。上游 = data-collecter + 连续合约生成器（用户领域，
  已归档 `_archive/2026H1_root_scripts/continuous/`）。⚠️ 2026-07-13 时点：`futures_daily` 与
  `continuous_contract_ohlc` 的 `standard`/`nanhua` 两套规则均冻在 04-29（连续合约已追平分合约行情，
  但 EOD 日更链仍停摆；见 market-monitor `DATABASE_INVENTORY.md`）。
- 入口：`cd futures_strategies && .venv/bin/python -m cta_gtja --source public-pg ...`；
  测试 `.venv/bin/python -m pytest -q`。详见项目 `README.md` 与
  `docs/operations/cta-strategy-replication.md`。
- **商品期货**类新策略（Carry、Bollinger 通道、道氏……）一律落这里；**股指期货**类策略（如国信
  开盘动量）按用户口径归股票生态、留 stock_selector（2026-07-12 用户拍板）。
