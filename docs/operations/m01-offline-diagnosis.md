# M01 固定归档离线诊断与重建

入口 `python -m forward_inputs.m01_offline --output-dir <全新目录> --purpose <本次用途>` 只接受本次已授权的固定归档，不提供取数、策略参数或输入日期选项。它不调用 `forward_inputs.m01` 的查库 CLI。

固定输入为 `output/forward_inputs/m01-20261015/current-target/source-daily.csv.gz`，SHA-256 `9cf01433ea3157e686ef9c8f91a72c69f65f0cb15cc78a472b72d6f256f86b4e`；参数文件为同目录 source-parameters.json，SHA-256 `fe2730f60fb5b2db2a6afb3189187009bca3917a05ffd00d9baf5470660af657`。另核对原归档 production-observation.json 及 24 个原生依赖文件摘要。保持 027 R150、close_to_close、T+1、252 日波动窗口、900 日输入窗口，禁止用新默认值替换归档参数。

原生计算代码保持不变。`forward_inputs/m01.py:current_weights` 的修复只处理原生 `weight==0`：缩放函数返回稀疏映射时省略了零键，`lever` 映射回完整权重表后对应项为 NaN；适配器将明确的原生零恢复为 `target_weight=0.0`。非零权重仍逐值沿用原生缩放结果，不再次缩放或归一化，未解释的缺失值仍拒绝。PB2612.SHF 在本次 67 品种截面中即为该零权重行。

## 离线限制和失败记录

运行时阻断 socket 创建、连接、DNS 解析、psycopg2 连接以及原取数模块的配置读取；计算只读固定本地文件。这个守卫覆盖所用依赖链，不声称是任意第三方代码的操作系统级网络沙箱。测试验证这些入口会拒绝调用，并在模拟完整流程中验证不触发取数路径。

入口检查虚拟内存上限≤2 GB；命令显式限制 BLAS/OMP 单线程。输出目录必须不存在且不能在原失败目录内。计算前记录 run-start/pipeline-start；诊断与复验各用独立目录。失败只记录阶段、异常类型和文件/函数/行号，不包含异常自由文本、DSN、环境、locals 或源码行。少数适配器内部固定校验有枚举 failure_check。

阶段分别为归档正常化、原生 panel、原生因子/权重、当前权重投影、离线产物导出。只保存当前日的原生权重/主力合约中间值，不保存历史收益、指数或绩效。不能将 74 个原始可接受品种、73 个原生主力行与最终 67 个入选权重行混用。

## 产物身份与时点

成功状态为 `offline_target_ready_execution_blocked`；`artifact_kind=archived_input_offline_reconstruction`，source_version 含 `offline-20261015`，明确是 **10-15 归档输入的离线重建**。`first_seen_at` 和 `available_at` 分别记录本次新目标落盘观察和新包发布时刻，`source_historical_available_at=null`。不得回写为原失败时就有目标，更不能据此修改旧 available_at。

`targets.csv` 保留完整 contract/target_weight（包含明确零权重）、原生 raw_weight/vol_scale、signal_date 及日线收盘参考。配套 source-parameters.json 原字节复制，manifest 绑定各文件摘要及实际执行源码 SHA。manifest 最后原子落定；源归档、参数及源码在前后核对。

`preview-evidence.json` 仅把归档 close 标为 EOD 参考，记录本次离线读取时刻和归档身份；不是新行情或可成交报价。授权归档没有逐合约正式规格，保证金、费用、最小手数、可买卖状态及有效执行会话继续列缺，禁止另查数据库补齐。真正结算价日更仍非单时点权重导出的闸门，未初始化账户或产出 ReturnFeed。

## 已授权运行与安全复验

本次诊断和修复验收各执行一次：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_inputs.m01_offline --output-dir output/forward_inputs/m01-offline-20261015/diagnosis-01 --purpose reproduce_archived_value_error'
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_inputs.m01_offline --output-dir output/forward_inputs/m01-offline-20261015/verification-02 --purpose verify_explicit_zero_weight_projection_fix'
```

以上目录已经使用，入口会拒绝覆盖。原始失败目录完全保留。代码/输入未变化时复用已完成验证，不再仅为汇报重复真实归档计算。

合成回归测试不访问网络或数据库：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_m01_current_target.py tests/test_m01_offline.py tests/test_citic_book.py -q'
```
