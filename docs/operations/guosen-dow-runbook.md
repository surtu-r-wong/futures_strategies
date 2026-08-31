# 国信道氏理论商品期货策略 —— 运行手册

复刻对象：国信证券《CTA 系列专题之四：基于道氏理论的商品期货交易策略》
（研报日期 2022-08-09）。设计见
[`../superpowers/specs/2026-08-27-guosen-bollinger-dow-design.md`](../superpowers/specs/2026-08-27-guosen-bollinger-dow-design.md)，
计划见 [`../superpowers/plans/2026-08-27-guosen-dow-strategy.md`](../superpowers/plans/2026-08-27-guosen-dow-strategy.md)。

## 依赖：先有面板 bundle

策略本身**不连数据库**，只读
[`commodity-panel-bundle.md`](commodity-panel-bundle.md) 产出的版本化 bundle。
与 Bollinger 共用同一份输入，两条线的差异因此完全落在策略语义上，而不是取数上。

## 信号是怎么定的

```text
MACD_t      = EMA(Close,12) - EMA(Close,26)      # adjust=False
Diff_t      = MACD_t - EMA(MACD,9)
CumMACD_t   = Diff_t              若 Diff_t · Diff_(t-1) < 0
            = CumMACD_(t-1)+Diff_t 否则
初步趋势     = up   若 CumMACD >= ATR20
             down 若 CumMACD <= -ATR20
             否则沿用
```

**累计距离只在严格变号时重置**：Diff 恰为 0 的一根不算变号，累计要继续；在零点
截断会把每一段"贴着零轴走但没穿过去"的累积全部砍掉。

**多头入场同时要三条**：初步趋势向上、`tempmin > lastmin1`（本段回撤没跌破前一
下降段低点）、`lastmin1 > lastmin2`（低点在抬高），再加收盘突破。

**突破比的是本 bar 纳入极值之前的临时极值**。先更新极值再比，`Close >= High`
几乎只在收盘恰等于本 bar 最高价时成立 —— 那是在描述一根 K 线，不是突破。

**入场后锁存**，持有到初步趋势切换或拐点条件失效；拐点失效只平仓，不反手。逐 bar
重验三道闸是敏感性变体（`--signal-mode literal`），不是可选默认。

## 固定参数：哪些是开关，哪些不是

| 量 | 值 | 是否命令行开关 |
|---|---|---|
| EMA 周期 | 12 / 26 / 9（adjust=False） | ❌ 常量 |
| ATR | 20 根有成交 15 分钟 bar | ❌ 常量 |
| 极值历史深度 | 2 段 | ❌ 常量 |
| 组合目标年化波动 | 15% | ❌ 常量 |
| 品种筛选 | 5 笔 + 累计收益 ≥ 0 | ❌ 常量 |
| 样本内截止 | 2022-07-29 | ❌ 常量 |
| 入场读法 | latched（忠实） | ✅ `--signal-mode {latched,literal}` |
| 单边成本 | 1.3 bp | ✅ `--cost-bps` |

## 运行

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_dow \
  --panel-dir output/commodity-panel-v1 \
  --start 2012-01-04 \
  --end 2026-01-30 \
  --output-prefix output/guosen_dow \
  --require-paper-faithful \
  --run-literal-sensitivity
```

linked worktree 用主 checkout 的绝对解释器并在仓根加 `PYTHONPATH=.`。全历史属于
长跑，按仓库惯例投 WSL2 并用 `setsid` 脱离进程组。

## 硬失败边界

- 区间超出交易时段规则资产的 **2011-01-04 .. 2026-01-30**；
- bundle 覆盖不含请求区间；
- 策略**确实要换仓**、而那一根的成交窗口不可定价（逐 bar 生效，与开关无关）；
  `--require-paper-faithful` 本身**不再**因为区间内存在不可定价窗口而拒跑 ——
  安静时段没人成交的窗口在全历史必然存在（探针三个月十四品种 20 根、0.16%），
  数量记进 `run_config.unpriceable_fill_windows` 与审计 JSON；
- 应成交 bar 缺 `fill_time` / `fill_price` / 乘数 / 复权因子；
- 影子与 bundle 的逐 bar 结构不一致；
- 月度已实现波动率为负或非有限。

**不**硬失败、但会计进 `data_quality` 的两种降级：ATR 未满窗或为零的 bar 不判趋势
（D8）；已实现波动窗口全为零视为预热未完成（F9）。这两条都是"还没开始交易"，
不是数据坏了。

## 资金分配与 resize

道氏的分母是**当前实际持仓的品种数**，不是当月入选品种数。任一品种进出都会改变
其余品种的目标 —— 但它们不能按别人的成交价调仓（那是编造成交），而是各自在自己
下一个可成交窗口调过来，`trades` 表里记为 `allocation_resize`。

## 市场断代

面板的 `continuity_segment` 标出品种被摘牌重挂的位置（全历史仅燃料油 2018 一处）。
断代两侧价格不可比，所以策略在段边界**全部重来**：指标重新预热、状态机清空、段末
最后一根强制平仓，且不向换月要成交单。保真度规则 F10，`data_quality` 里可查。

## 产物

三件套与 Bollinger 同构：`<prefix>.xlsx`（十一张表）、`<prefix>.png`（净值 /
回撤 / 杠杆 + 样本分隔线）、`<prefix>.audit.json`（manifest、commit、运行参数、
逐表行数与 SHA-256、保真度变体）。

`metrics` 里研报口径（年化 21.74%、Sharpe 1.42、最大回撤 9.90%、Calmar 2.20、
年化波动 15.34%）**只写在 `in_sample` 行**。`--run-literal-sensitivity` 另出
`<prefix>_literal.*`，`fidelity` 表 D6 标 `sensitivity_only` / `literal_every_bar`。
它只用来解释忠实口径与研报的差距，看到它指标更好就改默认，就是用研报样本内的结果
反推口径。
