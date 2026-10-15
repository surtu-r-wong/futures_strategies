# 回复：当前输入适配与来源验收

2026-10-15，futures_strategies → multi_asset_backtest_db。

已交付三策略的只读文件适配入口、参数模板、字段/时间/来源映射及合成测试。真实旧目标已取到并完成结构检查；**三份样本均为 blocked，尚无可执行真实账户输入包**。没有重跑目标、研究或分钟历史，也没有以旧样本代替当前有效目标。

## 接口与验收

- [适配实现](../../../forward_inputs/adapter.py)：`python -m forward_inputs --plan ... --output-dir <新目录>`，只写独立暂存目录；外部 publication 绑定完整批次摘要、参数版本和真实可用时刻，再转换为已有 source receipt/interval 契约。
- [参数模板](../../../config/forward_inputs/)：三种策略分别提供 plan/account 模板；生产 publication 模板的 complete、来源证据及账户经济参数均留 null，不默认完整、不默认空仓或资本/费率。
- [操作与映射说明](../../operations/forward-input-adapter.md)：字段、时间、来源声明、失败状态、容量限制及安全复现命令。
- [测试](../../../tests/test_forward_inputs.py)：新增 27 项，加既有账户 27 项，**54 passed**。实际验证三策略保留原生 contract/target_weight、日内/隔夜账户接口、XLSX、字段映射、读中变更/混版拒绝、缺资本/真实结算/可成交证据拒绝、未来日历与新合约经既有 extend-references 接收，以及旧经济参数迁移拒绝。Ruff check/format 与本次文件空白检查通过。

适配器放在独立 `forward_inputs/`，没有改动 `forward_account/`，避免其“包内全部 Python 文件”引擎指纹变化使已存在账户要求迁移。`ready` 的 calendar/instruments 可交既有 extend-references，interval 可交既有 advance；本次仅在合成测试中实际调用这些账户接口。未扩展 ReturnFeed 格式。

## 已取得的真实来源

只读检查登记的 WSL 入口 `ghls@100.120.152.1:2223`，正确 checkout 为 `/home/ghls/claude-code/futures_strategies`，非另一份 `/home/ghls/futures_strategies`。这次观察的 HEAD 为 `5511463208df9598ad02a04a1b7c7dc83586478d`。目录检查中最新目标为 2026-09-16 信号；这仅说明所检查目录，不代表其他主机或路径没有更晚产物。

六个原文件已只读复制到本机 `output/forward_inputs/current-20261015/`，每个文件传输前、接收后与远端再次计算的 SHA-256 一致。[运输观察记录](input-adapter-samples/transport-observations.json)保存远端路径、摘要、字节数、实际接收时间、当前 checkout 和三个当前生产入口文件摘要。复制行为不授予文件历史可用身份，也未改源文件。

| 适配身份 | 取得的目标与配套参数（远端 output/targets/ 下） | 结构结论与缺口 |
| --- | --- | --- |
| tsindex_pure 候选 | tsindex_20260909_next_targets.csv + tsindex_20260909.xlsx | 48 行，signal_date=09-09；工作簿未显式记录 basis_momentum_weight，不能补成 0，保留 source_parameters_mismatch |
| tsindex_bmom020 | tsindex_20260916_next_targets.csv + tsindex_20260916.xlsx | 51 行，signal_date=09-16；最低源参数匹配，包括 bmom 权重 0.20、窗口 500、覆盖 0.9、月度再平衡 |
| citic027_r150_t1 | citic027_20260916_next_targets.csv + citic027_20260916_config.json | 68 行，signal_date=09-16；最低源参数匹配，包括 R=150、close_to_close、execution_lag=1。两个文件摘要与上轮回函一致 |

每个策略的 plan 与实际 audit 已存于 [input-adapter-samples](input-adapter-samples/)。审计保留原生合约和带符号目标权重，不使用 CSV 的 lots/notional/vol_scale 再乘杠杆。参数匹配只证明指定字段相符，不能据此确认这些文件是目前有效的生产目标、完整发布或历史可用。

