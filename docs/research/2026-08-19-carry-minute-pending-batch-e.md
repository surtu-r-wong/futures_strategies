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

### ✅ 2026-08-19 收尾：两处都已闭合（用户提供 URL，本机核实）

| 项 | URL | 核实情况 |
|---|---|---|
| DCE 2026 元旦安排 | `http://www.dce.com.cn/dce/content/2025/ywggytz/18625821.html` | 用户提供；本机 412 取不到，按 DCE 既有口径记为 `reviewed_direct`（与 553 号同样由用户人工取回） |
| SHFE 2026 休市公告 | `https://www.shfe.cn/publicnotice/notice/202512/t20251217_829805.html` | ✅ **本机取回全文核实**（`shfe.cn` 可机读） |

上期所公告全文验证了批次 E 的全部日期 ——《上海期货交易所关于2026年休市安排的公告》
**〔2025〕157 号**，2025-12-17：

> 一、元旦：……1月5日（星期一）起照常开市。**2025年12月31日（星期三）晚上不进行夜盘交易。**
> 二、春节：……2月24日（星期二）起照常开市。**2月13日（星期五）晚上不进行夜盘交易。**
> 三、清明节：……4月7日（星期二）起照常开市。**4月3日（星期五）晚上不进行夜盘交易。**

三个前夕与本文从交易日历推出的完全一致。该公告还列明了 2026 年其余四个前夕
（04-30、06-18、09-24、09-30），超出当前数据窗口，留待窗口右移时使用。

**下面「真正的缺口」一节记录的是闭合前的状态，保留作为过程留痕。**

### 真正的缺口只有一个：DCE 的 2025-12-31（已闭合）

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

## 同时要改批次 D 中 2026-01-05 的 DCE 那行

原行挂的是郑商所的规则 PDF（域名都不对），且当时该项在登记表里未闭合。现已闭合：

```csv
DCE,commodity-v1,2026-01-05,none,none,no night session before the post-holiday session notice_evening=2025-12-31,http://www.dce.com.cn/dce/content/2025/ywggytz/18625821.html
```

## 可选：SHFE 三行改挂具名公告

登记表现记的是常年日历页（`reviewed_direct`，本文不推翻）。现已能取到当年具名公告全文，
建议把 SHFE 的 2026 三行（2026-01-05 / 02-24 / 04-07）改挂：

```text
https://www.shfe.cn/publicnotice/notice/202512/t20251217_829805.html
```

定稿文件优于活页，且**本机可复核**（`shfe.cn` 域名，非 `shfe.com.cn`）。

## 补全后的实测

把批次 C + D（2026-01-05 的 DCE 行改挂正式来源）+ E 合起来（**148 条例外行**）
对 11,741 条授权类歧义重放：

```text
残余 1
   2022-03-10  SHFE
```

**这最后 1 条也不该由权威行解决** —— 取回上期发〔2022〕73 号原文并逐合约核对分钟数据后
确认：那晚 NI 夜盘开着、五个合约正常成交，停的是七个具名合约，采集只是不巧选了其中之一
做代表。详见 `docs/research/2026-08-19-ni-2022-03-10-is-not-a-session-fact.md`。

CZCE 那 341 条能否采信是另一个问题（「对 CZCE 什么算权威逐年日历」），不在这个计数里。
CZCE 那 341 条的可采信性另属「逐年日历怎么算权威」的裁决，见
`docs/research/2026-08-19-batch-cd-replay-verification.md`。
