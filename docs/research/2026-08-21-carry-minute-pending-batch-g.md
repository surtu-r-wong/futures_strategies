# 待审批次 G：2018-01 → 2020-06 补全（2026-08-21）

> **这不是权威资产。** 与批次 A/C/D/E/F 同样，每行须经用户过目才允许写入
> `config/carry_minute_session_exceptions.csv`。

## 这批从哪来

批次 F 证明了分段有界采集能绕开 5 个缺分钟的 product-day。本次把剩下的空白窗口
**2018-01-03 → 2020-06-30** 分三段跑完，三段**全部完整跑完、无一 fail-closed**：

```text
--start 2018-01-03 --end 2018-12-31 --backtest-start 2020-01-03   products=38 checked_days=8430 ambiguous=1215
--start 2019-01-03 --end 2019-12-31 --backtest-start 2021-01-02   products=39 checked_days=8525 ambiguous= 441
--start 2020-01-03 --end 2020-06-30 --backtest-start 2022-01-02   products=39 checked_days=4198 ambiguous=2177
```

三段起点刻意跳过 2018-01-02 / 2019-01-02 / 2020-01-02（那 5 个 product-day 所在日）。
`backtest_start` 只用于 `validate_capture_coverage` 的 prewarm 断言，硬约束是
`capture_start ≤ backtest_start − 730`；观测范围是 `[start, end]`，与 `backtest_start` 无关。

**至此 2011-01-01 → 2026-04-29 全历史的权威行已全部有据可依**，不再有未采集的窗口。

## 候选行：304 条例外（无新增区间行）

| 分组 | 行数 | 解决 |
|---|---:|---:|
| G-1『节后首日无夜盘』例外（14 个目标日） | 52 | 420 |
| G-2『2020 疫情全市场暂停』例外（63 个目标日 × 四所） | 252 | 2,106 |
| 既有 + 批次 F 的区间行在本窗口内解决 | — | 1,281 |
| **残余** | — | **23**（另 2 条 `empirical_boundary`） |

三段 `night_authority_conflict` 合计 3,830 = 420 + 2,106 + 1,281 + 23，逐位对上。

> G-1 的 420 里有 20 条是 DCE C/L/PP/V 在 **2019-03-29 之后**的 5 个节后日：
> 那时批次 F 的日盘区间已到期，这些品种已有夜盘，改由节后例外行接手。这正是
> 区间右端定在 2019-03-29 的可观测后果。

**没有新的区间行**：2018–2020 的日盘品种全部已被批次 F 的 25 条区间或既有 12 条覆盖。

## G-1 的 14 个目标日

| 目标交易日 | notice_evening | 覆盖的所 |
|---|---|---|
| 2018-02-22 | 2018-02-14 | CZCE DCE SHFE |
| 2018-04-09 | 2018-04-04 | CZCE DCE SHFE |
| 2018-05-02 | 2018-04-27 | CZCE DCE SHFE |
| 2018-06-19 | 2018-06-15 | CZCE DCE SHFE |
| 2018-09-25 | 2018-09-21 | CZCE DCE INE SHFE |
| 2018-10-08 | 2018-09-28 | CZCE DCE INE SHFE |
| 2019-02-11 | 2019-02-01 | CZCE DCE INE SHFE |
| 2019-04-08 | 2019-04-04 | CZCE DCE INE SHFE |
| 2019-05-06 | 2019-04-30 | CZCE DCE INE SHFE ← 劳动节调整通知（override） |
| 2019-06-10 | 2019-06-06 | CZCE DCE INE SHFE |
| 2019-09-16 | 2019-09-12 | CZCE DCE INE SHFE |
| 2019-10-08 | 2019-09-30 | CZCE DCE INE SHFE |
| 2020-02-03 | 2020-01-23 | CZCE DCE INE SHFE ← 春节延长后恢复边界改到 02-03 |
| 2020-06-29 | 2020-06-24 | CZCE DCE INE SHFE |

