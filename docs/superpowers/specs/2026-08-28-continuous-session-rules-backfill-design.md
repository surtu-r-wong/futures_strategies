# 连续策略时段资产补采设计

## 背景与目标

`scripts/continuous/build_panel.py` 在全历史构建时崩于
`2011-05-17 SHFE AL session_rule_cardinality: expected exactly one matching rule; found 0`。
这是同一问题的第二次露头：首轮崩于 `2010-12-31 DCE A`，`b95a74f` 按「排除史前上下文」补掉了
最左边一例，未触及成因。

成因是两个宇宙不同。`config/carry_minute_sessions.csv` 由
`scripts/carry/capture_minute_sessions.py` 产出，其审计宇宙 = **Carry 流动性池**
（`CarryConfig` + `cta_carry.curve.aggregate_product_liquidity`，¥5e9 门槛 / 120 交易日滚动窗口）
**+ 2 个后继交易日**。国信连续信号的宇宙是研报 §5.1「近半年日均成交额 ≥50 亿」**按月重算**
（`cta_continuous.universe`，同门槛、不同窗口与聚合口径）。计划
`docs/superpowers/plans/2026-08-27-guosen-continuous-signal.md` line 173 写的
「本策略的宇宙基本被它覆盖」是未经验证的假设，且为假。

2026-08-28 全历史实测（`--start 2011-01 --end 2026-01`）：面板需要 **126,687** 个品种日，
其中 **1,713 个（1.35%）`resolve_session_rule` 返回 0 条**，涉及 39/63 品种；歧义（>1 条）为 **0**。

| 成因 | 天数 | 形态 |
|---|---:|---|
| 左边缘：连续宇宙比 Carry 池更早接纳该品种 | 1,401 (82%) | 22 个品种各一段 3-5 个月连续窗口 |
| 内部空洞：品种一度退出 Carry 池 | 267 (16%) | 177 可由两侧推导，69 日历歧义，21 跨制度变更 |
| 右边缘：退出 Carry 池 / 资产止于 2026-01-30 | 45 | CS 18、ZC 15、J 6、EC 5、BC 1 |

**分钟数据完整存在**，缺的只是派生资产。抽查 `AG1209@2012-06-01`、`JM1309@2013-05-02`、
`SC1809@2018-05-02`、`PG2011@2020-05-06`、`CS2407@2024-05-08`、`AL1107@2011-05-17`：
K 线一根不少，与已覆盖对照日 `AL1107@2011-05-16` 形态一致。这不是
[[carry-minute-five-missing-product-days]] 那类缺数据。

目标：为连续策略产出一份覆盖其完整宇宙的时段资产，**不改变任何其他策略的行为**，
并消除「跑到第 N 个月才发现覆盖不全」这一失效模式本身。

## 裁决

**D1 —— 补采，不裁剪宇宙。** 让采集机器把缺的时段声明补出来，而不是把没有规则的品种日
排除出宇宙。缺口高度集中在每个品种「刚变得流动」的前 3-5 个月，恰是突破类策略最可能有
行情的时段，裁剪的偏差方向无法界定。

**D2 —— 新开独立资产，Carry 资产逐字节不动。** 产出 `config/continuous_minute_sessions.csv`。
`validate_capture_request` 已有 `repository_capture_start` 闸：写 `carry_minute_sessions.csv`
必须 `start == 2011-01-04`（全历史整份重写）。补采写新路径是该闸明文允许的路径。
`config/index_minute_sessions.csv`（股指线）本就不相干。

**D3 —— 按连续宇宙整体重采，不只补缺口。** 新资产自成完整一份、单一出处，
`build_panel.py` 只读它，不做并集。重叠部分（Carry 已覆盖的品种日）构成对现有资产的
独立交叉验证。代价是全历史跑一轮而非 1.4%。

**D4 —— 采集键集由构造保证与面板一致。** 驱动脚本调用
`universe_for_month` + `choose_dominant_commodity` + `context_choices_for_month`
**同一批函数**算出品种日键，而非另写一份等价逻辑。「采集覆盖的」与「面板要求的」
因此是同一个集合。

**D5 —— 先普查，后权威采集。** 增加只读、不接发布路径的普查模式，一次跑出**完整的**
「算不上时段的品种日」清单供逐条裁决，而不是撞上第一个就停。理由与本设计的起因同源。

**D6 —— 缓存边界观测供复用。** 普查把 `capture_session_boundaries` 的观测落盘，
权威采集读缓存而非重查分钟表，使第二轮起从数小时降到数分钟。

**D7 —— 样本窗口保持 2026-01-30 不变。** 重采后计划 §4.1 的原始理由（Carry 资产在该日截止）
不再成立，但复刻窗口不随之变动；延长是独立决定。

## 数据模型与流向

