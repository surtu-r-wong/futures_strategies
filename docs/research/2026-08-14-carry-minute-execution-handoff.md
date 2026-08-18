# Carry 分钟级复现交接检查点（2026-08-14）

## 工作位置与停止边界

- 分支：`feature/carry-minute-execution`
- 隔离工作树：`/home/elfbob/claude-code/futures_strategies/.worktrees/carry-minute-execution`
- 总目标尚未完成；本次按用户要求停在 Task 12 的源码级 EXPLAIN 摘要切片，不把总体工作标记为完成。
- 本次没有启动 Task 12 回测器集成、会话规则 schema、权威 CSV 填充、全历史采集或其他新任务。
- 后续仍可使用子 agent，但不要频繁轮询；应让 agent 自然返回，同时主线程继续可独立推进的工作。

## 已完成并验证

实施计划 Task 1–11 的代码路径已经完成。最近关键提交：

- `857401b` / `2e8d187`：分钟执行编排及稀疏候选包络 fail-closed 修正。
- `1699e2b`：分钟执行 CLI 与 PostgreSQL 源接线。
- `5b00bb6`：执行对比输入校验加固。
- `11555bd`：分钟 VWAP 与盘中止损回测运行入口。
- `fd18b94`：日线与分钟执行结果对比。
- `28f6c8a`：分钟成交与止损报告。
- `6d10078`：关闭会话审计设计缺口并补权威来源登记。
- `13aeabd`：Task 12 源码级 EXPLAIN 摘要；增加不可变、可 JSON 序列化的 `MinutePlanSummary`，`PublicMinuteSource.explain_month()` 只执行 EXPLAIN、不执行数据 SELECT，`iter_month()` 保存稳定的计划审计快照，原有危险计划仍 fail-closed。

本次停止前的直接验证证据：

- Task 12 新测试：`3 passed`。
- 既有计划安全回归：`14 passed`。
- `tests/test_carry_minute_pg_source.py` 全量：`95 passed`。
- 两个变更文件的 Ruff check、Ruff format check、`git diff --check` 全部通过。
- 先前 Task 8 最终验证为 `47 passed`，Task 10 相关验证为 `127 passed`，规格与质量复核均通过。

## 尚未完成的现有计划

1. Task 12 还需把计划摘要接入分钟回测器质量表，生成 `minute_query_plan` 行并对预期月份数量 fail-closed；随后用真实数据库做“单合约、五个交易日、EXPLAIN-only”烟测。
2. Task 13 的端到端全链路与最终验证尚未执行。
3. 正式 `commodity-v1` 会话资产尚未生成；`config/carry_minute_sessions.csv` 不存在，三张权威/例外 CSV 仍只有表头。
4. 全历史采集仍被 AL 数据缺口与 DCE `6202113` 原文缺失阻塞。异常会话 schema 本身已于
   2026-08-18 实施完毕（见下节），不再是阻塞项。

## AL1803 数据阻塞

当前全量采集的首个硬阻塞是：

```text
2018-01-02 AL AL1803.SHF session_representative_missing_minutes
```

已确认 `AL1803.SHF` 是正式策略在该日需要访问的真实代表合约；本地分钟库从 2017-12-29 14:59 直接跳到 2018-01-02 21:00，同系列文件均缺失 2018-01-02 日盘。禁止使用日线合成、替代合约、伪造 `none`、删除审计键或硬编码绕过。

只读数据源调研结论：

- 未发现上期所公开、匿名可下载的历史一分钟接口；SMDP/Mduser 是实时授权分发接口，不是历史回取接口。
- 掘金/MyQuant 官方历史范围覆盖目标日期，目标代码为 `SHFE.al1803`，是首选尝试；需要账号、token、终端及实际历史权限。
- Wind 是上期所授权信息商，历史分钟产品可作为付费/试用备选；购买前必须先验证该退市合约该日仍有数据。
- 当前环境没有 `gm`/`WindPy`、相关 CLI、凭据或仓库适配器，不能直接下载。

下次若用户提供授权，先请求原始、无补值的 2018-01-02 日盘 1 分钟 OHLCV、成交额与持仓量，并对合约、交易日期、时段和行连续性做审计。若两条商业路径都无数据，再联系上期所市场数据部门询问历史档案授权。

## DCE 异常会话 schema（2026-08-18 已实施）

大商所 2019-12-25 晚间延迟至 22:30 开盘，对应目标交易日是 2019-12-26，夜盘为 22:30–23:00。旧官方公告编号 `6202113` 当前受反爬脚本保护，来源登记状态仍为 `pending_manual_fetch`；它不是 `none`，也不在 DCE 78 个 `none,none` 候选中。

原提案已获用户批准并按下列计划实施完毕：