`E→R` 全部取自登记表既有的年度休市公告，逐条与 `futures_daily` 真实日历核对通过。

## G-2：疫情暂停区间与登记表的算术分毫不差

观测到的 `none,none` 连续块是 **2020-02-03 → 2020-05-06 共 64 个交易日**，拆成：

- **2020-02-03**：按节后规则归因（春节休市延长至 02-02），归入 G-1
- **2020-02-04 → 2020-05-06**：**63 个目标日**，四所各一份 = 252 行，归入 G-2

登记表 §统计 早就写着「2020 suspension expanded rows = 252，SHFE、INE、DCE、CZCE 各 63 个目标日
（2020-02-04 至 2020-05-06）；不含另按节后规则归因的 2020-02-03」——**实测与它逐日吻合**。
恢复边界（05-06 晚恢复夜盘 → 首个有夜盘的目标日 2020-05-07）也吻合：05-06 是最后一个 `none,none`。

## 机器侧核对（2026-08-21）

用**仓库真实代码**（`load_session_authority` / `validate_session_exception_calendar` /
`authorize_night_observation`）对三段清单离线重放，不写 `config/`：

```text
权威 = 既有资产 + 批次 F(99) + 批次 G(304)          exceptions=527  day_only=37
527 行的 notice_evening → trade_date               全部通过（futures_daily 3,714 天真实日历）
三段重放  resolved=3,807  (day_only 1,281 + exception 2,526)  residual=23
批次 G 的 304 行中未被消费的                        0
```

**对既有两份清单零退化**（同一重放器，加 G 前后逐位相同）：

| 清单 | 只有批次 F | 批次 F + G |
|---|---|---|
| 2011-2017 | resolved 17,496 / residual 15 | resolved 17,496 / residual 15 |
| 2020-07 → 2026-04 | resolved 11,740 / residual 1 | resolved 11,740 / residual 1 |

例外行按 `(exchange, trade_date)` 键，批次 G 的日期全落在 2018-01 → 2020-06，
结构上不可能影响另外两段——重放只是把这件事实测了一遍。

## ⚠️ 要你判断的三件事

### 1. 🔴 2019-12-26 的延迟夜盘是**全市场**的，不止大商所（22 条）

登记表现在只登记了 DCE 一家（大商所发[2019]553号，22:30—23:00），§统计 里也写着
「known irregular-session candidates = **1**，DCE 目标交易日 2019-12-26」。
2019 段的实测**推翻了这个判断**——同一夜四所全部延迟到 22:30 起：

| 所 | 品种数 | 观测到的夜盘区间 |
|---|---:|---|
| DCE | （已授权，未进清单） | 22:30–23:00 |
| SHFE | 13 | **22:30–23:00 / 22:30–01:00 / 22:30–02:30**（三种） |
| CZCE | 8 | 22:30–23:00 |
| INE | 1 | 22:30–02:30 |

**两个独立的问题：**

**(a) 缺来源。** SHFE / CZCE / INE 2019-12-25 当晚的调整通知均未取回，需要在浏览器取
（三所站内页对脚本 412 / WAF，见 `2026-08-19-carry-minute-authority-source-extraction.md`）。

**(b) 🔴 schema 表达不了 SHFE 那 13 个品种。**
`carry_minute_session_exceptions.csv` 是**交易所级、单一 `night_start`/`night_end`**，
而 `authorize_night_observation` 要求 `observed == (row.night_start, row.night_end)`
**首尾都精确匹配**。SHFE 当晚是「起点统一延到 22:30、各品种保留各自的正常收尾」，
一行写不下三个收尾。DCE 之所以一行够用，是因为它全所正常收尾本来就是 23:00。

可选路子（**请拍**）：
- **路 A**：给 exception 加可选 `product` 列，SHFE 那晚写 3 行（按收尾分组）或 13 行（按品种）。
- **路 B**：加一种「只约束起点」的写法（如 `night_end=*`），语义是「起点为 X，收尾按该品种常规」。
- **路 C**：先不动 schema，把这 22 条连同 2017 那两夜一起挂起，等取回原文再定。

