# CTA 六因子基本面数据通路

> 计划：`docs/superpowers/plans/2026-07-27-cta-fundamentals-integration-acceptance.md`（Task 6）
> 设计：`docs/superpowers/specs/2026-07-27-commodity-fundamentals-design.md`
> 上游：market-monitor `commodity_research` schema（Plan 1 / Plan 2）

本文档是 CTA 六因子策略消费 **已发布 conservative 基本面构建（published conservative build）**
的操作契约。它描述命令、口径、闸门与报告字段；它 **不** 认证任何一次运行的收益结果。

更新：2026-10-08。标准通路已于 08-18 完成全历史端到端验收；办公室 09-29 恢复完整构建与日更，
09-30 首轮验收通过。本文原来的「无 build / EOD 停摆 / 固定止于 04-29」状态已失效。
10-08 实际只读选中的最新 build 覆盖到 09-30，共 86,703 行；
来源质量问题及后续请求见 [本次核查](research/2026-10-08-fundamental-data-quality.md)。
换源范围按 08-26/27 已有计划执行：九品种现货、库存、利润全量重填，并接续发布日历版工具；
实现分支与工作簿见 [接续记录](research/2026-10-08-fundamental-full-refresh-scope.md)。

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
| `inventory_sides` | 检查去均值后的实际信号，两侧各至少 2 个候选；不足时当日库存腿停用并记录 `flat`，不终止整个回测 | 审计保留两侧数量及原因 |

- 统计的是 **有限值**（`np.isfinite`），不是"非 null 对象"。
- `daily_fundamental` 仅豁免当前 build 的 `absence_slices`，标为 `waived`；不按旧文档猜缺失日期。
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
  --symbols M,RB,CU,AL,TA,PP,MA,BU,RU \
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

## 7. 解释边界与数据维护

08-18 已发布 build 的结果是同窗口研究证据，不是可交易性认证。尚未做样本外切分与成本敏感性，
六因子换手约为量价对照的两倍；研报收益与夏普不是数据修复的验收目标。
保留 PIT、未复权基差、覆盖闸和血统记录，不通过放宽陈旧度或静默填充改善覆盖。

日线已恢复，build 的可用区间应从消费者实际选中的 complete conservative 版本查询，
并与价格覆盖取交集；不能再把 2026-04-29 写死为运行上界。
既有基本面日更继续运行。替换源、回填、公式升级或重建需按 09-28 章程先写请求函到
`/home/elfbob/claude-code/data_manager/requests/`，由办公室受理并核算 Wind 批次额度。
旧的运行认证链和已停止的受控落库路线不属于本次质量工作。

## 8. 已完成的运行证据

08-18 基线：`conservative-20260818T060911Z-622e88840fa3`，catalog v1，
2016-01-04..2026-04-29，82,943 行；两条六因子运行均为
`fundamental_coverage: rows=9980 failed=0`。同九品种、1 bp 成本：

| 因子集 | 组合 | 年化收益 | 波动 | 夏普 | 最大回撤 | 换手 |
|---|---|---|---|---|---|---|
| 六因子 | medium_equal_weight | 3.65% | 8.22% | 0.444 | 15.9% | 0.204 |
| 六因子 | high_composite | 4.09% | 13.0% | 0.315 | 39.9% | 0.380 |
| 量价 | medium_equal_weight | 1.11% | 8.88% | 0.124 | 25.2% | 0.094 |
| 量价 | high_composite | −0.46% | 13.5% | −0.034 | 45.5% | 0.183 |

证据：[上游全窗口验收](/home/elfbob/market-monitor/docs/operations/commodity-fundamentals-full-window-build-20260818.md)。
09-29 恢复发布与日更首轮验收见
[发布处置](/home/elfbob/claude-code/data_manager/requests/2026-09-29-fundamental-publication/disposition.md)、
[首轮验收](/home/elfbob/claude-code/data_manager/findings/2026-09-30-fundamental-first-run/README.md)。
这些上游更新不等于已重跑上表的策略对照；新源比较须固定 build、价格输入、窗口、九品种和成本。
