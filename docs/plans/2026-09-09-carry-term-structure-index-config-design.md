# Carry 期限结构指数口径设计

设计日期：2026-09-09
关联：`docs/superpowers/specs/2026-07-14-carry-daily-strategy-design.md`（原始设计）、
`docs/specs/2026-08-06-carry-trend-hysteresis-design.md`（过滤器滞后）
依据：2026-09-03 至 09-09 的中信 CICSF025 复刻研究（记忆 `citic-full-replica-levered`、
`citic-transplant-to-cta-carry`；脚本在会话 scratchpad，未入仓）

---

## 1. 为什么改（实测依据）

同段 2013-01-04 至 2020-12-31、同为 15% 波动率目标：

| | 净年化 / 净夏普 | 毛年化 / 毛夏普 | 年换手 | 回撤 |
|---|---|---|---|---|
| cta_carry 基线（真实引擎） | 14.3% / 0.90 | 22.8% / 1.43 | 178 | −21.9% |
| 中信口径复刻（研究引擎，4 bps） | 29.0% / 1.82 | 29.7% / 1.87 | 13 | −22.6% |

毛夏普就输了，所以分档、动量过滤、止损三层在因子最好的年份也是减分。
研究引擎里的消融阶梯独立给出同样方向（秩权重 + 波动率目标对近似 Carry 口径：
2010-15 夏普 2.07 对 1.02，2016-20 1.25 对 0.81）。结论是走简单口径，但简单口径
目前只在研究引擎里跑过（收盘价成交、无涨跌停约束、主力链只向后切换），要在
真实引擎里重跑一遍才知道执行折损。

## 2. 目标口径（研究里已定案的部分，不再讨论）

- 信号腿：**近主力 / 远主力**。近主力 = 主力之前持仓最大的合约，没有就用主力本身；
  远主力 = 主力之后持仓最大的合约（引擎现有的 `strictly_later` 次合约）。
  R = (近收盘 − 远收盘) ÷ 近收盘 ÷ 相隔月数 × 12。主力 / 远主力已实测更差（全期
  夏普 1.37 → 0.96），不用。
- 回望期 90 个交易日算术平均（现有 `--carry-window`）。
- 权重：全截面秩权重 w = (Rank − (1+N)/2) ÷ (N(1+N)/2)，和为零，不分档、无符号闸门。
- 无动量过滤（现有 `--no-trend-filter`）、无止损（新开关）。
- 品种池：过去 20 日沉淀资金均值 ≥ 20 亿（沉淀资金 = Σ 持仓量 × 收盘 × 乘数）；
  叠加用户剔除名单 A（有色、贵金属、欧线、花生、红枣、苹果、鸡蛋，共 19 个）。
- 杠杆：15% 波动率目标、252 日窗口、毛敞口上限 4（现有）。成本 4 bps（现有）。
- 执行：沿用引擎的 T+1 开盘成交、主力按日重选。

## 3. 引擎缺口与改法

每一项都是 `CarryConfig` 上的**新字段 + 保持现状的默认值 + 同名 CLI 开关**，
基线逐位不动（`tests/fixtures/carry_daily_stateful_baseline.pkl` 的 golden 测试与
两条 CLI 对等测试都要继续通过）。

| # | 缺口 | 新字段 | 默认 | 开启值 |
|---|---|---|---|---|
| 1 | 信号腿是主力 / 更晚月 | `near_leg` | `"main"` | `"near_dominant"` |
| 2 | 分档 + ATR 风险预算定权重 | `weighting` | `"risk_budget"` | `"rank_linear"` |
| 3 | 吊灯止损没有关闭开关 | `stop_loss_enabled` | `True` | `--no-stop-loss` |
| 4 | 流动性用 120 日成交额均值 | `liquidity_measure` | `"turnover"` | `"open_interest_value"` |
| 5 | 没有品种剔除参数 | CLI `--exclude-products` | 无 | 逗号分隔代码 |
| 6 | 持仓合约缺开盘价硬失败 | `missing_open_policy` | `"abort"` | `"defer"` |

### 3.1 `near_leg = "near_dominant"`（`curve.py`）

在 `build_curve` 里，主力与次合约的选取不变；另选「近腿」：`delivery_yyyymm` 早于
主力的候选里按同一 OI / volume / contract 排序取第一个，没有则用主力。
`carry_raw` 改为 `(near_close / secondary_close − 1) × 12 / month_gap(near, secondary)`。

分母说明：引擎现有公式分母是远月（`main/secondary − 1`），中信文档分母是近腿。
开启该选项时按中信公式（分母近腿）算，为的是和研究复刻逐点可对；默认路径不动。

曲线表新增 `near_contract`、`near_close` 两列；审计表 `role` 多一个 `"near"` 值、
`reason` 多一个 `"earlier_highest_oi"`。持仓与成交仍然只用主力合约。