```
futures_daily ──load_public_carry_data──> prices(排除 FINANCIAL_FUTURES) + data_quality
                                            │
      cta_continuous.universe / continuous ──┤──> in_pool_source_keys  (交易所, 品种, 日期)
                                            │     history_status_by_key
                                            ▼
                              build_audit(宇宙无关核心)
                                            │
                     capture_session_boundaries ──> boundaries  ──[D6 缓存]──> parquet
                                            │
                    ┌───────────────────────┴───────────────────────┐
              [普查 D5]                                      [权威采集]
        逐行 classify_session_boundary                classify_authorized_boundaries
        捕获 SessionCaptureError 记账                  + 授权层 + sibling 二次判定
                    │                                             │
              阻塞清单 CSV                        config/continuous_minute_sessions.csv
              （不接发布路径）                     + inventory + audit report
```

`_build_default_liquidity_audit` 一分为二：

- **宇宙无关核心**（新）`build_audit(prices, *, in_pool_source_keys, history_status_by_key, start, end)`：
  保留 `_build_representative_index`、`global_calendar`、`build_audit_key_sets`、
  `_select_audit_candidates_from_index`；池子与历史状态从外部注入。
- **Carry 包装**：保留原名、原签名、原行为，内部仍算 `aggregate_product_liquidity`、
  推导池与历史状态、含 `must remain out of pool` 断言，再调核心。**默认路径逐点不变。**

`_capture_and_publish_outcome` 增加 audit builder 参数，默认为上述 Carry 包装；发布路径仍只有一条。

新驱动 `scripts/continuous/capture_sessions.py` 依赖方向为「连续驱动 → 采集模块」，
`cta_carry/` 与 `index_open_momentum/` **不新增任何依赖**。

**历史闸不平移。** Carry 的 `liquidity_history_incomplete` 防的是「日线历史不全导致池子判错」。
`universe_for_month` 明文把品种缺席日按 0 计（见 `cta_continuous/universe.py` 文档），
历史不全只会低估成交额、绝不会误纳，该失效模式在本口径下不存在。替换为一条断言：
日线加载起点须早于采集起点 6 个月（`prewarm_calendar_days=730` 富余满足）。
状态值发 `"lookback_complete"`；`authorized_history_gap_lines` 只认
`"authorized_history_gap"`、其余忽略 —— **绝不发**该值，以免静默要求一条不存在的授权行。

## 下游约束

- `scripts/continuous/build_panel.py` 与 `scripts/continuous/2026-08-27-panel-smoke.py`
  的 `SESSION_RULES` 改指 `config/continuous_minute_sessions.csv`。
- `cta_carry/__main__.py:46` 与 `index_open_momentum/sessions.py:46` **不改**。
- 新资产格式与 `commodity-v1` 完全一致，`load_session_rules` 无需改动。

## 运行审计与错误处理

**面板侧覆盖闸（原 bug 的正解）。** 在 `cta_continuous/panel.py` 新增

```python
def require_session_coverage(choices_by_month, rules) -> None
```

由 `build_panel.py` 在 `load_session_rules` 之后、月循环之前、**任何分钟查询之前**调用。
展开 `[--start, --end]` 全区间键集逐键解析，同时检 `found 0` 与 `found > 1` 两种基数；
有缺则抛错并落**完整清单** CSV + 摘要（品种数、天数、按年分布），不报第一个就死。
纯内存，成本可忽略。

这道闸同时是补采的**验收标准**：补采完成后它必须静默通过，通过即证明两个集合相同。

**普查模式**只记账不发布，调用的是生产用的 `classify_session_boundary` 本身，
`day_session_absent` 同样取自 `absent_product_days` 授权表，因此判定与权威跑法逐点一致。
`classify_authorized_boundaries` 不作修改。

## 验收测试

**一、证明 Carry 逐点不变**

1. `config/carry_minute_sessions.csv` 的 sha256 前后相同。
2. 拆分后跑一段真实 Carry 采集（改动前/改动后同区间），比对规则 CSV + inventory +
   audit report **逐字节相同**。按 [[shared-layer-extraction-lessons]]，「逐点不变」须拿真实资产比 sha256。
3. **全量**测试套件绿。按 [[test-doubles-lag-source-signatures]]，新增关键字参数会静默打断
   `tests/` 内手写替身，定向测试全绿而全量红。

**二、交叉验证（D3 的回报）**

两份资产在 (交易所, 品种, 日期) 重叠部分逐个品种日比对夜盘区间，产出重叠规模与一致率。
全部一致则写入审计报告；有分歧则逐条列名交裁决，**不自动取任何一方**。

**三、覆盖闸**

- `require_session_coverage` 单测（缺 1 条 / 多 1 条 / 全覆盖三态）。
- **接线测试**钉住 `build_panel.py` 确在月循环前调用它 —— 按
  [[wiring-finds-what-unit-tests-cannot]]，写好却没接线的闸比没有更糟。
- 变异验证新增逻辑；按 [[mutation-runs-need-pycache-clear]]，跑前先清 `__pycache__`，
  否则等长变异 + 同秒 mtime 会假报「测试没抓住」。

## 非目标

- 不修改 `carry_minute_sessions.csv`、`cta_carry/`、`index_open_momentum/` 的任何行为。
- 不延长样本窗口（D7）。
- 不把采集核心搬进 `common/` —— 按 [[shared-layer-extraction-lessons]]，搬家的反向依赖
  只有真搬才现形，本次不承担该风险。
- 不自动裁决交叉验证中的分歧，也不自动登记新的 `absent_product_days`。
