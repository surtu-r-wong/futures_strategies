# 批次 A/C/D/E 过目通过并写入权威资产（2026-08-20）

用户 2026-08-20 逐块过目后，三件待判事项**全部选路 A**，161 条候选行整批采纳。
本文记录：写了什么、机器侧核了什么、以及过目前新查出的两处来源登记漏网如何处理。

审阅页（一次性产物，非仓库资产）：
`https://claude.ai/code/artifact/c5b7f774-2193-4158-b4f7-7fe99648dda2`

## 写入的资产

| 资产 | 行数 | sha256 |
|---|---:|---|
| `config/carry_minute_session_exceptions.csv` | 149 | `47dfb5ced17ed644a0453d5807eef19921fa399e172fe8ad2426f5ac8ec3057c` |
| `config/carry_minute_day_only_regimes.csv` | 12 | `b9b34ce5e0ac038c9b6320dcbeb9a02ecd28577b78c672534a482628afe075bb` |
| `config/carry_liquidity_history_exceptions.csv` | 0（仍为纯表头） | `d2bf062f8d98cdb175c766e9f276910fed721833943af4845fece7b1bea74b80` |

149 = 批次 A 1 行（DCE 2019-12-26 延迟开盘）＋ 批次 D 140 行 ＋ 批次 E 8 行，
其中批次 D 的 `DCE 2026-01-05` 一行按批次 E 的更正改挂大商所来源。
候选行**不是手抄的**：由脚本直接从三份批次文档的 ` ```csv ` 代码块读出，
即评审者读的那份就是写进资产的那份。

流动性历史例外表保持为空是设计要求：按会话例外设计 §3.3，它只授权**日线流动性**缺口，
明文不能授权分钟会话缺失。

## 三件待判事项与裁决

### 1. DCE 2026-01-05 的来源不在登记表里 → 路 A（补登记）

候选行挂 `http://www.dce.com.cn/dce/content/2025/ywggytz/18625821.html`，
是用户 2026-08-19 提供、用来闭合 DCE 2026 元旦缺口的 URL。但**登记表当时没有补这一行** ——
DCE 的 2026 年只有 `02-13 → 18627505` 与 `04-03 → 18628241` 两条，全文搜不到 `18625821`。
而登记表自己写明该项「不在 inventory 与官方来源完成闭合前写 CSV」。

即：8-19 的收尾记录称「两处来源缺口闭合」，实际只闭合了**取回 URL** 这一半，
**登记动作漏做**。这不是候选行写错，是登记表与批次文档之间的一致性检查当时没做到位。

裁决：用户 2026-08-20 确认当时在浏览器读到的就是《大商所 2026 年休市安排》正文、
写明 12 月 31 日晚不进行夜盘。据此在登记表补一行 `reviewed_direct`，
登记方式与大商所发[2019]553号一致（本机 412 不可机读，按人工取回登记）。

### 2. 批次 C 的 8 行缺席证据 → 路 A（照单收）

CZCE 的 AP/CJ/PK/SF/SM/UR 与 DCE 的 JD/LH，理由都是「该品种**不在**夜盘上线通知
逐批列明的名单里」。两点弱处已向用户明示：

- `reason` 文字列了七批通知，`source_url` 只能挂一条，挂的是 2014-12 那份；
  而 AP（2017 上市）、CJ 与 UR（2019）、PK（2021）都晚于它 ——
  「不在 2014 年的名单里」对这四个品种本就不构成证据。
- 「窗口内零反例」不等于证明：正常 21:00 夜盘会被 `authorize_night_observation`
  自动放行、根本不进歧义清单。能说死的是**覆盖率给出的上界**。

用户裁决照单收，依据是「缺席证据 ＋ 经验覆盖 ＋ fail-closed 兜底」这一组合。
兜底确实没松：day-only 只在观测确为 `none,none` 时放行，
`test_day_only_product_that_traded_at_night_still_fails_closed` 钉着这条闸。

### 3. GFEX 三行与登记表明文相抵触 → 路 A（升级登记状态）

登记表原给 SI/LC/PS 的状态是 `reviewed_direct_launch_but_blocked_history`、
`rows_derived=0`，逐字写「证明上市日与日盘安排，**不足以授权连续 day-only 区间**」，
而批次 C 提议的正是从上市日起、右端开放的连续区间。

