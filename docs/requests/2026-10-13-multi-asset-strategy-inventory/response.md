# 回复：futures_strategies 已有策略与产物情况

回复日期：2026-10-13。受理方：futures_strategies；发起方：multi_asset_backtest_db。

本函回复 [request.md](request.md) 的现状盘点，沿用现有策略名称、代码入口和产物，不另建注册系统。已完成本地文档、代码入口、文件存在性、Excel 工作表与 CSV 日期范围核对；未重跑策略、联网查库、检查远端运行环境或修改生产作业。下文的“可运行”指已有实现及运行方式，不表示今天已实跑验收。

核对工作区：`/home/elfbob/claude-code/futures_strategies`，HEAD `f31d5ac`，工作区已有未提交文档及研究改动；本函按当前工作区读取。代码版本与产物版本须分别看，不能用当前 HEAD 给旧文件补版本。所有相对产物路径均相对于该仓库根目录。

## 1. 现有登记入口与范围

没有独立的统一策略 ID/版本注册表。当前状态入口是 [ROADMAP](../../ROADMAP.md)，辅以 [README](../../../README.md)、[运行手册目录](../../operations/) 和各模块 `__main__.py`。下表以现有模块名、策略名、指数代码标识，不新增下游内部 ID。

| 现有身份 | 版本、参数与策略说明 | 实现及运行状态 |
| --- | --- | --- |
| `cta_gtja / medium_equal_weight` | 国君 CTA 中波等权；`factor-set=price_volume` 为三因子 guarded 路径，`six_factor` 为标准六因子路径；目标波动 8%，缩放上限 2.5 | 两种因子集均已实现；日频研究入口；六因子依赖已发布 conservative build 和覆盖闸 |
| `cta_gtja / high_composite` | 国君 CTA 高波复合；同样有三因子与六因子配置；60% 等权 + 40% 因子轮动，单因子上限 50%；目标波动 12%，缩放上限 3.5 | 已实现；与中波策略分别输出，不把两个因子集混成同一条历史 |
| `cta_carry / daily` | 国信 Carry 日线研究版；10 日 Carry、120 日流动性池、严格晚月次主力、趋势过滤、三档吊灯止损；15% 目标波动、4 倍毛敞口上限 | 已实现；`python -m cta_carry --execution daily`，默认日线路径；有全历史研究产物 |
| `cta_carry / minute` | 同一 Carry 日线信号，15 分钟止损与分钟执行路径 | 已实现并完成七窗口比较；`python -m cta_carry --execution minute --source public-pg`；不是另一个独立信号。分钟执行改善不显著；更晚历史受商品时段资产限制 |
| `cta_carry / tsindex` | 期限结构指数配置：`near_dominant`、`rank_linear`、90 日 Carry、20 日沉淀资金门槛 20 亿元、关闭趋势过滤和止损、固定剔除名单、缺开盘延后；现行出单叠加基差动量 λ=0.20（500 日、覆盖 0.9、月度更新） | 已实现，路线图登记为生产出单；`scripts/carry_tsindex_daily.sh <资金> [as_of]`。库/CLI 的基差动量默认仍为 0，脚本显式设 0.20 |
| `cta_bollinger` | 国信 Bollinger 通道；300/1.5 通道、8 倍标准差止盈、150/300 OI 窗口；10% 目标波动，登记 `ddof=0`，`ddof=1` 为敏感性 | 已实现，09-02 全历史验收；有离线 bundle 即可运行；本机缺全历史 bundle 和登记报告 |
| `cta_dow` | 国信道氏；EMA 12/26/9、ATR20，15% 目标波动；09-15 登记升级为 `prior_extreme + latched`；`segment` / `literal` 是对照 | 已实现，09-15 登记产物重跑；本机缺全历史 bundle 和登记报告；仍未复刻出研报绩效，不等于实现未交付 |
| `cta_continuous` | 国信连续信号；EMA、TNR/DTNR、U2P 递推；研究选中 `20/120, N=20, narrow, entry`，1.3 bp；CLI 默认仍为 `12/26, wide, slot` | 全链路已实现；结论为复刻较弱但成立。输入为独立分片面板，不能用 Bollinger/道氏 bundle 直接替代；本机缺全历史面板和策略报告 |
| `index_open_momentum` | 国信股指开盘动量；IF/IC/IH 历史研究，15 分钟信号/止损，主力因果滞后一日；时段 `cffex-v1` | 已实现且全历史回测已完成，样本外为负；CLI 日期边界仍止于 2026-04-29，后段备用主力通路未接，换月口径仍待裁决 |
| `citic_index / CICSF023.WI` | 中信仓单策略指数复刻；`factor=warehouse_receipt`，p=5，历史基准窗口 lag=200/window=100；09-22 有 Wind/交易所/有效预报对照 | 纯复刻已实现，无专用每日出单；坏年份偏差仍待研究，交易所仓单旧格式有覆盖缺口，SHFE 派生解析未接 |
| `citic_index / CICSF025.WI` | 中信期限结构指数复刻/控制臂；`factor=term_structure`，已有 p=10 与 p=90、点名宇宙与开放宇宙产物 | 已实现；主要作控制臂，无专用每日出单；与 Carry `tsindex` 的执行、链条和参数不同 |
| `citic_index / CICSF026.WI` | 中信时序动量探针；`factor=time_series_momentum`；自身动量定方向，`1/σ²` 归一权重，有净敞口；最新文档研究点 L=14 | 因子和权重层、扫描入口已实现；登记仍是探针，不是完整生产策略交付；本地只有参数扫描，未发现独立逐日指数/权重产物 |
| `citic_index / CICSF027.WI` | 中信基差动量复刻及可成交版本；生产 R=150，OI 前两名按月份排序、不除月差、开放宇宙、日频重排；15% 目标波动、4 倍毛敞口上限 | 纯复刻及每日出单均已实现；`scripts/citic027_daily.sh <资金> [as_of]`。生产参数正本为 `citic_index/daily.py:PRODUCTION` |
| `scripts/research/alpha013_commodity.py / Alpha #013` | 商品原式 `-rank(covariance(rank(close),rank(volume),5))`；`literal`、`no_recent_roll`、五日调仓五个偏移 | 已完成研究并保留产物；本轮不支持原式作为独立商品交易策略。股票后续已交 stock_selector；本仓未登记生产日更 |

