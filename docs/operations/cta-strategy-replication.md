# CTA 因子组合策略复刻

> 2026-07-11 随 `cta/` → `futures_strategies/cta_gtja/` 迁移自 stock_selector；命令已同步更新。

本模块复刻参考 PDF 中的两条 CTA 因子组合策略：

- `medium_equal_weight`：CTA 因子组合（中波等权），6 个因子等权，目标波动率 8%，最大杠杆 2.5 倍。
- `high_composite`：CTA 因子组合 2 号（高波复合），60% 等权组合 + 40% 因子轮动，单因子权重上限 50%，目标波动率 12%，最大杠杆 3.5 倍。

## 数据口径

CTA 模块默认读取 PostgreSQL `public` schema：`continuous_contract_ohlc`、`spot_prices`、`inventory`。默认排除股指与国债期货（`IF`、`IC`、`IH`、`IM`、`T`、`TF`、`TL`、`TS`），需要纳入时加 `--include-financial`。也保留目录型离线数据源，目录中必须包含：

`prices.csv`

| column | required | meaning |
|---|---:|---|
| `trade_date` | yes | 交易日 |
| `symbol` | yes | 商品品种，例如 `CU`、`RB` |
| `open` | yes | 主力/次主力连续合约开盘价 |
| `close` | yes | 主力/次主力连续合约收盘价 |
| `volume` | no | 成交量，用于量价相关性因子 |
| `amount` | no | 成交额，可用于后续流动性筛选 |
| `contract` | no | 当日映射到的具体合约 |

`fundamentals.csv` 可选：

| column | meaning |
|---|---|
| `trade_date` | 交易日 |
| `symbol` | 商品品种 |
| `spot` | 现货价格；没有 `basis_rate` 时用于计算基差 |
| `basis_rate` | `(spot - futures_close) / futures_close` |
| `inventory` | 库存 |
| `warehouse_receipt` | 仓单，当前预留 |
| `profit` | 产业利润 |

public schema 映射：`continuous_contract_ohlc.base_symbol -> symbol`，`open_fa/close_fa` 优先作为连续复权价格，`volume` 用于量价相关性，`spot_prices.spot_price` 聚合为现货，`inventory.inventory_value` 聚合为库存。当前数据库里 `spot_prices` 与 `inventory` 只覆盖 `M`，未发现利润字段；利润因子在没有数据的品种上会自然降级为 0 权重贡献。

离线文件模式下，上游数据层应先完成主力/次主力合约映射、复权或换月处理。策略层只消费品种级连续序列。

## 因子

| factor | PDF 对应逻辑 | construction |
|---|---|---|
| `basis` | 做多贴水品种，做空升水品种 | time series |
| `inventory` | 做多库存下降多的，做空库存上升多的 | cross section |
| `profit` | 做多利润历史低位，做空利润历史高位 | time series |
| `long_rule_momentum` | 短均线高于长均线做多，反之做空 | time series |
| `long_cross_momentum` | 长周期截面动量 | cross section |
| `price_volume_corr` | 做多量价相关性低的品种，做空高的品种 | cross section |

## 运行

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --strategy both \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --symbols M,RB,CU,AL
```

输出：

- `output/cta_medium_equal_weight_medium_equal_weight.xlsx`
- `output/cta_medium_equal_weight_medium_equal_weight_equity.png`
- `output/cta_high_composite_high_composite.xlsx`
- `output/cta_high_composite_high_composite_equity.png`

Excel 包含 `metrics`、`period_returns`、`weights`、`factor_allocations`、`factor_returns`。

## 价格/量价 guarded smoke

在上游连续合约复权根因修复前，优先跑价格/量价 guarded smoke。该路径只启用
`long_rule_momentum`、`long_cross_momentum`、`price_volume_corr` 三个因子，并按
`cta_gtja.data_quality` 的 per-symbol 判定选择 `fa` 或 `ba` 价格线；两条复权线都坏的品种默认剔除。

上游 continuous 修复后，`ba_factor` 与 `fa_factor` 表示乘法复权因子，不再表示绝对价差偏移。
重算前必须确认 pi 上 `continuous_config.yaml` 指向的数据库是 CTA 要读取的目标库。全量重算会先按
`base_symbol` 与 `rule_type` 清理旧连续合约、主力与换月记录，避免有效生成区间之外的历史脏行残留。

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --strategy both \
  --factor-set price_volume \
  --adjustment-policy recommended \
  --start 2019-01-01 \
  --end 2025-09-30
```

若显式加 `--allow-raw-fallback`，两条复权线都坏的品种会使用 raw 价格。该结果只能作为研究排查，
不能视作连续复权回测。

输出 Excel 包含 `data_quality` sheet，用于审计每个品种的 `selected_adj`、`status`、
`raw_fallback` 和剔除原因。

## 连续合约数据质量扫描

普通扫描只读数据库、完整报告问题并返回 0：

```bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua
```

调度器或 CI 使用 `--strict`；默认允许表级最新日期落后 10 个自然日，长假或特殊场景可显式覆盖：

```bash
.venv/bin/python -m cta_gtja.data_quality --rule-type standard --strict
.venv/bin/python -m cta_gtja.data_quality --rule-type nanhua --strict --max-lag-days 12
```

`--strict` 仅阻断全 OHLC 非正/无限值、不可能的 `daily_return`/`return_index`、调整线损坏、
空结果和表级过期。NULL/NaN OHLC、单品种落后以及推断缺失交易日仅诊断，不单独阻断，
因为当前没有权威品种生命周期和交易日历。

`raw`/`ba`/`fa` 都派生自同一根源 bar；任一 lineage 出现非正或无限 OHLC 时，该交易日整根 bar
均视为可疑，即使策略选用的 `fa`/`ba` 价格仍为正。修复仍应在上游分合约数据或连续合约生成器完成。
