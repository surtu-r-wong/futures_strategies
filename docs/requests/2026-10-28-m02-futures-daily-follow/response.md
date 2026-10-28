# 回函：M02 期货观察账本 v2 —— 每日跟随 027（R1–R9）

v2 已实现并部署到 Debian 独立目录，v1 的 release、`current` 软链和 cron 行均未改动。待办两件：**用户安装 crontab 段**（[crontab-v2.txt](crontab-v2.txt)），以及 **13 个品种的费用单位**需要上报用户裁决（第五节）。当前 027 目标选中的 67 个品种都已闭合，这 13 个品种目前没有被选中，不影响 11-16 上线。

## 一、提交与部署

- 提交：功能提交 `d015c08`，另有三个部署修正 `992bd55`、`c62ecd5`、`7969cc3`（venv 复用、早上日历、Arrow 分配器）。均为本地提交，未推送 origin。
- Debian：`~/jobs/m02-futures-v2/releases/20261028T2007-7969cc37`，HEAD `7969cc3`，部署在独立根目录 `~/jobs/m02-futures-v2/`（`current` 软链、`targets/`、`state-{production,shadow,rehearsal}/`、`config/targets-calendar.json`）。
- **v2 不能与 v1 共用 release**：v1 的 store 在初始化时绑定了引擎指纹（`forward_account/*.py` 等 16 个文件），v2 改了引擎，若翻转 v1 的 `current`，v1 每晚都会被身份闸拒绝。所以 v1 继续在 `~/jobs/m02-futures/releases/20261027T1826-c6ac62e3` 上运行到 11-13。
- 部署脚本：`scripts/deploy/m02_v2_debian_deploy.sh {deploy|genesis|morning|nightly|status}`。

## 二、R1 v2 账户与 genesis 参数

| 项 | 值 |
|---|---|
| `account_id` / `account_version` | `m02-futures-observation-v2` / `m02-futures-observation-v2` |
| 构建产物 | [build/production/](build/production/)（`account.config.json`、`calendar.json`、`build-manifest.json`） |
| `config_hash`（canonical config 的 SHA-256） | `67bd14c46d1e5373bfabf798dd4dc86d39ab17309189db0fe3f48b529af62abe` |
| `source_version` | `citic027_r150_t1::m02-v2::c59b6eb430d9ee1d` |
| 首区间 | 2026-11-16，`signal_date` 11-13，`start_at` 11-13 15:00 |
| `initial_at` | 初始化时的真实时钟，**必须早于 11-13 15:00**（反回填闸：首个可推进区间是第一个 `start_at ≥ initial_at` 的区间） |
| 本金 / 单位净值 | 1 亿 / 1；`instruments` 初始为空（见第五节） |
| 成本 | 与 v1 相同（10-16 用户裁决）：交易所费 ×1.1 且无最低、单边 4bp 滑点、保证金 +2pp、现金不计息、无融资、09:30 执行、结算价盯市 |
| `target_policy` | 不设；新增 `daily_policy = {target_missing: hold_and_mark, untradable: hold_prior_quantity}` |

**genesis 时点与闸链**：建议在影子运行前四晚（11-09 至 11-12）全部通过后，于 11-13 上午 genesis。`initial_at` 在那一刻才确定，会在 genesis 回执 `state-production/account.sqlite.genesis.json` 里给出。`scripts/m02_v2_genesis.py` 按股票侧 10-22 的教训把闸链走到会话序闸，逐闸给出判定：G1 反回填闸（`now ≤ 首区间 start_at`，不满足则拒绝初始化）；G2 会话序闸（`next_interval` = 11-16，并在副本上探测跳日推进确实被拒）；G3 身份闸（库内 config、日历、引擎指纹与本次构建一致）；G4 目标窗口（首份目标须落在 [11-13 15:00, 11-16 09:30]）。预演账户的 genesis 实跑结果见第九节。

## 三、R2 每日目标生产

