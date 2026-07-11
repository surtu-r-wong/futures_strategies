# Futures Strategies

商品期货策略研究项目（券商研报复刻）。2026-07-11 从 `stock_selector/cta/` 剥离成独立项目；
与股票多因子系统（stock_selector）数据域、概念域完全平行。

## 布局

- `common/` — PG 连接 / settings 加载 / 净值指标（从 stock_selector 拷贝一次，此后独立演化，不回同步）
- `cta_gtja/` — 国君六因子 CTA 因子组合复刻（原 `stock_selector/cta/`，复刻计划见 `docs/plans/2026-05-31-cta-strategy-replication-plan.md`）
- `docs/plans|operations|specs/` — 随迁的计划 / runbook / 设计文档
- 下一个策略：国信《基于 Carry 的商品期货交易策略》（主力/次主力价比年化 + 动量缩量过滤 + 吊灯止损）

## 数据源

`public` schema（market_monitor 库，Debian 主库 `100.65.111.79`）：`continuous_contract_ohlc`
（主力连续，raw/ba/fa 三线 + 数据质量审计）、`spot_prices`、`inventory`。上游生产链是
data-collecter + 连续合约生成器（用户领域，已归档 `~/claude-code/_archive/2026H1_root_scripts/continuous/`）。

⚠️ 截至 2026-07-11 的已知停摆（见 `market-monitor/DATABASE_INVENTORY.md`）：`futures_daily`
冻结在 2026-04-29，`continuous_contract_ohlc` 冻结在 2026-03-06（EOD 日更链停）。做 Carry
历史回测前需先恢复上游日更。

## 配置

`config/settings.yaml`（gitignored，明文 PG 密码，单机惯例）；模板 `config/settings.example.yaml`。
代码只用 `database:` / `test_database:` 两节，schema 在读取层强制 `public`。

## 运行

```bash
cd ~/claude-code/futures_strategies
.venv/bin/python -m pytest -q                    # 单元测试
.venv/bin/python -W ignore::UserWarning -m cta_gtja --source public-pg \
    --strategy both --factor-set price_volume \
    --start 2019-01-01 --end 2025-12-31          # 出 output/cta_*.xlsx + 净值 PNG
```

runbook：`docs/operations/cta-strategy-replication.md`
