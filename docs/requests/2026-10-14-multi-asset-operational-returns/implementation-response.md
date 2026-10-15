# 回复：前向账户核算软件与离线验收

2026-10-15，futures_strategies → multi_asset_backtest_db。

已落实 [实施补充](forward-account-implementation.md) 的软件与合成验收范围：新增旁路增量 paper 账户、两条目标链路的三个源版本适配、本地持久状态及原 Schema 导出。**尚未初始化真实账户、接入真实当前行情或部署作业；目前没有真实收益样本和生产 SLA。** 待落实参数集中列在下文，不单独向用户追问，不启动历史回填。

已被接受的 `response.md` 保持原样，SHA-256 仍为 `5d9f9292aa283ebfcb2e2bcb8d87e4234adfc7c8aefdebd10e003b919d4f6925`。本函不替代原持仓/合约/杠杆契约，不重复现状盘点或研究收益排名。

## 1. 已完成代码、入口与版本

实际实现及验收主机：`elfbob-ThinkPad-X1-Carbon-Gen-8`；checkout：`/home/elfbob/claude-code/futures_strategies`。基线 HEAD 为 `f31d5acf2660c7cd48fc5fc2c595062cca1ef626`，本次代码是该工作区的新增未提交文件，**不将基线 HEAD 冒称包含本实现的发布提交**。没有在 WSL2 或 Debian 安装、迁移或检验新代码；旧 checkout 不作为当前生产版本证据。

本地候选实现标识为 `forward-linear-futures-v1`，实际运行文件摘要见 [implementation-manifest.json](implementation-manifest.json)。账户初始化还会固定引擎文件指纹，后续文件变化不能静默接续旧状态；正式部署时应使用该验收候选对应的固定 checkout/文件摘要，再登记真实发布版本。

| 交付 | 固定位置/入口 |
| --- | --- |
| 参数、源版本和日历契约 | [forward_account/contracts.py](../../../forward_account/contracts.py) |
| 原生 CSV + Carry run_config/027 配置适配 | [forward_account/adapters.py](../../../forward_account/adapters.py) |
| 固定数量、现金、费用与结算核算 | [forward_account/engine.py](../../../forward_account/engine.py)，复用既有 `common.minute.account.EventAccount` 的价格变动核算 |
| 持久状态、幂等、修订、发布恢复 | [forward_account/store.py](../../../forward_account/store.py)，独立本地 SQLite，不连 Market Monitor |
| CLI | `python -m forward_account --state <持久SQLite绝对路径> init / advance / status / extend-references / export` |
| 合成生成器 | [forward_account/demo.py](../../../forward_account/demo.py)，`python -m forward_account.demo --output-dir <新的合成目录>` |
| 运行与参数说明 | [forward-account.md](../../operations/forward-account.md) |
| 测试 | [test_forward_account.py](../../../tests/test_forward_account.py) |

真实发布路径约定不变：运行 checkout 下 `output/multi_asset/returns/latest.json`；本机绝对路径 `/home/elfbob/claude-code/futures_strategies/output/multi_asset/returns/latest.json` 当前没有生成。state 路径由部署配置显式传入，须处于持久目录，不能使用临时目录；本轮只在独立合成目录创建 SQLite。下游 Debian 输入副本仍由 multi_asset_backtest_db 衔接，本项目没有向其写入或传输文件。

## 2. 核算支持矩阵（原 14 条）

这是原生入口的实施支持状态，不是策略选择名单或新增分类注册表。代码中的 SOURCE_RULES 仅是适配器版本校验，不维护组合、权重或模块分类。

| 原生条目 | 当前支持状态与缺口 |
| --- | --- |
| `cta_gtja / medium_equal_weight` | 未接持续账户；已有研究 NAV 可复用，需解决前向信号日到实际收益区间、具体合约目标及版本/日历来源适配 |
| `cta_gtja / high_composite` | 未接；同上，三/六因子与中/高波须独立版本，不能套 tsindex 适配器 |
| `cta_carry / daily` | 未接；研究资金账可复用，需保留日线止损/成交规则，不能由无止损的 tsindex 适配器替代 |
| `cta_carry / minute` | 未接；需逐事件执行输入、真实会话与适配，现有分钟资产覆盖限制仍在；未重跑分钟研究 |
| `cta_carry / tsindex` | **已实现原生目标适配与合成贯通**：`tsindex_pure`（λ=0）和 `tsindex_bmom020`（λ=0.20、500/0.9/monthly）分别 pin 参数/源版本与 paper 版本；真实启用待下文参数与实时输入 |
| `cta_bollinger` | 未接；已有研究账本可复用，需分钟事件/会话适配，不改变登记 ddof 版本 |
| `cta_dow` | 未接；已有研究账本可复用，需分钟事件/会话适配，保留 prior_extreme/latched 与旧读法差异 |
| `cta_continuous` | 未接；需独立面板、执行事件及版本适配，不将其输入文件作为账户收益 |
| `index_open_momentum` | 未接；已有研究账户可复用，但持续分钟输入、CLI 后段及换月规则缺口仍需落实 |
| `citic_index / CICSF023.WI` | 未接；当前纯指数不满足账户净收益口径，需明确可成交合约目标及费用/账户适配 |
| `citic_index / CICSF025.WI` | 未接；保留纯指数控制臂身份，不能混入 tsindex 的执行账户 |
| `citic_index / CICSF026.WI` | 未接；探针缺持续逐日账户输入，扫描摘要不能转成收益 |
| `citic_index / CICSF027.WI` | **已实现原生目标适配与合成贯通**：`citic027_r150_t1` 固定 R=150、close_to_close、execution_lag=1；旧指数、settle_to_settle 复刻与新日盘模拟执行账分开；真实启用待参数/实时输入 |
| `scripts/research/alpha013_commodity.py / Alpha #013` | 未接；已有研究净收益不重造，需具体合约目标、五日偏移/成本场景及可用时点适配；没有因研究负面结论从盘点中删掉 |