Carry 还保留已实现实验配置：`trend_band_atr=0.5`、`trend_confirm_days=3`、`secondary_selection=second_by_oi`、`equal_weight_capital`；后者为负面结果，默认关闭。基差动量腿属于 Carry 的混合配置，不等同于独立的 CICSF027。各策略中的因子、敏感性参数和辅助数据工具不额外冒充独立策略。

在以上现有登记与入口范围内，未找到另一条“只有登记、尚无实现”的独立策略；未实现/暂缓事项已保留在对应行：股指备用主力、商品分钟时段延伸、023 部分换源，以及下述基本面质量更新等。未发现独立国债期货策略登记。`cta_gtja --include-financial` 是历史诊断开关，不构成国债策略交付。

## 2. 现有产物、覆盖和最近更新

“本地已核实”指文件在本工作区存在并读过相应表头/日期；“文档记载”不保证本机或远端今天仍可取得。生成/修改日与数据截止日分列，均不能替代实时数据库覆盖。研究任务通常按需运行；除两条已登记日出单流程外，未提供常态调度记录。本次没有核验定时器或券商执行状态。

| 策略/配置 | 现有产物位置与内容 | 历史覆盖、最近记录及可取得性 |
| --- | --- | --- |
| 国君中波/高波，三/六因子 | 手册输出 `output/cta_medium_equal_weight_medium_equal_weight.xlsx`、`output/cta_high_composite_high_composite.xlsx` 及 `_equity.png`；基本面对照前缀见 [基本面说明](../../cta-fundamentals.md) | 文档：08-18 旧 build 基线为 2016-01-04..2026-04-29，2498 交易日、九品种。**本机未发现这些工作簿**；09-29 恢复基本面发布不等于策略结果已重跑。最新策略产物日期未提供 |
| Carry 日线基线及实验 | 本地 `output/carry_base.xlsx`、`carry_verify.xlsx`、`carry_band050.xlsx`、`carry_confirm3.xlsx`、`carry_secondoi.xlsx`、`carry_eqwt.xlsx`，各有 `_overview.png` | 六份均为 2013-01-04..2026-04-29，3227 个收益日，文件修改日 08-06；`verify` 是复验，不是新策略 |
| Carry 分钟 | [七窗口研究报告](../../research/2026-08-26-minute-vs-daily-execution.md)；标准导出为 Carry 八张表再加 `executions / intraday_stops / minute_data_quality` | 文档：2013–2026 七窗口、3177 个交易日，08-26 完成、WSL2 运行。本机未发现对应工作簿；具体远端归档路径未提供；商品时段资产上界 2026-01-30 |
| Carry tsindex 纯期限结构历史 | 本地 `output/carry_tsindex.xlsx` + `_overview.png` | 2012-01-04..2026-09-08，修改日 09-09；这是启用 λ=0.20 前的历史，不能当现行混合策略全历史 |
| Carry tsindex 次日目标/短窗回测 | 本地 `output/targets/tsindex_{20260908,20260909,20260910,20260914}_next_targets.csv`；09/10/14 有同前缀 `.xlsx`，10/14 有 `_overview.png`；另有 `tsindex_20260910_switch.csv` | 最新目标 `signal_date=2026-09-14`，50 行，文件生成/修改于 09-15；最新工作簿收益为 2024-03-28..2026-09-14（599 日），含 λ=0.20。09-09 工作簿为旧短窗、07-13..09-09；09-10 为 2024-03-25..09-10，不能直接拼成连续实盘历史 |
| Bollinger | 文档：`output/guosen_bollinger.{xlsx,png,audit.json}`、`guosen_bollinger.signals.csv.gz`，另有 `_ddof1.*`；输入 `output/commodity-panel-v1/` | 2012-01-04..2026-01-30，09-02 全历史验收；**以上本机均未发现**，研究文档记 WSL2。信号超 Excel 行数上限时完整数据在 sidecar，Excel 只有指路行 |
| 道氏 | 文档：`output/guosen_dow.{xlsx,png,audit.json}`、`guosen_dow.signals.csv.gz`，另有 `_literal.*`；旧读法归档 `output/_segment_reading_20260915/` | 2012-01-04..2026-01-30，09-15 当前读法重跑，commit `80fb690`；**以上本机均未发现**。旧 `segment` 与当前 `prior_extreme` 不拼接 |
| 连续信号 | 文档：`output/continuous/panel_84d482d/`；运行示例前缀 `output/continuous/newpanel_cad-entry/run`，CLI 写工作簿、audit JSON、summary CSV、window JSON | 文档：2011-01..2026-01，09-01 有更新实验；全历史面板/策略报告**本机未发现**。本地 `output/continuous/panel_smoke.parquet` 与 universe/scope/turnover CSV 是面板核查资产，不是完整策略持仓或收益 |
| 股指开盘动量 | 本地 `output/index_open_momentum/index-open-momentum-paper.{xlsx,png,audit.json}`，另有 `provenance.txt` | 工作簿收益 2011-06-01..2026-04-29，3616 日，修改日 08-27。该产物起点不同于 CLI 可请求起点 2010-04-16；样本外分界 2021-05-13 |
| CICSF023 | 本地 `output/citic/cicsf023_p5_{index,weights}.csv` + `_config.json`（旧版）；09-22 `d0_p5_{index,weights}.csv`、`d_p5_{index,weights}.csv` 及各自 `_config.json`；`arms_023_*.csv` / `attribution_023.csv` 为研究汇总 | 指数文件均 2010-01-04..2026-09-10，4054 行；D0 为 Wind 现状复算，D 为 Wind + 交易所预报，不应把 D 误当已替换生产。D0/D 的配置明确 `settle_to_settle, execution_lag=1`，修改日 09-22 |
| CICSF025 | 本地 `output/citic/cicsf025_control_{index,weights}.csv`、`cicsf025_ctrl2_{index,weights}.csv` 及各自 `_config.json` | 指数 2010-01-04..2026-09-10；09-11 历史文件。`control` 为 p=10 点名宇宙，`ctrl2` 为 p=90 开放宇宙；配置不含 `execution_lag`，`ctrl2` 为旧 `close_to_prev_settle` |
| CICSF026 | 本地 `output/citic/scan_026.csv`、`scan_026_short.csv`、`scan_026_short.log`；[探针记录](../../plans/2026-09-15-cicsf026-time-series-momentum-probe.md) 与 [收益口径裁决](../../research/2026-09-16-citic-return-basis-ruling.md) | 扫描文件修改于 09-15/16；列为参数、相关及绩效汇总，无逐日日期轴。文档历史研究为 2010..2026-09；未提供本地当前 L=14 的独立逐日收益和持仓文件 |
| CICSF027 研究 | 本地 `output/citic/cicsf027_replica_{index,weights}.csv` + `_config.json`；`cicsf027_best_index.csv`、`cicsf027_best_close_to_close_index.csv`、`cicsf027_best_close_to_prev_settle_index.csv`；`calibration_027_SUPERSEDED_named37_universe.csv` 等扫描/归因表 | 指数 2010-01-04..2026-09-10；`replica` 配置为 R=500/最少450、near_dominant、除月差、点名宇宙，不是生产 R=150。部分 best 文件无同名前缀完整配置；不按文件名猜成交滞后。`SUPERSEDED` 文件保留历史身份 |
| CICSF027 次日目标 | 本地 `output/targets/citic027_{20260910,20260914}_next_targets.csv` + 各自 `_config.json` | 最新信号 09-14，68 行，修改日 09-15；只有目标及诊断，没有每日实盘账本。**两份配置均早于 09-16 修订且缺 `execution_lag`，不能视作当前 T+1 记账版本的样本** |
| Alpha #013 | 本地 `output/alpha013/`：`literal_scores.csv`、`literal_weights.csv`、`no_recent_roll_*`、`dominants.parquet`、18 份 `*_daily.csv`、绩效/IC CSV、`audit.json`、`reconciliation.json`、`overview.png` | 09-15 研究产物；信号/权重 2012-01-04..2026-09-08，账户收益 2012-01-05..2026-09-10（3567 日）。日频与五日各偏移、成本情景分别保留 |