- `forward_inputs/m02_daily.py` 与 `scripts/m02_v2_morning.sh`：08:30 运行，若未产出则 09:05 重试一次。计算路径与 m01 完全相同：只读 REPEATABLE READ 快照、900 日窗口、按月行数对账、`PRODUCTION` 参数（`citic027_r150_t1`）、`current_weights` 投影。原生算法和参数都没有改。
- 目标包写在 `targets/<T>/`，原子写入，产出后不可变：`targets.csv`、`source-parameters.json`、二者的 SHA-256、`signal_date`、`available_at`（运行完成时刻，不取文件 mtime）、`precheck.json`、全部查询记录，以及 `source-daily.csv.gz`（原始输入，每天约 11 MB）。留证格式沿用 `forward_inputs.m01`。
- **`source_version` 钉住原生计算文件**：取值为 `citic027_r150_t1::m02-v2::<sha16>`，sha16 来自决定权重的全部文件（m01 的 NATIVE_FILES 加 `forward_inputs/m01.py`）的摘要。这些文件只要改动，`source_version` 就变，引擎会拒绝该目标，夜链则判为 `validation_failed`。
- 实测：本机以信号日 10-28 跑一次，耗时 1 分 50 秒，峰值 747 MB（2 GB 地址空间上限），67 行，其中 66 行非零，预检全部通过；Debian 实测 36 秒，峰值 1.0 GB，**`targets.csv` 的 SHA-256 与本机那次逐字节相同**（`04ab85bd…`）。Debian 上 pandas 3.1 与 pyarrow 默认使用 mimalloc 内存池，会预留大量虚拟地址，在 2 GB 上限下报 MemoryError（当时 RSS 只有 0.75 GB）。早上脚本已改为 `ARROW_DEFAULT_MEMORY_POOL=system`，目标结果不受影响。

**输入落库时点复核**：目标管线只读 `public.futures_daily`（`citic_index.pg_source`，排除 CFE）。截断日取五个商品交易所 `max(trade_date)` 中最小的一个，并要求它等于日历上的前一交易日。我方按 `created_at` 逐所逐日核对了 09-01 至 10-28：

| 区间 | 结果 |
|---|---|
| 09-10 至 10-28 | 五所每天都在 T+1 08:30 之前落库（多数为 T 日 17:00 同批写入，少数在 T+1 03:09）。迟到 0 天 |
| 09-01 至 09-09 | DCE 迟到 6 天，最迟一次晚了 6 天 16 小时（人工投递时期） |

结论：runbook 里"大商所多数早晨落后一天"的说法已经过时，但不能说"一定齐备"。08:30 时若 T−1 不齐，目标包记为 `not_produced`，原因写 `signal_date_mismatch`（附五所实际覆盖日），当晚夜链据此记 `target_missing`。

## 四、R3 目标缺失与 R4 执行价、可交易判定

- **目标缺失**：判定完全机械。目标包不存在或状态不是 `ready` → `not_produced`；`signal_date` 不等于日历前一交易日 → `signal_date_mismatch`；`available_at` 不在 [start_at, 09:30] 内 → `not_available_before_execution`；SHA、参数、行数或 `source_version` 校验不过 → `validation_failed`。出现以上任一情况，当天不交易、沿用持仓、按结算价对结算价盯市，不算失败。夜链只读取 `targets/<T>/`，不会拿更早的目标替代。引擎对应新增显式路径（`forward_account/engine.py` 的 `daily_policy`），没有该键的旧配置仍是严格失败。
- **feed 标记放在旁路文件**：贵方 `ReturnFeed` 的行模型是 `StrictModel(extra="forbid")`（`src/backtest/strategy_portfolio/models.py:13`），往行里加字段会导致入库失败。因此逐日标记写在 feed 同目录的 `daily-status.json`（含 `target_status`、`target_missing` 原因与证据、`untradable`、成交数），账本审计里也有同样内容。若贵方愿意扩展 schema，我方再改为写进行内。
- **执行价**：五个交易所统一取 `public.futures_minute` 中 09:30:00 那根分钟线的开盘价，同一来源，不再需要 Wind 收据。
- **可交易判定**：分钟线存在，且开盘价严格位于 `public.futures_contract_info` 当日涨跌停价之间，才算可交易。已核实该表 T 日行给出的是 T 日限价（由 T−1 结算价推出：cu2612 在 10-28 的限价为 118410/98860，对应 10-27 结算价 108640 ±9%）。没有 09:30 分钟线记为 `no_0930_bar`；开盘价触及或越过涨跌停记为 `limit_locked_at_open`，两个方向都按不可交易处理，这是函中"开盘即涨跌停视为不可交易"的机械执行。不可交易的合约保持上日手数，不虚构成交。若持仓合约当天没有 09:30 价，以上日结算价为参考价，全天 PnL 一并计入日内盯市，总额不变。
- **整所缺数**：当日所涉交易所在 `futures_minute` 日盘时段的行数若为 0，夜链 fail-closed、零写入，在后续夜里补算，不会判成"全部不可交易"。另有一种情况同样 fail-closed：有分钟线却查不到当日限价行，因为当天无法判定可否交易，不做猜测。

