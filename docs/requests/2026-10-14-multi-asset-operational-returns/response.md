# 回复：原生策略入口、收益可用性与 Debian 接入落点

2026-10-15，futures_strategies → multi_asset_backtest_db。

本函一并回复 [原请求](request.md)、[职责补充](coordination-addendum.md) 和 [Debian 落点补充](debian-location-addendum.md)，继续沿用 [14 条策略盘点](../2026-10-13-multi-asset-strategy-inventory/response.md) 及 [版本补充回函](../2026-10-13-multi-asset-version-followup/response.md)，不另建注册表。

**当前没有可直接交付、覆盖本月及当前周的合格持续净收益文件，也尚不能承诺日收益完成时刻。** 本机未发现 `output/multi_asset/returns/latest.json`。已有研究净收益、纯指数和出单目标分别保留原身份；缺少持续运行证据不等于缺少研究实现，缺少实盘成交也不妨碍将来建立明确标识为 paper 的模拟账户。本轮先提供现状，不将研究文件升级为持续运行账户。

本次只读核对本机目录、现有回函、脚本、工作簿表名和部分 CSV 日期范围。历史工作簿的收益区间沿用 10-13 已完成的逐表核对；远端事实沿用版本回函的 10-13 核验，**本轮未重新连接 WSL2 或 Debian，也未核验定时器、实时库覆盖或远端最新产物**。不据本机旧文件推断生产停跑。

## 1. 原生登记与实际位置

本机主机名实查为 `elfbob-ThinkPad-X1-Carbon-Gen-8`，仓库绝对路径为 `/home/elfbob/claude-code/futures_strategies`。没有独立的统一策略 ID/版本注册文件，现有入口是：

- `/home/elfbob/claude-code/futures_strategies/docs/ROADMAP.md`：交付状态正本。
- `/home/elfbob/claude-code/futures_strategies/README.md` 和 `/home/elfbob/claude-code/futures_strategies/docs/operations/`：模块目录、运行方式与参数说明。
- 各原生模块 `__main__.py`；日出单入口是 `/home/elfbob/claude-code/futures_strategies/scripts/carry_tsindex_daily.sh` 与 `/home/elfbob/claude-code/futures_strategies/scripts/citic027_daily.sh`。

下表的身份沿用原生模块/策略名，不创设下游专用 ID；同一行中的因子集、执行模式及参数版本仍需分别登记。当前源码 HEAD 不能替旧文件补版本，具体参数以成对的配置、工作簿 `run_config`、audit 和生成记录为依据。