两条日更产物在本地最新均止于 09-14；只能说明**本工作区现存文件覆盖**，不据此推断数据库停止日更或生产作业已暂停。原始日志位置为 `output/targets/daily.log` 与 `citic027_daily.log`。

已有输入缓存：`data/citic_index/`、`data/citic_open/`、`data/citic_023/` 下的 `prices.csv / official.csv`，023 另有仓单及对照臂 CSV。它们是行情/官方比较序列/因子输入，不能代替策略产物。官方指数输入的读取位置见 `scripts/citic_index_fetch.py`；本次未复核数据库实时范围。基本面最新本地核查记录见 [10-08 质量报告](../../research/2026-10-08-fundamental-data-quality.md)：当时 build 覆盖到 09-30，不作为 10-13 最新值。

## 3. 数据含义、运行节拍与可用时点

### 3.1 国君 CTA

运行和因子定义见 [CTA 运行手册](../../operations/cta-strategy-replication.md)，参数入口 [cta_gtja/__main__.py](../../../cta_gtja/__main__.py)。可选择两个策略和两个因子集；六因子额外依赖标准基本面发布与 PIT/覆盖信息。

报表已有 `metrics / period_returns / weights / factor_allocations / factor_returns`，以及按实际情况输出的 `data_quality / fundamental_coverage / fundamental_lineage / fundamental_build`。`weights` 是按 `trade_date, symbol` 的品种级策略权重，已应用策略自身波动缩放，不是因子内部比例，也没有逐笔真实成交及整数手数。

