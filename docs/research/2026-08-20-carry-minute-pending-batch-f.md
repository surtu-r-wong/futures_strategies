# 待审批次 F：2011–2017 补全（2026-08-20）

> **这不是权威资产。** 与批次 A/C/D/E 同样，每行须经用户过目才允许写入
> `config/carry_minute_day_only_regimes.csv` 与 `config/carry_minute_session_exceptions.csv`。

## 这批从哪来

2026-08-20 实测发现，5 个缺分钟的 product-day 全在 **2018-01-02 / 2019-01-02 / 2020-01-02**，
窗口避开就能跑。于是跑了一次有界采集：

```text
--start 2011-01-01 --end 2017-12-31 --backtest-start 2013-01-01
products=34  checked_days=36579  ambiguous=17511  publication_status=blocked
```

**完整跑完，没有在缺分钟上 fail-closed。** 17,511 条歧义**全部**是 `none,none`，
**零条不规则时段** —— 不存在 2019-12-26 那种意外。

## 候选行：25 条区间 + 74 条例外 = 99 行

| 分组 | 行数 | 解决 |
|---|---:|---:|
| F-1『上市前无夜盘』区间 | 25 | 16,984 |
| F-2『节前无夜盘』例外 | 74 | 512 |
| **残余** | — | **15** |

残余 15 条正是两个整所零成交夜（见文末），**不能由权威行解决**。

## 三方交叉：16/25 精确到天吻合，零冲突

把每个品种的**经验末日**（清单里最后一个非节前 `none,none` 日）与**登记表的夜盘上线日**对表：

- **16 个品种逐日吻合**：AU/AG 2013-07-05、CU/ZN 2013-12-20、RB/RU 2014-12-26、
  CF/RM/SR/TA 2014-12-12、FG 2015-06-11、A/I/JM/M/Y 2014-12-26、J/P 2014-07-04
- **9 个品种经验早于登记**：入池中断所致（AL 2012-02-22 vs 2013-12-20 等），不构成矛盾
- **0 个品种经验晚于登记**

这反过来**验证了登记表那批 `session_launch` 行**，包括两条只有 `reviewed_corroborative` 的。

由此坐实了区间右端的口径：通知里的「自 X 日起开展夜盘」中的 X 是**当晚**，
所以 `effective_end = X`（X 当天的夜盘属于前一晚，仍无夜盘）。CZCE 的
`notice_evening=2015-06-11 目标交易日 2015-06-12` 是同一件事的显式写法。

## ⚠️ 要你判断的三件事

### 1. 🔴 两个整所零成交夜（15 条，我解决不了）

| 交易日 | 熄火的所 | 同夜其它所 |
|---|---|---|
| 2017-03-31（前夜 03-30） | **DCE** 全部 7 个受审品种零成交 | SHFE 4,364,772 手、CZCE 2,300,458 手，正常 |
| 2017-04-19（前夜 04-18） | **CZCE** 全部 8 个受审品种零成交 | SHFE 3,718,732 手、DCE 2,567,862 手，正常 |

两晚都**不是节前**。归档 K 线**补齐完整**（根数与对照夜一模一样），只是 volume 全 0；
对照夜同样合约有量比例 87–96%。横向对照排除了「全市场休市」与「归档整段缺失」。

**两种解释，需公告裁决**：该所当晚真的没有夜盘，或供应商丢了该所那一夜的成交而照常补 K 线。
**不得凭空断言**——性质与 NI 2022-03-10 相反（那次是单合约停牌被选作代表，公告可查）。
DCE 与 CZCE 站内页均 412，需在浏览器取。

**影响**：这 15 条会和 5 个 product-day 一起挡住全量资产。阻塞项从一个变成两个。

### 2. 🟠 5 条区间行的右端超出本次窗口

DCE 的 **C / CS / L / PP / V** 夜盘上线日是 2019-03-29，而本次采集止于 2017-12-31。
依据是登记表那份 DCE 官方回顾材料（`reviewed_direct`，`rows_derived=22`），来源没问题，
但 **2018-01 → 2019-03 这段没有经验佐证**。

- **路 A**：照写到 2019-03-29。日后 2018/2019 分段采集会自然验证它——若那段里这几个品种
  真开了夜盘，观测不是 `none,none`，规则 1 照炸。
- **路 B**：先截到 2017-12-29，等分段采集跑完再延。多一轮改动，但每一行都有经验背书。

