# Carry 日线研究版 runbook

`cta_carry/` — 国信《基于 Carry 的商品期货交易策略》日线研究版。
设计文档：`docs/superpowers/specs/2026-07-14-carry-daily-strategy-design.md`
实施计划：`docs/superpowers/plans/2026-07-14-carry-daily-strategy.md`

---

## 1. 口径

分合约 `public.futures_daily`（**不走连续合约**，因此不受 `continuous_contract_ohlc`
的复权残留影响）。按前 120 个品种交易日的日均成交额（门槛 50 亿元）建立动态交易池，
逐日选主力（持仓量最高）与**严格晚月**次主力，在**下一交易日开盘**执行。

```
carry_raw = main_close / secondary_close - 1，年化后按品种做 10 日均值
  carry_ma > 0  近月高于远月 = backwardation（现货升水）→ 做多
  carry_ma < 0  近月低于远月 = contango（期货升水）    → 做空
```

⚠️ **方向易错点。** 2026-08-05 前的实现把这个方向写反了（做多 contango），
全历史毛夏普因此是 -0.629。修正依据与实测见设计文档 §6.4「2026-08-05 方向修正」，
回归测试 `tests/test_carry_signals.py::test_backwardation_is_long_and_contango_is_short`
锁定当前方向。改动这一段前先读那两处。

## 2. 运行

```bash
cd /home/elfbob/claude-code/futures_strategies

# 全历史
.venv/bin/python -m cta_carry \
  --source public-pg \
  --start 2013-01-04 \
  --end 2026-04-29 \
  --output-prefix output/carry_daily

# 离线文件源
.venv/bin/python -m cta_carry --source files --data-dir DATA_DIR ...
```

`DATA_DIR` 需含 `prices.csv` 或 `prices.parquet`，字段
`trade_date, contract, open, high, low, close, volume, oi, turnover`（`settle` 可选）。

**`--end` 不要晚于 2026-04-29。** `futures_daily` 的 EOD 日更链自该日起停摆
（2026-08-05 实测滞后 98 天）。填更晚的日期不会报错，只会安静地少一截。

**预热是硬失败。** 默认预热 730 自然日；正式起始日之前若未积累 252 个影子收益、
至少 126 个实际持仓日和正的有限波动率，命令以非零状态退出并抛
`WarmupInsufficientError`，**不会**静默推迟回测起点。

单次全历史运行约 15–20 分钟、峰值内存约 2.7 GB。这台机器只有 15 GB 且常有其他
会话在跑，**一次只跑一个**，起之前先 `free -g`。

## 3. 输出

`*_overview.png` 与 Excel，八张工作表：
`metrics`、`daily_returns`、`positions`、`trades`、`signals`、`curve_selection`、
`data_quality`、`run_config`。

`run_config` 记录全部参数、实际查询范围、实际绩效范围、`code_version`、
`report_start_date`、`signal_ready_date`、`vol_ready_date` 和数据覆盖 —— 复核任何
结论都先看这张表。

`daily_returns` 同时给 `gross_return` 与 `net_return`，**永远分开看**：这条策略的
成本拖累占毛收益的比重很大，只看净值会把信号问题和成本问题混为一谈。

## 4. 成本假设

`cost_bps` 默认 **4.0**（单边，含手续费与滑点）。这是**保守建模假设，不是精确值**。

实测依据（2026-08-05，见设计文档 §3）：

| | 单边 bps |
|---|---|
| 手续费 · 商品期货实测 | 0.2 ~ 1.1（个别 3.5） |
| 滑点 · 持仓加权一跳 | 3.13 |
| 合计 · 限价单半跳 | ≈ 2.1 |
| 合计 · 市价单一跳 | ≈ 3.6 |
| **默认取值** | **4.0** |
| 净收益打平点 | 6.94 |

滑点**无法实测**（成交明细无对应时点盘口中价），只能锚定最小变动价位。一跳是**下限**：
本策略在次日开盘执行，那是全天价差最宽的时点，真实滑点会超过一跳，且规模上去后的
冲击成本完全未计入。

**任何绩效结论都要连同敏感性一起给。** 改成本重算：

```bash
.venv/bin/python -m cta_carry ... --cost-bps 3
```

## 5. 趋势滞后参数（2026-08-06 新增）

