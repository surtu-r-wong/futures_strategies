# 待审批次 G-3：三个元旦日补全（2026-08-21）

> **这不是权威资产。** 每行须经用户过目才允许写入 `config/carry_minute_session_exceptions.csv`。

## 这批从哪来

2018-01-02 / 2019-01-02 / 2020-01-02 从未落在任何采集窗口内 —— 批次 F 止于 2017-12-31、
批次 G 起于 2018-01-03，因为那三天正是 5 个缺分钟 product-day 所在日，采集会 fail-closed。

`145e943` 的授权缺席机制落地后，这三天**能采集了**。三个窄窗口全部跑通，
**5 条登记缺席全部被消费**：

```text
--start 2018-01-02 --end 2018-01-05   checked_days=136 ambiguous=46   absent_count=1
--start 2019-01-02 --end 2019-01-04   checked_days=105 ambiguous=40   absent_count=2
--start 2020-01-02 --end 2020-01-03   checked_days= 74 ambiguous=35   absent_count=2
```

## 候选行：11 条例外

| 目标交易日 | notice_evening | 覆盖的所 |
|---|---|---|
| 2018-01-02 | 2017-12-29 | CZCE DCE SHFE（INE 的 SC 2018-03-26 才上市） |
| 2019-01-02 | 2018-12-28 | CZCE DCE INE SHFE |
| 2020-01-02 | 2019-12-31 | CZCE DCE INE SHFE |

来源全部取自登记表既有的年度休市公告。**一处特别**：CZCE 2019-01-02 不走常设规则，
用登记表已收录的专门公告（`notice_evening=2018-12-28`；2019-01-02 晚恢复夜盘）。

## 机器侧核对

```text
权威 = 既有 + 批次 F(99) + 批次 G(304) + 2017 两条 + 本批 11 条   exceptions=540
540 行的 notice_evening → trade_date            全部通过
三个元旦日重放  resolved=97  residual=0
```

**全历史逐段重放**（同一重放器，真实仓库代码）：

| 清单 | resolved | residual |
|---|---:|---|
| 2011-2017 | 17,511 | **0** |
| 2018 | 1,215 | **0** |
| 2019 | 418 | 23 |
| 2020H1 | 2,174 | **0** |
| 2020-07 → 2026-04 | 11,740 | 1 |

**全历史只剩两处未解**：

1. **2019-12-26 的 22 条** —— 批次 H 已提出，依赖 schema 加 `product` 列
2. **2019-12-23 SHFE AL 1 条 ＋ AG/FG 两条 `empirical_boundary`** —— 依赖夜盘边界观测口径放宽
3. （2022-03-10 SHFE NI 那 1 条是 `3920c91` 已修、旧清单未重跑的产物，非真实残余）

## 待写入的 11 行

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
CZCE,commodity-v1,2018-01-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2017-12-29,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2018-01-02,none,none,no night session before the post-holiday session notice_evening=2017-12-29,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html
SHFE,commodity-v1,2018-01-02,none,none,no night session before the post-holiday session notice_evening=2017-12-29,https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html
CZCE,commodity-v1,2019-01-02,none,none,no night session before the post-holiday session per the CZCE notice notice_evening=2018-12-28,https://www.czce.com.cn/cn/rootfiles/2018/12/24/1545632831296256-1545632831311552.pdf
DCE,commodity-v1,2019-01-02,none,none,no night session before the post-holiday session notice_evening=2018-12-28,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html
INE,commodity-v1,2019-01-02,none,none,no night session before the post-holiday session notice_evening=2018-12-28,https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html
SHFE,commodity-v1,2019-01-02,none,none,no night session before the post-holiday session notice_evening=2018-12-28,https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html
CZCE,commodity-v1,2020-01-02,none,none,no night session on the first trading day after a statutory holiday per the exchange standing rule notice_evening=2019-12-31,https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf
DCE,commodity-v1,2020-01-02,none,none,no night session before the post-holiday session notice_evening=2019-12-31,http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6201818/index.html
INE,commodity-v1,2020-01-02,none,none,no night session before the post-holiday session notice_evening=2019-12-31,https://www.ine.cn/publicnotice/notice/201912/t20191224_812027.html
SHFE,commodity-v1,2020-01-02,none,none,no night session before the post-holiday session notice_evening=2019-12-31,https://www.shfe.com.cn/publicnotice/notice/201912/t20191224_795447.html
```
