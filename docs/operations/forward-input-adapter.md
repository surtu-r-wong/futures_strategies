# 前向账户的只读输入适配

入口为 `python -m forward_inputs --plan <plan.json> --output-dir <新暂存目录>`。支持 `tsindex_pure`、`tsindex_bmom020`、`citic027_r150_t1`。只读取已有文件并写入指定的新目录；不会连接数据库、执行 SSH、运行策略、初始化/推进账户或发布 ReturnFeed。这个包位于 `forward_account/` 外，不改变现有账户的引擎指纹。

## 配置与供给方责任

[模板目录](../../config/forward_inputs/) 每种策略各有 plan/account 模板，另有 `publication.template.json`。所有 null 都需要实际依据；模板本身不能产生可执行包。最低源策略参数按现有 `SOURCE_RULES` 明列，账户金额、费率、执行/现金规则、来源版本、日历、合约规格均没有默认值。供给方还应把实际需要锁定的排除品种等参数加入 `source_parameters`，适配器会逐项核对原生配置。

plan 必填 `adapter, synthetic, trade_date, files, columns`。所有文件路径相对 plan 所在目录；未取得的来源用 null。`trade_date` 是待检查的账户区间，不是由目标信号日推算出来的交易日。

| files 的角色 | 现有文件/输入要求 |
| --- | --- |
| target_csv | 原生 CSV，保留 `signal_date, contract, target_weight` |
| source_metadata | 同次 Carry XLSX 的 `run_config`，或原生 027 参数 JSON；也支持有来源绑定的参数 JSON 摘录 |
| publication | 外部供给方已有的完整批次声明，见下文；缺失时仅作观察，适配器不自行生成它 |
| execution / settlement | 既有来源的 CSV 摘录，字段见下表 |
| instruments | 合约规格 CSV，完整的新引用集合 |
| calendar | 现有账户日历 JSON，必须由供给方显式声明来源与完整覆盖 |
| account_config | 已落实的账户配置 JSON，沿用[账户契约](forward-account.md) |
| previous_references | 已有账户最新完整引用快照 `{calendar, instruments}`；初始提案可为 null；日历或规格扩展必须提供 |

`publication` 必须显式给出：`complete=true`、完整性证据 `evidence`、`source_version`、`synthetic`、目标真实 `available_at` 及其 `availability_evidence`、`signal_date`、`row_count`、`held_contracts` 及 `holdings_evidence`。只有有证据的空仓才填 `[]`，不默认空仓；旧仓需要执行报价用于平仓。

其 `production` 对象包含 `project, host, checkout, git_head, source_tree_sha256`；后两项分别是生成版本的完整提交号和实际源树摘要（含生成时未提交改动的发布证据）。**今天读取 checkout 的 HEAD/脚本摘要不能代替历史生成源码证据。** 参数身份通过原始 metadata 字节摘要绑定，账户的 `source_version/source_parameters` 另行核对。

`publication.files` 必须恰好覆盖 plan 中所有非 null 来源角色（除 publication 自身），值为对应原始字节 SHA-256，包含报价、规格、账户配置和旧引用文件。可先保留生产目标已有的可用时刻收据，再由独立输入供给流程汇总当前批次声明；这份汇总文件的取得时间不替代目标可用时间。未提供的 previous_references 应从 files 摘要对象中删除。

该声明是外部证据的显式输入，软件校验绑定和一致性，**不独立证明声明的真实性、交易所日历完整性或真实可成交性**。不得让适配器按当前读取到的文件自行填 true、补历史时间或推定文件属于同次生产。

## 字段映射

CSV 的标准字段如下。`columns` 可按角色指定“标准字段 → 来源列名”，例如 `{"settlement":{"price":"settle"},"execution":{"price":"open"}}`。未列出的字段用同名列；不能只提供行情 open/settle 就省略时间和证据。

| 角色 | 标准字段 | 转换与拒绝规则 |
| --- | --- | --- |
| 原生目标 | signal_date, contract, target_weight | 合约代码不转换；权重保留符号、杠杆和零权重。不使用 lots/notional/vol_scale，不归一化，不重选合约；重复合约拒绝 |
| 执行参考价 | contract, price, price_type, at, available_at, source, tradable, tradable_evidence | price_type 必须 `day_open`，表示未复权日盘开盘参考价；tradable 必须字面 `true` 且逐合约有非空证据，不能由 volume>0 或有价格自动推出 |
| 真正结算价 | contract, price, price_type, at, available_at, source | price_type 必须 `settlement`；拒绝 `close`。必须来自真正 settle 字段，列重命名不能改变其经济含义 |
| 合约规格 | contract, asset_type, currency, multiplier, margin_rate, valid_from, valid_to, source | 仅 linear_futures/CNY，乘数与保证金率正数，保证金率≤1，有效日期覆盖所需区间；不采用原生 CSV 的反推 multiplier 充当正式规格 |
| 日历 | calendar_id, timezone, source, complete, coverage_start/end, intervals | 原 JSON 契约不变，不能由观测日期集推成完整交易日历 |