### 3.2 `weighting = "rank_linear"`（`signals.py`、`decision.py`）

`build_signals` 新增 `rank_weight` 列（默认路径全为 NaN）。开启时，对当天
`input_ready` 的 N 个品种（N ≥ 5 不变）按 `carry_ma` 升序取名次，
`rank_weight = (rank − (1+N)/2) / (N(1+N)/2)`，`rank_direction = sign(rank_weight)`
（中位品种为 0），忽略 `selection_fraction` 与符号闸门。过滤器逻辑照旧作用于
`strength`（配合 `--no-trend-filter` 时 strength 恒 1）。

`plan_signal_targets` 在开启时用 `raw_weight = rank_weight × strength` 代替
ATR 风险预算公式；`tranches_remaining` 不参与。`input_ready` 的 ATR 要求保留
（止损关闭后 ATR 不再被使用，但保留门槛让两条路径的样本一致）。

### 3.3 `stop_loss_enabled = False`（`backtest.py::_close_plan`）

关闭时跳过 `apply_chandelier`，状态原样传给 `plan_signal_targets`，
`reason_hints` 不会出现 `stop_*`。

### 3.4 `liquidity_measure = "open_interest_value"`（`curve.py::aggregate_product_liquidity`）

日线路径没有乘数元数据。开启时按品种从日线反推：每个有效合约日
（volume > 0 且 turnover > 0）算 `turnover / (volume × close)`，取**该品种截至当日的
扩展中位数**作为乘数（不用全样本中位数，避免前视）；
沉淀资金 = Σ_合约 oi × close × 乘数；再按现有 `liquidity_window` 滚动均值、
`shift(1)`、阈值判定。配合 `--liquidity-window 20 --liquidity-threshold 2e9`。

### 3.5 `--exclude-products`（`__main__.py`、`pg_source.py`）

与 `--products` 同型的 CLI 参数，规范化后并入 SQL 的 `excluded_products`
（原来只含 `FINANCIAL_FUTURES`），`run_config` 记录。不是 `CarryConfig` 字段。

### 3.6 `missing_open_policy = "defer"`（`backtest.py`）

全截面持仓一定会碰到持仓合约当天没有开盘价的日子（全历史 `open IS NULL AND
close IS NOT NULL` 有 32 万行，剔除镍与国际铜后仍会有）。`"defer"` 的含义是
**推迟，不合成价格**：

- 持仓合约当天缺开盘价：该合约当日贡献记 0，其权重原样带到明天；
  `previous_open` 改为「最后一次有效开盘价」而不是昨天的价格表，所以下一个
  有开盘价的日子按两日累计收益一次性补记。
- 目标合约当天缺开盘价（无论是换仓、进场还是平仓）：该合约当日不交易，
  旧权重带过去，其余合约照常调整。
- 每次推迟写一行 `data_quality`：`object_type="execution"`、`check="open_price"`、
  `status="deferred"`、`action="carried"`，理由注明是持仓还是目标。
- 影子权重（`shadow_raw_*`）与正式权重同样处理。

默认 `"abort"` 保持现有硬失败，逐位不变。

### 3.7 明确不做

- 主力合约「只向后切换」规则：引擎按日重选主力，研究引擎用向后锁定链。
  记忆里两种规则 90.94% 一致、相关不变，先不做，用交叉验证量它。
- 上市满 3 个月：90 根曲线行的 `min_periods` 已经比它严。
- 中信「涨跌停品种排除」：研究复刻也没做。

## 4. 目标命令

```bash
.venv/bin/python -m cta_carry --source public-pg \
  --start 2012-01-04 --end 2026-09-01 \
  --near-leg near_dominant --weighting rank_linear --no-stop-loss --no-trend-filter \
  --carry-window 90 \
  --liquidity-measure open_interest_value --liquidity-window 20 --liquidity-threshold 2e9 \
  --exclude-products CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD \
  --missing-open-policy defer \
  --output-prefix output/carry_tsindex
```

`--end` 取各交易所最后一天的最小值（2026-09-09 时点是大商所的 09-01），每次现算。

## 5. 验收

1. 全套测试通过；golden 测试与两条 CLI 对等测试不改夹具即通过。
2. 每个新开关有单元测试，断言值手算：近腿选取与回退、CITIC 公式、秩权重 N=5 的
   五个字面值、止损关闭时状态不变、沉淀资金乘数反推、defer 的零收益 + 两日补记 +
   审计行、abort 路径不变。
3. 全历史在 WSL2 跑（本机到库走 DERP 中继，长跑必炸）。
4. 交叉验证：引擎日收益对研究复刻（同口径、同品种池）逐日相关与分段年化 / 夏普；
   分歧要能归到 3.7 列出的已知差异或 T+1 开盘成交，否则视为实现缺陷。
5. `data_quality` 里 `deferred` 行数报出来，作为执行可行性的量。
