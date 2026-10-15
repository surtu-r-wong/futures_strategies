# 前向 paper 账户：配置、输入与运行

本模块是旁路软件，入口 `python -m forward_account`。不连共享库、不拉行情、不下单、不推送，不调用原研究回测或目标生成作业。`forward_account.demo` 只生成合成验收文件。真实初始化和生产启用须待统一验收及参数落实，本次没有执行。

## 核算模型与边界

当前支持 CNY 线性期货 paper 账户，日盘一次目标执行、每日真实结算价估值/变动保证金结算。日历逐项给定前次结算、执行和本次结算时点，收益区间为两次估值之间。夜盘已有持仓的变动包含在前次结算至日盘执行的 PnL 中；不模拟夜盘新下单、分钟止损或多次盘中成交。

`tsindex_pure`、`tsindex_bmom020`、`citic027_r150_t1` 三个适配器分别读取原生 `contract/target_weight`。前两者是同一原生策略的不同源参数版本，最后一个固定 R=150、close_to_close、execution_lag=1。source 参数是信号生成账的定义；**本模块日盘模拟执行/结算账户是独立的新 paper 版本，不能宣称复现原 close_to_close 研究曲线**。

保留源目标的名义/NAV 权重，不再乘 vol_scale、归一化、选主力或使用目标表的 lots/notional。显式整数取整会产生可审计的目标偏离；fractional 模式是允许分数合约的研究型 paper 规则，均须账户配置确认，不默认选取。

固定数量核算（q 为带符号合约数量，m 为合约乘数）：

```text
执行前 NAV = 前次 NAV + Σ q_old × m × (execution_price − previous_settlement)
q_target = target_weight × 执行前 NAV / (execution_price × m)
fee = Σ |q_target − q_old| × execution_price × m × cost_bps / 10000
结束 NAV = 执行前 NAV − fee + Σ q_target × m × (settlement − execution_price)
daily_return = 结束 NAV / 前次 NAV − 1
unit_nav = initial_unit_nav × 结束 NAV / initial_capital
margin = Σ |q_target| × settlement × m × margin_rate
结束 cash = 结束 NAV；free_cash = cash − margin
```

价格变动部分复用 `common.minute.account.EventAccount`；每个估值事件将固定数量转换为当时的敞口权重，而非持有不变权重或重新跑研究历史。费用按真实模拟成交名义单独入现金账，`cost_bps` 是每侧成交的总费用率。期货名义金额不作为买入股票那样扣现金，保证金冻结不作为费用。收盘快照在每日变动保证金结算后保存，未结算 PnL 已归入现金；审计分别保留隔夜/日内 PnL、成交、费用与执行前 NAV。

当前仅支持明确的无现金利息、无外部申赎、无借款规则。现金流、多币种、期权、非正价格、按手/今平差异费用、动态保证金率/乘数迁移、强平、限价排队与容量模型尚不支持。合同规格必须声明 `asset_type=linear_futures` 和 `currency=CNY`。保证金不足或明确不可成交会拒绝整个区间，不缩仓、替换合约或补零。

## 必填配置

配置可从合成验收的 `config.json` 看完整结构；其中每一个金额/费率/合约/日期都只是测试参数，不能直接作为生产默认值。严格校验拒绝缺字段和未知字段。

| 字段 | 含义与约束 |
| --- | --- |
| `account_id / account_version / label` | 独立账户与 paper 版本；同一原生策略的不同账户必须有不同 account_version |
| `adapter / source_version / source_parameters` | 适配器名、固定源码/参数发布身份、需逐项匹配的源参数；最低参数见 `forward_account/contracts.py:SOURCE_RULES`，可额外固定排除品种、因子集等参数。source_version 应由实际生成 checkout 的版本与 dirty 指纹/发布记录确认，不以旧 checkout 的 HEAD 代替 |
| `mode / currency / synthetic` | 当前仅 paper/CNY；真实输入必须 synthetic=false，并与目标收据及两组报价一致 |
| `initial_at / initial_capital / initial_unit_nav` | 资金和单位 NAV 必填。真实配置的 initial_at 必须显式 null，init 在事务前记录实际当前时间；合成测试才可指定测试时间 |
| `cost_bps / fee_description` | 显式费用值、已计及未计项说明；无隐含 4 bp 或零成本默认值 |
| `lot_rule` | fractional 或 nearest_integer_ties_even；数量以执行前 NAV 和执行价重新计算，费用扣除后不再二次缩放 |
| `cash_rule / financing_rule` | 仅 no_interest_no_external_flows / no_borrowing；这仍是待确认的模型选择，不代表真实账户天然满足 |
| `execution_rule / valuation_rule` | 仅 calendar_day_open / daily_settlement；结算价不得以 close 冒充 |
| `calendar_id / calendar_sha256` | 与完整日历一致，摘要为解析后 JSON 的 canonical SHA-256（`forward_account.contracts.digest`），不是原文件空白字符的 hash |
| `instruments` | 逐合约 asset_type、currency、multiplier、margin_rate、valid_from、valid_to、source；必须有参数依据和有效区间。缺乘数/保证金拒绝，不能沿用 CSV 的估计 multiplier 冒称正式规则 |