- 设计：`docs/superpowers/specs/2026-08-17-carry-minute-session-exceptions-design.md`（提交 `320e9dc`）
- 计划：`docs/superpowers/plans/2026-08-17-carry-minute-session-exceptions.md`（提交 `803b7f7`）

实施提交（Task 1–4）：

| 提交 | 内容 |
|---|---|
| `594b2d5` | 会话规则 CSV 增加 `night_start`；`night_label_to_offset` / `night_offset_to_label` / `parse_night_interval` 严格时钟接缝 |
| `84ebb83` | `NoNightDate` → `SessionException`；精确双向区间授权；资产更名为 `carry_minute_session_exceptions.csv` |
| `b121707` | 经验边界分类改为返回精确 `(night_start, night_end)` 对（不取整）；例外消费追踪与 `session_exception_unconsumed` |
| `585a6f1` | 按精确区间对折叠、发布与回放；`_expected_night_interval` 取代 `_expected_night_end` |

已实施的不变量：

- 生成会话 CSV 表头为 `exchange,product,effective_start,effective_end,night_start,night_end,version`：常规为 `21:00/...`，日盘-only 为 `none,none`，异常日可为 `22:30,23:00`。
- 权威资产已泛化为按交易所/目标交易日的 session exceptions，异常开盘时间必须由机器可核验的权威记录授权；哈希清单键为 `session_exception`。
- 经验分钟边界与 authority 精确匹配，否则 fail-closed；已加载但未被任何审计产品日消费的 exception 会产出 `session_exception_unconsumed`，因此不允许把供应商缺行误判为延迟开盘。
- 正式资产尚未发布，因此仍维持 `commodity-v1`，不为未发布格式做迁移。

**schema 就绪不等于权威就绪。** 本次实施没有提交任何 exception 行：
`config/carry_minute_session_exceptions.csv` 仍是纯表头。正式资产采集仍被两项外部输入阻塞——
DCE `6202113` 原文与 AL1803 2018-01-02 原始分钟数据。

## 已钉死的口径

- FU 原油沥青期货于 2004 年上市；不得编造 2016 年之前的特殊回填，也不得设置 FU 例外名单。
- eligibility 必须复用 `aggregate_product_liquidity` 与默认 `CarryConfig()`；预热固定使用默认 730 个自然日，不得退回采集命令曾用的 30 天覆盖。
- 分钟审计访问包络为 `P(T) ∨ P(prev(T)) ∨ P(prev²(T))`，并以引擎动态访问日必须属于 `audit_keys` 为运行时不变量。
- `carry_minute_session_exceptions.csv.trade_date` 表示“该夜盘例外所归属的目标交易日”，不是公告中的前夕自然日。
- 会话折叠只跨相邻、已审计、同值日期；未审计间隙不得外推。节后首个目标交易日的单日 `none` 区间是预期形态，但必须有权威资产双向授权。
- `capture_start <= backtest_start - prewarm_calendar_days` 是显式覆盖不变量。

## 下次恢复顺序

1. 先读本文件及下列设计/计划，不重新推导已确认口径。
2. 完成 Task 12 回测器 `minute_query_plan` 质量行与月份计数 fail-closed，并运行 EXPLAIN-only 数据库烟测。
3. 在获得用户数据授权后回取并审计 AL1803 2018-01-02 原始分钟数据；缺口未解决前不启动全历史正式采集。
4. ~~取得用户对 DCE `session_exceptions` / `night_start` schema 的明确批准后再改代码和资产。~~
   已于 2026-08-18 完成，见上节；此步不需重做。
5. 整理权威 CSV，预期用 2–3 轮全量 ambiguous 清单收敛；随后生成并复读会话资产。
6. 执行 Task 13 全链路、动态访问覆盖检查、全量测试与最终复核。

## 主要文档

- 母设计：`docs/superpowers/specs/2026-08-13-carry-minute-execution-design.md`
- 母计划：`docs/superpowers/plans/2026-08-13-carry-minute-execution.md`
- eligibility v2 设计：`docs/superpowers/specs/2026-08-14-carry-minute-session-eligibility-design-v2.md`
- eligibility v2 计划：`docs/superpowers/plans/2026-08-14-carry-minute-session-eligibility-v2.md`
- 权威来源登记：`docs/research/2026-08-14-carry-minute-session-authority-sources.md`
- 采集审计：`docs/research/2026-08-14-carry-minute-session-capture-audit.md`
- session exception 设计：`docs/superpowers/specs/2026-08-17-carry-minute-session-exceptions-design.md`
- session exception 计划：`docs/superpowers/plans/2026-08-17-carry-minute-session-exceptions.md`
- session exception 实施记录：`docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`
