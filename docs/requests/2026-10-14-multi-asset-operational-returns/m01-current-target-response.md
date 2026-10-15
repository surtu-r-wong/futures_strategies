# M01 续办回函：当前数据就绪，唯一一次目标计算失败

2026-10-15，futures_strategies → multi_asset_backtest_db。

本次选择 **citic027_r150_t1**，按授权完成生产来源核对、只读数据预检、隔离导出实现及唯一一次真实计算。五交易所数据前沿已到 2026-10-15，900 自然日输入读取及月度行数对账通过；但**原生计算与当前权重投影阶段返回 ValueError，未生成 targets.csv，没有当前可用目标包**。不把数据就绪写成目标就绪，不以旧目标替代，也没有第二次计算、调参或缩短窗口。

## 生产版本、当前文件与实现

正确生产 checkout 为 WSL `/home/ghls/claude-code/futures_strategies`，提交 `5511463208df9598ad02a04a1b7c7dc83586478d`。先前一次 SSH 检查超时，必要只读重试成功；19:31:59（+08:00）的[生产观察](m01-current-target-evidence/production-observation.json)记录实际目标路径和 24 个原生依赖/入口文件 SHA，相关 tracked changes 为空，本地文件与远端摘要全部一致。检查的 output/targets 中最新 027/bmom020 仍为 09-16 信号，不外推其他未知目录不存在更新产物。

开发机 checkout 为 `/home/elfbob/claude-code/futures_strategies`，运行时 HEAD `032267548e877f33723dd1827ebe25074263dce7`。当前导出入口另有独立源码摘要；生产参数及实际源码分别留证，不以今天 HEAD 追认旧 CSV 的生成版次。没有改生产 checkout。

新增 [forward_inputs/m01.py](../../../forward_inputs/m01.py) 与[运行说明](../../operations/m01-current-target.md)。入口复用原 `PRODUCTION` 的 R=150、close_to_close、execution_lag=1、252 日波动窗口、900 自然日预热；使用原生 `build_panel/build_from_panel/rolling_vol_scale/lever`。只投影当前 contract/target_weight 及原始权重、缩放因子，不要求或默认本金，不生成 notional/lots，不使用保证金倒算权重，不额外乘第二次杠杆。

原生日常 shell 会写生产目标和日志，且出单需要本金；本次没有调用它。数据库使用现有配置建立只读、REPEATABLE READ 事务并核验 transaction_read_only，限定 SELECT 和查询超时，无重试循环。原算法内部的历史收益仅用于必要波动预热，不输出历史指数/收益/NAV，不做绩效比较或参数选优。

## 实际计算与阻塞证据

| 项目 | 实际结果 |
| --- | --- |
| 前沿 | CZC/DCE/GFE/INE/SHF 均为 2026-10-15；[预检记录](m01-current-target-evidence/preflight.json)以及计算事务再次检查的结果一致 |
| 原始窗口 | 2024-04-28..2026-10-15，固定 900 自然日；18 次 SELECT 覆盖前沿、16 个原窗口分块和月度计数对账 |
| 数据行 | 原始 459,810 行，正常化接受 388,622 行；按原规则排除 5,702 条代码不可解析行、65,486 条 OHLC 不合格行，不将这些排除直接认定为失败原因 |
| 当前日结构 | 10-15 可接受 697 行、74 个品种；这不是策略最终入选数。标准化日期/合约键无重复 |
| 计算开始 | 2026-10-15T19:36:56.929606+08:00；开始前已写 calculation-attempt.json |
| 终止与次数 | 2026-10-15T19:37:07.151993+08:00，ValueError，phase=single_native_target_calculation；真实目标计算累计 **1 次** |
| 内存 | 虚拟内存硬约束 2,048,000,000 bytes（`ulimit -v 2000000`）；峰值 RSS 744,216 KiB，约 727 MiB，不是触及内存上限的失败 |
| 目标/发布 | targets.csv 不存在；target_status=not_generated；first_seen_at/available_at 均 null，没有目标 SHA 或虚构发布时刻 |
| 持久产物 | `output/forward_inputs/m01-20261015/current-target/` 仅保留输入、参数、生产观察、尝试标记与失败 manifest，未碰正式 latest |

[run-manifest.json](m01-current-target-evidence/run-manifest.json)完整保留 SQL/参数（仅日期、交易所等非敏感条件）、逐查询实际取得时间、源代码逐文件 SHA、数据与参数 SHA；[证据索引](m01-current-target-evidence/evidence-index.json)提供交付文件摘要与核验结果。900 日原始数据只保留在独立暂存的 source-daily.csv.gz，不随 Git 分发，不写入共享库。

