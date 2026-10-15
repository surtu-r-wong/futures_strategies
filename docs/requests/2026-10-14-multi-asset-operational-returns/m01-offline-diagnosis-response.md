# M01 离线诊断回函：零权重投影缺陷已修复

2026-10-15，futures_strategies → multi_asset_backtest_db。

已按追加授权完成固定归档诊断、实现修复、回归测试和一次同输入复验。**成功导出 10-15 归档输入的离线重建目标：67 行，33 多、33 空、1 个明确零权重。** 原失败记录及全部旧回函保持原字节；没有连接数据库、网络或供应商，没有取得新数据、改生产或推进账户。

实现与证据提交：`96442e6881a443e00ac5fe4f79c46c407a9e19ea`。本回函随后单独归档，最终回报另列回函提交号；修复前代码基线为 `181172f`。

## 根因与修复范围

第一次离线诊断精确复现了 ValueError，位置为修复前 `forward_inputs/m01.py:132` 的 current_weights，阶段 current_weight_projection。原生 panel、因子与权重计算均已成功；[去敏错误栈](m01-offline-diagnosis-evidence/diagnosis-01/manifest.json)只记录文件/函数/行号和固定校验码 missing_dominant_or_weight，不含异常自由文本、DSN、环境或 locals。

当日原生排名截面为 **67 个品种**，PB 居中，原生 weight 明确为 `0.0`，主力合约为 **PB2612.SHF**。`cta_carry/risk.py:scale_weights` 返回稀疏映射，设计上省略零权重键；`citic_index/book.py:lever` 再用 pandas map 映射回完整权重表时，该零键变成 NaN。M01 投影原先把这个 NaN 误判为缺失权重，拒绝整张目标。这是稀疏结果转完整表的兼容缺陷，不是缺少原生信号、合约或波动预热。

修复仅在 [current_weights](../../../forward_inputs/m01.py) 中，对原生 `weight==0` 的行恢复 `target_weight=0.0`；非零权重逐值沿用原缩放结果，不填其他缺失值，不重排、不归一化、不补本金或再次缩放。原生因子、排名、原生缩放函数、策略参数、源数据正常化和生产代码均未修改。

这也明确了上一轮归因限制：先前只有阶段和异常类型，不能据其判定根因；本次同输入离线复现提供了准确抛错位置和中间值证据，现已排除该次错误来自原生 panel/因子阶段的假设。

## 固定输入、运行次数与时点

唯一原始输入仍为 `/home/elfbob/claude-code/futures_strategies/output/forward_inputs/m01-20261015/current-target/source-daily.csv.gz`，SHA-256 为 `9cf01433ea3157e686ef9c8f91a72c69f65f0cb15cc78a472b72d6f256f86b4e`；参数 SHA 为 `fe2730f60fb5b2db2a6afb3189187009bca3917a05ffd00d9baf5470660af657`。程序核对上述固定值及原生产观察、24 个原生依赖摘要；本次新离线入口、投影适配实现分别记录实际源码 SHA。

维持 027 R150、close_to_close、execution_lag=1、252 日波动率窗口、900 自然日输入（2024-04-28..2026-10-15）。每次读取原归档 459,810 行，原规则正常化接受 388,622 行；未缩短窗口、重新取数或做参数试验。必要历史计算只服务当前权重与波动率，不导出历史收益/绩效比较。

| 离线运行 | 用途与真实时间（+08:00） | 结果 |
| --- | --- | --- |
| diagnosis-01 | 20:02:35.501623 → 20:03:21.909596；复现原 ValueError | 精确定位 current_weight_projection，保存当前日原生权重与主力表，未发布目标 |
| verification-02 | 20:05:31.830594 → 20:06:21.433429；修复后同输入必要复验 | offline_target_ready_execution_blocked，67 行完整权重 |

本次新增离线管线运行 **2 次**；此前真实取数后的失败计算仍为原来 1 次，没有再次调用旧查库 CLI。两个新目录分别位于 `output/forward_inputs/m01-offline-20261015/diagnosis-01/`、`verification-02/`。单线程 BLAS/OMP，虚拟内存限制 2,048,000,000 bytes；两次峰值 RSS 均为 702,016 KiB，约 686 MiB。

