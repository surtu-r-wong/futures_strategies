# 待审批次 C / D（2026-08-19 生成）

> **这不是权威资产。** `config/carry_minute_day_only_regimes.csv` 与
> `config/carry_minute_session_exceptions.csv` 仍是零数据行的纯表头。
> 本文是**提请复核的候选行**，每行须经用户逐行过目才允许写入。

来源：`full2_inventory.csv`（2020-07-01 → 2026-04-29 采集，11,861 条），
窗口右端取 **2026-01-31**，以甩掉 `futures_daily` 2026-03 空洞造成的 119 条噪声
（见 `docs/research/2026-08-19-carry-minute-multiyear-inventory.md`）。

## ⚠️ 定稿前必须修正的三处（我已发现，尚未修）

1. **批次 C 的 `effective_start` 取的是「入池首日」，不是品种上市日。** 例如 CZCE.AP 写成
   2020-07-01（采集窗口左端）。日盘制度自品种上市即成立，全历史采集会漏。
   定稿前应改为该品种在 `futures_daily` 的首个交易日。
2. **批次 C 中 DCE 的 `source_url` 在生成时被截断。** 正确 URL 是
   `https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf`
   （下方 CSV 里仍是截断值，需替换）。
3. **`SHFE.NI 2022-03-10` 尚无经核实的官方来源。** 网络搜索给出的
   `https://www.shfe.com.cn/news/notice/911341410.html` 实测 **HTTP 404**，不可采用。
   上期所站点本身可机读（首页 200），需要一个能打开的真实公告 URL。

## 批次 C：日盘 only 品种（写 config/carry_minute_day_only_regimes.csv）

```csv
version,exchange,product,effective_start,effective_end,reason,source_url
commodity-v1,CZCE,AP,2020-07-01,,day-only product: AP 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,CJ,2021-08-19,,day-only product: CJ 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,PK,2022-02-18,,day-only product: PK 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,SF,2021-01-14,,day-only product: SF 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,SM,2020-07-01,,day-only product: SM 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,CZCE,UR,2021-04-12,,day-only product: UR 不在郑商所夜盘上线通知逐批列明获得夜盘的品种（2014-12-12 SR/CF/RM/MA/TA；2015-06-12 OI/FG/ZC；CY；SA；PF；SH/PX；PR）,https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf
commodity-v1,DCE,JD,2020-07-01,,day-only product: JD 不在大商所夜盘通知逐批列明获得夜盘的品种（2014-07-04 P/J；2014-12-26 A/B/M/Y/JM/I；2019-03-29 新增 L/V/PP/EG/C/CS）,https://www.dce.com.cn/dalianshangpin/resource/cms/2016/07/
commodity-v1,DCE,LH,2021-07-09,,day-only product: LH 不在大商所夜盘通知逐批列明获得夜盘的品种（2014-07-04 P/J；2014-12-26 A/B/M/Y/JM/I；2019-03-29 新增 L/V/PP/EG/C/CS）,https://www.dce.com.cn/dalianshangpin/resource/cms/2016/07/
commodity-v1,GFEX,LC,2024-01-16,,day-only product: 上市通知列明三个日盘小节,https://www.gfex.com.cn/gfex/tzts/202307/33f2a342d80f4ee69966df4a554c26a4.shtml
commodity-v1,GFEX,PS,2025-06-30,,day-only product: 上市通知列明三个日盘小节,https://www.gfex.com.cn/gfex/tzts/202412/34bc2f9dbfc34b4b81e1a043ff526589.shtml
commodity-v1,GFEX,SI,2023-06-26,,day-only product: 上市通知列明三个日盘小节,https://www.gfex.com.cn/gfex/tzts/202212/44ccfcb613e442658c8ac94861e0de18.shtml
commodity-v1,INE,EC,2024-02-21,,day-only product: 上市通知完整枚举 09:00-10:15、10:30-11:30、13:30-15:00 三个日盘小节,https://www.ine.cn/publicnotice/notice/202308/t20230811_814262.html
```

| 交易所 | 品种 | 冲突天数 | 首日 | 末日 | 证据强度 |
|---|---|---:|---|---|---|
| CZCE | AP | 1358 | 2020-07-01 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| CZCE | CJ | 293 | 2021-08-19 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| CZCE | PK | 551 | 2022-02-18 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| CZCE | SF | 1143 | 2021-01-14 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| CZCE | SM | 1358 | 2020-07-01 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| CZCE | UR | 1058 | 2021-04-12 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| DCE | JD | 1074 | 2020-07-01 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| DCE | LH | 1076 | 2021-07-09 | 2026-01-30 | 🟠 缺席证据（不在夜盘名单） |
| GFEX | LC | 495 | 2024-01-16 | 2026-01-30 | 🟡 上市通知列明日盘小节 |
| GFEX | PS | 147 | 2025-06-30 | 2026-01-30 | 🟡 上市通知列明日盘小节 |
| GFEX | SI | 634 | 2023-06-26 | 2026-01-30 | 🟡 上市通知列明日盘小节 |
| INE | EC | 427 | 2024-02-21 | 2025-11-21 | ✅ 已登记正面证据 |