### 3. 🟠 9 条区间行的来源不是 `reviewed_direct`

| 来源状态 | 行数 | 品种 | 经验佐证 |
|---|---:|---|---|
| `reviewed_corroborative` | 5 | SHFE AL/CU/ZN、RB/RU | CU/ZN/RB/RU **逐日吻合**；AL 入池中断 |
| `reviewed_pair_required` | 4 | CZCE CF/RM/SR/TA | 四个**全部逐日吻合** |

登记表对这两条来源的保留意见是「尚未恢复原通知」与「需与 23:30 时段材料配对」。
但本次经验交叉验证给了它们独立支撑：**8/9 精确到天**。
是否据此收下（并把登记表状态升为 `reviewed_corroborative_plus_empirical`），请拍板。

## 陈述项（不必拍，但请知悉）

- **`effective_start` 取 `futures_daily` 实测上市日**，与批次 C 同一口径。25 条里 **16 条早于
  2011-01-01**，最早 CU 的 1995-04-17。这些区间陈述的是真话（那时全市场都没有夜盘）。
- **CZCE 的 21 条例外行走「常设规则 ＋ 三所并集」**，2026-08-19 已裁决，此处不重开。
  另 53 条来自各所年度公告，登记表逐日列明。
- **来源缺口 0，空转行 0**：74 条例外行每条都被至少一个产品日消费。

## 机器侧核对（2026-08-20）

```text
批次 F 候选行                          25 区间 + 74 例外
来源缺口                                0
223 行例外的 notice_evening → trade_date  全部通过（futures_daily 真实日历）
2011-2017 重放  resolved=17,496  residual=15
本窗口未被消费的批次 F 例外行            0
```

## F-1『上市前无夜盘』区间（写 config/carry_minute_day_only_regimes.csv）

```csv
version,exchange,product,effective_start,effective_end,reason,source_url
commodity-v1,CZCE,CF,2004-06-01,2014-12-12,no night session before the product joined night trading: SR/CF/RM/MA/TA 自 2014-12-12 晚开展夜盘,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,FG,2012-12-03,2015-06-11,no night session before the product joined night trading: OI/FG/ZC 自 notice_evening=2015-06-11 起开展夜盘，目标交易日 2015-06-12,https://www.czce.com.cn/cn/rootfiles/2015/05/27/1431080885614168-1431080885616494.pdf
commodity-v1,CZCE,OI,2012-07-16,2015-06-11,no night session before the product joined night trading: OI/FG/ZC 自 notice_evening=2015-06-11 起开展夜盘，目标交易日 2015-06-12,https://www.czce.com.cn/cn/rootfiles/2015/05/27/1431080885614168-1431080885616494.pdf
commodity-v1,CZCE,RM,2012-12-28,2014-12-12,no night session before the product joined night trading: SR/CF/RM/MA/TA 自 2014-12-12 晚开展夜盘,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,SR,2006-01-06,2014-12-12,no night session before the product joined night trading: SR/CF/RM/MA/TA 自 2014-12-12 晚开展夜盘,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,TA,2006-12-18,2014-12-12,no night session before the product joined night trading: SR/CF/RM/MA/TA 自 2014-12-12 晚开展夜盘,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,DCE,A,1999-01-04,2014-12-26,no night session before the product joined night trading: A/B/M/Y/JM/I 自 2014-12-26 晚加入夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,C,2004-09-22,2019-03-29,no night session before the product joined night trading: L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,CS,2014-12-19,2019-03-29,no night session before the product joined night trading: L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,I,2013-10-18,2014-12-26,no night session before the product joined night trading: A/B/M/Y/JM/I 自 2014-12-26 晚加入夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,J,2011-04-15,2014-07-04,no night session before the product joined night trading: P/J 为大商所首批夜盘品种，自 2014-07-04 晚起,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,JM,2013-03-22,2014-12-26,no night session before the product joined night trading: A/B/M/Y/JM/I 自 2014-12-26 晚加入夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,L,2007-07-31,2019-03-29,no night session before the product joined night trading: L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,M,2000-07-17,2014-12-26,no night session before the product joined night trading: A/B/M/Y/JM/I 自 2014-12-26 晚加入夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,P,2007-10-29,2014-07-04,no night session before the product joined night trading: P/J 为大商所首批夜盘品种，自 2014-07-04 晚起,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,PP,2014-02-28,2019-03-29,no night session before the product joined night trading: L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,V,2009-05-25,2019-03-29,no night session before the product joined night trading: L/V/PP/EG/C/CS 自 2019-03-29 晚起新增夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,DCE,Y,2006-01-09,2014-12-26,no night session before the product joined night trading: A/B/M/Y/JM/I 自 2014-12-26 晚加入夜盘,https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf
commodity-v1,SHFE,AG,2012-05-10,2013-07-05,no night session before the product joined night trading: AU/AG 自 2013-07-05 晚开展连续交易 21:00-02:30,https://www.shfe.com.cn/publicnotice/notice/201306/t20130607_789661.html
commodity-v1,SHFE,AL,1995-04-17,2013-12-20,no night session before the product joined night trading: CU/AL/ZN/PB 自 2013-12-20 晚开展连续交易 21:00-01:00,https://www.shfe.com.cn/docview/docview_310288380.htm
commodity-v1,SHFE,AU,2008-01-09,2013-07-05,no night session before the product joined night trading: AU/AG 自 2013-07-05 晚开展连续交易 21:00-02:30,https://www.shfe.com.cn/publicnotice/notice/201306/t20130607_789661.html
commodity-v1,SHFE,CU,1995-04-17,2013-12-20,no night session before the product joined night trading: CU/AL/ZN/PB 自 2013-12-20 晚开展连续交易 21:00-01:00,https://www.shfe.com.cn/docview/docview_310288380.htm
commodity-v1,SHFE,RB,2009-03-27,2014-12-26,no night session before the product joined night trading: RB/HC/BU 自 2014-12-26 晚为 21:00-01:00，RU 为 21:00-23:00,https://www.shfe.com.cn/content/lxjy/mtbd6.html
commodity-v1,SHFE,RU,1995-05-16,2014-12-26,no night session before the product joined night trading: RB/HC/BU 自 2014-12-26 晚为 21:00-01:00，RU 为 21:00-23:00,https://www.shfe.com.cn/content/lxjy/mtbd6.html
commodity-v1,SHFE,ZN,2007-03-26,2013-12-20,no night session before the product joined night trading: CU/AL/ZN/PB 自 2013-12-20 晚开展连续交易 21:00-01:00,https://www.shfe.com.cn/docview/docview_310288380.htm
```

