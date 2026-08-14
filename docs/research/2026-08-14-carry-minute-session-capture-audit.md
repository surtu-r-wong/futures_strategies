# Carry 分钟时段采集审计

**日期：** 2026-08-14
**目标范围：** 2011-01-01 至 2026-04-29
**状态：** FU 证据已复现；权威表歧义收敛与正式资产发布待完成

## 1. FU 只读审计命令

审计 SQL 位于 `scripts/carry/audit_fu_minute_history.sql`。它以
`BEGIN TRANSACTION READ ONLY` 开始、以 `ROLLBACK` 结束，不创建临时表，不修改
数据库。实际复现命令如下；密码仅从仓库外实际配置读入子进程环境，不写入命令
输出或仓库：

```bash
/home/elfbob/claude-code/futures_strategies/.venv/bin/python -c \
'import os, subprocess; from common.config import load_config; from common.db import pg_config_from; p=pg_config_from(load_config("/home/elfbob/claude-code/futures_strategies/config/settings.yaml")); e=os.environ.copy(); e["PGPASSWORD"]=p["password"]; subprocess.run(["psql", "-X", "-h", str(p["host"]), "-p", str(p["port"]), "-U", str(p["user"]), "-d", str(p["name"]), "-v", "ON_ERROR_STOP=1", "-f", "scripts/carry/audit_fu_minute_history.sql"], env=e, check=True)'
```

2026-08-14 实际执行成功，事务输出为 `BEGIN` 后全部查询完成并 `ROLLBACK`。

## 2. 物理分钟表与 FU 年度覆盖

总表边界用 `(bar_time, symbol, exchange)` 的确定性索引探针取得，不运行全表
`COUNT(*)`：

| boundary | bar_time | symbol | exchange |
|---|---|---|---|
| first | 2005-01-04 09:00:00+08 | A0501 | DCE |
| last | 2026-08-05 23:59:00+08 | ZN2611 | SHFE |

FU 日线从 2004-08-25 开始，并在采集截止日 2026-04-29 前连续按年存在。与本次
缺口判断直接相关的年度摘要为：

| year | first daily date | last daily date | daily contract-days | physical minute coverage |
|---:|---|---|---:|---|
| 2010 | 2010-01-04 | 2010-12-31 | 2,662 | present |
| 2011 | 2011-01-04 | 2011-12-30 | 2,664 | absent |
| 2012 | 2012-01-04 | 2012-12-31 | 2,688 | absent |
| 2013 | 2013-01-04 | 2013-12-31 | 2,598 | absent |
| 2014 | 2014-01-02 | 2014-12-31 | 2,716 | absent |
| 2015 | 2015-01-05 | 2015-12-31 | 2,684 | absent |
| 2016 | 2016-01-04 | 2016-12-30 | 2,664 | present |

物理分钟 FU 行存在于 2005–2010 年，2011–2015 年完全缺失，2016 年起恢复。
因此不能声称 2016 年无数据，也不能对 2011–2015 做回填、插值或日线合成。

## 3. FU1604 日线/成交分钟一致性

目标交易日 2016-01-04 的 `FU1604.SHF` 日线为：

| open | high | low | close | volume | turnover |
|---:|---:|---:|---:|---:|---:|
| 2,576 | 2,581 | 2,572 | 2,572 | 8 | 1,030,600 |

对应分钟窗口为 2015-12-31 21:00 至 2016-01-04 15:01（Asia/Shanghai）。严格
过滤 `volume > 0` 后有 4 行，首末成交分钟为 09:38 和 10:09，聚合 OHLC、
成交量、成交额与日线完全一致；按合约乘数 50 计算的隐含 VWAP 为 2,576.5。

过滤条件是审计口径的一部分：分钟归档保留零成交占位行，其携带价格不能用于
成交 OHLC，否则会制造没有实际成交的极值。

## 4. 2011–2015 FU eligibility

输入范围 2010-01-04 至 2015-12-31 共 1,456 个 FU 品种交易日。SQL 先按品种日
汇总所有具体合约成交额，再使用与策略一致的
`ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING`，即前 120 个实际品种交易日且
向后移动一日。目标范围从 2011-01-01 开始，首个完整目标日为 2011-01-04。

| metric | result |
|---|---:|
| maximum shifted liquidity mean | 4,299,422,549.91666667 |
| date of maximum | 2011-02-23 |
| default threshold | 5,000,000,000.0 |
| eligible target days, 2011–2015 | 0 |

结论是：2011–2015 的 FU 分钟物理缺口不会被默认 Carry 策略访问，因为共享流动性
口径从未使其入池。实现继续复用 `aggregate_product_liquidity`，不把上述数值写成
程序常量，也不增加 FU 例外名单。

## 5. 权威表收敛记录

### 5.1 Round 1a：标准化预检阻断

第一次全范围命令按实施计划运行，但在分钟源构造前由 normalization 门禁阻断：

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python \
  scripts/carry/capture_minute_sessions.py \
  --start 2011-01-01 --end 2026-04-29 \
  --backtest-start 2013-01-04 \
  --settings /home/elfbob/claude-code/futures_strategies/config/settings.yaml \
  --inventory-output /tmp/carry-minute-session-inventory-round1.csv \
  --audit-report /tmp/carry-minute-session-audit-round1.md \
  --output config/carry_minute_sessions.csv