日历 JSON：`calendar_id, timezone=Asia/Shanghai, source, complete=true, coverage_start, coverage_end, intervals`。每条 interval 必须包含 `trade_date, signal_date, start_at, execution_at, end_at`；相邻 start/end 完全衔接，signal_date 等于上一条 trade_date；不能用观测到的行情日期拼出日历。source/complete 是供给方的来源与完整覆盖声明，软件校验结构、时间和摘要，**不独立证明交易所日历内容正确**。当前资料未核验为真实日历，实际数据供给仍待落实。

真实初始化后保持空仓，跳过已经开始的部分区间；只从其后的第一个完整区间核算。初始化不会产收益；首个成功完整区间的 trade_date 才是 feed 的 inception_date。初始账户不是旧仓迁入，不接受期初持仓或倒填历史 NAV。

## 每次输入

`advance --input interval.json` 的路径字段相对于该 JSON 的目录：

```text
trade_date
target_csv        原作业的 next_targets.csv
source_metadata   Carry 同次运行 xlsx 的 run_config；027 同次运行 config.json
source_receipt    配套来源收据 JSON
execution        {kind, at, available_at, source, synthetic, prices, tradable}
settlement       {kind, at, available_at, source, synthetic, prices}
```

`prices` 按原始 contract 映射到未复权价格。执行价必须覆盖原持仓和新目标，包括旧合约平仓；结算价必须覆盖执行后的持仓。执行 `tradable` 对每个所需合约都须显式 true，不能把缺行情或涨跌停无成交当作可成交。source 应携带实际行情来源/版本证据；at 与日历事件精确对应，available_at 不早于报价时点且不晚于核算时点。没有行情采集器时只交参数/输入缺项，不调用历史重算或供应商补数。

来源收据包含 `source_version, available_at, csv_sha256, metadata_sha256, row_count, signal_date, synthetic`。两个 SHA-256 对应原始文件字节；row_count 防止不完整文件被当空仓；空目标只接受有显式 row_count=0 的完整收据。收据须由实际生成/接收流程留证，available_at 在信号日结算后且不晚于模拟执行时点；不能用旧文件 mtime 或今天读取时间补造过去可用时刻。已有旧文件没有这种收据，当前不合格。

Carry 可读取原生 xlsx（openpyxl read_only，不读巨大信号表）；也接受保留原始参数值的独立 JSON 摘录，但其与原始生成版本的绑定仍须来源证据负责。JSON 与 CSV 都逐项核对配置中的固定参数；不会因文件名像 027 而接纳旧 lag=0 或纯指数参数。新适配器可扩展 `SOURCE_RULES`/原生读取器和输入验证；不能通过改配置把尚不支持的策略伪装成这三个适配器。

## 本地持久状态、修订和发布

独立 SQLite 由调用者指定 `--state`，生产部署须位于持久目录。这里的本地 DDL 不涉及 Market Monitor。accounts 保存不可变初始配置/日历/引擎文件指纹；ledger 在事务中追加输入、状态、净收益和修订号，更新 head；attempts 单独保存失败；reference_updates 保存追加参考资料；publications 保存不可变发布内容。

同一账户同一交易日同一输入只读返回原成功结果，不改 available_at、不重复费用。漏日不能跳过；坏输入事务回滚，保留前一成功时间和状态。只支持尾日显式 `--revise-reason` 修订，并从尾日前的状态计算一次，旧版永存；较早日修订拒绝，不自动重放历史。若需要更早修订、经济参数变化或引擎升级，须另行设计和验收迁移，不重置账户掩盖断点。