新目标首次取得时刻 `2026-10-15T20:06:17.651021+08:00`，新包 available_at 为 `2026-10-15T20:06:21.433269+08:00`。manifest 的 artifact_kind 为 archived_input_offline_reconstruction，source_version 明确带 `offline-20261015`，source_historical_available_at=null。**这些是本次离线重建的实际时间，不能写回为 19:37 失败时已经发布的实时目标。**

## 目标包与独立验收

完整便携小文件已随提交归档至 [verification-02](m01-offline-diagnosis-evidence/verification-02/)，原始 900 日压缩输入仍只留原暂存位置，不重复分发。

- [targets.csv](m01-offline-diagnosis-evidence/verification-02/targets.csv)：完整 contract/target_weight、signal_date、原生 raw_weight/vol_scale 与当日 close 参考；目标 SHA-256 `3ce0759efb9c4a3f2ae105fc864768ab50eeccd0d107ab0b9b9ca260e909f248`。
- [source-parameters.json](m01-offline-diagnosis-evidence/verification-02/source-parameters.json)：原参数字节未变；没有新增本金、费用默认值或执行数量。
- [manifest.json](m01-offline-diagnosis-evidence/verification-02/manifest.json)：来源/代码/参数/全部文件 SHA、阶段、时刻、身份及兼容既有字段的 source_receipt。
- [验收结果](m01-offline-diagnosis-evidence/acceptance.json)：两次运行的 current-native-weights.csv 和 current-native-legs.csv 逐字节一致；所有 67 合约唯一且目标权重有限，PB2612.SHF 明确保留零值。原生权重乘一次缩放、再按原杠杆上限独立核验，最大浮点差 `4.44e-16`。

总毛权重 `2.2617946883905655`，净权重约 `−7.81e−17`（浮点误差），未用保证金倒算，也未为凑净敞口再次归一化。67 行目标及配套收据已通过现有 forward_account.adapters.read_targets 校验，未初始化或推进账户。完整权重数 67 不等于归档当前日的 74 个可接受原始品种或 73 个主力行，验收分别记录，未混用。

[证据索引](m01-offline-diagnosis-evidence/evidence-index.json)提供小文件摘要、旧回函摘要及原始输入位置。旧失败目录的文件集合、manifest 及每个文件 SHA 均核对未变。

## 价格、规格与持续限制

[preview-evidence.json](m01-offline-diagnosis-evidence/verification-02/preview-evidence.json)只使用固定归档中的 10-15 close，标明 eod_close_reference、归档身份和本次读取时刻；不将其称为新行情或次日可成交价，精确 price_at 与源系统历史 available_at 继续为空。

授权归档没有正式逐合约规格。可买/可卖证据、正式乘数及有效期、保证金率与单位、费用适用规则、最小下单手数、到期限制和有效执行会话仍缺，未另查数据库/网络补齐，未反推乘数或默认保证金/费用。valid_execution_interval=null 不表示无限有效。真正结算的日更适配仍单独列缺，不作为本次单时点权重导出的闸门。

因此，本包可供 M01 **离线单时点权重/格式验收**，不是已经批准的实时可执行组合或账户输入；没有初始化账户、advance、ReturnFeed、正式 latest、生产部署、下单或通知。

## 测试与复现

先新增零权重回归，确认修复前在同一 current_weights 校验处失败；修复后 **26 passed**：11 项 M01 当前投影（含零行保留、其他缺失仍拒绝、微小非零不归零）、4 项纯离线隔离/固定摘要/目录保护/重建身份、11 项原生 book。Ruff check/format 和本次差异空白检查通过。

[离线入口与运行说明](../../operations/m01-offline-diagnosis.md)记录实际两次命令和目录。安全合成复验命令：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_m01_current_target.py tests/test_m01_offline.py tests/test_citic_book.py -q'
```

真实归档诊断及必要复验均已完成，输入/代码未变时复用这次证据，不因汇报或提交重复计算。本次没有改共享库、回填、联网取数、生产作业或原策略语义；其他人的工作区修改保留。