**日期不能直接按日收益表名理解**：[backtest.py](../../../cta_gtja/backtest.py) 将日期 t 的信号收益写为 `open(t+2)/open(t+1)-1`，收益仍索引在 t；权重用于下一交易日开盘，再持有到再下一交易日开盘。接入方需保留这一前向区间，不能把 t 当成收益已经实现/已可用的日期。成本默认 1 bp，按权重变动绝对值计；连续合约价格与具体合约实际持仓不是同一层。保证金、现金账和实盘持仓未提供。

### 3.2 Carry，包括 tsindex

正本为 [Carry 手册](../../operations/carry-daily-research.md)、[期限结构配置设计](../../plans/2026-09-09-carry-term-structure-index-config-design.md)、[基差动量设计](../../plans/2026-09-10-basis-momentum-design.md)。原始日线使用具体合约；日线研究信号在收盘后形成，下一交易日开盘执行；日线止损是收盘判定后次日执行，不能冒充 15 分钟止损。

八张基础表是 `metrics / daily_returns / positions / trades / signals / curve_selection / data_quality / run_config`。收益含 `gross_return, turnover, cost, net_return, equity, gross_leverage`；持仓含 `trade_date, product, contract, direction, raw_weight, weight`；交易表是权重变化及原因，日线账不是整手资金账。默认单边 4 bp（手续费与滑点合计假设），默认不模拟保证金、现金利息及容量；分钟模式另输出执行和止损审计，亦不能当券商成交。