2026-08-06 归因发现：绩效的主要拖累不是止损（只触发 661 次、占交易 1.79%，98.34% 持仓日
满档），而是动量过滤器的**状态抖动** —— 83.6% 的平仓发生时 Carry 排名仍指向同方向，
其中 43.6% 隔一个交易日就原方向重新进场；这些往返占 **53.3% 的总换手 / 年化 4.16% 成本**。
完整证据见 `docs/specs/2026-08-06-carry-trend-hysteresis-design.md`。

为此在 signals 层引入 per-product 趋势状态（只由 close / price_ma / atr 驱动，**不读仓位
状态**，止损与锁定不会扰动它），两个参数控制它何时翻转：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--trend-band-atr` | `0.0` | 缓冲带半宽（单位 ATR）。close 需越过 `MA ± k×ATR` 才翻转，带内维持原状态 |
| `--trend-confirm-days` | `1` | 翻转所需的连续同侧收盘天数 |

**默认值下行为与改动前逐点相同**（`k=0` 时没有带可停留，`close == price_ma` 仍解析为
中性 → `strength=0`，涨跌停锁死的合约会真实产生这种情况）。全历史默认参数复跑必须与
基准逐日一致，这是硬验收项。

```bash
# ATR 缓冲带
.venv/bin/python -m cta_carry ... --trend-band-atr 0.5
# 连续确认
.venv/bin/python -m cta_carry ... --trend-confirm-days 3
```

`signals` 工作表新增 `trend_state` 列（-1 / 0 / +1）可供审计。

### 次主力口径 `--secondary-selection`

| 值 | 含义 |
|---|---|
| `strictly_later`（默认） | 交割月**严格晚于**主力的合约中持仓量最高者。Carry 符号严格锚定"近月 vs 远月"的教科书定义 |
| `second_by_oi` | 持仓量第二的合约，**不筛月份**。复现研报口径（研报提到次主力却从未定义选取规则） |

实测 `second_by_oi` 下次主力有 **29.0%** 确实是更近月，但研报公式里的 `1/(M_2 − M_1)`
带符号，分母转负会把符号翻回来，**方向结论不变**（净 6.91% / 夏普 0.433，略优于默认口径）。

⚠️ 该口径会撞上一个上游数据缺陷：2015–2017 期间 10 个郑商所品种的同一合约被 3 位与 4 位
两种代码各存一份（如 `TA701.CZC` 与 `TA1701.CZC`，逐字段相同，共 2,565 对），两者解析到同一
交割月。代码已跳过同月合约，否则 `month_gap = 0` 直接抛错。默认口径不受影响。

### 资金分配 `--equal-weight-capital`（⛔ 已验证更差，不要开）

默认**关闭**：每个品种按自己的 ATR 风险预算独立定权重，总仓位随入选品种数增长（到 4 倍封顶）。
开启后除以当日入选品种数 N，复现研报的「等权分配资金」。

实测开启后**三项同时恶化**：净年化 5.89% → **2.92%**、净夏普 0.371 → **0.195**、
最大回撤 −44.70% → **−51.05%**，且年化成本反升 7.79% → 8.99%。

两个原因：
1. 入选品种数（breadth）本身携带 alpha —— 宽日毛夏普 **3.78**、窄日仅 0.41，
   归一恰好在机会最多时减仓；
2. 归一使权重依赖 N，任何品种进出都迫使全部在手仓位重算 ——
   `rebalance` 笔数不变（24,514）但单笔幅度涨到 **2.4 倍**，属纯机制性换手。

保留该开关只是为了**记录这个已验证的负面结果**，避免有人看到研报那句「等权分配资金」再试一遍。

## 6. 已知边界

- 日线近似：原研报的 15 分钟吊灯止损改为**日收盘触发**，下一 5 分钟 VWAP 改为
  **下一交易日开盘**，ATR 默认 20 个合约交易日。
- 不换算张数、乘数和保证金，不模拟涨跌停与容量，不计现金利息。
- 研报的收益率/夏普/回撤**不是**验收指标（设计文档 §2.2）；验收看规则正确、
  结果可复现、账户可对账、数据问题可审计。
- 上游 `futures_daily` 停摆期间，2026-04-29 之后无数据。