每组报价内 `at/available_at/source` 必须一致；若来源分批到达，供给方须以有证据的整组可用时刻汇总，并保留各原始来源。报价事件分别与日历 `execution_at/end_at` 精确匹配；`at ≤ available_at ≤ 本次取得时刻`。执行报价须覆盖旧仓与全部目标；结算须覆盖所有非零目标（取整后可能为零也保守要求）。报价必须有限且为正。

现有只读接口的字段依据：`cta_carry/pg_source.py:_contract_query` 从 `public.futures_daily` 读取 `trade_date, symbol AS contract, open, close, settle` 等；`common/minute/pg_source.py:_METADATA_QUERY` 从 `public.futures_contract_info` 读取 `交易日期/合约乘数`，按既有合约代码映射使用。这些查询本身不提供完整的可成交声明、历史到达时间或保证金规则；本入口不会调用这些查询，也不会把表结构存在认定为实际覆盖。已有接口的独立、带证据摘录可以映射为上述 CSV。

## 时间、输出和失败

每个原文件先复制到 `captured/`，记录原路径、字节数、SHA-256、`first_seen/observed_at`，其中 `first_seen_scope=this_capture_only`。重复观察不延续历史首次时间；应保留旧捕获目录。若经过运输，最初接收时间由独立 transport receipt 保存；本地再次 staging 是新的观察事件。

信号日来自原生行；它不是 available_at。mtime 完全不参与。没有外部可用时刻证据时，审计 `target.available_at=null`。目标收据的时间还必须在日历前次结算与执行之间；报价 at、报价 available_at、目标 available_at、实际观察时间分别保存，互不替代。

单文件限 32 MiB；XLSX 只读 `run_config`。全部输入读完后逐一重新读取比对，同时核对外部 publication 的全批次摘要，拒绝读取过程变更和稳定但只更新了一部分的混版。适配基于捕获字节，随后原来源再次更新不会改变该包。新目录不覆盖旧目录；无覆盖、重放或自动重试发布行为。

退出码 0 表示 `ready`：输出 `audit.json`、`source-receipt.json`、`calendar.json`、`instruments.json` 和 `interval.json`。最后才原子落定 `interval.json`，它是包完成标志；中断遗留的 pending 文件不可执行。`ready` 表示输入契约及来源声明通过检查，并不证明生产就绪、账户有足够保证金或 live state 与供给方旧仓声明一致；实际账户仍会再次校验。

退出码 2 表示阻塞或无效请求。可捕获的来源问题写入 audit 的稳定错误码，不输出原始异常/环境；无效 plan 或不可写目标目录只输出固定错误信息。主要错误：

| 错误码 | 处理 |
| --- | --- |
| missing_* / unreadable_* / invalid_* | 补实际来源、字段/时间或账户配置；不选默认值 |
| source_parameters_mismatch / source_version_mismatch | 核对实际生产参数版；不能根据文件名或新代码默认值改旧文件身份 |
| source_metadata_date_mismatch | 参数内信号日与原生目标不一致 |
| incomplete_publication / publication_hash_mismatch / source_changed_during_capture | 等完整稳定批次；拒绝半成品 |
| target_not_available_before_execution | 保留不可用，不能将本次观察倒填为历史可用 |
| missing_execution_contracts / missing_settlement_contracts / missing_or_expired_instruments | 补实际旧仓/新目标来源与规格，不能填零或自动缩仓 |
| missing_previous_references / reference_migration_required | 提供最新引用快照；旧经济参数、旧会话或来源变化交专门迁移，不重置账户 |

`calendar.json/instruments.json` 可直接作为既有 `extend-references --calendar ... --instruments ...` 的参数；`interval.json` 可直接作为既有 `advance --input ...` 的参数。本入口只准备这些文件，不调用上述写账户命令。引用检查与现有接口保持同样约束：只能追加会话/新合约，或同规格同来源延长 valid_to；既有会话、乘数、保证金率、来源和 valid_from 不得改写。测试通过现有接口实际验证合成引用扩展。

## 安全复现

仓库根目录执行，使用新目录名，不需要资金、DB 连接或生产作业即可观察已有输入：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_inputs --plan docs/requests/2026-10-14-multi-asset-operational-returns/input-adapter-samples/tsindex_bmom020.plan.json --output-dir output/forward_inputs/recheck-bmom-new'
```

本机已有样本预期退出 2，审计明确列出真实缺口。原文件位于 `output/forward_inputs/current-20261015/`，不随 Git 分发；其他机器须先通过已授权渠道取得文件并按 transport receipt 核对摘要，再调整 plan 路径，不能仅凭提交中的审计 JSON 当作原文件。

合成测试不依赖远端或数据库，所有测试产物在 pytest 临时目录，包含三策略适配、原生 XLSX、费用/NAV 对接、混版/读取竞态、晚到/缺失证据和引用迁移拒绝：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_forward_inputs.py tests/test_forward_account.py -q'
```
