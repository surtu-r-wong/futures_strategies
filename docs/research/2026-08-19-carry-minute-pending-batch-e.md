# 待审批次 E：2026 年补全，附批次 D 两处来源更正（2026-08-19）

> **这不是权威资产。** 与批次 C/D 同样，每行须经用户逐行过目才允许写入。

## 为什么有这一批

优先级修改（`11137f8`）落地后重放全清单，残余 93 条，其中 92 条落在两个目标日：

```text
2026-02-24   46 条   春节后首个交易日
2026-04-07   46 条   清明后首个交易日
```

批次 D 止于 2026-01-05，这两个日子没覆盖。补上就只剩 SHFE.NI 一条。

## 前夕日期：两条独立证据一致

| 目标交易日 | 前夕（notice_evening） | 证据 |
|---|---|---|
| 2026-02-24（周二） | **2026-02-13**（周五） | ① `futures_daily` 交易日历：02-13 后直接跳到 02-24；② 大商所公告正文「2月13日（周五）晚上不进行夜盘交易」 |
| 2026-04-07（周二） | **2026-04-03**（周五） | ① 交易日历：04-03 后直接跳到 04-07；② 大商所公告正文「4月3日（周五）晚上不进行夜盘交易」 |

大商所 2026 年休市安排（经检索得到的正文）：春节 2/15–2/23 休市、2/24 开市；
清明 4/4–4/6 休市、4/7 开市。与交易日历逐日吻合。

## 来源：登记表里已经有，我先前漏查了

> ⚠️ **本节推翻本文初稿。** 我最初写「2026 年有两处来源断档、需你去浏览器取」，
> 是因为只看了批次 D 的 CSV 而没查来源登记表。查过之后：**DCE 这两天的公告早已登记**，
> SHFE 的来源也是登记过并复核过的。**声明来源缺失之前必须先查登记表。**

登记表 `docs/research/2026-08-14-carry-minute-session-authority-sources.md` 的相关行：

| 交易所 | 覆盖 | 来源 | 状态 |
|---|---|---|---|
| SHFE | 2025-12-31、2026-02-13、04-03 | `shfe.com.cn/services/calenderandholidays/holiday/` | `reviewed_direct`（3 行） |
| INE | 2025-12-31、2026-02-13、04-03 | `.../202512/t20251217_829804.html` | `reviewed_direct`（3 行） |
| DCE | `notice_evening=2026-02-13` | `dce.com.cn/dce/content/2026/ywggytz/18627505.html` | `reviewed_direct` |
| DCE | `notice_evening=2026-04-03` | `dce.com.cn/dce/content/2026/ywggytz/18628241.html` | `reviewed_direct` |

所以批次 E 的八行来源齐备，无需外出取。

### 真正的缺口只有一个：DCE 的 2025-12-31

登记表里 `2025-12-31` **只有 SHFE 与 INE，DCE 没有**，而且登记表自己写明：

> 「2025-12-31 晚间对应 2026 年元旦安排……当前不计入 78，**也不在 inventory 与官方来源
> 完成闭合前写 CSV**。」

批次 D 的 `DCE,2026-01-05` 那一行正是把这个未闭合项写成了事实行，并随手挂上 CZCE 的
规则 PDF 充数。**所以那不是笔误，是违反了登记表的明令**，该行应撤下，
等 DCE 的 2026 年元旦安排来源闭合后再补。入口：
`http://www.dce.com.cn/dalianshangpin/ywfw/fjap/index.html`（官方页 412，需浏览器取）。

### 一处可选的来源升级（不是缺陷）

SHFE 2026 那三个前夕挂的是常年日历页 `services/calenderandholidays/holiday/`——
登记表已按 `reviewed_direct` 复核通过，本文不推翻。但检索到了当年的具名公告：

```text
https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
标题（检索所得）：上海期货交易所关于2026年休市安排的公告
```

**为什么像真的**：INE 与 SHFE 历年同日发布、id 相邻——2024 年是 INE `t20241223_824108`
/ SHFE `t20241223_824109`；2026 年 INE 是 `t20251217_829804`，推测 SHFE 为 `_829805`，
检索结果正好是它，标题也对得上。

**为什么只是「可选」**：日历页是活页会变，具名公告是定稿文件，引后者更稳；
但我取不到正文（上期所全站 WAF，见
`docs/research/2026-08-19-authority-url-reachability.md`），**不能替你确认**。
你若在浏览器打开确认，可把 SHFE 的 2026 三行改挂它。

## 批次 E 候选行（8 行 = 4 所 × 2 个目标日）

来源全部取自登记表已复核的行。CZCE 两行沿用常设规则来源，其可采信性属批次 D 已提出的
同一问题（「对 CZCE 什么算权威逐年日历」，见重放验证文档），**不因本批次而改变**。

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
CZCE,commodity-v1,2026-02-24,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2026-02-13,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,http://www.dce.com.cn/dce/content/2026/ywggytz/18627505.html
INE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html
SHFE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,https://www.shfe.com.cn/services/calenderandholidays/holiday/
CZCE,commodity-v1,2026-04-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2026-04-03,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,http://www.dce.com.cn/dce/content/2026/ywggytz/18628241.html
INE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html
SHFE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,https://www.shfe.com.cn/services/calenderandholidays/holiday/
```

## 同时要从批次 D 撤下一行

```csv
DCE,commodity-v1,2026-01-05,none,none,...,https://www.czce.com.cn/cn/rootfiles/2014/11/24/...pdf
```

理由见上：登记表明令该项闭合前不得写 CSV，且该行挂的是别家交易所的规则 PDF。
撤下后 2026-01-05 只剩 CZCE / INE / SHFE 三行，DCE 的 15 个夜盘品种当天回到歧义清单——
**这是正确的状态**，它如实反映「DCE 的 2026 年元旦安排来源尚未闭合」。

## 补全后的实测

把批次 C + D（撤下 DCE 2026-01-05）+ E 合起来（**147 条例外行**）
对 11,741 条授权类歧义重放：

```text
残余 16
   2022-03-10  SHFE   1 条   ← 单品种停盘，等 schema 裁决
   2026-01-05  DCE   15 条   ← DCE 2026 元旦安排来源未闭合，如实留在清单里
```

这 16 条都是**有名有姓的待办**，不是未知问题。CZCE 那 341 条能否采信是另一个问题
（「对 CZCE 什么算权威逐年日历」），不在这个计数里。
CZCE 那 341 条的可采信性另属「逐年日历怎么算权威」的裁决，见
`docs/research/2026-08-19-batch-cd-replay-verification.md`。