我倾向 **路 B**：它精确描述了通知本身说的事（调整的是开盘时间），不引入品种级权威行；
但它新增一种通配语义，会削弱「首尾精确匹配」这条现有的 fail-closed 性质，所以要你定。

### 2. 🔴 2019-12-23 SHFE AL：**第六个归档缺陷**，不是行情事实（1 条）

这条 `none,none` 是**唯一**一条孤立的、不在任何节假日或暂停区间里的残余。查实：

| 事实 | 证据 |
|---|---|
| AL 那晚**确实有夜盘** | AL2003 成交 142 根（21:00→00:59）、AL2001 118 根、AL2004 104 根、AL2005 70 根 |
| 被选作代表的是 AL2002 | 它是 2019-12-23 的主力，日线成交 136,738 手（第二名 AL2003 才 46,016） |
| AL2002 那晚 240 根 K 线**全部 volume=0** | 且 **close 只有 1 个不同值 = 14195** |
| 14195 **等于当日结算价** | `futures_daily` 2019-12-20 AL2002：`close=14175`、`settle=14195` |
| 它前后每一晚都正常成交 | 2019-12-09 → 2020-01-10 逐夜 1.7 万–8.3 万手，**唯独 12-20 是 0** |

即：供应商丢了 `AL2002` 这一个 contract-night 的成交，并用结算价平铺补齐了 K 线。
与 2017 那两个整所零成交夜**同类但更容易判**——那两夜整所全零、无法自证；
这次同品种其它合约当场作证了会话存在。

`night_untraded_padding` 闸门（8-19 修的那个计数器）**又一次起作用**：它没把「零成交」
默认成「没有夜盘」，而是老实抛成歧义。

### 3. 🟠 两条 `empirical_boundary`：代表合约错过开盘首分钟（2 条）

```text
2020-05-19 SHFE AG  AG2012.SHF  night_traded_first: session_rule_time: invalid night label '21:01'
2020-05-26 CZCE FG  FG009.CZC   night_traded_first: session_rule_time: invalid night label '21:02'
```

`night_label_to_offset` 要求分钟落在 15 分钟网格（`minute % 15`），21:01 / 21:02 直接判非法。
查实两例同型——**会话确实 21:00 开，只是代表合约在 21:00 那根 K 线上没成交**：

| 日期 | 代表合约 | 它的首笔 | 同品种 21:00 就有成交的合约 |
|---|---|---|---|
| 2020-05-19 | AG2012 | 21:01（21:00 那根 volume=0） | AG2009、AG2010 |
| 2020-05-26 | FG2009 | 21:02 | FG2008、FG2101、FG2010、FG2007、FG2011 |

### 三件事其实是**同一个设计问题**

第 2 条和第 3 条——AL 全零、AG 21:01、FG 21:02——**根子完全一样**：
**夜盘边界只由「代表合约」一个合约作证**。而会话是**品种/交易所级的事实**，
同品种任一合约在 21:00 成交就足以证明会话 21:00 开了。

`3920c91`（NI 2022-03-10）已经沿这个方向修过一次，但它只按**日线成交量**把「当日停牌」的合约
排到后面。AL2002 日盘成交 13.6 万手排第一，那次的修法够不着夜盘维度。

**建议（请拍）**：把夜盘边界的观测口径从「代表合约」放宽到「该品种当夜有成交的全部合约的并集」——
取并集里最早的成交分钟作 `night_start`、最晚作 `night_end`。这样：
- AL 2019-12-23 由 AL2003 作证，正常授权，**不需要权威行也不需要补数据**
- AG / FG 两例的 `night_start` 回到 21:00，`empirical_boundary` 消失
- fail-closed 性质不变：全品种当夜都无成交时并集仍为空，闸门照常触发

代价是采集侧要多查该品种的其它合约（分钟表按 `symbol` 裸列走索引，成本可控）。
**这是采集语义的改动，我不自作主张，等你拍。**