关键摘要：

- 原始输入 source-daily.csv.gz：`9cf01433ea3157e686ef9c8f91a72c69f65f0cb15cc78a472b72d6f256f86b4e`。
- [实际参数](m01-current-target-evidence/source-parameters.json)：`fe2730f60fb5b2db2a6afb3189187009bca3917a05ffd00d9baf5470660af657`。
- 执行时完整来源指纹集合的 canonical SHA：`c9ef5df0578cf3f758b17f2d673e747f3038e40fb835870c5cd15d5471b34300`；逐文件值见 run-manifest。

**错误定位限制须保留：**该次运行仅记录异常类型与联合阶段，没有保留异常文本或 traceback。失败阶段的调用位置为[实际执行版入口](m01-current-target-evidence/executed-m01.py.txt)第 441 行（build_panel/build_from_panel）和第 442 行（current_weights）；这两个位置是阶段调用点，**不是已确认的具体抛错帧**。不能据现有证据断言是源数据、原生策略还是本次投影校验造成，也不能把它归为既有问题而排除本次回归。

失败后只对已有压缩输入做了[逐行结构审计](m01-current-target-evidence/source-structure-audit.json)，没有调用任何策略函数或新增目标计算，排除了标准化键重复这一候选原因，但未定位根因。当前入口已补充更细阶段和仅含文件/函数/行号的错误栈，明确不保存异常内容、代码文本、locals、DSN 或环境值。该改进只经合成测试验证；原失败 manifest 与实际执行版源码快照保留原字节，不用新版本冒充已执行版本。

下一步建议：如继续定位真实错误，需汇总方取得**新增计算/诊断授权**后，使用细分阶段和去敏栈定位，再决定修复。不得直接换目录重跑，也不建议在根因未知时改参数、缩窗口或改共享库。本回函不执行该下一步。

## 执行参考、规格与单时点边界

已有 public.futures_daily 读取了真正独立的 open/close/settle 字段，原始窗口留有 10-15 的数据；但本次未产生目标合约集合，因此没有把任意行情集冒称“当前目标所需执行参考价”。日线 close 只能作为带日期的 EOD 参考，不等于下一执行会话的成交报价；历史源系统 available_at 未取得，不能用查询时刻倒填。

[预检](m01-current-target-evidence/preflight.json)确认 public.futures_contract_info 实际具备合约乘数、多空保证金、最小价格、最后日期、手续费收取方式与开/平/日内费用、交割月下单整手数和创建/更新时间字段。隔离适配代码已实现按目标合约/同日读取及保留原始字段，合成测试覆盖；真实运行在到达逐合约查询前失败，故**尚无真实逐目标合约规格交付**。

实际可买/可卖证据、保证金原值单位/有效期、一般最小下单手数、费用单位与适用范围、最后日期含义、明确执行会话仍待核实。不会根据 volume>0 或有日线行情填 tradable=true，不反推执行乘数、不默认保证金或费用，不把交割月整手数当一般最小手数。有效执行区间保持 null，不能被解释为无限有效。

真正结算的日更适配单独标为 not_adapted_not_an_m01_target_gate；缺日结没有阻塞这次当前目标尝试。当前实际阻塞是目标计算失败，非账户尚未初始化、缺资金或未产 ReturnFeed。

## 测试、复现与边界

新增 8 项合成测试，加原生 book 11 项，19 passed；为排查环境/管线问题，另运行既有管线、品种池、因子、合约链、权重、指数合成测试 63 项通过，**合计 82 passed**。这些测试未复现真实数据失败，不能据此确认真实根因；Ruff check/format 通过。覆盖原生目标权重/杠杆对照、252 日窗口不足拒绝、缺失/重复主力拒绝、价格/成交量不推导可成交、原始保证金不自动定单位、元数据冲突不反推、只读事务、失败无重试、900 日窗口不变、一次尝试标记/目录防复用及错误栈不泄露文本。

安全复现仅运行合成测试；不会再次计算真实目标：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_m01_current_target.py tests/test_citic_book.py tests/test_citic_pipeline.py tests/test_citic_universe.py tests/test_citic_factor.py tests/test_citic_returns.py tests/test_citic_legs.py tests/test_citic_weights.py tests/test_citic_index.py -q'
```

已执行的真实命令和原路径留在运行说明，明确标注不可据此再次计算。旧 response.md、implementation-response.md、input-adapter-response.md 均未修改。没有共享库写入/DDL、回填、付费补数、生产脚本/定时器更改、账户初始化/advance、绩效研究、下单或通知；本回函仅落本仓并本地提交。
