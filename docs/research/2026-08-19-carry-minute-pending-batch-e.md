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

## 先修批次 D 的两处来源缺口

按年梳理批次 D 的来源，规律是**每所每年一份年度公告**。2026 年有两处断档：

| 交易所 | 2020–2025 | 2026 |
|---|---|---|
| INE | 每年一份 `publicnotice/notice/YYYYMM/...` | ✅ `t20251217_829804.html` |
| SHFE | 每年一份 `publicnotice/notice/YYYYMM/...` | ❌ 退化成 `services/calenderandholidays/holiday/`（日历页，非当年公告） |
| DCE | 每年一份 `ywtz/<id>/index.html` | ❌ 退化成 **CZCE 的规则 PDF**（域名都不对） |

**这两处不是笔误，是同一个缺口**：2026 年度公告没取到，生成器各自退化。
SHFE 那处更隐蔽 —— URL 看着像模像样，其实是常年日历页。

### SHFE：URL 已找到，但需你在浏览器确认

```text
https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
标题（检索所得）：上海期货交易所关于2026年休市安排的公告
```

**为什么可信**：INE 与 SHFE 历年同日发布、id 相邻 —— 2024 年是 INE `t20241223_824108`
/ SHFE `t20241223_824109`；2026 年 INE 是 `t20251217_829804`，则 SHFE 应为 `_829805`，
检索结果正好是它。

**为什么仍需你确认**：上期所全站在 WAF 后面（返回 200 + 人机校验页），我取不到正文，
只能看到检索标题。见 `docs/research/2026-08-19-authority-url-reachability.md`。

### DCE：仍缺，入口在这里

年度公告的官方入口：`http://www.dce.com.cn/dalianshangpin/ywfw/fjap/index.html`（放假安排）。
检索只给到转载页，未给出 `dce.com.cn` 原文 URL。DCE 官方页与 `qhxy` 镜像均 412，需浏览器取。

**取到后要补三行的来源**：2026-01-05、2026-02-24、2026-04-07 共用这一份。

## 批次 E 候选行（12 行 = 4 所 × 3 个目标日，含改写 2026-01-05）

⚠️ DCE 三行的 `source_url` 留空待补；SHFE 三行用上面待确认的 URL；
CZCE 三行沿用常设规则来源，其可采信性属批次 D 已提出的同一问题（见重放验证文档）。

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
CZCE,commodity-v1,2026-02-24,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2026-02-13,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,PENDING_DCE_2026_ANNUAL_NOTICE
INE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html
SHFE,commodity-v1,2026-02-24,none,none,no night session before the post-holiday session notice_evening=2026-02-13,https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
CZCE,commodity-v1,2026-04-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2026-04-03,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,PENDING_DCE_2026_ANNUAL_NOTICE
INE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html
SHFE,commodity-v1,2026-04-07,none,none,no night session before the post-holiday session notice_evening=2026-04-03,https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
```

改写批次 D 中 2026-01-05 的两行（其余两行不变）：

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
DCE,commodity-v1,2026-01-05,none,none,no night session before the post-holiday session notice_evening=2025-12-31,PENDING_DCE_2026_ANNUAL_NOTICE
SHFE,commodity-v1,2026-01-05,none,none,no night session before the post-holiday session notice_evening=2025-12-31,https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
```

`PENDING_*` 占位符**不得写入资产** —— `SessionException` 校验只要求 `source_url` 非空文本，
占位符能通过校验却毫无证据价值，所以取到 DCE 原文之前这三行只能留在本文。

## 补全后的实测（已验证，非预期）

把批次 C + D + E 合起来（148 条例外行）对 11,741 条授权类歧义重放：

```text
残余 1            SHFE NI 2022-03-10
未被消费的例外行 0
```
CZCE 那 341 条的可采信性另属「逐年日历怎么算权威」的裁决，见
`docs/research/2026-08-19-batch-cd-replay-verification.md`。
