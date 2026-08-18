# Session exception 待审批次（2026-08-18）

> **这不是权威资产。** `config/carry_minute_session_exceptions.csv` 仍是零数据行的纯表头。
> 本文只是**提请复核的候选行清单**，每行都必须经用户逐行过目后，才允许写入仓库资产。
> 用户 2026-08-18 明确口径：只出待审批次，不自主写仓库资产。

目标资产表头：

```text
exchange,version,trade_date,night_start,night_end,reason,source_url
```

唯一键 `(version, exchange, trade_date)`。`trade_date` 是该夜盘例外**所归属的目标交易日**，
不是公告中的前夕自然日。`reason` 必须含且仅含一个 `notice_evening=YYYY-MM-DD` token，
校验时用全局交易日历映射：`trade_date = min(global_trade_date > notice_evening)`。

---

## 批次 A：DCE 2019-12-26 延迟开盘（原文已取回，可直接定稿）

### 提议行

```csv
DCE,commodity-v1,2019-12-26,22:30,23:00,delayed night open per 大商所发[2019]553号 notice_evening=2019-12-25,<source_url 待确认>
```

### 权威原文（用户 2026-08-18 提供，逐字保留）

```text
关于调整夜盘交易时间的通知
大商所发[2019]553号

各会员单位：

　　根据《大连商品交易所交易管理办法》相关规定，我所决定，2019年12月25日晚夜盘交易时间调整为22:30—23:00，集合竞价时间为22：25—22：30。

　　请各会员单位及时通知客户。

　　特此通知。

大连商品交易所

2019年12月25日
```

### 逐字段坐实

| 字段 | 取值 | 原文依据 |
|---|---|---|
| `exchange` | `DCE` | 落款「大连商品交易所」 |
| `version` | `commodity-v1` | 当前资产版本，未发布故不迁移 |
| `trade_date` | `2019-12-26` | 2019-12-25 晚的夜盘归属下一交易日；`min(global_trade_date > 2019-12-25) = 2019-12-26` |
| `night_start` | `22:30` | 「夜盘交易时间调整为22:30—23:00」 |
| `night_end` | `23:00` | 同上。语义为**排他上界**：段为 `[22:30, 23:00)`，最后一根 K 线 22:59，共 30 根 |
| `reason` | 见提议行 | 含唯一 `notice_evening=2019-12-25` token；**原文未给出调整原因**，因此不得编造缘由 |
| `source_url` | **待确认** | 用户已取回正文但未告知实际生效的 URL |

### 两处此前不确定、现已由原文解决

1. **22:30/23:00 两个数字**：此前只有二手记录，现由原文直接坐实。
2. **适用范围**（此前最大的 schema 风险）：原文表述为「我所决定，2019年12月25日晚**夜盘交易时间**调整为……」，
   面向「各会员单位」，**无任何品种限定** → 是全所口径。
   因此 `SessionException` 的 `(version, exchange, trade_date)` 唯一键（**无 product 列**）
   足以表达该事实，2026-08-17 实施的 schema **不需要返工**。

### 定稿前仍需解决

| 项 | 说明 |
|---|---|
| `source_url` | 需用户告知实际取到正文的 URL。登记表原记为 `http://www.dce.com.cn/dalianshangpin/yw/fw/jystz/ywtz/6202113/index.html`；2026-08-18 实测该 URL 与 `qhxy.dce.com.cn` 镜像均返回瑞数 JS 挑战页（HTTP 412），机器不可读 |
| 集合竞价 K 线 | 原文另给出**集合竞价 22:25—22:30**。需在真实分钟数据上确认供应商**是否**为集合竞价成交单独打一根 22:25–22:29 的 K 线。若打了，经验分类得到的 `night_first` 就不是 22:30，会 fail-closed 报 `night_first` 错误 —— 那不是缺陷，是需要先确认的事实 |

---

## 批次 B：节前无夜盘 `none,none` 候选（采集窗口 2018-06-01 → 2020-06-30）

从权威来源登记机械提取的已复核 `notice_evening`，**共 45 条**落在本次有界采集窗口内。
提取脚本见会话 scratchpad `parse_sources.py`，产物 `reviewed_no_night_sources.csv`。

**这批还不能定稿**，缺两样：

1. **`trade_date` 映射**：必须用采集产出的全局交易日历算
   `min(global_trade_date > notice_evening)`，不能拿公告里的「恢复日期」直接当 `trade_date`。