日出单时序为 T 日行情到齐后在 T+1 早上运行，目标用于日盘开盘；脚本每次按五家商品交易所最后日期的最小值取截止日。若滞后，`signal_date` 是最后完整日，不能因文件今天生成便改成昨天的信号。

目标已有 `raw_weight, vol_scale, target_weight, current_weight, weight_change, close, multiplier, notional, lots`：

- `target_weight` 是有符号目标名义金额/传入资金，已包含波动缩放与毛敞口上限；`notional = target_weight × capital`。接入时不能再乘一次 `vol_scale` 或默认商品杠杆。
- `lots = round(notional / (close × multiplier))`。这是按参考收盘价估算的目标整手数；取整后名义金额、下一开盘估值与目标 notional 有差异。Carry 乘数由近期成交额/成交量/价格估算，来源见 [targets.py](../../../cta_carry/targets.py)，不是经纪商账户回报。
- **`current_weight` 是回测路径持仓，`weight_change` 也是回测差量，不能代表真实账户应下订单**。切换 λ 时新旧路径甚至对应不同反事实账本。真实实际持仓及现金/保证金占用未提供。
- `run_config` 保存版本、参数、查询和绩效区间；旧文件有不同版本、不同预热长度。基差动量上线后脚本回看 900 天；不能以 λ=0 历史替代 λ=0.20 全历史。

现行代码在目标文件输出 `order_code / code_exchange / code_qmt / code_ctp` 四种写法；本地 09-14 文件仍是旧表头，只有 `order_code`（09-08 Carry 甚至尚无此列）。必须按产物时期理解合约代码，不能将 09-16 的代码约定追认到旧文件。保留原 `contract`，不根据文件名猜交易所格式。

### 3.3 中信四条指数与 027 日出单

入口 [citic_index/__main__.py](../../../citic_index/__main__.py) 通过 `--factor` 选择 `warehouse_receipt / term_structure / time_series_momentum / basis_momentum`，读取 `--data-dir` 缓存，写 `*_index.csv / *_weights.csv / *_config.json`。023 另需仓单输入。各参数须沿用产物配置；CLI 默认不等于研究选点或生产参数。