| 原生身份与版本区分 | 已有收益/目标位置和可用状态 | 持续净收益接入缺口 |
| --- | --- | --- |
| `cta_gtja / medium_equal_weight`；`price_volume` 与 `six_factor` 分开 | 手册约定 `output/cta_medium_equal_weight_medium_equal_weight.xlsx`，本机没有；真实远端文件绝对路径未确认 | 已实现研究净收益；无已核验持续产物，且收益索引是前向信号日，不能直接作当日收益 |
| `cta_gtja / high_composite`；同样区分两种因子集 | 手册约定 `output/cta_high_composite_high_composite.xlsx`，本机没有；真实远端文件绝对路径未确认 | 同上；不能混合中波、高波或两个因子集 |
| `cta_carry / daily`；基线与实验分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/carry_base.xlsx`，`daily_returns` 有研究净收益与 equity；2013-01-04..2026-04-29。其他实验文件见原盘点 | 已有研究收益，不是当前周/月持续账户；费用假设、估值区间及可用时点须随版本保留 |
| `cta_carry / minute` | 已完成七窗口研究，原报告记载 WSL2 运行；本机无对应工作簿，具体归档绝对路径尚未定位 | 有分钟研究账本实现，未取得可接入的持续收益文件；不与 daily 拼接 |
| `cta_carry / tsindex`；纯期限结构与基差动量 λ=0.20 分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/carry_tsindex.xlsx` 是混合前历史；`/home/elfbob/claude-code/futures_strategies/output/targets/tsindex_20260914.xlsx` 是混合短窗，收益 2024-03-28..2026-09-14；同前缀 `_next_targets.csv` 是 50 行目标 | 日出单已实现，工作簿有研究净收益；滚动重算窗口不是持续发布/修订账本，也不是按实际日盘成交核算的账户 |
| `cta_bollinger`；登记 `ddof=0`，敏感性 `ddof=1` 分开 | 文档约定 `output/guosen_bollinger.{xlsx,png,audit.json}`，本机没有；文档记 WSL2 全历史验收，报告实际绝对路径未确认 | 有研究执行账本实现；无已核验持续文件，不能把已找到的输入 bundle 当作收益 |
| `cta_dow`；登记 `prior_extreme + latched`，旧 `segment` / `literal` 分开 | 文档约定 `output/guosen_dow.{xlsx,png,audit.json}`，本机没有；登记报告实际绝对路径未确认 | 研究已实现；旧读法与当前登记历史不能拼接，无已核验持续文件 |
| `cta_continuous`；研究选点与 CLI 默认分开 | 文档示例前缀 `output/continuous/newpanel_cad-entry/run`，本机无完整策略报告，真实归档绝对路径未确认；本机 `output/continuous/` 的 scope/turnover 文件是输入核查 | 已实现研究；没有已核验持续收益文件，输入面板不是策略账本 |
| `index_open_momentum` | 本机 `/home/elfbob/claude-code/futures_strategies/output/index_open_momentum/index-open-momentum-paper.xlsx`，配同前缀 `.audit.json`；研究收益 2011-06-01..2026-04-29 | 有历史账户研究收益；没有当前持续运行记录，CLI 后段边界仍未延伸；文件名 paper 不代表已启用 paper 日更 |
| `citic_index / CICSF023.WI`；旧版、D0、D 分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/citic/d0_p5_index.csv`、`d_p5_index.csv` 及同前缀 `_weights.csv`、`_config.json`；2010-01-04..2026-09-10 | 纯指数，未扣交易成本、未应用账户波动缩放；没有合格净 NAV 收益文件 |
| `citic_index / CICSF025.WI`；p=10 control 与 p=90 ctrl2 分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/citic/cicsf025_control_index.csv`、`cicsf025_ctrl2_index.csv`，成对配置/权重；2010-01-04..2026-09-10 | 纯指数；旧时序/收益口径不追认为当前版本，无持续净账户收益 |
| `citic_index / CICSF026.WI` | 本机 `/home/elfbob/claude-code/futures_strategies/output/citic/scan_026.csv`、`scan_026_short.csv` 是参数扫描汇总；未发现独立逐日指数/净收益文件 | 探针实现；缺逐日账户序列，不能从绩效摘要还原日收益 |
| `citic_index / CICSF027.WI`；纯指数、缩放资金账、日盘执行账分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/citic/cicsf027_replica_index.csv` 等是旧研究指数；本机 `/home/elfbob/claude-code/futures_strategies/output/targets/citic027_20260914_next_targets.csv` 是旧目标；修订后 WSL2 目标绝对路径见下文 | 生产 R=150、`close_to_close`、`execution_lag=1` 已有目标及配置，尚无已定位的同版本独立日收益归档；目标没有 NAV/PnL 分母账，不能由 lots/notional 反推已实现收益 |
| `scripts/research/alpha013_commodity.py / Alpha #013`；成本情景与五日偏移分开 | 本机 `/home/elfbob/claude-code/futures_strategies/output/alpha013/literal_4bps_daily.csv` 等 18 份日表；2012-01-05..2026-09-10，有 gross/cost/net/gross_exposure | 现成研究净收益，不是当前持续账户；未登记生产日更，不将各成本/偏移混成一个系列 |

上表“文档约定/示例”的相对路径仅说明原代码输出命名，**不宣称是已存在的文件落点**。本机其他实验产物、参数和版本差异可沿用原盘点，不为本补充重跑研究。

### 已核验过的 WSL2 落点

10-13 版本回函确认的既有读取入口为 `ssh -p 2223 ghls@100.120.152.1`，仓库是 `/home/ghls/claude-code/futures_strategies`，不是同机的 `/home/ghls/futures_strategies` 旧 checkout。

| 文件绝对路径 | 身份及证据日期 |
| --- | --- |
| `/home/ghls/claude-code/futures_strategies/output/targets/citic027_20260915_next_targets.csv` | 修订后 027 目标，68 行，信号日 09-15；10-13 已核验 |
| `/home/ghls/claude-code/futures_strategies/output/targets/citic027_20260915_config.json` | 配套 R=150、`close_to_close`、`execution_lag=1` 配置 |
| `/home/ghls/claude-code/futures_strategies/output/targets/citic027_20260916_next_targets.csv` | 修订后 027 目标，68 行，信号日 09-16；10-13 已核验 |
| `/home/ghls/claude-code/futures_strategies/output/targets/citic027_20260916_config.json` | 同日期配套配置；日志记对应运行 09-17 14:53 +08:00 完成 |

四文件 SHA-256 与生成记录在 [版本补充回函第 1 节](../2026-10-13-multi-asset-version-followup/response.md)。如需取得现有目标样本，可通过该既有 SSH 入口只读复制 CSV 与 JSON，并与原回函摘要比对；本轮未传输、未新增权限。这些是版本/持仓样本，**不是 ReturnFeed 合格收益样本**。

同机 `/home/ghls/claude-code/futures_strategies/output/commodity-panel-v1/manifest.json` 与 `commodity-panel-w2526/manifest.json` 已在 10-13 核验，截止 2026-01-30；它们是分钟输入资产，不是 Bollinger/道氏收益文件，不能据此宣称收益报告已经定位。

## 2. 更新方式和可承诺时刻

| 范围 | 现有更新方式 | 净收益截止承诺 |
| --- | --- | --- |
| Carry tsindex、027 日出单 | 原脚本按传入资金和 as_of 运行；手册安排 T+1 早上 08:00–08:30 运行，供 09:00 日盘目标。运行时按五家商品交易所最后日期的最小值截断；行情滞后时仍可能输出较旧信号日 | **尚不能承诺**。该时段是手册运行安排，不是完成 SLA，更不是日收益发布时刻；本轮未查实际调度 |
| 其他研究策略与指数 | 现有 CLI/研究脚本按需运行；未取得持续调度和当前收益发布证据 | **尚不能承诺**，无当前周/月合格收益文件 |

