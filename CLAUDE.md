## Futures Strategies (`futures_strategies/`)

商品期货策略研究（券商研报复刻），2026-07-11 从 `stock_selector/cta/` 剥离成独立项目（迁移计划：
stock_selector `docs/superpowers/plans/2026-07-11-extract-cta-to-futures-strategies.md`）。

- 布局：`common/`（PG 连接 / settings 加载 / 净值指标，拷贝自 stock_selector 后独立演化）+
  `cta_gtja/`（国君 CTA：量价 guarded 与标准六因子路径均已交付）+
  `cta_carry/`（国信 Carry 日线研究版已交付，实验参数默认保持基线兼容）。当前状态见
  `docs/ROADMAP.md`。
- 数据源：market_monitor `public` schema（`continuous_contract_ohlc` / `spot_prices` / `inventory`；
  Carry 还需 `futures_daily` 分合约行情）。上游 = data-collecter + 连续合约生成器（用户领域，
  已归档 `_archive/2026H1_root_scripts/continuous/`）。日线 EOD 已恢复（09-15 核验）；
  每次运行按实际覆盖确定截止日。基本面完整构建与日更于 09-29 恢复、09-30 首轮验收通过；
  商品分钟时段资产仍止于 2026-01-30。当前证据和边界见 `docs/ROADMAP.md`。
- 入口：`cd futures_strategies && .venv/bin/python -m cta_gtja --source public-pg ...`；
  测试 `.venv/bin/python -m pytest -q`。详见项目 `README.md` 与
  `docs/operations/cta-strategy-replication.md`。
- **商品期货**类新策略（Carry、Bollinger 通道、道氏……）一律落这里。
- **研究职责（2026-09-15 用户裁决）**：本仓保持期货专业研究；Alpha #013 的股票后续研究交接到 stock_selector。接手入口：[股票交接总结](../stock_selector/ALPHA013_HANDOFF.md)；本仓保留 [商品期货试验结论](docs/research/2026-09-15-alpha013-commodity.md)。
- **股指期货**类策略也落这里（**2026-08-25 用户裁决，推翻 2026-07-12 那条"归股票生态"**）：
  分钟表 `public.futures_minute`（661,966,168 行 / IF 2010-04-16 起无缺口 / 有 `amount` 可算精确
  VWAP）是本仓 2026-08-13 建的（`docs/plans/2026-08-12-futures-minute-ingestion-design.md`），
  15 分钟执行引擎已合并，公共分钟层在 `common/minute/`。
  国信开盘动量已完成实现与全历史回测（样本外为负）；尾部备用主力通路仍未接线。
  计划 `docs/superpowers/plans/2026-07-09-guosen-open-momentum.md`。
- ⚠️ 只挪策略归属，不挪数据责任：`cta_gtja` / `cta_carry` 的 `public-pg` 路径默认排除股指与
  国债期货（`FINANCIAL_FUTURES`）这条**不变**。
- ⚠️ 分钟数据一律用 `public.futures_minute`，**不要用 `public.market_data_minute`**（后者是 Wind
  实时快照：中金所只有 IC/IM、无 `amount`、`volume` 是日内累计 ⇒ 算不出 VWAP）。