裁决：升为 `reviewed_launch_plus_empirical`、`rows_derived=1`，
判据与 CZCE 逐年日历那次同一手法 —— **通知给制度，经验数据给连续覆盖**。
广期所没有夜盘品种，所里不存在节前例外行，故不存在与之相撞的问题
（8-19 的自然实验正是拿 GFEX 做对照组）。

`GFEX.PT` 与 `GFEX.PD` 不在批次 C 内（不在审计窗口），两行维持 `rows_derived=0` 与原状态。

## 顺手改掉的一处登记表可读性问题

INE 2025 那行原写「2025 年 6 个公告前夕，与 SHFE 2025 年安排一致」，**没列日期**，
机器核不到覆盖，6 条候选行因此被标成「登记表未逐日列明」。
手工比对确认候选的六个前夕（2024-12-31、2025-01-27、04-03、04-30、05-30、09-30）
与 SHFE 2025 行列明的六个逐一相同，故补写日期。事实没有改变。

**未动的一项**：SHFE 2026 三行仍挂常年日历页
`shfe.com.cn/services/calenderandholidays/holiday/`（登记表已按 `reviewed_direct` 复核）。
可选升级到具名公告〔2025〕157 号（`shfe.cn` 域名可机读，全文已核实）——
用户未就此表态，故不改。

## 机器侧核对（2026-08-20 重跑，非引用旧结论）

用仓库真实代码对**写入后的资产**跑：

```text
load_session_authority           exceptions=149  day_only=12
validate_session_exception_calendar   149/149 通过（futures_daily 3,227 个真实交易日，2013-01-04 .. 2026-04-29）
唯一键 (version, exchange, trade_date)  无重复
域名 × 交易所错配                 0
```

对 `rerun_inventory.csv`（11,861 条，2020-07-01 → 2026-04-29 采集）离线重放：

```text
empirical_boundary（非授权问题）      120
resolved                          11,740
  ├─ 经 day-only 放行              10,138
  └─ 经 session exception 消费      1,602
残余（trade_date ≤ 2026-01-31）         1   ← SHFE.NI 2022-03-10
残余（trade_date >  2026-01-31）         0   ← 批次 E 补上 2026-02-24 / 04-07 后归零
空转的 day-only 行                      0
空转的 session exception 行             1   ← DCE 2019-12-26，见下
```

**DCE 2019-12-26 显示「未被消费」不是缺陷**：该日落在本次采集窗口（2020-07-01 起）之外，
清单里没有那天。它的经验佐证来自 2026-08-19 的竞价归位验证 ——
当天五个 DCE 主力（I/J/M/P/Y2005）经验边界全部为 `(22:30, 23:00)`，与写入值一致。
全历史采集跑通后，`session_exception_unconsumed` 会真正检验这一行。

残余的 SHFE.NI 2022-03-10 已确认**不是会话事实**（那晚 NI 夜盘开着，停的是七个具名合约），
由采集侧代表合约选取修正 `3920c91` 解决，不由权威行承担。

## 测试与资产口径的一处改动

`tests/test_carry_session_authority.py::test_repository_uses_only_the_session_exception_authority_contract`
原先断言两个资产**必须是纯表头**。那条断言的用意是钉住 schema 迁移的结果
（旧 `carry_minute_no_night_dates.csv` 契约已移除），而不是钉住「资产必须为空」——
资产为空只是迁移当时的状态。现改为：

- 断言两个资产的**首行**等于契约表头；
- 断言旧资产文件不存在、旧符号不再导出；
- 断言资产能经 `load_session_authority` 加载且行数为 149 / 12；
- 流动性历史例外表仍断言**整份**等于表头（它按设计就该是空的）。

## 仍未解除的阻塞

正式资产 `config/carry_minute_sessions.csv` **依然发不出来**，
因为全历史采集在 5 个 product-day 的分钟缺口上 fail-closed
（`SHFE.al1803` 2018-01-02、`SHFE.cu1903` 与 `ru1905` 2019-01-02、
`SHFE.al2002` 与 `fu2005` 2020-01-02）。本次过目不解决那个，
它需要从外部补分钟数据。母计划 Task 12 Step 2 仍卡在该资产上。