2. **经验清单交叉验证**：登记表自己的规矩是「官方来源、目标日期映射和经验 inventory
   三者一致后才写权威 CSV」。有界采集（2018-06-01 → 2020-06-30）正在跑，其
   ambiguity 清单是第三方证据。

窗口内已复核前夕（按日期排序）：

| notice_evening | 交易所 | 公告所称恢复日 | review_status |
|---|---|---|---|
| 2018-06-15 | DCE / INE / SHFE | 2018-06-19 | `reviewed_direct` |
| 2018-09-21 | DCE / INE / SHFE | 2018-09-25 | `reviewed_direct` |
| 2018-09-28 | DCE / INE / SHFE | 2018-10-08 | `reviewed_direct` |
| 2018-12-28 | CZCE | （登记未给恢复日） | `reviewed_direct` |
| 2018-12-28 | DCE / INE / SHFE | 2019-01-02 | `reviewed_direct_superseded_part` |
| 2019-02-01 | DCE / INE / SHFE | 2019-02-11 | `reviewed_direct_superseded_part` |
| 2019-04-04 | DCE / INE / SHFE | 2019-04-08 | `reviewed_direct_superseded_part` |
| 2019-04-30 | DCE / INE / SHFE | 2019-05-06 | `reviewed_direct_override` |
| 2019-06-06 | DCE / INE / SHFE | 2019-06-10 | `reviewed_direct_superseded_part` |
| 2019-09-12 | DCE / INE / SHFE | 2019-09-16 | `reviewed_direct_superseded_part` |
| 2019-09-30 | DCE / INE / SHFE | 2019-10-08 | `reviewed_direct_superseded_part` |
| 2019-12-31 | DCE / INE / SHFE | 2020-01-02 | `reviewed_direct` / `_superseded_part` |
| 2020-01-23 | INE / SHFE | 2020-01-31 | `reviewed_direct` |
| 2020-04-03 | DCE / INE / SHFE | 2020-04-07 | `reviewed_direct` / `_superseded_part` |
| 2020-04-30 | DCE / INE / SHFE | 2020-05-06 | `reviewed_direct` / `_superseded_part` |
| 2020-06-24 | DCE / INE / SHFE | 2020-06-29 | `reviewed_direct` / `_superseded_part` |

### ⚠️ 提取器的三个已知缺口（人工复核时必看）

1. **DCE 2020-01-23 未被抓到。** 登记里该条写作
   `2020-01-23→修订后 02-03`，箭头右侧不是纯日期，正则未匹配。SHFE/INE 的同日条目正常抓到。
   人工复核时必须把 DCE 这条补回，并注意其恢复边界由延长通知 `6204380` 修订为 02-03。
2. **全量提取 201 条 vs 登记表自称 219 条**，差 18 条。差额来源未逐条追平，
   可能来自本提取器跳过的 `holiday_no_night_rule`（CZCE 规则型来源，明确不派生行）、
   缩写日期跨年推断，以及 2020 疫情暂停区间的单独展开（252 条，另按区间归因）。
   **本批次不得据此数字下任何结论**，它只是复核用的便利索引。
3. **2020 疫情暂停区间（2020-02-04 → 2020-05-06）完全未纳入本表。**
   该区间在登记表里按「暂停边界 + 恢复边界 + 全局日历」展开为 252 个候选，
   与节前候选去重后才是最终键。本次采集窗口**覆盖该区间**，所以采集清单里会大量出现，
   但本表没有对应行 —— 不要误判为"来源缺失"。

---

## 明天定稿的推荐顺序

1. 确认 DCE `source_url`（唯一阻挡批次 A 定稿的字段）。
2. 读有界采集产物：`round1_inventory.csv`（ambiguity 清单）与 `round1_audit.txt`。
3. 用采集的全局交易日历把批次 B 的 45 条 + DCE 2020-01-23 映射成 `trade_date`。
4. 三方交叉（官方来源 × 日历映射 × 经验清单），只有三者一致的行才进批次。
5. 用户逐行过目后，才写 `config/carry_minute_session_exceptions.csv` 并提交。
6. 注意：即使这批写完，**正式全历史资产仍被 AL1803 2018-01-02 的分钟缺口阻塞**
   （见 `docs/research/2026-08-14-carry-minute-execution-handoff.md`）。
   本批次只覆盖 2018-06 → 2020-06，不足以生成 `config/carry_minute_sessions.csv`。