以后可新增适配器及相应输入验收，不要求固定只支持两条；也不会把尚不具备输入的 12 条补零加入 feed。

## 3. 已验收的账户行为

1. **数量与杠杆**：按固定源版本接收原始 contract/target_weight。目标权重已含原生杠杆，软件不再缩放或归一化；不换主力，不把目标 lots/notional 直接当收益。已有持仓数量在下一次明确调仓前固定，按原合约价格计算 PnL。
2. **独立起点**：真实 init 用实际当前时间，不允许传入过去的初始化时间；第一段已开始的区间跳过，保持空仓等待首个完整区间。首条成功收益的 trade_date 是 inception_date，不倒填此前周/月。
3. **显式现金/费用/结算**：每侧交易名义 × 配置费率；费用扣现金，期货名义不扣本金，保证金冻结不作为成本。日结后变动 PnL 入现金，cash=NAV，另存 margin/free_cash。估值和可用时间带 +08:00，日历明确交易日、前一信号日及执行/结算时点。
4. **事务和恢复**：保存上一已成功现金、数量、结算基准、NAV 和时间；重复输入不入账/不计费，并发同日也只提交一次。坏输入、漏日、缺行情、源版本不符、不可成交或保证金不足拒绝整段，失败记录与成功状态分开。
5. **修订与扩展**：尾日修订须显式理由，从前一成功状态重算该日，旧输入/状态/修订号保留；更早日期不自动重放。可追加未来日历会话/新合约，不覆盖旧参考资料或重置 NAV。修改既有乘数/保证金经济参数及引擎升级迁移尚不支持，须另行验收。
6. **发布**：导出所有已有成功区间，符合原 Schema；真实 generated_at 在首次构建发布内容时保存，重复发布和中断恢复不刷新旧时间。发布内容先持久化，再原子替换，保留每版归档；失败可只重试 export。未完成首段则拒绝空 feed。合成输出显式标记并禁止写入正式 `output/multi_asset/returns/latest.json`。

当前模型仅覆盖 CNY 线性期货、一次日盘调仓和日结。夜盘持仓价格变化被计入跨结算区间，**不支持夜盘新执行或分钟止损**；外部现金流、多币种、期权、借款/利息、强平及更复杂费用规则都明确拒绝或未实现，不作能力承诺。

## 4. 最小合成验收与复现

实际产物目录：`/home/elfbob/claude-code/futures_strategies/output/forward_account/synthetic-acceptance-20261015-v1/`。有三套目标 CSV、参数 JSON、来源收据、两日输入、合成 SQLite、审计以及 feed。为便于回函携带，另保存 [合成收益样本](synthetic-acceptance/synthetic-returns.json) 和 [合成核算审计](synthetic-acceptance/synthetic-account-audit.json)；**这些不是策略真实收益，不得导入真实接入目录**。

三种源版本使用同一组独立算例测试核算，不代表三策略真实收益相同：合成初始资金 10,000、单位 NAV=1；A/B 乘数分别 10/5、保证金率 10%、每侧总费用 10 bp，仅供测试，均非生产默认。

| 区间 | 独立算式与实际结果 |
| --- | --- |
| 2026-10-12 15:00 → 10-13 15:00 | 09:00 以 A=100、B=200，目标 1.5/−0.5 得 A=15 手、B=−5 手，毛名义 20,000；费用 20。结算 A=110、B=190，PnL=1,750，NAV/cash=11,730，日收益 0.173，保证金 2,125 |
| 2026-10-13 15:00 → 10-14 15:00 | 次日执行 A=112、B=188，旧仓隔夜 PnL=350，执行前 NAV=12,080；平 A、B 目标 1.0 得数量 12,080/(188×5)。调仓费 33.58，B 结算 200，结束 NAV/cash=12,817.483829787236；日收益按本段结束 NAV/11,730−1 |
| 重复与恢复 | 重跑第二段状态/费用/时间不变；同时两次同日调用只留一条账。模拟发布替换前中断，旧文件保持，重试发布使用原内容/时间且不重计费用 |