本机现存两条目标的最新信号日仍为 2026-09-14；WSL2 的 027 最新已核验记录为 09-16（核验日 10-13）。两者不能混作同一生产目录，也不据此断言远端今天无更新。日志入口分别为运行仓库下 `output/targets/daily.log` 和 `output/targets/citic027_daily.log`。

20:45 仅作为下游拟定汇总时间；本项目没有承诺当晚已完成 T 日净收益，不为该时间调整现有作业。

## 3. 交易日、日历和收益口径

时区约定为 `Asia/Shanghai`（`+08:00`）。现存 CSV/Excel 多只有交易日期，并没有可直接复用的完整 `period_start_at / period_end_at / available_at`。文件 mtime、信号日和本次读取时间都不能补作历史首次可用时刻。

- 国君 CTA 的 `period_returns` 以信号日 t 索引 `open(t+2)/open(t+1)-1`；实际区间是后续两次开盘之间，不能按 t 当日已经实现的收益上报。
- Carry 研究与目标分开：研究 `daily_returns.trade_date` 的具体估值边界需按日线/分钟版本保留；目标 `signal_date` 只表示最后完整行情日，不是成交日或收益所属日。日线交易日开盘（可能含夜盘）与计划日盘 09:00 执行不得混同。
- 中信当前时序是 T 信号、T+1 建仓、T+1→T+2 起计收益；纯复刻 `settle_to_settle` 与生产研究 `close_to_close` 分开，且旧文件未必采用该时序。日盘开盘目标不证明已经形成收盘账或开盘成交账。
- 分钟收益须依赖原生会话映射与实际估值事件；夜盘不按自然日猜交易日。Alpha 等研究日表保留原日期轴，未经完整区间核验不转换为本接口的已实现日收益。

**calendar_id：尚未确认并绑定合格的完整交易日历，因此本轮不发布该字段的占位值或收益系列。** 原请求给出的 `public.trading_calendar` 是候选来源，本轮没有查库确认各市场字段及所需覆盖。已有 `config/carry_minute_sessions.csv`、`commodity_minute_sessions.csv`、`continuous_minute_sessions.csv`、`index_minute_sessions.csv` 是原生时段资产；商品可靠上界为 2026-01-30，`cffex-v1` 是会话规则版本，均不能直接冒充当前完整交易日历。观测收益日期集与 coverage 脚本的最后日期闸也不证明缺日是休市。

已有研究账的分母和成本信息可复用，但仍须分别验收：Carry 日线研究有 `net_return / equity / gross_leverage`，默认单边 4 bp 的手续费与滑点合计假设，未模拟保证金、现金利息及容量；国君默认按权重变化扣 1 bp；Alpha 成本情景在文件名/审计中分开。分钟/股指依相应 run_config/audit，不统一套用日线成本。**不能把这些已有研究净收益统称为“缺 NAV”，其当前主要缺口是持续产出、版本/区间/日历及可用时点证据。**

纯中信指数则未扣成本、未按波动目标形成净账户收益，配置中出现 `cost_bps` 或 `target_vol` 不会改变指数文件的含义。出单目标的 `notional` 与传入资金用于计划敞口，不是持续 NAV/PnL 账。未来符合条件时保留原生杠杆，`gross_ratio` 仅取已核验的总绝对名义/NAV，未知保证金比率写 null，不能补 0。

## 4. 固定入口与本次交付

约定相对入口继续为 `output/multi_asset/returns/latest.json`。本机对应绝对路径 `/home/elfbob/claude-code/futures_strategies/output/multi_asset/returns/latest.json` **当前不存在**；WSL2 对应候选路径 `/home/ghls/claude-code/futures_strategies/output/multi_asset/returns/latest.json` 本轮未核验，不宣称已有文件或已启用导出作业。最终以实际承担导出的运行仓库为准。

本次没有现成合格 ReturnFeed 可先交；可取得的是上表研究文件与两份远端版本目标样本。没有创建空壳 latest、填零、使用合成示例冒充真实收益，或给旧研究文件刷上新的 generated_at。合格源具备后的发布仍需遵守原请求：当前周/月完整窗口、真实生成及可用时点、版本/修订保留、成功后原子替换，失败保留旧文件和原时间并另记状态。本次未实现或启用该发布流程。

下游 Debian `/home/elfbob/jobs/multi-asset/state/inputs/futures_strategies/` 是 multi_asset_backtest_db 自管接入副本目录；本项目未向其写文件、不承担跨机同步或迁移。文件读取/传输、来源校验及首次取得记录按补充函由下游衔接。

本次仅新增本回函。Schema 和示例链接已核对为同目录 [return-feed.schema.json](return-feed.schema.json)、[return-feed.example.json](return-feed.example.json)，原 request.md 当前链接也已正确，无需再次修改。没有重跑研究或回填、写共享库/DDL、申请付费补数、改生产推送，也没有发送外部消息。