`extend-references` 接受完整新日历与 instruments JSON：只能追加未来会话、新合约，或在同一规格和来源下延长合约有效期；不能改旧会话/旧合约经济参数。追加不重置 NAV、不改 feed 身份，并记录引用版本。交易日历覆盖用尽时停止推进，不能把没有日历当作休市。

export 从成功状态构建全初始化以来的日收益；不只保留当前月，因此跨月周也完整保留已有区间。零条收益拒绝发布。发布 JSON 严格沿用原 ReturnFeed Schema；版本字段由独立 account_version、初始配置摘要和引擎摘要组成，source_version 也包含在配置摘要中。

feed 首次构建时写真实 generated_at，并先持久提交发布内容，再同目录临时文件、fsync、原子替换目标；每版另存 `<输出文件stem>.revisions/<内容hash>.json`。失败保留原 latest，重试使用已保存内容和时间，不再核算。并发发布用文件锁；同一 store 的多个账户统一导出，避免逐账户覆盖别的策略。**同一正式输出只能指定一个权威 store**；跨不同 store 的合并不在本模块范围。

导出可保留较旧但真实的成功区间，不会为失败账户补零；下游按交易日及可用时点判缺失。status 同时输出成功快照、下一预期区间和 last_failure（失败可能早于最新成功，须比较时间）。导出有独立失败记录；崩溃时若未及记失败，SQLite 回滚/发布 outbox 仍可恢复。

## 可复现离线验收

在仓库根目录执行，所有本地 Python 命令先施加 2 GB 内存上限：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_forward_account.py tests/test_carry_minute_account.py tests/test_citic_book.py tests/test_carry_targets.py -q'
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_account.demo --output-dir output/forward_account/synthetic-new-run'
```

demo 要求一个尚不存在的新目录，生成三套源版本输入、独立账户、两区间结果与重复运行验收；输出 `synthetic-returns.json`、`synthetic-account-audit.json` 和 `synthetic.sqlite`。不会写真实 latest。Schema 测试优先当前 Python 的 jsonschema；本机 .venv 缺该包时使用已安装验证器的 `/usr/bin/python3`。其余运行仅用标准库及原有账户代码，读 Carry xlsx 才需要既有 openpyxl。

CLI 独立进程复现（先由 demo 生成输入，在其目录下另建测试 store）：

```bash
bash -c 'ulimit -v 2000000; .venv/bin/python -m forward_account --state output/forward_account/synthetic-new-run/cli.sqlite init --config output/forward_account/synthetic-new-run/citic027_r150_t1/config.json --calendar output/forward_account/synthetic-new-run/citic027_r150_t1/calendar.json'
bash -c 'ulimit -v 2000000; .venv/bin/python -m forward_account --state output/forward_account/synthetic-new-run/cli.sqlite advance --account synthetic-citic027_r150_t1 --input output/forward_account/synthetic-new-run/citic027_r150_t1/interval-13.json'
bash -c 'ulimit -v 2000000; .venv/bin/python -m forward_account --state output/forward_account/synthetic-new-run/cli.sqlite advance --account synthetic-citic027_r150_t1 --input output/forward_account/synthetic-new-run/citic027_r150_t1/interval-14.json'
bash -c 'ulimit -v 2000000; .venv/bin/python -m forward_account --state output/forward_account/synthetic-new-run/cli.sqlite export --synthetic --output output/forward_account/synthetic-new-run/synthetic-cli.json'
```

真实部署拟用相同 init/advance/export/status 入口，配置为 synthetic=false，输出为运行 checkout 的 `output/multi_asset/returns/latest.json`；部署前须把 state 与发布归档放持久位置。**这些真实动作尚未执行，也没有安装 timer。**

## 触发方案与依赖

先独立收存原目标及来源收据；完整日历指定的结算已结束、旧/新合约执行报价、成交可行性证据和结算行情全部取得后，按下一 trade_date 调用 advance，成功后 export。失败只重试当前区间；若账已提交而 export 失败，只重试 export 即可。建议未来调度按输入就绪触发并以 status 检查滞后；不更改原目标作业。

本机合成验收为小文件秒级任务，不据此承诺生产 SLA。实际完成时刻取决于目标发布、各交易所行情和结算数据到齐、合法参数/日历覆盖。20:45 和原 T+1 08:00–08:30 都不是本账户的完成承诺；生产调度、真实初始化时刻和截止时间由统一验收后落实。