## 五、R5 规格参数：按品种建表，合约按需派生

- **品种表** [config/m02_v2/product-specs.json](../../../config/m02_v2/product-specs.json)（构建回执 `build-receipt.json`）：82 个商品品种，其中 **69 个已闭合、13 个待裁**。每个品种记录交易所、乘数（`futures_contract_info`，要求品种内只有一个值）、费用单位来源（交易所声明 / 10-16 用户裁决 / 待裁）、涨跌停来源和生效日期。
- **合约派生规则**（`forward_inputs/m02_v2_specs.py`）：合约第一次被需要的那晚，用信号日（T−1）交易所结算参数派生。交易所费按 open→`open_fee`、close→`offset_fee`、intraday_open→`short_open_fee`、close_today→`short_offset_fee` 映射。保证金取 max(投机买, 投机卖) + 2pp。有效期从首次需要的交易日到 `最后日期`。经纪商费为交易所费 ×0.1，滑点 4bp。派生出的规格通过 `extend_references` 追加，新合约的费率规则随之追加，已有合约的费率表必须逐字节不变。
- **10-16 闭合口径直接复用并逐位验证**：v1 的 67 只合约用各自 10-15 的源行重新派生，乘数、保证金、四类交易所费的值与单位、经纪商费、滑点逐字段比对，**0 处差异**。这一比对已固化为离线测试（`tests/test_m02_v2_specs.py`，夹具为 10-15 源行的只读快照）。
- **合约规格登记后终身冻结**：持仓批次和 DCE 日内费用匹配都绑定在各条费率规则的 digest 上，改动已登记合约的费率会切断证据链。因此交易所调费只作用于调费之后新登记的合约，保证金同样在登记时冻结。"带生效日期"体现在品种层面：哪天登记的合约用哪天的参数。
- **预检与夜间**：早上的 `precheck.json` 逐个检查目标中的非零合约能否派生规格，不能派生的会被点名。夜间遇到规格缺失则 fail-closed（`spec_gate`），不会丢弃合约。
- 当前（10-28 信号）目标选中的 67 个品种全部在已闭合集合中。

**待裁清单（请转用户裁决，我方没有自行默认）**：

| 品种 | 交易所 | 源数据 `fee_style` | 问题 | 若按已有裁决类推 |
|---|---|---|---|---|
| CY、JR、PM、RI、RS、WH、ZC | CZCE | 绝对值 | `fee_scale` 为空；10-16 第二轮裁决 1 只覆盖了 13 个品种 | YUAN_PER_LOT |
| BB、FB、LG | DCE | 比例值 | `fee_scale` 为空；10-16 裁决只点名 I/J/JD/JM/LH | PER_TEN_THOUSAND（用户原话"比例的单位**均**为万分"） |
| L-F、PP-F、V-F | DCE | 绝对值 | 月均价合约，不是标准品种代码 | 建议不纳入 |

