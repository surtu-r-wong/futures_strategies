# CTA 六因子基本面数据通路

> 计划：`docs/superpowers/plans/2026-07-27-cta-fundamentals-integration-acceptance.md`（Task 6）
> 设计：`docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`
> 上游：market-monitor `commodity_research` schema（Plan 1 / Plan 2）

本文档是 CTA 六因子策略消费 **已发布 conservative 基本面构建（published conservative build）**
的操作契约。它描述命令、口径、闸门与报告字段；它 **不** 认证任何一次运行的收益结果。

---

## 1. 数据源选择（`--fundamentals-source`）

| 取值 | 含义 | 用途 |
|---|---|---|
| `auto`（默认） | `six_factor` → `standard`；`price_volume` → `none` | 常规 |
| `standard` | 读 `commodity_research.fundamental_daily`，锁定唯一一个最新 `status='complete'` 且 `pit_mode='conservative'` 的 build | **唯一可信通路** |
| `legacy` | 读 `public` 稀疏旧表（实际只有 M 一个品种有料） | **仅诊断用**，不得作为研究结论的数据来源 |
| `none` | 不查任何基本面表，元数据 `source="none"` | 量价对照组 |

约束：

- `six_factor` + `none` 组合直接 `SystemExit`（六因子必须有基本面）。
- schema 只接受白名单 `commodity_research`；CLI 字符串永不拼进 SQL。
- `standard` 读取器在两条查询里都带两个防御子句：
  `b.status = 'complete'` 单 build 选择，以及
  `d.available_at <= ((d.trade_date::timestamp + time '15:00') AT TIME ZONE 'Asia/Shanghai')`。
  后者是叠在 builder 自身 PIT 截断之上的 defense-in-depth。
- 出现多个 `build_version` / `catalog_version`、重复 `(trade_date, symbol, metric)`、
  值查询与审计查询 build 不一致、结果为空 —— 一律 `ValueError` 终止，不做静默截断。

## 2. 试点品种

```python
PILOT_FUNDAMENTAL_SYMBOLS = ("M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU")
```

`AU` / `AG` **不在**试点内，缺品种时不得拿它们顶替。`six_factor` 且未显式给 `--symbols` 时
默认就是这九个；`price_volume` 保持"全部价格品种"的旧行为（`AU`/`AG` 在那里仍可用）。

## 3. 覆盖度闸门（权重生成之前）

`cta_gtja/coverage.py`，在缺失因子分数被 `.fillna(0.0)` 变成零权重 **之前** 执行：

| 检查 | 口径 | 失败信息 |
|---|---|---|
| `daily_fundamental` | 每个交易日、每个 metric（`basis_rate`/`inventory`/`profit`）至少 **6/9** 个品种有有限值 | `2020-01-02 basis_rate coverage=5 required=6` |
| `inventory_sides` | 库存因子分数至少 2 个多头候选（`>0`）与 2 个空头候选（`<0`）；从第一个"至少 4 个有限分数"的日期起评估（保留 warm-up，不豁免其后的失败） | `2020-01-02 inventory long=1 short=3 required_each=2` |

- 统计的是 **有限值**（`np.isfinite`），不是"非 null 对象"。
- `enforce=True` 是默认值，抛 `FundamentalCoverageError`，策略层 **不捕获** —— CLI 带着精确原因终止。
- `enforce_coverage=False` 只是合成 fixture 的逃生口：同样产出 `status="fail"` 的审计行与确定性
  `reason` 字符串，但不抛异常。它 **不是** 一种运行分级（run class），报告里也不会因此打任何"合格"戳。

## 4. 基差用未复权价（Tier 1 口径）

- 优先用已发布的 `basis_rate`。
- 只有在没有 `basis_rate` 时才回退计算 `spot / close_raw - 1`，且分母是 **未复权收盘价** `close_raw`
  （非零/正值掩码）。复权价（`close_fa` / `close_ba`）永远不进基差。
- 没有 `basis_rate` 又没有 `close_raw` 时基差保持缺失 —— 绝不用复权价凑数。
- 当 `fundamental_metadata["materialized_daily"] is True`（即 `standard` 通路）时，因子层
  **不再** 前向填充：builder 已按 catalog 的 `max_staleness_trading_days` 做过 PIT 判断，
  策略层再 ffill 等于推翻那个判断。`legacy` / 文件输入保留原有 ffill 行为。

## 5. 操作命令

### 5.1 六因子 + conservative build（medium equal weight）

```bash
cd /home/elfbob/claude-code/futures_strategies
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set six_factor \
  --fundamentals-source standard \
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
  --strategy medium_equal_weight \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_fundamental_medium_conservative
```