## F-2『节前无夜盘』例外（写 config/carry_minute_session_exceptions.csv）

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
SHFE,commodity-v1,2013-09-23,none,none,no night session before the post-holiday session notice_evening=2013-09-18,https://www.shfe.com.cn/publicnotice/notice/201309/t20130910_789836.html
SHFE,commodity-v1,2013-10-08,none,none,no night session before the post-holiday session notice_evening=2013-09-30,https://www.shfe.com.cn/publicnotice/notice/201309/t20130910_789836.html
SHFE,commodity-v1,2014-01-02,none,none,no night session before the post-holiday session notice_evening=2013-12-31,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
SHFE,commodity-v1,2014-02-07,none,none,no night session before the post-holiday session notice_evening=2014-01-30,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
SHFE,commodity-v1,2014-04-08,none,none,no night session before the post-holiday session notice_evening=2014-04-04,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
SHFE,commodity-v1,2014-05-05,none,none,no night session before the post-holiday session notice_evening=2014-04-30,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
SHFE,commodity-v1,2014-06-03,none,none,no night session before the post-holiday session notice_evening=2014-05-30,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
DCE,commodity-v1,2014-09-09,none,none,no night session before the post-holiday session notice_evening=2014-09-05,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/1988119/index.html
SHFE,commodity-v1,2014-09-09,none,none,no night session before the post-holiday session notice_evening=2014-09-05,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
DCE,commodity-v1,2014-10-08,none,none,no night session before the post-holiday session notice_evening=2014-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/1987919/index.html
SHFE,commodity-v1,2014-10-08,none,none,no night session before the post-holiday session notice_evening=2014-09-30,https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html
CZCE,commodity-v1,2015-01-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2014-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-01-05,none,none,no night session before the post-holiday session notice_evening=2014-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-01-05,none,none,no night session before the post-holiday session notice_evening=2014-12-31,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-02-25,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-02-17,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-02-25,none,none,no night session before the post-holiday session notice_evening=2015-02-17,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-02-25,none,none,no night session before the post-holiday session notice_evening=2015-02-17,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-04-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-04-03,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-04-07,none,none,no night session before the post-holiday session notice_evening=2015-04-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-04-07,none,none,no night session before the post-holiday session notice_evening=2015-04-03,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-05-04,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-04-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-05-04,none,none,no night session before the post-holiday session notice_evening=2015-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-05-04,none,none,no night session before the post-holiday session notice_evening=2015-04-30,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-06-23,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-06-19,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-06-23,none,none,no night session before the post-holiday session notice_evening=2015-06-19,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-06-23,none,none,no night session before the post-holiday session notice_evening=2015-06-19,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-09-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-09-02,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-09-07,none,none,no night session before the post-holiday session notice_evening=2015-09-02,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5011746/index.html
SHFE,commodity-v1,2015-09-07,none,none,no night session before the post-holiday session notice_evening=2015-09-02,https://www.shfe.com.cn/publicnotice/notice/201508/t20150825_791123.html
CZCE,commodity-v1,2015-09-28,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-09-25,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-09-28,none,none,no night session before the post-holiday session notice_evening=2015-09-25,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-09-28,none,none,no night session before the post-holiday session notice_evening=2015-09-25,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2015-10-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2015-10-08,none,none,no night session before the post-holiday session notice_evening=2015-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html
SHFE,commodity-v1,2015-10-08,none,none,no night session before the post-holiday session notice_evening=2015-09-30,https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html
CZCE,commodity-v1,2016-01-04,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2015-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-01-04,none,none,no night session before the post-holiday session notice_evening=2015-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-01-04,none,none,no night session before the post-holiday session notice_evening=2015-12-31,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-02-15,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-02-05,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-02-15,none,none,no night session before the post-holiday session notice_evening=2016-02-05,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-02-15,none,none,no night session before the post-holiday session notice_evening=2016-02-05,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-04-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-04-01,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-04-05,none,none,no night session before the post-holiday session notice_evening=2016-04-01,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-04-05,none,none,no night session before the post-holiday session notice_evening=2016-04-01,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-05-03,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-04-29,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-05-03,none,none,no night session before the post-holiday session notice_evening=2016-04-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-05-03,none,none,no night session before the post-holiday session notice_evening=2016-04-29,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-06-13,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-06-08,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-06-13,none,none,no night session before the post-holiday session notice_evening=2016-06-08,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-06-13,none,none,no night session before the post-holiday session notice_evening=2016-06-08,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-09-19,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-09-14,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-09-19,none,none,no night session before the post-holiday session notice_evening=2016-09-14,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-09-19,none,none,no night session before the post-holiday session notice_evening=2016-09-14,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2016-10-10,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2016-10-10,none,none,no night session before the post-holiday session notice_evening=2016-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html
SHFE,commodity-v1,2016-10-10,none,none,no night session before the post-holiday session notice_evening=2016-09-30,https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html
CZCE,commodity-v1,2017-01-03,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2016-12-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-01-03,none,none,no night session before the post-holiday session notice_evening=2016-12-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-01-03,none,none,no night session before the post-holiday session notice_evening=2016-12-30,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
CZCE,commodity-v1,2017-02-03,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-01-26,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-02-03,none,none,no night session before the post-holiday session notice_evening=2017-01-26,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-02-03,none,none,no night session before the post-holiday session notice_evening=2017-01-26,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
CZCE,commodity-v1,2017-04-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-03-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-04-05,none,none,no night session before the post-holiday session notice_evening=2017-03-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-04-05,none,none,no night session before the post-holiday session notice_evening=2017-03-31,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
CZCE,commodity-v1,2017-05-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-04-28,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-05-02,none,none,no night session before the post-holiday session notice_evening=2017-04-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-05-02,none,none,no night session before the post-holiday session notice_evening=2017-04-28,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
CZCE,commodity-v1,2017-05-31,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-05-26,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-05-31,none,none,no night session before the post-holiday session notice_evening=2017-05-26,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-05-31,none,none,no night session before the post-holiday session notice_evening=2017-05-26,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
CZCE,commodity-v1,2017-10-09,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-09-29,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2017-10-09,none,none,no night session before the post-holiday session notice_evening=2017-09-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html
SHFE,commodity-v1,2017-10-09,none,none,no night session before the post-holiday session notice_evening=2017-09-29,https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html
```