## 批次 D：节前无夜盘（写 config/carry_minute_session_exceptions.csv）

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
CZCE,commodity-v1,2020-10-09,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2020-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2020-10-09,none,none,no night session before the post-holiday session notice_evening=2020-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6201818/index.html
INE,commodity-v1,2020-10-09,none,none,no night session before the post-holiday session notice_evening=2020-09-30,https://www.ine.cn/publicnotice/notice/201912/t20191224_812027.html
SHFE,commodity-v1,2020-10-09,none,none,no night session before the post-holiday session notice_evening=2020-09-30,https://www.shfe.com.cn/publicnotice/notice/201912/t20191224_795447.html
CZCE,commodity-v1,2021-01-04,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2020-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-01-04,none,none,no night session before the post-holiday session notice_evening=2020-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-01-04,none,none,no night session before the post-holiday session notice_evening=2020-12-31,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-01-04,none,none,no night session before the post-holiday session notice_evening=2020-12-31,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-02-18,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-02-10,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-02-18,none,none,no night session before the post-holiday session notice_evening=2021-02-10,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-02-18,none,none,no night session before the post-holiday session notice_evening=2021-02-10,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-02-18,none,none,no night session before the post-holiday session notice_evening=2021-02-10,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-04-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-04-02,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-04-06,none,none,no night session before the post-holiday session notice_evening=2021-04-02,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-04-06,none,none,no night session before the post-holiday session notice_evening=2021-04-02,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-04-06,none,none,no night session before the post-holiday session notice_evening=2021-04-02,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-05-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-04-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-05-06,none,none,no night session before the post-holiday session notice_evening=2021-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-05-06,none,none,no night session before the post-holiday session notice_evening=2021-04-30,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-05-06,none,none,no night session before the post-holiday session notice_evening=2021-04-30,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-06-15,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-06-11,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-06-15,none,none,no night session before the post-holiday session notice_evening=2021-06-11,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-06-15,none,none,no night session before the post-holiday session notice_evening=2021-06-11,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-06-15,none,none,no night session before the post-holiday session notice_evening=2021-06-11,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-09-22,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-09-17,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-09-22,none,none,no night session before the post-holiday session notice_evening=2021-09-17,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-09-22,none,none,no night session before the post-holiday session notice_evening=2021-09-17,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-09-22,none,none,no night session before the post-holiday session notice_evening=2021-09-17,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2021-10-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2021-10-08,none,none,no night session before the post-holiday session notice_evening=2021-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html
INE,commodity-v1,2021-10-08,none,none,no night session before the post-holiday session notice_evening=2021-09-30,https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html
SHFE,commodity-v1,2021-10-08,none,none,no night session before the post-holiday session notice_evening=2021-09-30,https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html
CZCE,commodity-v1,2022-01-04,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2021-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-01-04,none,none,no night session before the post-holiday session notice_evening=2021-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-01-04,none,none,no night session before the post-holiday session notice_evening=2021-12-31,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-01-04,none,none,no night session before the post-holiday session notice_evening=2021-12-31,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-02-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-01-28,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-02-07,none,none,no night session before the post-holiday session notice_evening=2022-01-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-02-07,none,none,no night session before the post-holiday session notice_evening=2022-01-28,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-02-07,none,none,no night session before the post-holiday session notice_evening=2022-01-28,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-04-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-04-01,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-04-06,none,none,no night session before the post-holiday session notice_evening=2022-04-01,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-04-06,none,none,no night session before the post-holiday session notice_evening=2022-04-01,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-04-06,none,none,no night session before the post-holiday session notice_evening=2022-04-01,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-05-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-04-29,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-05-05,none,none,no night session before the post-holiday session notice_evening=2022-04-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-05-05,none,none,no night session before the post-holiday session notice_evening=2022-04-29,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-05-05,none,none,no night session before the post-holiday session notice_evening=2022-04-29,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-06-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-06-02,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-06-06,none,none,no night session before the post-holiday session notice_evening=2022-06-02,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-06-06,none,none,no night session before the post-holiday session notice_evening=2022-06-02,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-06-06,none,none,no night session before the post-holiday session notice_evening=2022-06-02,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-09-13,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-09-09,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-09-13,none,none,no night session before the post-holiday session notice_evening=2022-09-09,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-09-13,none,none,no night session before the post-holiday session notice_evening=2022-09-09,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-09-13,none,none,no night session before the post-holiday session notice_evening=2022-09-09,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2022-10-10,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2022-10-10,none,none,no night session before the post-holiday session notice_evening=2022-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html
INE,commodity-v1,2022-10-10,none,none,no night session before the post-holiday session notice_evening=2022-09-30,https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html
SHFE,commodity-v1,2022-10-10,none,none,no night session before the post-holiday session notice_evening=2022-09-30,https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html
CZCE,commodity-v1,2023-01-03,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2022-12-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-01-03,none,none,no night session before the post-holiday session notice_evening=2022-12-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-01-03,none,none,no night session before the post-holiday session notice_evening=2022-12-30,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-01-03,none,none,no night session before the post-holiday session notice_evening=2022-12-30,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2023-01-30,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-01-20,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-01-30,none,none,no night session before the post-holiday session notice_evening=2023-01-20,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-01-30,none,none,no night session before the post-holiday session notice_evening=2023-01-20,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-01-30,none,none,no night session before the post-holiday session notice_evening=2023-01-20,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2023-04-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-04-04,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-04-06,none,none,no night session before the post-holiday session notice_evening=2023-04-04,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-04-06,none,none,no night session before the post-holiday session notice_evening=2023-04-04,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-04-06,none,none,no night session before the post-holiday session notice_evening=2023-04-04,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2023-05-04,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-04-28,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-05-04,none,none,no night session before the post-holiday session notice_evening=2023-04-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-05-04,none,none,no night session before the post-holiday session notice_evening=2023-04-28,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-05-04,none,none,no night session before the post-holiday session notice_evening=2023-04-28,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2023-06-26,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-06-21,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-06-26,none,none,no night session before the post-holiday session notice_evening=2023-06-21,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-06-26,none,none,no night session before the post-holiday session notice_evening=2023-06-21,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-06-26,none,none,no night session before the post-holiday session notice_evening=2023-06-21,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2023-10-09,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-09-28,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2023-10-09,none,none,no night session before the post-holiday session notice_evening=2023-09-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html
INE,commodity-v1,2023-10-09,none,none,no night session before the post-holiday session notice_evening=2023-09-28,https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html
SHFE,commodity-v1,2023-10-09,none,none,no night session before the post-holiday session notice_evening=2023-09-28,https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html
CZCE,commodity-v1,2024-01-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2023-12-29,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-01-02,none,none,no night session before the post-holiday session notice_evening=2023-12-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-01-02,none,none,no night session before the post-holiday session notice_evening=2023-12-29,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-01-02,none,none,no night session before the post-holiday session notice_evening=2023-12-29,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-02-19,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-02-08,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-02-19,none,none,no night session before the post-holiday session notice_evening=2024-02-08,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-02-19,none,none,no night session before the post-holiday session notice_evening=2024-02-08,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-02-19,none,none,no night session before the post-holiday session notice_evening=2024-02-08,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-04-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-04-03,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-04-08,none,none,no night session before the post-holiday session notice_evening=2024-04-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-04-08,none,none,no night session before the post-holiday session notice_evening=2024-04-03,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-04-08,none,none,no night session before the post-holiday session notice_evening=2024-04-03,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-05-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-04-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-05-06,none,none,no night session before the post-holiday session notice_evening=2024-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-05-06,none,none,no night session before the post-holiday session notice_evening=2024-04-30,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-05-06,none,none,no night session before the post-holiday session notice_evening=2024-04-30,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-06-11,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-06-07,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-06-11,none,none,no night session before the post-holiday session notice_evening=2024-06-07,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-06-11,none,none,no night session before the post-holiday session notice_evening=2024-06-07,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-06-11,none,none,no night session before the post-holiday session notice_evening=2024-06-07,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-09-18,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-09-13,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-09-18,none,none,no night session before the post-holiday session notice_evening=2024-09-13,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-09-18,none,none,no night session before the post-holiday session notice_evening=2024-09-13,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-09-18,none,none,no night session before the post-holiday session notice_evening=2024-09-13,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2024-10-08,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2024-10-08,none,none,no night session before the post-holiday session notice_evening=2024-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html
INE,commodity-v1,2024-10-08,none,none,no night session before the post-holiday session notice_evening=2024-09-30,https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html
SHFE,commodity-v1,2024-10-08,none,none,no night session before the post-holiday session notice_evening=2024-09-30,https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html
CZCE,commodity-v1,2025-01-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2024-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-01-02,none,none,no night session before the post-holiday session notice_evening=2024-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-01-02,none,none,no night session before the post-holiday session notice_evening=2024-12-31,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-01-02,none,none,no night session before the post-holiday session notice_evening=2024-12-31,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2025-02-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-01-27,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-02-05,none,none,no night session before the post-holiday session notice_evening=2025-01-27,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-02-05,none,none,no night session before the post-holiday session notice_evening=2025-01-27,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-02-05,none,none,no night session before the post-holiday session notice_evening=2025-01-27,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2025-04-07,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-04-03,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-04-07,none,none,no night session before the post-holiday session notice_evening=2025-04-03,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-04-07,none,none,no night session before the post-holiday session notice_evening=2025-04-03,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-04-07,none,none,no night session before the post-holiday session notice_evening=2025-04-03,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2025-05-06,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-04-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-05-06,none,none,no night session before the post-holiday session notice_evening=2025-04-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-05-06,none,none,no night session before the post-holiday session notice_evening=2025-04-30,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-05-06,none,none,no night session before the post-holiday session notice_evening=2025-04-30,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2025-06-03,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-05-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-06-03,none,none,no night session before the post-holiday session notice_evening=2025-05-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-06-03,none,none,no night session before the post-holiday session notice_evening=2025-05-30,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-06-03,none,none,no night session before the post-holiday session notice_evening=2025-05-30,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2025-10-09,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-09-30,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2025-10-09,none,none,no night session before the post-holiday session notice_evening=2025-09-30,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html
INE,commodity-v1,2025-10-09,none,none,no night session before the post-holiday session notice_evening=2025-09-30,https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html
SHFE,commodity-v1,2025-10-09,none,none,no night session before the post-holiday session notice_evening=2025-09-30,https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
CZCE,commodity-v1,2026-01-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2026-01-05,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2025-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
INE,commodity-v1,2026-01-05,none,none,no night session before the post-holiday session notice_evening=2025-12-31,https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html
SHFE,commodity-v1,2026-01-05,none,none,no night session before the post-holiday session notice_evening=2025-12-31,https://www.shfe.com.cn/services/calenderandholidays/holiday/
```

批次 D 行数：140；涉及 36 个目标交易日

### ⚠️ 无法映射到任何已登记前夕的日期（须人工处理）

- 2022-03-10 SHFE：1 个品种 ['NI']

### 不属于这两批的剩余歧义：1 条

- 2022-05-26 INE BC check=empirical_boundary

---

## 两条不属于这两批的残余（11,861 条里只剩这 2 条）

### 一、`SHFE.NI` 2022-03-10 —— 全库第二个真实异常时段，且是**单品种**

实测（伦镍逼空期间）：

```text
NI2205 2022-03-09 夜:  K线=179  有量=  0     ← 整夜零成交
CU2205 2022-03-09 夜:  K线=179  有量=179     ← 同夜正常
```

`SessionException` 的唯一键是 `(version, exchange, trade_date)`，**没有 product 列**，
表达不了单品种停盘。2026-08-17 定该 schema 时的依据是 DCE 553 号为全所口径，
当时不知道存在单品种案例。

**建议用 `day_only_regimes` 表达，零 schema 改动**：该表有 product 列与日期区间，
写一条 `effective_start = effective_end = 2022-03-10` 的 SHFE/NI 行即可，
`authorize_night_observation` 会先查 day-only 区间并放行 `none,none`。

⚠️ 语义上要接受一点妥协：该表原本表示「产品级**长期**日盘制度」，用它装单日停盘会模糊这层含义。
另一条路是给 `SessionException` 加 product 列（资产未发布，无迁移义务）。**请拍板。**

### 二、`INE.BC` 2022-05-26 —— 稀薄夜盘，非缺陷

21:00–21:04 为补齐空 K，21:05 才有首笔成交。时段确实开了，只是五分钟无人交易。
经验数据无法与「延迟开盘」区分（2019-12-25 结构相同、只是补齐更长），
故只能由权威裁决。详见 `docs/research/2026-08-19-carry-minute-multiyear-inventory.md`。

## 另有一处我引入的缺陷（待修）

`night_untraded_padding` 审计计数**恒为 0**，即使 NI 2022-03-10 这类「有 K 无量」确实发生。
原因是我把 note 的收集放在 `authorize_night_observation` **之后**，
授权一抛异常（该行成为 ambiguity）note 就丢了。**观测事实不应依赖授权结果**，
应把收集移到授权调用之前。
