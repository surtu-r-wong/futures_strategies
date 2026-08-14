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

首轮 inventory、后续批量补表、年度 coverage、权威资产 SHA-256、歧义分类和
最终 `ambiguous=0` 结果将在真实采集运行后追加。本节留空不代表发布通过；在
正式 loader 复读和反向键集合校验完成前，`config/carry_minute_sessions.csv`
不得发布。