## 顺带：批次 F 的决策 ② 已被经验判定

批次 F 待你拍的第 ② 件是「DCE C/CS/L/PP/V 的区间右端写到 2019-03-29（超窗口、无经验佐证）
照写还是先截断」。本次 2018 段与 2019 段的观测**精确到天地支持了照写（路 A）**：

- **2018 全年**：C / L / PP / V 各 **242 天**（窗口内每一个交易日）全是 `none,none`，**零反例**
- **2019**：`none,none` 从 01-03 连续到 **2019-03-29** 为止；**2019-04-01 起就有夜盘了**，
  此后只在节后首日再出现

即 `effective_end = 2019-03-29` 一天不差，与 DCE 官方回顾材料所述的
「L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘」完全一致。这段本来「无经验佐证」的区间，
现在有了。**决策 ② 建议选路 A。**

（CS 只在 2018 出现 85 天、2019 未进池，属流动性池进出，不构成反例。）

## 登记表要改的一处

**INE 2018 的 `rows_derived=5` 应改为 2。** 登记表写「只派生 2018-03-26 夜盘品种上市后的
五个前夕」（04-09、05-02、06-19、09-25、10-08），但 SC 2018-03-26 上市后要满 120 天流动性
窗口才进池，实测**只有 09-25 和 10-08 两个目标日有 INE 产品日可消费**。

这不是记账问题：`relevant_keys` 按 `trade_date in loaded_dates` 判空转，**全量运行时
多派生的那 3 行会以 `session_exception_unconsumed` 挡住发布**。所以批次 G 只派生 2 行。

## 残余与硬阻塞的最新账

加入批次 F + 批次 G 之后，**全历史 2011-01-01 → 2026-04-29 的残余**是：

| 残余 | 条数 | 性质 | 挡发布？ |
|---|---:|---|---|
| 2017-03-31 DCE、2017-04-19 CZCE 整所零成交夜 | 15 | 缺公告，性质未定 | 是 |
| 2019-12-26 SHFE/CZCE/INE 延迟夜盘 | 22 | 缺来源 **＋ schema 表达不了** | 是 |
| 2019-12-23 SHFE AL 归档缺陷 | 1 | 已查实；采集口径改动可解 | 是 |
| 2020-05-19 AG、2020-05-26 FG 代表合约首笔偏移 | 2 | 采集口径改动可解 | 是 |
| 2022-03-10 SHFE NI | 1 | `3920c91` 已修，旧清单未重跑 | 否 |
| 2018/2019/2020-01-02 的 5 个 product-day 缺分钟 | — | 缺数据，采购中 | 是 |

**硬阻塞从两个变成三个**（按可解难度排序）：

1. **采集口径**（2 + 1 = 3 条）—— 只要你拍板放宽夜盘边界的观测口径即可解，**不需要外部资源**
2. **缺来源**（15 + 22 = 37 条）—— 需要你在浏览器取四份公告原文
3. **缺数据**（5 个 product-day）—— 需要外部采购

好消息是第 1 类**完全在项目内部**，第 2 类的 22 条是本次新增但与第一类的 15 条同性质、
可以一次出行一起办。