测试结果：**74 passed**（27 项新增账户测试、27 项既有逐事件账户、11 项 027 资金/目标、9 项 Carry 目标）；没有跳过测试。三系列/六条收益的生成文件另经相邻 ReturnFeed Schema + format checker 验证通过。Ruff check、格式检查和 diff 空白检查通过。未运行无关全历史回测或全仓测试。

复现命令（仓库根目录；新目录名称可自定，demo 不覆盖旧验收）：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_forward_account.py tests/test_carry_minute_account.py tests/test_citic_book.py tests/test_carry_targets.py -q'
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_account.demo --output-dir output/forward_account/synthetic-new-run'
```

Schema 校验复用已有 `/usr/bin/python3` 的 jsonschema（.venv 没有该包），没有安装依赖。独立 CLI 的 init/advance/export/status 及修订、参考资料追加用法见 [运行说明](../../operations/forward-account.md)。

## 5. 待落实参数与输入

请由 multi_asset_backtest_db 汇总后集中落实；软件不以假定数值替代，也不要求各上游重复向用户提问。

| 前置条件 | 已知依据与尚缺内容 |
| --- | --- |
| 账户身份/起点 | 每个选用策略及参数版的独立 paper account_version、标签、初始资金、单位 NAV、真实初始化安排；不默认 100 万、组合比例或复用旧研究起点 |
| 源发布版本 | 适配器最低参数已从既有 tsindex 脚本/CarryConfig 和 `citic_index/daily.py:PRODUCTION` 固定。还须核实实际生产 checkout 的完整源码/dirty/参数发布版本、排除品种等需追加 pin 的参数；本机旧文件和旧 checkout 不能代替当前生产状态 |
| 目标持续供给与可用时点 | 同次生成的原生 CSV + 配置/工作簿及真实收据，绑定文件摘要、行数、signal_date、发布版本和实际 available_at。既有目标不带完整收据，本轮不补造历史时间；缺日需保留缺失状态 |
| 费用与数量规则 | Carry/027 研究有 4 bp 假设可作待确认依据，但本模块不自动继承。须明确新 paper 的总费率、已计/未计项，以及分数合约或整数 ties-even 取整；更复杂费用模型需另适配 |
| 合约规格与保证金 | 逐合约乘数、CNY/线性期货声明、保证金率、有效区间及来源证据；原目标的反推乘数不是当然可用的正式单位。10% 只在合成算例使用，没有采用下游研究参数为生产事实 |
| 执行/行情 | 是否采用本模块一次日盘开盘模拟规则；旧仓和新目标的未复权执行价、可成交证据及真正结算价、行情来源/版本和到达时间。代码没有接行情源，不能用 close 冒充 settle 或缺价填零 |
| 现金/融资 | 是否接受明确的无利息、无外部申赎、无借款模型；若不接受，须先扩展相应核算并验收，不能将未知费用设零 |
| 完整日历 | calendar_id、可信来源、完整覆盖及明确交易日/夜盘所属、日盘执行和结算时点。当前仅有合成日历；不把现有观测日期集或截止 2026-01-30 的商品会话资产当作当前完整日历 |
| 部署/运行 | 实际运行 checkout/发布版本、持久 state/发布归档目录、唯一权威 store、备份/保留策略、真实初始化和正式启用时点；此次均未执行 |

## 6. 触发方案、完成时刻与未部署边界

任务入口已交付：取得目标及可用时点收据 → 当期执行和结算行情、完整日历/合同规格全部就绪 → `advance` 当前下一交易日 → 成功后 `export`。失败保留前一成功状态；已入账而发布失败时仅重试 export。status 暴露下一待处理区间和失败记录。将来可由独立输入就绪任务触发；本轮不生成或启用 timer，不更改原出单脚本/排期。

预期软件计算为小型日增量任务，合成验收为秒级；**真实完成时刻尚不能承诺**，须以实际交易所行情/结算、目标可用时点和运行环境验收为准。原 T+1 出单时点、20:15/20:30 或汇总拟定 20:45 均不是收益 SLA。

本次没有真实账户初始化、正式 latest、生产 timer、下单、企业微信推送、跨机同步或远端部署；没有修改已接受 response.md、现有目标作业、共享库/DDL，也没有历史研究重跑、回填、付费补数。真实参数及统一验收落实后再安排启用；周/月汇总、组合比例/账本和新增收益通知继续由 multi_asset_backtest_db 负责。