`*_index.csv` 的列为 `trade_date, n_products, daily_return, index_value`，基点 1000；`*_weights.csv` 为品种级因子/权重，**没有具体合约手数**。023 文件中的 `basis_momentum` 是复用排序层的列名，实际值为经方向转换的仓单因子，不能按字段名认成 027。

纯指数累计使用原始因子权重，**没有波动目标加杠杆或交易成本扣除**；即使 config 中带 `target_vol / cost_bps`，也不能据此把指数收益当作 15% 波动、4 bp 费后的资金账。023/025/027 使用零和秩权重，原始 gross 约 0.49 而非 1；026 是自身方向和波动定权，有净敞口。权重分母、缩放与收益须成套使用，不能强行归一至 1。

09-16 当前裁决是复刻 `settle_to_settle`、生产 `close_to_close`，配置 `execution_lag=1`：T 信号、T+1 价格建仓、T+1→T+2 开始计收益。旧 `close_to_prev_settle` 与旧 signal-day 建仓结果留作历史诊断。上述最新规则来自 [收益口径裁决](../../research/2026-09-16-citic-return-basis-ruling.md)，不意味着所有现存 CSV 已更新。

027 日出单见 [生产手册](../../operations/citic027-daily.md)、[daily.py](../../../citic_index/daily.py)、[book.py](../../../citic_index/book.py)。信号可用时点与 Carry 一致；目标以主力具体合约、乘数、整手数给出，`target_weight` 已应用 15% 波动目标及 4 倍 gross 限额。它不输出真实当前持仓；日盘开盘执行安排与收盘对收盘的研究/波动估计账要分别保留，不能当作已实现的开盘成交净值。

### 3.4 商品三条分钟策略

运行参数与命令见 [Bollinger 手册](../../operations/guosen-bollinger-runbook.md) 和 [道氏手册](../../operations/guosen-dow-runbook.md)；两条均通过 `python -m <模块> --panel-dir <bundle> --start ... --end ... --output-prefix ...` 运行，连续信号则用 `python -m cta_continuous --panel <分片目录> --start YYYY-MM --end YYYY-MM --output-prefix ...`。

Bollinger/道氏的 [共享 bundle](../../operations/commodity-panel-bundle.md) 保存具体合约原始价格及复权因子：信号计算应用复权，成交和换月两腿使用原始合约价。15 分钟信号后按下一可成交窗口执行；品种筛选、组合波动乘数按月更新，日内信号仍可引起交易。道氏在**当前持仓品种之间**等分资金，入退场会触发其他品种在其各自下一窗口 resize。

两条报表均有十一张表：`metrics / daily_returns / positions / trades / signals / universe / selection / dominant_rolls / data_quality / fidelity / run_config`，另有净值图和 audit。`positions` 是日末逐品种回测记录，含合约、`target_weight / actual_weight / prevol_weight` 及杠杆分解；`trades` 含 timestamp、trade_date、具体合约、价格、前后权重、成本与原因。当前导出代码将日末 `target_weight` 和 `actual_weight` 都取已执行的回测状态，不能把两列当独立的“未成交目标/真实成交”证据。默认成本 1.3 bp；权重账本不提供实盘整数手数、保证金或资金冻结账。要重建日内敞口，日末表本身不足，需检查交易事件与估值语义。

连续信号有独立面板与执行循环。现有 CLI 导出的核心是 `segments / annual / fidelity / turnover / leverage / daily`，可另带成本敏感性等表；**内部 signals/executions 不等于已导出的完整持仓接口**。现有导出未提供可直接交付的逐日逐合约目标/实际持仓表。参考 [当前结论及配置](../../ROADMAP.md)、[报告代码](../../../cta_continuous/report.py)；研究参数必须显式指定，不能直接用默认命令复现选中点。

