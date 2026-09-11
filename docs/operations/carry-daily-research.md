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

`run_config` 记录全部参数、实际查询范围、实际绩效范围、`code_version`、`code_dirty`、
`code_diff_sha256`、`report_start_date`、`signal_ready_date`、`vol_ready_date` 和数据覆盖。
`code_dirty=false` 表示结果来自记录的干净提交。`code_dirty=true` 时，该提交不足以重建结果：
必须另行归档生成时的精确 patch 或工作树快照，并在恢复后用 `code_diff_sha256` 核对；摘要本身
不包含源码 diff。未归档源码状态的 dirty 产物，以及 provenance 捕获不可用或失败而记录为
`unknown` 的产物，都不能作为可重建证据。复核任何结论都先看这张表。

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

## 6. 期限结构指数口径（2026-09-09 新增）

把中信 CICSF025 期限结构指数的构造搬进本引擎的一组开关，设计与实测依据见
`docs/plans/2026-09-09-carry-term-structure-index-config-design.md`。
每个开关都是**独立可选**的，默认值全部复现基线（golden 夹具不用重生成）。

| 开关 | 默认 | 开启值 / 含义 |
|---|---|---|
| `--near-leg` | `main` | `near_dominant`：近腿取交割月**早于**主力、持仓最高的合约（没有则用主力），R = (近 − 远) ÷ 近 ÷ 相隔月数 × 12（中信公式，分母是近腿） |
| `--weighting` | `risk_budget` | `rank_linear`：全截面秩权重 w = (Rank − (1+N)/2) ÷ (N(1+N)/2)，和为零、无符号闸门；`--selection-fraction` 与 ATR 预算不再参与 |
| `--no-stop-loss` | 关 | 不跑吊灯止损，档位永不下降 |
| `--liquidity-measure` | `turnover` | `open_interest_value`：沉淀资金 = Σ 持仓量 × 收盘 × 乘数；乘数按品种日取「成交额 ÷（成交量 × 收盘）」的当日中位数，再做逐日扩展中位数（无前视）。列名仍叫 `product_turnover` |
| `--missing-open-policy` | `abort` | `defer`：持仓合约当天没有开盘价 ⇒ 当日贡献记 0、按最后一次有效开盘价在下一个有价日一次性补记；目标合约没有开盘价 ⇒ **该品种整体**保持昨日权重与昨日状态，其余品种照常。每次写一行 `data_quality`（`object_type=execution`、`status=deferred`） |
| `--exclude-products` | 无 | 逗号分隔代码，在流动性筛选之前剔除；`run_config` 记 `excluded_products` |

目标命令（与研究复刻同口径，剔除名单 A）：

```bash
.venv/bin/python -m cta_carry --source public-pg --start 2012-01-04 --end <各所最后一天的最小值> \
  --near-leg near_dominant --weighting rank_linear --no-stop-loss --no-trend-filter \
  --carry-window 90 --liquidity-measure open_interest_value \
  --liquidity-window 20 --liquidity-threshold 2e9 \
  --exclude-products CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD \
  --missing-open-policy defer --output-prefix output/carry_tsindex
```

与研究复刻仍有的已知差异：主力按日重选（研究用只向后切换的链）、T+1 开盘成交
（研究按收盘）、乘数用扩展中位数（研究用全样本中位数）。交叉验证时分歧只能归到这三条。

### 每日出单 `--emit-next-targets` / `--capital`

日线到库时间：交易日 T 的行凌晨 03:00–03:10 才齐，所以每日流程是 **T+1 早上（08:00–08:30）跑，
出当天 09:00 日盘开盘要执行的目标持仓**。用分钟表实测「日盘 09:00 开盘成交」与回测的
「交易日开盘（含夜盘）成交」绩效无差别（2012–2026-08：18.90%/1.19 对 18.79%/1.18），可以放心。

```bash
scripts/carry_tsindex_daily.sh <资金规模CNY> [as_of=今天]
```

**截断日每次现算**：脚本先跑 `python -m cta_carry.coverage --as-of <as_of>`，取五家商品交易所
（CZC / DCE / GFE / INE / SHF，不含中金所）各自 `max(trade_date)` 的**最小值**作 `--end`，
不用全表 `max(trade_date)`、也不写死日期。原因：大商所没有可脚本化的日度接口、靠人工投递
（实测 09-08 的行 09-08 16:51 才到，其余四所 09-09 的行 09-10 03:06 到），大多数早晨它落后一天；
若照全表最大日跑，18 个大商所持仓品种当天没有 K 线，引擎会把它们全标成 `signal_exit` 出平仓单
（2026-09-10 早实跑撞到）。滞后的交易所会打到 stderr 和 `daily.log`（`lagging DCE: max trade_date …`），
此时出的是**最后一个完整日**的目标（与前一天相同），等大商所投递到位后**再跑一遍**。
引擎侧还有一道闸：出单模式下持仓合约在 signal_date 没有 K 线 ⇒ `NextTargetDataError`、
退出码 2、不写任何产物（`missing_open_policy=defer` 也不放行）。

