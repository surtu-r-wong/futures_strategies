# 夜盘竞价归位：真实数据验证（2026-08-19）

分支：`feature/carry-minute-execution`
设计：`docs/superpowers/specs/2026-08-19-carry-minute-auction-attribution-design.md`
实施：`de6d1f7`（查询与帧校验）+ `52156ab`（分类器与审计）

## 一、决策所依据的实证：过滤的爆炸半径按边界分裂

样本 RB / CU / I / M / SC 五品种主力 × 2018-09、2020-06、2023-06、2025-06 四个月
（横跨供应商三次补齐策略变化）。全部窄查询：symbol 裸列 + 时间字面量 +
`max_parallel_workers_per_gather=0` + `work_mem=32MB` + 120 s 超时，无全表聚合。

| 边界 | 样本数 | 加 `volume > 0` 后被挪动 | 谁被挪动 |
|---|---:|---:|---|
| 夜盘起点 | 372 | **0** | — |
| 日盘起点 | 395 | 9 | 全部为临交割的 SC |
| 夜盘收尾 | 每月 5–8 例 | 多 | 连 CU 主力都中招（`23:59 → 23:57`） |
| 日盘收尾 | 每月 3–6 例 | 多 | SC `14:59 → 13:30` |

被挪动的日盘起点全部来自 SC2007 / SC2307 / SC2507 在其交割月最后几周
（`09:00 → 09:01/09:02/09:04/09:06/09:20/09:26`）；四例「整段零成交」同样全是这三个合约。
真实采集逐日重选代表合约，不会选中它们。

**这就是「只滤夜盘起点」的全部依据**：末分钟无成交是常态，首分钟无成交在夜盘从未发生。

## 二、定点验证：真实 2019-12-25 / 12-26 的五个 DCE 主力

对每个交易日构造五个 `session_representative` 候选，调
`PublicMinuteSource.iter_session_boundaries`（真实生产库 Debian primary
`100.65.111.79` / `market_monitor` / `public.futures_minute`），再喂
`classify_session_boundary`。

### 2019-12-26（延迟夜，公告 `大商所发[2019]553号`：22:30—23:00）

```text
 I I2005.DCE  night_first(raw)=21:00 traded_first=22:29 traded_second=22:30 flat=True  night_last=22:59 -> (22:30, 23:00) note=night_auction_attributed
 J J2005.DCE  night_first(raw)=21:00 traded_first=22:29 traded_second=22:30 flat=True  night_last=22:59 -> (22:30, 23:00) note=night_auction_attributed
 M M2005.DCE  night_first(raw)=21:00 traded_first=22:29 traded_second=22:30 flat=True  night_last=22:59 -> (22:30, 23:00) note=night_auction_attributed
 P P2005.DCE  night_first(raw)=21:00 traded_first=22:29 traded_second=22:30 flat=True  night_last=22:59 -> (22:30, 23:00) note=night_auction_attributed
 Y Y2005.DCE  night_first(raw)=21:00 traded_first=22:29 traded_second=22:30 flat=True  night_last=22:59 -> (22:30, 23:00) note=night_auction_attributed
```

五个品种全部得到 `("22:30", "23:00")`，与公告原文逐字吻合；`night_first` 的裸值
仍是补齐空 K 线给出的 `21:00`，证实归位规则确实在起作用而非巧合。

### 2019-12-25（对照的正常夜）

```text
 I I2005.DCE  night_first(raw)=21:00 traded_first=21:00 traded_second=21:01 flat=False night_last=22:59 -> (21:00, 23:00) note=None
 J J2005.DCE  ... 同上 ...  M / P / Y 同上
```

五个品种全部 `("21:00", "23:00")`、`note=None`，**规则未触发**，正常夜口径未受影响。

## 三、查询计划：新列没有让计划退化

用同一探针对**改动前**（`0bd748e`）与**改动后**的 `build_session_boundary_query`
各跑一次 `EXPLAIN (FORMAT JSON)`，两者结果完全相同：

| 项 | 改动前 | 改动后 | 闸门 |
|---|---|---|---|
| 引用的 Timescale chunk | `_hyper_2_2185` / `_hyper_2_2186`（2 个） | 同 | ≤ 3 ✅ |
| 最大节点估计行 | 4,470,401 | 4,470,401 | < 10,000,000 ✅ |
| hypertable 顺序扫描 | 无 | 无 | ✅ |
| 唯一 `Seq Scan` | `_carry_minute_candidates`（5 行临时表，估 460） | 同 | 设计要求的小表驱动 ✅ |

三个新增聚合列（两个 `array_agg` 下标取值 + 一个 `high = low` 比较）对计划形状零影响。

### ⚠️ 一处与 Task 12 Step 1 的差异（既有，非本次引入）

Task 12 Step 1 的**批量取数查询**在 `Index Cond` 里能看到裸 `symbol = c.minute_symbol`，
最大估计行 852,013。**边界查询不是这个形状**：它的 `Index Cond` 只有
`_ts_meta_min_1 / _ts_meta_max_1` 的时间元数据条件，symbol 匹配退到 join 侧
（`Nested Loop` + `Materialize`），故估计行高一个量级。

**该差异在改动前就存在**，本次已用改动前版本跑同一探针逐项比对确认。四道闸门仍全过，
实际执行也在秒级。记录于此供后续优化参考，不属于本次变更范围。

## 四、测试

- 新增 13 个用例：分类器 7 个（正常夜不触发／补齐空 K 归位／下一分钟无成交拒绝／
  挪一分钟仍 off-grid 拒绝／无竞价签名拒绝／有 K 无量判 `none,none`／完全无夜 K 判
  `none,none`），查询与帧校验 4 个，授权与审计 2 个。
- 端到端授权测试 `test_authorized_delayed_open_is_classified_for_every_dce_product`
  已改为**真实形态**（补齐起点 21:00 + 22:29 竞价根），不再用理想化的 22:30 直给。
- 全量：`1 failed, 848 passed`。唯一失败是既有闸门
  （缺 `config/carry_minute_sessions.csv`，计划起点与 master 上同样红）。

## 五、复现

一次性脚本存于本会话 scratchpad，未纳入仓库（依赖主仓库 `config/settings.yaml`）：
`probe_volume_filter.py`（爆炸半径）、`probe_degenerate.py`（退化样本认领）、
`probe_ends.py`（收尾侧影响）、`verify_attribution.py`（定点验证）、
`verify_plan.py`（计划比对，用 monkeypatch 截获 `_validate_plan` 的返回）。
按本文给出的形态可直接重写。