```

有效配置为 `liquidity_window=120`、`liquidity_threshold=5000000000.0`、
`prewarm_calendar_days=730`，日线加载起点为 2009-01-01。三个表头权威资产的
SHA-256 为：

| asset | sha256 |
|---|---|
| no-night | `16ef85a65901288c1a2816e3e0ea85d4ae4258a4ed316067c05e29611d2ea239` |
| day-only | `d2bf062f8d98cdb175c766e9f276910fed721833943af4845fece7b1bea74b80` |
| history exception | `d2bf062f8d98cdb175c766e9f276910fed721833943af4845fece7b1bea74b80` |

门禁最初报告 5,709 条 `normalization_unkeyable_rows`，年度分布为 2020:337、
2021:390、2022:388、2023:724、2024:872、2025:1,488、2026:1,510；未知日期为
0。inventory 只有表头，`checked_days=0`，正式
`config/carry_minute_sessions.csv` 保持不存在。

随后用生产库只读分组查询确认 5,709 条的精确构成：

| suffix | product/trailing code | rows | first | last | distinct symbols |
|---|---|---:|---|---|---:|
| DCE | `L*F` | 624 | 2025-10-29 | 2026-04-29 | 9 |
| DCE | `PP*F` | 624 | 2025-10-29 | 2026-04-29 | 9 |
| DCE | `V*F` | 624 | 2025-10-29 | 2026-04-29 | 9 |
| INE | `SC*TAS` | 3,837 | 2020-01-13 | 2026-04-29 | 78 |

这些是归一化层有意排除的 F/TAS 非标准具体合约，不是无法识别的交易所、品种或
日期。根因是 coverage 统计误用了“必须能映射具体分钟合约”的严格解析器，而该
统计只需要 `(exchange, product, trade_date)`。修复必须使用专门的排除行
product-day 身份解析；未知后缀或空品种仍维持 unkeyable 和发布阻断。

第二条生产库只读查询按上述四类排除行去重到 1,852 个 product-day，并与同日
规范化存活行做反连接：1,852 个全部存在同交易所、同品种、同交易日的规范合约，
`without_canonical_survivor=0`。因此修复后的预期不是把这些日期计为
`normalization_excluded_product_days`，而是先可靠归键，再因为同 product-day
仍有规范行而不增加排除日计数；只有真正无法解析身份的原始排除行才进入
`normalization_unkeyable_rows`。

本轮旧实现把 5,709 错写为 `ambiguous=5709`，但没有任何经验时段或权威冲突，
因此这不是有效的 ambiguity 轮次，不能计入预计的 2–3 轮收敛。修复并通过复核后
重新运行的全量 inventory 才记为正式 round 1。

后续批量补表、年度 coverage、权威资产 SHA-256、歧义分类和最终
`ambiguous=0` 结果将在真实分钟边界采集后追加。在正式 loader 复读和反向键集合
校验完成前，`config/carry_minute_sessions.csv` 不得发布。

### 5.2 Round 1：真实执行所需 AL 分钟缺口阻断

修复 normalization 后用同一命令重新运行约一小时，年度
`normalization_unkeyable_rows` 和未知日期计数全部为 0；1,187 个
`normalization_excluded_product_days` 仍只是没有规范行存活的可归键排除日。
采集随后在 2018-01-02 的 `AL1803.SHF` 抛出
`session_representative_missing_minutes`，未进入时段歧义分类。正式 CSV
保持不存在，本轮也不能计入 2–3 轮 authority 收敛。

生产库只读、立即回滚的诊断得到：

| check | result |
|---|---|
| 2018-01-02 AL 日线主力 | `AL1803.SHF`；OI 320,394、volume 162,174，按既定排序唯一胜出 |
| 目标窗口 | 2017-12-29 21:00 至 2018-01-02 15:01（Asia/Shanghai） |
| 目标窗口内全部 `AL*` 分钟行 | 0 |
| 同窗口 SHFE 覆盖 | 31,050 行、138 个合约；其他夜盘产品正常 |
| AL 恢复 | 2018-01-02 21:00 起恢复；2018-01-03 每个 AL 合约均有 465 行 |
| 其他整品种缺口 | FU 同日无分钟，但仍不在流动性池；其余 SHFE 日线产品有分钟 |
| AL prior-120-day mean | 2017-12-29 为 51,313,212,341.8667 元，2018-01-02 为 51,594,749,870.6083 元，均 `in_pool=True` |

因此 AL 同时满足 `P(prev(T))` 和 `P(T)`，不是仅由保守 `P(T)` 项多审计的
候选。真实默认日线引擎也在 2018-01-02 生成并执行：

```text
product=AL contract=AL1803.SHF reason=entry
old_weight=0.0 new_weight=-0.135499 direction=-1 tranches_remaining=3
```

该动态腿需要当日开盘五分钟 VWAP、盘中止损观察和收盘计价。日线合成、其他
合约替代、`none` authority 或删除包络键都不能恢复这些价格，且会违反已确认
设计。进一步枚举库内行情表确认，`public.market_data_minute` 的覆盖仅从
2026-01-22 开始，目标窗口内 `AL1803` 为 0 行；其余带 OHLC、symbol 和时间列的
对象只是现有 Timescale hypertable 的内部分块，不是独立备份源。因此库内没有
可替代的历史分钟表。继续完整分钟复现前必须从可审计来源修复 2018-01-02 AL 日盘分钟数据；
在此之前采集和分钟引擎保持 fail-closed。
