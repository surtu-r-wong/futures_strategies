# Futures Strategies

商品期货策略研究项目（券商研报复刻）。2026-07-11 从 `stock_selector/cta/` 剥离成独立项目；
与股票多因子系统（stock_selector）数据域、概念域完全平行。

## 布局

- `common/` — PG 连接 / settings 加载 / 净值指标（从 stock_selector 拷贝一次，此后独立演化，不回同步）
- `cta_gtja/` — 国君六因子 CTA 因子组合复刻（原 `stock_selector/cta/`，复刻计划见 `docs/plans/2026-05-31-cta-strategy-replication-plan.md`）
- `cta_carry/` — 国信 Carry 商品期货日线研究版（分合约期限结构 + 动量缩量缩仓过滤 + 吊灯止损）
- `docs/plans|operations|specs/` — 随迁的计划 / runbook / 设计文档

## 数据源

`public` schema（market_monitor 库，Debian 主库 `100.65.111.79`）：`futures_daily`（分合约）、
`continuous_contract_ohlc`（主力连续，raw/ba/fa 三线 + 数据质量审计）、`spot_prices`、`inventory`。上游生产链是
data-collecter + 连续合约生成器（用户领域，已归档 `~/claude-code/_archive/2026H1_root_scripts/continuous/`）。

⚠️ 截至 2026-07-13 的已知停摆（见 `market-monitor/DATABASE_INVENTORY.md`）：`futures_daily`
与 `continuous_contract_ohlc` 的 `standard`/`nanhua` 两套规则均冻结在 2026-04-29，连续合约已追平
分合约行情，但整个 EOD 日更链仍停摆。历史回测可显式截止 2026-04-29，且不受 `--strict` 扫描影响；
需要覆盖后续区间或投入调度/实盘前，需先恢复上游日更。

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

## Carry 日线研究版

`cta_carry/` 使用分合约 `public.futures_daily`，按前 120 个品种交易日的日均成交额建立动态交易池，逐日选择主力与严格晚月次主力，并在下一交易日开盘执行 Carry、动量/缩量缩仓过滤和三档日线吊灯止损。

```bash
.venv/bin/python -m cta_carry \
  --source public-pg \
  --start 2012-01-01 \
  --end 2026-04-29 \
  --output-prefix output/carry_daily
```

离线源要求 `DATA_DIR/prices.csv` 或 `DATA_DIR/prices.parquet`，字段为 `trade_date, contract, open, high, low, close, volume, oi, turnover`，可选 `settle`。使用文件源时增加 `--source files --data-dir DATA_DIR`。

默认预热 730 日历日；若正式起始交易日之前未积累 252 个影子收益、至少 126 个实际持仓日和正的有限波动率，命令以非零状态退出，不会静默推迟回测起点。

输出为 `*_overview.png` 与 Excel，工作表包括 `metrics`、`daily_returns`、`positions`、`trades`、`signals`、`curve_selection`、`data_quality`、`run_config`。

本版是日线研究近似：原研报的 15 分钟止损改为日收盘触发，下一 5 分钟 VWAP 改为下一交易日开盘，ATR 默认 20 个合约交易日，成本固定为单边 13 bps，不换算张数、乘数和保证金，也不模拟涨跌停与容量。
