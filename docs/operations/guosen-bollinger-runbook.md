# 国信 Bollinger 通道商品期货策略 —— 运行手册

复刻对象：国信证券《CTA 系列专题之二：基于 Bollinger 通道的商品期货交易策略》
（研报日期 2021-10-13）。设计见
[`../superpowers/specs/2026-08-27-guosen-bollinger-dow-design.md`](../superpowers/specs/2026-08-27-guosen-bollinger-dow-design.md)，
计划见 [`../superpowers/plans/2026-08-27-guosen-bollinger-strategy.md`](../superpowers/plans/2026-08-27-guosen-bollinger-strategy.md)。

## 依赖：先有面板 bundle

策略本身**不连数据库**。它只读
[`commodity-panel-bundle.md`](commodity-panel-bundle.md) 产出的版本化 bundle
（`bars` / `universes` / `dominants` / `roll_fills` 四表 + 逐表 SHA-256 的
`manifest.json`）。没有 bundle 就跑不了，这是刻意的：亿行分钟表只过一次网，
后续所有参数与敏感性运行都复用同一份内容摘要可查的输入。

## 固定参数：哪些是开关，哪些不是

| 量 | 值 | 是否命令行开关 |
|---|---|---|
| 通道长度 / beta | 300 / 1.5 | ❌ 常量 |
| 止盈系数 | 8 × 入场 bar 标准差 | ❌ 常量 |
| OI 均线 | 150 / 300 | ❌ 常量 |
| ATR | 20 根有成交 15 分钟 bar | ❌ 常量 |
| 组合目标年化波动 | 10% | ❌ 常量 |
| 样本内截止 | 2021-09-30 | ❌ 常量 |
| 标准差自由度 | 0（忠实） | ✅ `--ddof {0,1}` |
| 单边成本 | 1.3 bp | ✅ `--cost-bps` |

**只有研报没披露的量才可调，且必须成对出敏感性产物。** 把 300 或 1.5 做成开关，
等于在「复刻指标 vs 研报指标」这个差距上留一个可以事后拧的旋钮——那不是复刻，
是拟合。

## 运行

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_bollinger \
  --panel-dir output/commodity-panel-v1 \
  --start 2012-01-04 \
  --end 2026-01-30 \
  --output-prefix output/guosen_bollinger \
  --require-paper-faithful \
  --run-ddof-sensitivity
```

linked worktree 没有自己的 `.venv`，用主 checkout 的绝对解释器
`/home/elfbob/claude-code/futures_strategies/.venv/bin/python`，并在仓根加
`PYTHONPATH=.`。

全历史属于长跑，按仓库惯例投 WSL2（`ssh -p 2223 ghls@100.120.152.1`）并用
`setsid` 脱离进程组，否则 harness 结束时会连带杀掉后台任务。

## 硬失败边界

以下情况非零退出，不降级、不填补：

- `--end` 超出交易时段规则资产上界 **2026-01-30**，或 `--start` 早于起点
  **2011-01-04**（`config/carry_minute_sessions.csv`）；
- bundle 覆盖不含请求区间；
- `--require-paper-faithful` 下区间内存在无法定价的应成交窗口；
- 应成交 bar 缺 `fill_time` / `fill_price` / 乘数 / 复权因子；
- 影子与 bundle 的逐 bar 结构不一致（说明影子不是这份 bundle 产出的）；
- 月度已实现波动率应存在却为零、负或非有限。

## 敏感性产物怎么读

`--run-ddof-sensitivity` 另出一份 `<prefix>_ddof1.*`，`fidelity` 表里 B1 的
`status` 标成 `sensitivity_only`，审计 JSON 里 `sensitivity_only: true`。

它**只用来解释忠实口径与研报的差距**，不参与任何口径选择。看到 ddof=1 的
夏普更高就改默认，就是用研报样本内的结果反推参数。

## 产物

每次运行三件：

- `<prefix>.xlsx` —— `metrics` / `daily_returns` / `positions` / `trades` /
  `signals` / `universe` / `selection` / `dominant_rolls` / `data_quality` /
  `fidelity` / `run_config` 十一张表；
- `<prefix>.png` —— 净值、回撤、杠杆三格，样本内外分隔线画在图上；
- `<prefix>.audit.json` —— bundle manifest、commit、运行参数、逐表行数与
  SHA-256、保真度变体、研报指标与复刻指标的差距。

`metrics` 里研报口径（年化 17.52%、Sharpe 1.72、最大回撤 8.27%、Calmar 2.12、
年化波动 10.16%）**只写在 `in_sample` 行**——研报没有测过样本外，样本外行不许
借它的数字。

## 市场断代

面板的 `continuity_segment` 标出品种被摘牌重挂的位置（全历史仅燃料油 2018 一处）。
断代两侧价格不可比，所以策略在段边界**全部重来**：指标重新预热、状态机清空、段末
最后一根强制平仓，且不向换月要成交单。保真度规则 F10，`data_quality` 里可查。

## 已知限制

- 影子层的成本口径由 `--cost-bps` 同时驱动影子与组合两本账；
- `positions` 逐日逐品种成行，全历史约十万量级，Excel 可容但打开偏慢；
- 全历史验收依赖 `output/commodity-panel-v1`，该 bundle 需先行构建。