另有 RR（DCE）、WR（SHFE）两个品种的单位由交易所声明，已闭合。上表中 LG（原木）目前在 `futures_daily` 有行情，是最可能先进入 027 宇宙的一个。

## 六、R6 交易日历与到期护栏

- **只能覆盖到 2026-12-31**：`data_manager.business_calendar` 的 CN 日历目前截至 12-31，2027 年放假安排尚未公布。为了不预填未经核实的会话，v2 日历止于 12-31（生产账户 34 个区间）。2027 年各所公告后，再用 `extend_references` 追加，该函数只允许追加，不允许改写。日历剩余不足 15 个区间时（约 12-10 起），夜链 verdict 会报警。与函中"至少到 2027-03-31"的偏差源于此。
- **到期日来自交易所数据，不靠规则外推**：规格的 `valid_to` 直接取 `futures_contract_info.最后日期`。我方逐只核对了贵方更正后的日历，16 只合约全部吻合：BZ/EB/EG2611 为 11-25，JD/PG2612 为 12-28，B/CS/PS2611 为 11-13，AL/BC2611 为 11-16，SC/LU2612 与 EC2611 为 11-30，NR2612 为 12-15，PF/PD2612 为 12-14。
- **护栏**：早上预检对"持仓合约今天是最后交易日、目标却仍持有"报警；夜间提交后，对"持仓已越过最后交易日"报警（下一晚会因缺结算价 fail-closed，提前一晚说明原因）。正常情况下换月由原生选约完成。

## 七、R7 v1 收尾

- v1 原 cron 行加日期闸，11-13 之后不再运行。11-13 22:30 运行 `scripts/m02_seal.py` 封账：先核对最后一行确为 11-13，否则拒绝；再记录 store 和 feed 的 SHA-256，写入旁路文件 `account.sqlite.sealed.json`，并把 store 与 feed 设为只读。**库和 feed 的字节保持原样**（封账信息写在旁路文件里，没有往库里写表）。
- 封账后历史仍可读取：store 的读函数改为 SQLite 只读模式（`mode=ro`），不再申请写锁。从 v2 release 读取 v1 历史不受只读权限影响。
- 若 v2 提前上线，把封账行的日期改为 v2 首日的前一交易日即可。
- 替换后的整段 crontab 见 [crontab-v2.txt](crontab-v2.txt)，替换原 `M02 FUTURES NIGHTLY 20261027` 段，其余段不动，由用户安装。

## 八、R8 影子运行（11-09 至 11-13）

- 影子账户 `m02-futures-observation-v2-shadow` 使用独立 store，日历从 11-09 开始（[build/shadow/](build/shadow/)），**genesis 截止 11-06 15:00**。cron 21:10 运行，有日期闸。
- **对"不提交"的执行方式**：每晚先在 store 副本上把整个区间干跑一遍（规格追加加推进），报告写进 verdict 的 `dry_runs`；随后提交到**影子 store**，不导出 feed，也不进入生产账本。如果每晚都从空仓干跑，第二天起就报不出"拟交易手数"，所以采用这种方式。每日报告包含：目标 SHA、`available_at`、执行价、不可交易清单、拟成交与手数、费用、NAV。
- **预演账户（在函要求之外加做）**：`m02-futures-observation-v2-rehearsal` 从 10-30 起，用真实数据每晚运行，到 11-06 为止。目的是在影子运行之前先把真实数据的问题暴露出来。

## 九、R9 夜链与验收接口

