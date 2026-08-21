# 授权缺席：让采集跳过看不见的日盘（2026-08-21）

用户 2026-08-21 拍板：数据商暂时补不上，**跳过这些日期并诚实记录**，未来数据到了再补。

> ⚠️ 用户建议里的「完全复制前一日行情来补位」**未采纳**。那会往归档写一天并未发生的行情，
> 回测会照常撮合、算 VWAP、结转盈亏，且下游无法分辨真假；复制来的一天价格零变动，
> 在 Carry 这种靠价差的策略里等于凭空多一天无风险收益。
> 本实现让**缺失保持缺失**，只是被具名登记 —— 资产说「这几个产品日我们没观测到」，
> 而不是「这几天长这样」。可审计、可回滚。

## 硬失败原来在哪

不在分类层。实跑 `--start 2018-01-02 --end 2018-01-05` 定位到：

```text
cta_carry/minute_pg_source.py:1227  _validate_boundary_frame
MinuteDataError: check='session_representative_missing_minutes'
  trade_date=2018-01-02 product='AL' contract='AL1803.SHF'
  reason='session representative has no minute observations in its target window'
```

AL1803 在 T=2018-01-02 的窗口 `[2017-12-29 21:00, 2018-01-02 15:01]` 内**一根 K 线都没有**：
2017-12-29 是元旦前夕本就无夜盘，日盘又整段缺失，于是整个窗口为空，**查询层先炸**，
分类层根本没跑到。

## 新资产

`config/carry_minute_absent_product_days.csv`，5 行：

```csv
version,exchange,product,trade_date,absent_segment,reason,source_url
commodity-v1,SHFE,AL,2018-01-02,day,...,docs/research/2026-08-21-minute-archive-data-request.md
commodity-v1,SHFE,CU,2019-01-02,day,...
commodity-v1,SHFE,RU,2019-01-02,day,...
commodity-v1,SHFE,AL,2020-01-02,day,...
commodity-v1,SHFE,FU,2020-01-02,day,...
```

⚠️ **没有借用 `carry_liquidity_history_exceptions.csv`** —— 设计 §3.3 明文只授权日线流动性缺口、
不能授权分钟会话缺失。这是一种**新的、独立的**授权，`absent_segment` 目前只接受 `day`。

## 三处改动

| 层 | 改动 | 守住的性质 |
|---|---|---|
| `minute_pg_source._validate_boundary_frame` | 已登记的候选允许零行通过，保留全 None 的边界行 | **未登记**的零行仍抛 `session_representative_missing_minutes` |
| `capture_minute_sessions.classify_session_boundary` | 新增 `day_session_absent=`，为真时跳过日盘断言 | 日盘**部分存在**仍致命（`day_session_partially_present`）—— 那是时钟异常，不是缺数据 |
| `capture_minute_sessions` 主流程 | 从权威构造 `absent_identities` 并透传；逐条打印 | **绝不静默** |

运行时输出：

```text
authorized_absent_day_session key=SHFE/AL/2018-01-02 contract=AL1803.SHF source_url=docs/research/...
authorized_absent_day_session_count=1 registered=5
```

## 实跑验证

同一条命令，改动前后：

```text
改前  MinuteDataError: session_representative_missing_minutes   ← 中止，不产出文件
改后  products=34 rules=0 checked_days=136 ambiguous=46         ← 跑通
```

AL 2018-01-02 现在归为 `night_authority_conflict` / 观测 `none,none`，
与同日其它 10 个上期所品种**完全一致** —— 闸门只是让分类器看见了缺席，没有编造任何东西。
（那 46 条歧义全是预期内的待写权威行：2018-01-02 的节后例外 31 条 + DCE 日盘品种 15 条。）

## ⚠️ 尚未完成：回测端的「当日不可交易」

用户选定的口径是「登记为未观测，**当日不可交易**」。采集端已落地，**回测端还没有**。

原因是它比预想深。`_prepare_candidates` 里少生成一个 `_CandidateContext` **不等于**不交易 ——
消费点（`minute_backtest.py:1287`）拿不到 context 会抛
`dynamic_execution_leg_missing_minutes`，那仍是 fail-closed 而非排除。
真正的「当日不交易」要在**信号/研究层**把这些产品日剔出候选，
而那会改动策略的持仓逻辑，属于会影响回测结果的改动。

**待定的分岔**（需用户拍）：这 5 个产品日的信号该
①「视为无信号」（当天不开新仓、已有持仓自然顺延），还是
②「沿用前一交易日的信号」（持仓状态不变，但那是一种推断）。
①与本文的「不断言未观测之事」一致，②更接近「跳过」的字面含义但引入了推断。

在此之前，全历史资产仍发不出来 —— 因为 2018-01-02 / 2019-01-02 / 2020-01-02 三天
还缺节后例外行（那三天从未落在任何采集窗口内，见批次 F 止于 2017-12-31、批次 G 起于 2018-01-03）。
这批行现在可以采集出来了，属于批次 G 的自然延伸。