它以 `end − 900 天` 为 `--start` 跑一段回测（prewarm 730 天保证 90 日因子与 252 日波动窗口就绪；
起点原为 60 天，基差动量腿上线后必须放宽，见下），
再多算最后一天收盘的计划，输出：
- 工作簿多一张 `next_targets` 表，另写 `<prefix>_next_targets.csv`，终端打印排序后的表；
- 列：`signal_date`（= 截断日，**核对它是不是昨天**；不是就是有交易所滞后，见上）、`product`、`contract`（持仓合约 = 主力）、
  `order_code`（交易所自己的合约代码：郑商所年份一位 `PL611`、其余四所小写 `m2701`，下单用这列）、
  `direction`、`close`、`raw_weight`（秩权重）、`vol_scale`、`target_weight`、`current_weight`（回测里此刻的持仓）、
  `weight_change`、`reason`；给了 `--capital` 再加 `multiplier`（近 60 个成交日
  成交额 ÷（成交量 × 收盘）的中位数）、`notional`、`lots`（四舍五入到整手）。
- 秩权重每天全量重算、没有止损与过滤器，所以短回测最后一天的目标与全历史回测一致；
  `current_weight` 只有回测语义，真实持仓以账户为准，手数按 `lots` 对账。
- `target_weight` 是占净值的比例（`notional = target_weight × capital`），多账户各按自己的资金乘；
  多空两侧各 0.74 左右、合计精确为 0，只有整手取整后的名义金额合计才不为零。
- `vol_scale` 为空表示波动窗口未就绪（数据不足 252 个影子日），此时表为空、终端提示。

#### 基差动量腿：2026-09-11 起以 0.20 开启

脚本带 `--basis-momentum-weight 0.20`（用户裁决 2026-09-11）。`--basis-momentum-window`、
`--basis-momentum-min-coverage`、`--basis-momentum-rebalance` 保持默认 500 / 0.9 / monthly ——
这正是 `docs/plans/2026-09-10-basis-momentum-design.md` §11 敏感性表所用的配置，别单独动其中一个。

**预期量级**：全期 Δ夏普 +0.03~0.05、成本 +0.02pp/年、Calmar 微升、回撤几乎不动。不是
立项时报的 +0.17（那个数是两次噪声抽样的比值，§8 已更正）。JK 配对检验 p≈0.35，
单段点估计都不显著，支持它的是**两段符号一致**与**截面 IC**（因子 t=5.12、控制 carry 后
增量 t≈2.1）。

**每次跑完必看的判据**：工作簿 `signals` 表应出现 `basis_momentum` / `bmom_ready` /
`bmom_weight` / `blend_weight` 四列（关闭时这四列根本不存在）。

⚠️ 判据**不是**「最后一天 `bmom_ready` 为 True」—— 2026-09-11 上线当天证明那条在缺陷下
是绿的。正确的判据是 **`bmom_ready` 在 `vol_scale` 所用的整个 252 个影子日窗口内一直为
True**。`vol_scale = 0.15 ÷ 最近 252 个影子日的年化波动`，窗口里混进纯 carry 的日子会把
波动读低、把账本多加杠杆：60 天起点那次窗口里只有 57 个混合日，波动读成 4.13%（收敛值
4.77%），**多加 15.6% 杠杆、49 行错 47 行、最大一行差 42 手，全程不报错**。起点因此固定
为 900 天（`6513ab3`）；400 天已能逐手复现，但晚上市或缺口会吃掉那点余量。

`bmom_ready` 为 False 的少数品种是正常的：2026-09-10 有 5/49 为 False，都是上市不足
500 个交易日的新品种。整列全 False 才是静默退化（出的单等于 λ=0）。

**开启当天的一次性调仓**：账上持的是 λ=0 那条路径的仓位，而开启后引擎 `current_weight`
报的是"若一直按 λ=0.20 跑到今天本该持有的仓"，是个从未成交过的反事实账本。所以切换当天
真正要下的手数 = **新单的 `target_weight` − 旧单（λ=0）的 `current_weight`**，不是新单自己的
`weight_change`。做法：先用 `git stash` 或 `--basis-momentum-weight 0` 跑一份 λ=0 的单留作
账面基准，再跑正式单，两份按 `product` 对齐相减。此后每天照常，不必再做。

## 7. 已知边界

- 日线近似：原研报的 15 分钟吊灯止损改为**日收盘触发**，下一 5 分钟 VWAP 改为
  **下一交易日开盘**，ATR 默认 20 个合约交易日。
- 不换算张数、乘数和保证金，不模拟涨跌停与容量，不计现金利息。
- 研报的收益率/夏普/回撤**不是**验收指标（设计文档 §2.2）；验收看规则正确、
  结果可复现、账户可对账、数据问题可审计。
- 上游 `futures_daily` 停摆期间，2026-04-29 之后无数据。
