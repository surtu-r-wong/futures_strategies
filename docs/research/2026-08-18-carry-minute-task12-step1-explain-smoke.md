# Task 12 Step 1：真实数据库 EXPLAIN-only 烟测

日期：2026-08-18
分支：`feature/carry-minute-execution`
母计划：`docs/superpowers/plans/2026-08-13-carry-minute-execution.md` Task 12

## 结论

**Step 1 的四项计划期望在真实生产库上全部通过。** 全程只执行
`EXPLAIN (FORMAT JSON)`，没有打开命名游标、没有执行数据 SELECT，
`audit.minute_query_months` 与 `audit.minute_rows` 均为 0 可证。

## 运行参数

- 库：Debian primary `100.65.111.79` / `market_monitor` / schema `public`
- 表：`public.futures_minute`（6.6 亿行、264 个压缩 chunk 的 TimescaleDB hypertable）
- 单一活跃合约：`RB2005.SHF` → minute symbol `RB2005`
- 五个 2020 交易日：2020-01-03、01-06、01-07、01-08、01-09
  （各自窗口 = 前一交易日 21:00 → 当日 15:01，Asia/Shanghai）
- 月度边界：`lower=2020-01-01T00:00:00+08:00`，`upper=2020-02-01T00:00:00+08:00`
- 事务参数（`_TRANSACTION_SETTINGS`，全部 `SET LOCAL`，事务结束自动复原）：
  `max_parallel_workers_per_gather=0`、`work_mem='32MB'`、
  `statement_timeout='300s'`、`enable_hashjoin=off`、`enable_mergejoin=off`

数据前置探查（symbol + 时间字面量界定，走 PK 索引）确认 `RB2005` 在
2020-01 每个交易日均有 345 根 K 线（225 日盘 + 120 夜盘），是真实活跃合约
而非空壳。

## MinutePlanSummary

```json
{
  "query_kind": "explain_only",
  "lower_bound": "2020-01-01T00:00:00+08:00",
  "upper_bound": "2020-02-01T00:00:00+08:00",
  "candidate_contract_days": 5,
  "referenced_chunks": [
    "_hyper_2_2186_chunk",
    "_hyper_2_2198_chunk"
  ],
  "maximum_plan_rows": 852013,
  "node_types": [
    "Append", "Custom Scan", "Index Scan",
    "Nested Loop", "Seq Scan", "Sort"
  ]
}
```

```text
plan_audit entries        : 1
audit.minute_query_months : 0
audit.minute_rows         : 0
```

## 四项期望逐条判定

| 期望 | 结果 | 证据 |
|---|---|---|
| 裸 `m.symbol` 索引条件 | PASS | `Index Cond: ((symbol = c.minute_symbol) AND (_ts_meta_min_1 < '2020-02-01 00:00:00+08'::timestamptz) AND (_ts_meta_max_1 >= '2020-01-01 00:00:00+08'::timestamptz))`，`symbol` 无任何表达式包裹 |
| 至多三个 Timescale chunk | PASS | 2 个：`_hyper_2_2186_chunk`、`_hyper_2_2198_chunk`（264 个 chunk 中剪枝到 2） |
| 无节点估计行数 ≥ 10,000,000 | PASS | 最大 852,013 |
| 无对 hypertable 的顺序扫描 | PASS | 见下 |

### 关于 `Seq Scan` 的判定口径

`node_types` 里出现 `Seq Scan` **不等于**违反第四项。原始计划显示：

```text
Sort   rows=852013
  Nested Loop   rows=852013
    Seq Scan on _carry_minute_candidates   rows=460      ← 5 行的临时候选表
    Append   rows=5560
      Custom Scan on _hyper_2_2186_chunk   rows=2738
        | Filter: ((bar_time >= c.window_start) AND (bar_time < c.window_end))
        Index Scan on compress_hyper_3_2369_chunk   rows=3
          | Index Cond: ((symbol = c.minute_symbol) AND ...)
      Custom Scan on _hyper_2_2198_chunk   rows=2822
        | Filter: ((bar_time >= c.window_start) AND (bar_time < c.window_end))
        Index Scan on compress_hyper_3_2210_chunk   rows=3
          | Index Cond: ((symbol = c.minute_symbol) AND ...)
```

唯一的 `Seq Scan` 打在 `_carry_minute_candidates` 上 —— 那是本次查询自己建的
5 行临时表（规划器无统计信息，估成 460 行）。hypertable 侧全部走
`Index Scan on compress_hyper_*_chunk`。这正是既定设计要求的形态：
**用小临时表驱动、join 键是 `futures_minute` 上的裸列**。

因此按"是否对 hypertable 或其 chunk 做顺序扫描"这一精确口径判定为 PASS；
仅按字符串匹配 `Seq Scan` 的粗判会误报。

## 与既有硬规的对应

三条查这张表的硬规（见 `futures-minute-ingestion` 记录）在本次计划中均被证实生效：

1. **join 键是裸列** → `symbol = c.minute_symbol`，规划器成功反推出索引条件；
   若对 `m.symbol` 套表达式会导致 264 个 chunk 全解压扫（>300 s vs 284 ms）。
2. **时间边界写字面量** → `'2020-01-01 00:00:00+08'::timestamptz` 出现在
   `_ts_meta_min_1/_ts_meta_max_1` 的索引条件里，chunk exclusion 在 plan 期发生。
3. **新查询先 EXPLAIN** → 本步骤本身就是这条规矩的制度化。

## Step 1 尚未完成的部分

计划 Step 1 最后一句要求「Save the plan summary into the smoke run's
`minute_data_quality`」。该持久化发生在回测器实际运行时（`minute_query_plan`
质量行），属于 **Step 2 的五品种真实烟测**，而 Step 2 当前被阻塞：

```console
$ PYTHONPATH=. .venv/bin/python -m cta_carry --execution minute --source public-pg \
    --products RB,HC,I,J,JM --start 2020-01-02 --end 2020-03-31 ...
[Errno 2] No such file or directory: '.../config/carry_minute_sessions.csv'
```

`cta_carry/__main__.py:294` 在 `--execution minute` 路径上硬编码加载
`config/carry_minute_sessions.csv`，而该正式资产尚未生成（生成条件见
`docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`
的两个外部闸门）。

**所以：Step 1 的计划验证部分已完成并有据可查；其质量表持久化部分随 Step 2 一起等待正式会话资产。**

## 复现脚本

本次使用的两个一次性脚本保存在会话 scratchpad，未纳入仓库（它们依赖主仓库的
`config/settings.yaml`，且属于一次性诊断而非可维护资产）：

- 数据前置探查：按 symbol + 时间字面量分组计数
- EXPLAIN 判定：调用 `PublicMinuteSource.explain_month(...)`，再单独抓一次原始
  EXPLAIN JSON 递归遍历计划树，按 `Relation Name` 精确判定 Seq Scan 归属

要复现只需构造 5 个 `MinuteCandidate`（`RB2005.SHF`）并调用
`explain_month(candidates, lower=..., upper=...)`。