开发机观察身份另记于 audit.observer：`/home/elfbob/claude-code/futures_strategies`，当前 HEAD `f3a752a0eb915a9f024496ca679e6c040e09239e`，另附适配器字节摘要。远端当前脚本摘要、原始参数文件摘要与本地适配器摘要分别记录；**历史生成时完整源树/dirty 指纹未取得，保留 null**，不把今天 HEAD 或脚本摘要冒充历史生产发布版本。

## 时间证据与真实缺口

| 时间/证据 | 本轮实际依据 |
| --- | --- |
| signal_date | 原生 CSV 各行，分别 2026-09-09、2026-09-16；027 JSON 中 signal_date 也一致 |
| 本次 first_seen / observed_at | 六文件实际接收时间为 2026-10-15 18:42:00～18:42:10（+08:00），逐项记录在 transport-observations；本地 staging 再次观察时间独立记录，first_seen_scope=this_capture_only |
| 目标历史 available_at | 未取得绑定当前文件字节的执行前可用证据，全部 null；不使用 mtime、信号日、先前日志时间或今天读取时间倒填 |
| 完整批次/源发布 | 未取得外部完成声明、逐角色摘要和生成时源码发布证明；现有文件不自行生成 complete=true 的声明 |
| 执行事件、报价到达、可成交证据 | 未取得可用于当前账户区间的带时点/来源/旧仓覆盖的整组输入，缺 execution；没有根据有行情或 volume>0 推出 tradable=true |
| 真正结算及到达时间 | 缺可验收 settlement 文件；没有把目标 close 当结算价 |
| 日历与合约规格 | 缺当前完整日历和逐合约有效乘数、保证金率与来源；没有以旧 bundle、反推乘数或 10% 假设补齐 |

既有代码 `cta_carry/pg_source.py` 已区分 public.futures_daily 的 open/close/settle，`common/minute/pg_source.py` 有 futures_contract_info 的交易日期/合约乘数只读入口；本轮只核对接口代码，没有查询共享库。适配器支持这些既有来源的带证据 CSV 摘录，但它们**尚未作为真实账户行情或规格接通**。不将“有表/字段/接口”表述成已证明当前数据覆盖，更不为缺字段启动历史取数。

三份真实 audit 共同保留 `missing_publication, missing_account_config, missing_calendar, missing_execution, missing_settlement, missing_instruments`；pure 候选另有参数不匹配。缺口不会产出 source-receipt.json 或 interval.json，因此不存在看似可执行的空壳包。

## 复现、账户参数与持续运行

以下命令仅重新观察已取得的 bmom020 旧文件，预期退出 2，并产生阻塞审计。使用未存在的新目录；另外两策略替换 plan 文件名即可。源文件留在本机 output，不随 Git 交付。

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_inputs --plan docs/requests/2026-10-14-multi-asset-operational-returns/input-adapter-samples/tsindex_bmom020.plan.json --output-dir output/forward_inputs/recheck-bmom-new'
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_forward_inputs.py tests/test_forward_account.py -q'
```

账户参数仍由 multi_asset_backtest_db 集中落实：账户/版本/标签、初始资金与单位 NAV、费用及未计项目、数量取整、一次日盘执行/日结规则、现金利息/申赎/融资规则、合约保证金与乘数有效期、完整交易日历及真实初始化安排。最终组合比例和未确定标的不妨碍本接口开发；但缺实际账户配置时拒绝生成可执行包。

持续运行还需实际当前目标供给、生产版本和可用时刻留证、外部完整批次声明、旧仓覆盖、真正报价与成交可行性证据；缺日不得跳过。未来会话/新合约需完整引用集合与旧引用快照，已有经济参数变化返回 reference_migration_required，交迁移缺项，不重置 NAV。单文件暂限 32 MiB，当前六文件均在限制内；未建立自动运输/采集器、触发器或运行 SLA。

没有真实初始化、advance、正式 latest 更新、生产部署/定时器、下单或企业微信发送；没有付费取数、回填、共享库写入/DDL，也没有向外发送本回函。已验收 response.md、implementation-response.md 保持原字节，其 SHA-256 分别为 `5d9f9292aa283ebfcb2e2bcb8d87e4234adfc7c8aefdebd10e003b919d4f6925`、`168f6f58c3936683f0b33affde546659410d4571a65faccdcf745c6ae16b82ed`。
