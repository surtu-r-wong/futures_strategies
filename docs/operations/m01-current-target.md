# M01：当前 027 权重与来源留证

`forward_inputs.m01` 是单时点目标导出入口，使用 `citic_index.daily.PRODUCTION` 的原生参数和 `LOOKBACK_DAYS=900`。仅支持本次选定的 `citic027_r150_t1`，不接收本金、费用覆盖、缩短预热或参数选择选项。数据与源版本缺失时失败，不切换策略或自动重试计算。

与日常 `citic_index.daily` 的关系：原日常入口先计算原生指数/权重和滚动波动率，再调用 `lever`，最后在 `next_targets` 中按本金与估计乘数产生 notional/lots。本入口复用前段计算、252 日完整波动窗口和 `lever`，仅投影当前日的原生主力合约和目标权重，不输出本金、notional、lots 或反推规格。原算法内部用于流动性筛选的乘数估计仍属原算法；它不会进入执行规格。合成测试对照原生 `next_targets` 验证相同 contract/target_weight 和总杠杆，缩放仅一次。

## 预检与唯一一次计算

先检查已登记生产路径与现成目标，不能为了改变文件日期重算。独立生产观察 JSON 应记录 `checkout, git_head, observed_at, targets, tracked_changes, local_mismatches, files`；files 对应代码中 `NATIVE_FILES` 的逐文件 SHA-256。24 个原生依赖/入口必须与实际计算 checkout 相同，不能拿当前 HEAD 充当旧文件生成证据。观察 JSON 是受信任的人工/采集证据，入口不会自动发现全部生产路径，也不独立认证其真实性。

实际运行要求虚拟内存上限≤2 GB，并使用未存在的新输出目录。数据库连接通过现有本机配置取得，既不打印也不归档配置值。连接设置 `readonly=True, REPEATABLE READ` 并核实 transaction_read_only；SQL 入口只接受 SELECT，每条限时 45 秒，失败不重试。读取当前 30 日五交易所数据前沿，必须全部到指定 as-of，才读取原生 900 自然日窗口。读取按原接口的 60 日分块，并在同一快照内按月总行数对账；然后按原正常化函数处理。

在调用一次原生 `build_panel/build_from_panel` 之前，先落 `calculation-attempt.json`。失败保留阶段/异常类型和计数；同目录重跑拒绝。目录保护不代替跨目录的业务授权计数：同一 M01 续办已执行真实计算后，不能换新目录再运行。历史输入只用于当前目标必要预热，不导出历史指数、收益、NAV、绩效比较或研究结果。

本次授权实际命令（**已执行，不作为建议重跑命令**）：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_inputs.m01 --as-of 2026-10-15 --production-observation output/forward_inputs/m01-20261015/production-observation.json --output-dir output/forward_inputs/m01-20261015/current-target'
```

未来新增计算需另有相应授权和当次预检证据。

## 交付文件与时点

| 文件 | 内容和限制 |
| --- | --- |
| targets.csv | 当前日完整 signal_date/product/contract/raw_weight/vol_scale/target_weight/signal_close_reference；保留原生杠杆，不是按保证金倒算权重 |
| source-parameters.json | 实际原生全参数、900 日窗口、信号日；其中 cost_bps 是原策略参数，不是已核准的 M01 执行费用 |
| source-daily.csv.gz | 实际只读捕获的原始 900 日输入与 SHA，仅存独立暂存，不写共享库、不随 Git 传递 |
| source-contract-info.csv | 同一快照下，目标合约在信号日的元数据原始行，不回退到其他日期或日线反推 |
| production-observation.json | 实际生产 checkout、当前目标路径和逐文件代码摘要观察 |
| calculation-attempt.json | 计算开始前的计数/代码/参数/源窗口记录，保留失败证据 |
| preview-evidence.json | 每合约价格参考、原始规格字段和逐项执行缺口；没有数据的字段显式 null |
| manifest.json | 最后原子落定的包标志：状态、计数、所有其他文件 SHA、查询与实际取得时间、源代码树摘要、源版本、source receipt |

`first_seen_at` 是新目标 CSV 写成后本次读取进程的首次观察。`available_at` 是新隔离包完成核对、准备发布的本次时刻；它不追认任何旧目标的历史可用时间。信号日是数据日期，查询的 observed_at 是取得时间；日线 close 的精确 price_at 和源系统历史 available_at 未取得时保留 null。source receipt 使用既有字段名，绑定 targets.csv 与参数原字节；不生成真实账户配置或初始化账本。

`manifest.status=target_ready_execution_blocked` 和 `target_status=ready` 仅表示当前权重已产出并留证，执行仍有缺口。`valid_execution_interval=null` 不得被下游解释为无限有效：意图是信号后下一个经核实交易日的日盘开盘，具体会话/起止时刻须实际日历及新报价证据落实。不能用旧日线开盘或收盘价假装次日可成交价。日结适配单独标 `not_adapted_not_an_m01_target_gate`，不阻塞当前权重独立交付。

## 行情与合约规格的保守映射

当前 reference_price 只接 `public.futures_daily.close`，明示 `eod_close_reference` 和数据日期；不是可成交报价。成交量与日线高低价都不用于填 tradable_buy/sell，两个字段在缺独立实际证据时保持 null。

`public.futures_contract_info` 按同日和目标裸合约代码读取；缺行、重复/冲突记录拒绝规格。合约乘数只取有效正数原值，不从成交额/成交量反推。当前表可提供多空保证金原值、手续费收取方式及开/平/日内费率或金额、最小价格、最后日期和交割月下单整手数；原值及创建/更新时间完整保留。

原表字段名不自动证明单位、生效区间或经纪商实际费用：保证金原值不自行除以 100，手续费不映射为默认 bps，`交割月下单整手数` 不冒充所有会话的最小手数，`最后日期` 不未经核实便等同最后交易日。因此有效保证金率、费用解释、有效会话、可买卖状态、一般最小手数和到期限制的核实仍可阻塞 M01 可执行预览；不能用字段齐全来掩盖语义缺失。

## 离线安全检查

不重新计算目标即可检查现有 manifest 文件摘要和真实缺口；合成测试只使用 pytest 临时目录和模拟源：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_m01_current_target.py tests/test_citic_book.py -q'
```

本入口不调用生产 shell 作业，不改正式 latest、定时器、策略参数或生产文件；不初始化/推进账户、不下单、不发送通知，也不写数据库或回填任何历史表。