- `scripts/m02_v2_nightly.{py,sh}`，生产账户 21:05 运行、21:35 重试一次。v1 的 `m02_futures_nightly` 没有改为 v2：它仍在 v1 的 release 上运行到 11-13，见第一节。
- 闸序：封账 → 到期区间 → 目标判定（不合格即 `target_missing`）→ 整所分钟线 → 执行价与限价 → 规格 → 结算价 → 干跑（store 副本）→ 提交 → feed 与 `daily-status.json` → 金额链自检（另核对提交 NAV 与干跑 NAV 是否一致）→ verdict。多日补算的逻辑与 v1 相同。
- **复算所需证据**：每个区间的 `ledger.input_json` 保存完整 packet，包括目标权重与 SHA、`available_at`、执行价、`tradable` 与 `untradable_reasons`、结算价，以及 `target_missing` 的原因和证据。`state_json.audit` 保存成交、费用、所用规则 SHA、`target_status`、`untradable`。新登记的合约规格在 `reference_updates` 表里（instruments 与 fee_rules 全量）。目标包原件在 `targets/<T>/`。
- **测试**：新增 `tests/test_forward_daily_policy.py`（25 项）、`tests/test_m02_v2_nightly.py`（20 项）、`tests/test_m02_v2_specs.py`（8 项），覆盖函中要求的六类场景：每日目标校验、目标缺失沿用持仓、不可交易沿用持仓、整所缺数 fail-closed、规格缺失 fail-closed、封账后不可再推进。全量测试结果：本机 **2624 passed**（7 分 01 秒）。Debian release 上 v2 相关 157 项中 155 项通过；另 2 项失败是因为测试调用系统 `/usr/bin/python3` 做 jsonschema 校验，而 Debian 系统 Python 未装 jsonschema，在 v1 release 上同样复现，属于既有环境问题，与本次改动无关。
- **mutation**：每个变异都在清除 `__pycache__` 后以 `-B` 运行，测完逐字节还原。

| # | 变异位置 | 结果 |
|---|---|---|
| E1 | 引擎：DCE 匹配状态不重绑 | 2 红 |
| E2 | 引擎：不可交易持仓不保留手数 | 2 红 |
| E3 | 引擎：目标缺失不走持有路径 | 2 红 |
| E4 | 引擎：不可交易合约仍按目标定手数 | 1 红 |
| E5 | 引擎：无价持仓不取上日结算价作参考 | 1 红 |
| E6 | 引擎：同日也重绑匹配状态 | 1 红（补测试后） |
| N1 | 夜链：去掉 `available_at` 窗口判定 | 2 红 |
| N2 | 夜链：涨跌停判定 `>=` 改 `>` | 2 红 |
| N3 | 夜链：去掉整所分钟线闸 | 1 红 |
| N4 | 夜链：去掉 `signal_date` 判定 | 1 红 |
| N5 | 夜链：结算价闸只查持仓 | 1 红 |
| N6 | 夜链：不登记新合约规格 | 7 红 |
| N7 | 夜链：忽略封账 | 1 红 |
| N8 | 夜链：`--dry-run-only` 仍提交 | 1 红 |
| S1 | 规格：日内两列对调 | 2 红 |
| S2 | 规格：保证金 +3pp | 2 红 |
| S3 | 规格：裁决推广到未点名品种 | 1 红 |
| S4 | 规格：郑商所代码不去十年位 | 2 红 |
| S5 | 预检：已换出的合约也报到期 | 1 红 |

**预演账户实跑（10-28 20:08）**：genesis 完成，`initial_at` 为 `2026-10-28T20:08:01.726846+08:00`，`config_hash` 为 `23eff6da…`，`engine_hash` 为 `e30cb3eb…`。四道闸全部通过：G1 `now ≤ 10-29 15:00`；G2 `next_interval` 为 10-30，跳日探测被拒并返回 "no missing-day skip"；G3 config、日历、引擎三项一致；G4 窗口为 [10-29 15:00, 10-30 09:30]。当晚夜链返回 SKIP（无到期区间），生产账户的夜链记一条 pre-genesis skip。crontab 安装后，预演账户 10-30 起每晚按真实数据运行，结果会在后续回函中补报。

## 十、边界

只读查询；零 DDL，共享库零写入。没有新增 Wind 或付费调用。027 的原生算法和参数没有改动。首次收益通知不在本次范围内。crontab 由用户安装。涉及 PIT 与资金路径的改动都由本会话直接完成，没有委派 GLM。