把 `--strategy` 换成 `high_composite`、`--output-prefix` 换成
`output/cta_fundamental_composite_conservative`，其余参数不变，即得第二条比较运行。

### 5.2 量价对照组（不查基本面）

```bash
.venv/bin/python -m cta_gtja \
  --source public-pg \
  --factor-set price_volume \
  --fundamentals-source none \
  --strategy both \
  --start 2019-01-01 \
  --end 2025-09-30 \
  --cost-bps 1 \
  --output-prefix output/cta_price_volume_control
```

### 5.3 每次运行的两行血统输出

```text
fundamentals: source=standard pit_mode=conservative build=<build_version> catalog=<catalog_version>
fundamental_coverage: rows=<n> failed=0 minimum=6
```

`failed > 0` 时后面还会追加最多 3 条确定性 `reason`（按字典序）。

### 5.4 起始日期与闸门的关系

如果已发布 build 的起点晚于 `--start`，闸门会在 **第一个未覆盖日期** 失败并给出该日期。
正确处置是把 `--start` 改成实际首个覆盖日并记录该日期；读取器 **不会** 静默截断区间。

## 6. 工作簿里的血统表

`write_cta_outputs` 除既有 sheet 外写入：

| sheet | 条件 | 内容 |
|---|---|---|
| `fundamental_coverage` | 非空时 | 每个日期/检查一行，列见 `COVERAGE_COLUMNS` |
| `fundamental_lineage` | 非空时 | `fundamental_daily` 审计列（`lineage_hash`、`available_at`、`vintage_quality`、`staleness_trading_days` …）；JSON 字段序列化成紧凑字符串 |
| `fundamental_build` | **总是** | 一行，固定列 `source, pit_mode, build_version, catalog_version, source_recorded_cutoff, schema, materialized_daily`；`source` 缺失时写 `"unknown"` |

量价对照组只有 `fundamental_build` 一张，且 `source="none"` —— 它不查、也不宣称任何 build。
`fundamental_build` 里 **不放** JSON 血统字典。

## 7. ⚠️ 解释边界（在上游价格复权修复落地之前一直有效）

这些是 **数据通路比较运行**，不是策略证据：

- 其收益序列尚不可作为可信的策略结论；
- 研报的 Sharpe `2.43`、年化 `17.14%`、持仓周期等数字 **不是验收目标** —— 现在不是，
  上游修复之后也不是；
- 本工作的验收口径是：PIT 隔离已证明、基差用未复权价、覆盖度强制执行、可复现、
  每份报告都带血统。

在上游价格复权修复落地、且用户把 CTA 提升为月度使用之前，**不存在"正式运行"（formal run）**。
运行分级 / 认证链是被显式 deferred 的（见计划文末 Deferred 段）。

## 8. 运行证据表

> **状态：PENDING —— 尚无已发布的 conservative build。**
>
> `commodity_research.fundamental_build` 在生产库尚不存在，`sync_state` 中 `commodity_research`
> 为 0 行。Plan 1 Task 8–9 的 Wind 人工确认产物（reviewed `catalog.v1.yaml`、
> `catalog-v1-preflight.csv`、catalog version + config hash、覆盖结论
> basis 9/9 · inventory ≥8/9 · profit ≥7/9、2025-01 smoke recovery package 的 `run_id`、
> manifest SHA-256）必须先在持牌 Wind Windows 机器上产出。
> 详见 market-monitor `docs/operations/commodity-fundamentals-wind.md`
> §"Pending Plan 1 handoff boundary"。
>
> 在拿到真实 build 之前，本表保持为空。**不得**填入合成或推测数值。

| run_at | strategy | source | build_version | catalog_version | 区间 | 品种数 | coverage minimum | coverage failed | ann_return | ann_vol | sharpe | max_drawdown | avg_turnover | workbook |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| _(pending)_ | | | | | | | | | | | | | | |

填表规则：

- `coverage minimum` = `fundamental_coverage` 表 `available_products` 的最小值；
  `coverage failed` = `status == "fail"` 的行数。两者直接抄 CLI 的
  `fundamental_coverage:` 行，不得手算。
- 指标列取 `result.metrics` 的 `ann_return` / `ann_vol` / `sharpe` / `max_drawdown` / `avg_turnover`。
- `workbook` 写 `output/` 下的相对路径。生成的 `.xlsx` / `.png` **不入 Git**。
- 三条运行（medium equal weight、high composite、price-volume control）并排记录，
  差异只作为 **数据/因子证据** 解读，不作为策略优劣结论。
