# 待审批次 H：2019-12-26 延迟夜盘（2026-08-21）

> **这不是权威资产。** 每行须经用户过目才允许写入。
> ⚠️ **本批依赖 schema 变更**（例外行加 `product` 列），未落地前无法写入。

## 四份公告已全部取回

2019-12-25 晚，**四所同时**依各自细则的例外条款把夜盘开市延到 22:30：

| 所 | 文件 | 援引条款 | 结束时间的表述 |
|---|---|---|---|
| DCE | 大商所发〔2019〕553号 | — | **全所统一** 22:30—23:00 |
| CZCE | **郑商发〔2019〕228号** | 《夜盘交易细则》第八条 | **全所统一** 22:30-23:00 |
| SHFE | 《关于连续交易开市时间的通知》 | 《连续交易细则》第七条 | **逐组列明**：23:00 / 01:00 / 02:30 |
| INE | 《关于连续交易开市时间的通知》 | 《交易细则》第二十九条 | **逐组列明**：20号胶 23:00、原油 次日 02:30 |

四家的集合竞价时间**一致为 22:25–22:30**。

## 公告 × 实测：22/22 精确吻合

把四份公告推出的应然区间与采集实测逐条对表，**22 条残余全部命中，零出入**：

| 所 | 品种 | 公告推出 | 实测 |
|---|---|---|---|
| CZCE | CF FG MA OI RM SR TA ZC（8） | 22:30–23:00 | 22:30–23:00 ✓ |
| INE | SC | 22:30–02:30 | 22:30–02:30 ✓ |
| SHFE | BU FU HC RB RU SP（6） | 22:30–23:00 | 22:30–23:00 ✓ |
| SHFE | AL CU NI PB ZN（5） | 22:30–01:00 | 22:30–01:00 ✓ |
| SHFE | AG AU（2） | 22:30–02:30 | 22:30–02:30 ✓ |

## 为什么是 15 行而不是 22 行

- **CZCE 1 行**：公告是全所口径，`product` 留空即覆盖 8 个品种。
- **INE 1 行**：公告列了 20号胶与原油两组，但 **NR 当日不在流动性池**，
  写了会以 `session_exception_unconsumed` **挡住发布**，故只写 SC。
- **SHFE 13 行**：公告列的 SN、SS 同理不在池，不写；其余 13 个品种逐一成行。

## ⚠️ 依赖的 schema 变更（路 A）

`carry_minute_session_exceptions.csv` 增加可选 `product` 列：

- **空值 = 适用该所全部品种** → 既有 453 行（149 + 批次 G 304）向后兼容，不需改动
- 匹配规则改为「`(exchange, trade_date)` 相符 **且**（`product` 为空 **或** 等于当前品种）」

**必须同改的地方**：`authorize_night_observation` 现在取 `exceptions[0]`，
同一 `(exchange, trade_date)` 有多行时**只用第一行且静默**。不改这里，13 行 SHFE 只有 1 行生效。

> 为什么不用路 B（`night_end=*` 只约束起点）：SHFE 与 INE 的公告**逐组列明了结束时间**，
> 通配符会丢掉公告明确给出的信息。CZCE 与 DCE 是全所统一口径，两条路都能表达。
> 以「如实转录来源」为准，路 A 覆盖四家、路 B 只覆盖两家。

## 待写入的 15 行

```csv
exchange,version,trade_date,product,night_start,night_end,reason,source_url
CZCE,commodity-v1,2019-12-26,,22:30,23:00,delayed night open per 郑商发〔2019〕228号 22:30-23:00 auction 22:25-22:30 (夜盘交易细则第八条) notice_evening=2019-12-25,https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2019/12/1572881274442160.htm
INE,commodity-v1,2019-12-26,SC,22:30,02:30,delayed night open to 22:30 with auction 22:25-22:30 per INE 关于连续交易开市时间的通知 2019-12-25 (交易细则第二十九条); close unchanged for this product notice_evening=2019-12-25,https://www.ine.cn/publicnotice/notice/201912/t20191225_812034.html
SHFE,commodity-v1,2019-12-26,AG,22:30,02:30,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,AL,22:30,01:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,AU,22:30,02:30,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,BU,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,CU,22:30,01:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,FU,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,HC,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,NI,22:30,01:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,PB,22:30,01:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,RB,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,RU,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,SP,22:30,23:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
SHFE,commodity-v1,2019-12-26,ZN,22:30,01:00,delayed night open to 22:30 with auction 22:25-22:30 per SHFE 关于连续交易开市时间的通知 2019-12-25 (连续交易细则第七条); close unchanged for this product notice_evening=2019-12-25,https://www.shfe.cn/publicnotice/notice/201912/t20191225_795464.html
```
