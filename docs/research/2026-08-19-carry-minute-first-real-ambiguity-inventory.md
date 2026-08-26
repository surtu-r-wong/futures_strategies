# 第一份真实 ambiguity 清单（2026-08-19）

## 为什么现在能跑

原判断是「重跑有界采集须先补齐 5 个缺失 product-day」。实际上那 5 个缺口**只卡含缺口的窗口**：
全部阻塞性缺口都落在 2018-01-02、2019-01-02、2020-01-02 三个元旦后首日。把采集窗口挪到
**2020-07-01 之后**即可全部绕开——2021/2024/2025/2026 无缺口，2022 RI 与 2023 JR/ZC 均不在
流动性池（见 `docs/research/2026-08-18-carry-minute-empirical-findings.md` 第三节）。

窗口约束是 `capture_start ≤ backtest_start - 730 天`（`prewarm_calendar_days=730`），
故 `--start 2020-07-01 --backtest-start 2022-07-01` 合法。产物全部落临时目录，
**未触碰任何仓库资产**。

## 运行与结果

```bash
python -m scripts.carry.capture_minute_sessions \
  --start 2020-07-01 --end 2020-12-31 --backtest-start 2022-07-01 \
  --output <scratch>/probe_sessions.csv \
  --inventory-output <scratch>/probe_inventory.csv \
  --audit-report <scratch>/probe_audit.txt
```

```text
publication_status=blocked
coverage_year=2020 all_product_days=6516 in_pool_days=4800 in_pool_ratio=0.736648
  audited_days=4800 normalization_excluded_product_days=0 normalization_unkeyable_rows=0
products=40 rules=0 checked_days=4800 ambiguous=414
```

**未 fail-closed**：整个窗口没有一个缺分钟阻塞点，采集跑完并写出诊断产物。
耗时约 11 分钟 / 半年。

## 清单的精确分解：零残余

414 条 ambiguity 全部是 `night_authority_conflict`，且观测值**全部**是 `none,none`
（没有任何一条是其它时段形态）：

| 类别 | 行数 | 内容 |
|---|---:|---|
| 节前无夜盘的目标交易日 | **39** | 全部落在 **2020-10-09** 一天：CZCE 11 + DCE 14 + SHFE 13 + INE 1 个在池品种 |
| 日盘 only 品种 | **375** | `CZCE.AP`、`CZCE.SM`、`DCE.JD` 各 125 个交易日（窗口内全部交易日） |

39 + 375 = 414。**没有第三类，没有一条无法归因。**

## 三方交叉（官方来源 × 日历映射 × 经验清单）

来源腿用 2026-08-19 重做的提取器（219 条逐条对齐，见
`docs/research/2026-08-19-carry-minute-authority-source-extraction.md`）；
日历腿用 `futures_daily` 的 distinct `trade_date` 算 `min(global_trade_date > E)`，
与采集自身的全局日历同源。

| 结果 | 数量 | 说明 |
|---|---:|---|
| 三者一致 | **3** | `2020-10-09` × DCE / INE / SHFE，`notice_evening=2020-09-30` |
| 仅经验有 | 251 | CZCE 2020-10-09（1 个键 / 11 品种）+ 三个日盘 only 品种（250 个键） |
| 仅来源有 | **0** | 没有任何「公告说停夜盘、但采集没看到」的日子 |

「仅来源有 = 0」是个强结果：本窗口内官方来源与真实数据**没有一处矛盾**。

## ⚠️ 两个结构性缺口（都需用户裁决，都不是数据问题）

### 一、CZCE 在当前权威模型下永远无法被授权

CZCE 2020-10-09 的 11 个在池品种实测 `none,none`，但登记表里**没有对应来源**。查证原因：
CZCE 在登记表里只有**一条**逐日公告（`notice_evening=2018-12-28`），其余全部依赖一条

```text
| CZCE | holiday_no_night_rule | 法定节假日后的首个交易日没有前一晚夜盘 | ... | 0 | reviewed_rule_only：没有权威逐年日历 |
```

`rows_derived=0` —— 规则型来源**明确不派生行**。而权威资产
`carry_minute_session_exceptions.csv` 的唯一键是 `(version, exchange, trade_date)`，
只能表达**逐个日期**的例外。

**后果**：只要不动这一处，CZCE 每一个节前目标日在全部历史上都会是 ambiguity，
正式资产就永远发布不了。两条出路：

1. **补登记 CZCE 逐年休市公告**（CZCE 确实逐年发布休市安排，只是尚未登记）→ 模型不动；
2. **扩展权威模型**，允许「规则 + 权威节假日日历」派生 → 改 schema。

我倾向 1：它不动 schema，且与其它三所的处理方式一致。

#### 路线 1 的可达性已实测（2026-08-19）

| 目标 | 结果 |
|---|---|
| CZCE `rootfiles` 下的 **PDF 直链** | ✅ **HTTP 200**，可机读（实测取回登记表已引用的 2018-12-24 那份，113 KB） |
| CZCE **公告列表页** | ❌ **HTTP 412**，curl 与 WebFetch 皆然 —— 与 DCE 同一个瑞数 JS 挑战 |
| CZCE 月刊《交易所通讯》 | ⚠️ 可机读，但只转载「公告」，**不含休市通知**（实测 2021 年第 12 期 3,659 行无「休市／夜盘」字样） |
| 第三方转发（期货公司、新闻） | ⚠️ 存在且含逐字「X 月 X 日晚上不进行夜盘交易」，但**违反登记表「只登记交易所官方域名材料」的规矩**，不得采用 |

**因此分工是**：用户在浏览器里打开 CZCE 公告列表（与取回 DCE 553 号同样的方式），
**只需把年度《关于 XXXX 年部分节假日休市安排的通知》的 PDF 直链贴过来**；
之后取回正文、逐字存证、生成登记行与待审批次都可由脚本完成。
用户不必转录正文 —— 只需给 URL。

### 二、日盘 only 品种缺 `day_only` 权威行

`CZCE.AP`、`CZCE.SM`、`DCE.JD` 三个品种本来就没有夜盘，采集判 `none,none` 完全正确，
但 `config/carry_minute_day_only_regimes.csv` 是零数据行的纯表头，于是每天都冲突。

这是本窗口 **375/414 = 90%** 的 ambiguity 来源。往后窗口还会加上 GFEX 五个品种
（SI 2022-12-22、LC 2023-07-21、PS 2024-12-26、PT/PD 2025-11-27），登记表状态为
`review_blocked_history`（上市通知只证明上市日与日盘结构，仍缺连续制度证据）。

## 今天的归位规则在真实数据上的表现

审计报告里 `night_auction_attributed` 与 `night_untraded_padding` **各 0 次**
（4,800 个产品日）。即窗口内没有任何一个夜盘起点被规则改写，也没有「有 K 无量」的夜盘。

这与设计预期一致：规则只在 off-grid 竞价根出现时触发，而该形态在本窗口不存在。
配合 2019-12-26 的定点验证（五个 DCE 主力全部归位到 22:30），
**规则该动的时候动了，不该动的时候一次都没动。**

## 复现

`probe_inventory.csv` / `probe_audit.txt` 在会话 scratchpad。三方交叉脚本
`cross_validate_batch.py` 同目录：读 inventory → 按 `(trade_date, exchange)` 归并
`none,none` 观测 → 与来源提取器 × 日历映射求交。

⚠️ 解析 inventory 的 `reason` 列时注意：`context` 按**键的字母序**序列化，
所以 `observed_night_end` 出现在 `observed_night_start` **之前**。
按书写直觉写的正则会把 414 条全判成「未解析」。
