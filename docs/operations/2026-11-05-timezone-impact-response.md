# Futures Strategies 时区核查与本地修复反馈

日期：2026 年 11 月 5 日。接收对象：用户及数据管理者办公室。

本项目已修复中国市场默认日期、日更脚本重试时刻和 PostgreSQL 连接时区的本地代码。
本机保持 `America/New_York`，市场日期与数据库会话采用 `Asia/Shanghai`。
修复尚未发布到 Debian、WSL2 或 Windows；远端实际版本、调度时区与历史结果需要现场核对。

依据：[2026 年 11 月 4 日跨项目时区影响说明](/home/elfbob/claude-code/report_tools/docs/2026-11-04-cross-project-timezone-impact.md)。

## 运行位置与影响

本地核查目录为 `/home/elfbob/claude-code/futures_strategies`，基线提交 `f477fa5`；
本轮修复在此基线上完成。`/etc/localtime` 指向 `America/New_York`。
研究与检查入口包括 `python -m cta_gtja.data_quality`、`python -m cta_carry.coverage`、
两个中信数据导出脚本、基本面审计和日更 shell 脚本。

本仓部署模板指向 Debian 的 `/home/elfbob/jobs/m02-futures/current` 与
`/home/elfbob/jobs/m02-futures-v2/current`，分别包含 v1 夜间作业、v2 早间及夜间作业。
这些路径表示模板目标，不构成远端现役 release 或安装状态的核验。
Windows Wind 采集采用独立 host 脚本；其数据日期由参数指定。

| 入口 | 原有影响 | 本地修复 |
|---|---|---|
| `cta_gtja/data_quality.py` | 美东仍为前一天时，表过期天数少算一天，严格检查可能漏报 | 默认取上海日期，新增 `--as-of` 便于历史核查 |
| `cta_carry/coverage.py` | 默认回溯窗口与报告参照日可能少一天 | 默认取上海日期，保留 `--as-of`；截止日仍来自各商品交易所实际覆盖的最小值 |
| `scripts/citic_index_fetch.py`、`scripts/citic_receipts_exchange_fetch.py` | 默认导出结束日可能漏掉北京时间当日 | 默认 `--end` 取上海日期，显式历史区间保持有效 |
| `scripts/research/audit_fundamentals.py` | UTC 日期在北京时间 00:00 至 07:59 可能少一天 | 中国市场查询上限改取上海日期；审计瞬时时间仍为带时区 UTC |
| `scripts/carry_tsindex_daily.sh`、`scripts/citic027_daily.sh` | 包装层传入的本机日期覆盖了 Python 默认值 | 进程内显式设置 `TZ=Asia/Shanghai`，统一日期及日期运算 |
| `scripts/m02_v2_morning.sh` | 默认目标交易日期可能少一天，重试钟点取错时区 | 进程内采用上海日期与重试时刻，保留手工日期覆盖 |
| `scripts/m02_futures_nightly.sh`、`scripts/m02_v2_nightly.sh` | Python 已用上海日期，但包装层重试可能延至错误时刻 | 包装层日志日期与重试时刻统一为上海时间 |
| `scripts/m02_seal.py` | 封存追赶回执目录按美东日期命名 | 按上海日期分目录；追赶上限仍严格使用 `--expect-last` |
| `scripts/m02_czce_wind_capture_host.py` | `captured_at` 无偏移，跨机器交换依赖隐含约定 | 新采集时间使用明确 UTC 偏移；源行情日期与 09:30 bar 标签保持原口径 |
| M02 v2 cron 模板与部署日志查看命令 | 日期条件及日志查找依赖调用进程时区 | 日期条件与日志查找显式使用上海日期；调度触发时区另行核实 |

以上上海日期均为自然日。夜盘归属、交易日判断和数据截止仍由现有交易所日历、
显式数据日期与覆盖逻辑决定。已经显式使用上海时区的 M02 Python 主流程和分钟时段算法保持原口径；
仅用于 release 编号的本机时间没有改动。

## 数据库连接

`common/db.py`、`citic_index/pg_source.py`、两个中信导出脚本、股指 CLI 及基本面审计
在新连接的启动参数中传入 `-c timezone=Asia/Shanghai`。
`forward_inputs.m01.ReadOnlySource` 复用中信连接配置，因此一并覆盖。
此方式不依赖客户端系统时区，也不通过业务事务内的临时 `SET` 建立会话约定。

离线测试检查了公共连接、重新建立连接及上述独立入口实际传给驱动的参数。
本次没有建立真实数据库连接；远端连接的 `SHOW TimeZone`、字段类型与已存时间值仍待现场核对。
历史日期固定的 `scripts/index_open_momentum/2026-08-27-explain-smoke.py` 留作原始诊断脚本，
没有作为现役入口修改或重新运行。

## 验证结果

新增离线用例覆盖北京时间跨日、月末与年末、周一、美东冬夏令时及切换时刻，
并验证显式历史日期、质量检查过期阈值、查询窗口、封存回执日期和 shell 重试前后边界。
shell 测试复制脚本到临时目录，以替身 Python、固定时钟和替身 sleep 执行，
不运行实际业务程序，不产生真实等待。

修复前，日期与包装脚本测试为 49 失败、71 通过；数据库连接参数测试为 7 失败。
修复后，相关日期、连接与既有 M02 流程合计 188 项通过；
测试夹具整理后的新增 127 项也全部通过。
全项目离线回归 **2,762 项全部通过**，耗时 476.10 秒，退出码 0。
测试报告有 289 条警告，包括报告绘图的中文字体缺字；不影响上述测试通过。
六个修改过的 shell 脚本通过 `bash -n`，`git diff --check` 通过。

运行验证时设置 `ulimit -v 2000000`，BLAS 单线程及 Arrow system allocator；
全量回归显式设置 `TZ=America/New_York`，并在 pytest 进程阻断真实 PostgreSQL 及 socket 连接。

## 发布与历史处理

本次仅修改本仓代码、测试和模板，未修改系统时区、远端任务、数据库配置或共享库数据，
未执行采集、付费 Wind 调用、账户推进、历史修复或对外通知。

发布前需核对 Debian 的实际 `current`、release、启用的 cron 条目和 cron 守护进程时区。
模板中的 08:30、21:05 等调度字段要求守护进程按上海时区解释；
命令内设置 `TZ` 只影响命令，不能修复触发时间。
若目标主机使用美东时区，应使用明确支持 `Asia/Shanghai` 触发器的调度配置，
不应切换主机时区或默认认定 Debian cron 支持 `CRON_TZ`。

当前证据确认了代码缺陷，尚不能确定历史数据或回测结果已经错误。
历史影响需对照当时入口、执行时刻、显式日期、输入覆盖与输出日期逐次判断。
若后续需要共享库回填或修复，仍须先向办公室提交具体请求；本轮不涉及此类操作。