以上商品分钟资产可靠上界仍为 2026-01-30。日线恢复不代表分钟可用期延伸；交易日须按交易时段资产映射，夜盘时间戳的自然日期不能直接替代 `trade_date`。已有时间戳写 Excel 时存在上海本地墙钟转换，详见各报告导出器。

### 3.5 股指开盘动量

运行边界见 [运行手册](../../operations/guosen-open-momentum-runbook.md)。已有工作簿七表：`metrics / daily_returns / annual_returns / trades / signals / leverage / data_quality`。`daily_returns` 列为 `trade_date, IF, portfolio, IC, IH`；`trades/signals` 实际是 product-day 汇总，包含 contract、direction、entry_price、gross/net return、cost、leverage、scale_downs、carried_overnight 等，**不是逐笔委托/成交表，也没有完整 positions 表**。

策略使用当日开盘动量和后续 15 分钟信息，不能将整天 signals 表在开盘前全部视为可用。主力选择滞后一日；成本为单边 1.3 bp。2016 年前分钟档案缺官方收盘前最后 15 分钟，报告不得标 paper-faithful；备用主力通路及后段日期边界仍未完成。保证金、实际账户持仓、完整逐事件数量快照未提供。

### 3.6 Alpha #013

口径和运行命令见 [商品研究报告](../../research/2026-09-15-alpha013-commodity.md)。`*_scores.csv / *_weights.csv` 是信号日的品种级分数与目标权重；`dominants.parquet` 是具体合约映射；18 份 `*_daily.csv` 给 `gross / turnover / cost / net / gross_exposure / unquoted_holdings / skipped_targets`，不是完整逐合约持仓账本。

日频信号收盘 t 生成、t+1 开盘交易；五日实验保持五个偏移分别计算，换月按日维护。最高组多 0.5、最低组空 0.5，重叠净额不再放大至 1 倍。成本 0/1/4/8 bp 各自成套；缺报价暂估及跳过新目标有审计，不应转换成空仓。整数手数、保证金、容量和涨跌停排队未建模；权重/收益区间错开是执行时序的一部分。

## 4. 已知缺项与受理结论

本次已按现有登记回复全范围，包括探针、负面研究结果及无本地产物的策略。下列信息目前不能从已核实产物直接交付：

- **真实实际持仓、成交回报、资金 NAV、现金和保证金账**：本次范围内未发现统一账户产物；回测 positions、模拟 actual_weight、目标 lots 均不能代替。
- **连续逐日目标快照**：两条日出单仅有上述若干日期；不存在可据此声称完整的生产持仓历史。其他策略各按研究导出情况列出，缺快照不等于空仓。
- **当前配置全历史**：Carry 混合版本、09-16 后 027 成交滞后及四代码格式，与本地旧样本不完全一致；025/027 的部分旧研究产物缺显式 return_basis/execution_lag 或匹配配置，未知项保留未知。
- **缺本地文件**：国君 CTA 的工作簿、Carry 分钟七窗口工作簿、Bollinger/道氏全历史 bundle 及报告、连续信号全历史面板及报告，本次只有既有文档证据。未访问 WSL2，也未为了盘点新算或补传。
- **未完成的数据/实现事项**：商品分钟时段延伸、股指备用主力通路、基本面九品种全量质量更新及发布日历升级仍按各自现有记录处理。023 的历史“无仓单数据”阻塞已在 09-15/16 解决，不继续误记为当前完全不可跑；剩余是覆盖及口径问题。
- **精确可用时间和保证金参数**：已有手册描述日出单时段，不能替代逐次数据到达/信号发布时间记录；未提供统一逐历史合约保证金率、经纪商加成和资金占用口径。

本函完成策略情况盘点回复，**不表示已完成 [完整持仓与敞口标准](design.md) 的接入验收**。multi_asset_backtest_db 可据现有资料适配、识别关键缺项并另列最小补充需求；策略选择、配比、优化和模块归属由该项目配置。本次仅新增本回复，不新建计算作业、专用持仓文件、历史快照，不回填或改写共享库。