## 待写入的 304 行（`config/carry_minute_session_exceptions.csv`）

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
CZCE,commodity-v1,2018-02-22,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-02-14,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-02-22,none,none,no night session before the post-holiday session notice_evening=2018-02-14,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
SHFE,commodity-v1,2018-02-22,none,none,no night session before the post-holiday session notice_evening=2018-02-14,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2018-04-09,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-04-04,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-04-09,none,none,no night session before the post-holiday session notice_evening=2018-04-04,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
SHFE,commodity-v1,2018-04-09,none,none,no night session before the post-holiday session notice_evening=2018-04-04,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2018-05-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-04-27,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-05-02,none,none,no night session before the post-holiday session notice_evening=2018-04-27,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
SHFE,commodity-v1,2018-05-02,none,none,no night session before the post-holiday session notice_evening=2018-04-27,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2018-06-19,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-06-15,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-06-19,none,none,no night session before the post-holiday session notice_evening=2018-06-15,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
SHFE,commodity-v1,2018-06-19,none,none,no night session before the post-holiday session notice_evening=2018-06-15,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2018-09-25,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-09-21,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-09-25,none,none,no night session before the post-holiday session notice_evening=2018-09-21,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
INE,commodity-v1,2018-09-25,none,none,no night session before the post-holiday session notice_evening=2018-09-21,https://www.ine.cn/publicnotice/notice/201803/t20180314_811180.html
SHFE,commodity-v1,2018-09-25,none,none,no night session before the post-holiday session notice_evening=2018-09-21,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2018-10-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2018-09-28,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-10-08,none,none,no night session before the post-holiday session notice_evening=2018-09-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
INE,commodity-v1,2018-10-08,none,none,no night session before the post-holiday session notice_evening=2018-09-28,https://www.ine.cn/publicnotice/notice/201803/t20180314_811180.html
SHFE,commodity-v1,2018-10-08,none,none,no night session before the post-holiday session notice_evening=2018-09-28,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2019-02-11,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-02-01,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-02-11,none,none,no night session before the post-holiday session notice_evening=2019-02-01,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-02-11,none,none,no night session before the post-holiday session notice_evening=2019-02-01,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-02-11,none,none,no night session before the post-holiday session notice_evening=2019-02-01,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2019-04-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-04-04,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-04-08,none,none,no night session before the post-holiday session notice_evening=2019-04-04,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-04-08,none,none,no night session before the post-holiday session notice_evening=2019-04-04,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-04-08,none,none,no night session before the post-holiday session notice_evening=2019-04-04,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2019-05-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-04-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-05-06,none,none,no night session before the post-holiday session notice_evening=2019-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6159945/index.html
INE,commodity-v1,2019-05-06,none,none,no night session before the post-holiday session notice_evening=2019-04-30,https://www.ine.cn/publicnotice/notice/201904/t20190415_811704.html
SHFE,commodity-v1,2019-05-06,none,none,no night session before the post-holiday session notice_evening=2019-04-30,https://www.shfe.com.cn/publicnotice/notice/201904/t20190415_794430.html
CZCE,commodity-v1,2019-06-10,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-06-06,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-06-10,none,none,no night session before the post-holiday session notice_evening=2019-06-06,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-06-10,none,none,no night session before the post-holiday session notice_evening=2019-06-06,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-06-10,none,none,no night session before the post-holiday session notice_evening=2019-06-06,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2019-09-16,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-09-12,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-09-16,none,none,no night session before the post-holiday session notice_evening=2019-09-12,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-09-16,none,none,no night session before the post-holiday session notice_evening=2019-09-12,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-09-16,none,none,no night session before the post-holiday session notice_evening=2019-09-12,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2019-10-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2019-10-08,none,none,no night session before the post-holiday session notice_evening=2019-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-10-08,none,none,no night session before the post-holiday session notice_evening=2019-09-30,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-10-08,none,none,no night session before the post-holiday session notice_evening=2019-09-30,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2020-02-03,none,none,"no night session on the first trading day after a statutory holiday per the exchange standing rule, spring festival reopening revised to 2020-02-03 notice_evening=2020-01-23",https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2020-02-03,none,none,"no night session before the post-holiday session, spring festival reopening revised to 2020-02-03 notice_evening=2020-01-23",http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6201818/index.html
INE,commodity-v1,2020-02-03,none,none,"no night session before the post-holiday session, spring festival reopening revised to 2020-02-03 notice_evening=2020-01-23",https://www.ine.cn/publicnotice/notice/201912/t20191224_812027.html
SHFE,commodity-v1,2020-02-03,none,none,"no night session before the post-holiday session, spring festival reopening revised to 2020-02-03 notice_evening=2020-01-23",https://www.shfe.com.cn/publicnotice/notice/201912/t20191224_795447.html
CZCE,commodity-v1,2020-02-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-03,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-03,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-03,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-04,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-04,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-04,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-04,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-05,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-05,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-05,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-05,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-06,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-06,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-06,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-06,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-07,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-07,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-07,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-07,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-10,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-10,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-10,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-10,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-11,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-11,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-11,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-11,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-12,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-12,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-12,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-12,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-13,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-13,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-13,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-13,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-14,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-14,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-14,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-14,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-17,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-17,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-17,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-17,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-18,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-18,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-18,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-18,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-19,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-19,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-19,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-19,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-20,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-20,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-20,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-20,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-21,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-21,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-21,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-21,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-24,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-24,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-24,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-24,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-25,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-25,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-25,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-25,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-26,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-26,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-26,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-26,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-02-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-27,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-02-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-27,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-02-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-27,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-02-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-27,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-28,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-28,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-02-28,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-02,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-02,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-02,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-02,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-03,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-03,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-04,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-03,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-04,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-04,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-04,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-05,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-04,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-05,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-05,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-05,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-05,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-06,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-06,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-06,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-06,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-09,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-09,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-09,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-09,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-10,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-10,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-10,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-11,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-10,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-11,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-11,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-11,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-12,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-11,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-12,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-12,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-12,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-12,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-13,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-13,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-13,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-13,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-16,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-16,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-16,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-16,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-17,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-17,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-17,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-18,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-17,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-18,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-18,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-18,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-19,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-18,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-19,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-19,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-19,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-19,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-20,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-20,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-20,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-20,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-23,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-23,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-23,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-23,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-24,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-24,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-24,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-25,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-24,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-25,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-25,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-25,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-26,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-25,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-26,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-26,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-26,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-26,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-27,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-27,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-27,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-27,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-03-31,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-30,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-03-31,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-03-31,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-30,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-03-31,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-30,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-01,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-31,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-01,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-01,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-31,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-01,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-03-31,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-01,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-01,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-01,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-02,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-01,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-02,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-02,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-02,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-03,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-02,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-03,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-03,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-07,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-03,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-08,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-07,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-08,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-07,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-08,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-07,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-08,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-07,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-08,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-08,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-08,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-09,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-08,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-09,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-09,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-09,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-10,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-09,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-10,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-10,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-10,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-13,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-10,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-13,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-13,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-13,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-14,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-13,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-15,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-14,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-15,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-14,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-15,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-14,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-15,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-14,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-15,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-15,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-15,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-16,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-15,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-16,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-16,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-16,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-17,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-16,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-17,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-17,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-17,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-20,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-17,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-20,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-20,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-20,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-21,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-20,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-22,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-21,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-22,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-21,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-22,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-21,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-22,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-21,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-22,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-22,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-22,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-23,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-22,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-23,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-23,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-23,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-24,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-23,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-24,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-24,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-24,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-27,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-24,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-27,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-27,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-27,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-28,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-27,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-29,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-28,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-29,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-29,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-28,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-29,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-28,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-04-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-29,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-04-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-04-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-29,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-04-30,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-29,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-05-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-30,http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm
DCE,commodity-v1,2020-05-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html
INE,commodity-v1,2020-05-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-30,https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html
SHFE,commodity-v1,2020-05-06,none,none,no night session during the exchange-wide suspension announced for 2020-02-03 evening notice_evening=2020-04-30,https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html
CZCE,commodity-v1,2020-06-29,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2020-06-24,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2020-06-29,none,none,no night session before the post-holiday session notice_evening=2020-06-24,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6201818/index.html
INE,commodity-v1,2020-06-29,none,none,no night session before the post-holiday session notice_evening=2020-06-24,https://www.ine.cn/publicnotice/notice/201912/t20191224_812027.html
SHFE,commodity-v1,2020-06-29,none,none,no night session before the post-holiday session notice_evening=2020-06-24,https://www.shfe.com.cn/publicnotice/notice/201912/t20191224_795447.html
```
