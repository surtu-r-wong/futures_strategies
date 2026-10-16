# M02 用户单位裁决与 DCE 日内手续费适配回函

已完成本地 `contract-fees-v2` 适配及独立禁用候选。**100 手隔夜仓下，官方三例在多、空方向均得到 99／180／120 元；v1 仍为 189／189／189 元，FIFO 持仓结果不变。11 个比例合约单位按用户裁决补为万分，剩余 13 个绝对值单位未确认。** 上轮冻结交付未覆盖，真实候选未启用。

源码、测试及完整证据本地提交：`9bfaf42`（`feat: add versioned DCE intraday fee matching and user unit decisions`）。本行提交号补录另作文档提交；未推送。

## 实现与版本边界

- `forward_account/fees.py`：保留 `account_trades` 的 v1 双返回值接口；新增 `new_matching_state(model)` 和 `account_trades_with_matching(..., matching_state=state)`，返回成交明细、原 FIFO 持仓批次、独立费用匹配状态。v2 必须显式指定 `dce_intraday_matching={method: prior_open_fifo_same_side, components: [exchange], source: ...}`。缺状态、模型/开仓规则 SHA 改变、逆序时间或无效开仓证据均拒绝。
- 仅 `.DCE` 原生合约的**交易所费用**采用当日先开后平：开仓先收普通开仓费，后续平仓匹配此前未用完的同向开仓数量，平仓腿收日内费，已匹配开仓腿调整为日内开仓费；支持部分匹配、多次开平、返还与补收。先平后开的量不反向匹配，跨日未匹配开仓不退款。比例调整使用各开仓腿原价，平仓腿使用本次成交价。
- `action/closed_lot_id` 继续表示持仓 FIFO 核算；`exchange_fee_action/fee_match_*` 单列费用匹配依据，不改持仓年龄、持仓批次或 PNL 算法。非 DCE 的整个成交费用明细保持 v1 行为。经纪商费沿用既有显式费表和持仓批次语义，不将交易所 FAQ 自动推广至经纪商。
- `forward_account/engine.py`、`store.py` 将 v2 费用状态保存在同一账户 JSON 状态中，支持既有幂等重放与尾部修订；未新增数据库表。账户配置及引擎指纹仍不可变，未迁移、改名或删除任何旧生产账户。旧账户运行仍须遵守原引擎指纹约束。
- **日结引擎仍是既有一天一次目标核算**。多笔顺序验证针对费用原语，不代表新增任意盘中多次账本执行能力；不支持一笔净目标变化中不可观测的对冲开平或多个交易编码混合。

## 官方事实、用户裁决与模型选择

[用户提供的官方 FAQ PDF](sources/dce-intraday-fee-faq.pdf)页面发布日期为 2021-01-21，打印时点为 2026-10-16 10:28；正文及页脚原链接见[提取文本](sources/dce-intraday-fee-faq.txt)。文件 SHA、复制来源及复制时刻见[source-manifest.json](source-manifest.json)。本轮没有重新联网取数，打印时间不作为规则生效时间。

FAQ 明确同交易编码、同合约、当日先开后平，并给出 3 元普通费／1.5 元日内费的三例。**异价多笔开仓之间按成交顺序 FIFO 分配、相同时间戳按调用顺序处理、同向净持仓匹配及各腿成交金额规则，是本次显式模型选择，不是 PDF 明文。** 一个账户状态假定对应一个交易编码；示例费率与日期用于合成验证，不替代当前或未来合约费表。夜盘仍受既有 day-session 模型限制。

[unit-decisions.json](unit-decisions.json)保留用户原话“比例的单位均为万分，1就代表成交额的万分之一”、请求文件 SHA、原始数值及原来源版本。新增单位不是交易所独立认证，未改变费率数字、来源 SHA 或有效区间。

|处理|合约|数量|
|---|---|---:|
|用户裁决 `PER_TEN_THOUSAND`|I2701、J2701、JD2612、JM2701、LH2701（DCE）|5|
|用户裁决 `PER_TEN_THOUSAND`|MA2701、PR2612、PX2612、SA2701、SH2701、UR2701（CZC）|6|
|绝对值单位继续未确认|AP2701、CF2701、CJ2701、FG2701、OI2701、PF2612、PK2612、PL2612、RM2701、SF2701、SM2701、SR2701、TA2701（CZC）|13|

已有 SHFE/INE `PERMILLE` 数字和单位保持原样。J 的日内值 1.4、LH 的日内值 2 等原数值保留，并覆盖向上补收开仓费的测试。

## 表证据及禁用候选

复用汇总仓只读证据：[schema](schema/columns.json)、[DCE 行](data/contract_info.json)、[CZCE 行](data-czce/contract_info.json)及各目录收据。本项目没有再次查库。实际表为 `public.futures_contract_info`，29 列无显式币种、报价单位或乘数单位列。19 个 DCE 合约只有 10-15 行，乘数及四费数值与旧快照一致，10-16 无行。因此数值乘数并不缺；19 个独立官方物理量纲证明仍缺。

19 个 CZCE 行的 `日内开仓手续费=-1.0000` 是语义未证的占位值。本轮保留证据，不将它当负费、零费或已确认普通开仓费；候选继续采用原 `exchange_data` 两列投影：普通费映射开仓／普通平仓／日内开仓，日内费映射日内平仓。该投影不是新增的 CZCE 日内政策确认。

[account.disabled.json](account.disabled.json)为新文件，显式 `enabled=false/runtime_ready=false/executable=false`，D 为空；保留 67 合约、原乘数／保证金、1 亿元本金与既定取整规则。原生权重引用冻结证据，未生成或重算目标。原参数有效窗仍仅 2026-10-15；经纪商、滑点、全局／非 DCE 日内政策、账户身份与正式起点等未确认项仍为空。

[remaining-gaps.json](remaining-gaps.json)列出增量与余缺；DCE 五项源 SHA 差异、AU 非零目标取整为零、完整会话及实际成交／结算证据缺口继续保留。`build_candidate.py` 只派生当前目录产物，[parent-manifest.json](parent-manifest.json)绑定上轮冻结文件 SHA。

## 验证及复核

- [pytest-results.txt](pytest-results.txt)：**70 passed**，其中新版 DCE 27 项、既有费用 16 项、既有账户 27 项。覆盖正反方向、足够隔夜仓、先平后开、部分与分次匹配、反手、异价各腿金额及单位换算、开仓补收、跨日、不同合约隔离、同时间戳顺序、v1／非 DCE 保留、状态异常拒绝、账户幂等／修订及不可变配置。
- [v2-reproduction.json](v2-reproduction.json)：六个内存回放案例，绑定最终 fees／engine／store／contracts 文件 SHA；新旧费用及 FIFO 持仓逐例对照。不初始化账本。
- [candidate-validation.json](candidate-validation.json)：**414 项**来源字节、冻结父文件、单位增量、所有费率数字、未知成本、日期窗口、只读表证据及禁用状态核对通过。Ruff 检查通过。

复核命令（本地离线；测试仅创建临时合成 SQLite）：

```sh
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest -q tests/test_forward_dce_fees.py tests/test_forward_contract_fees.py tests/test_forward_account.py'
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python docs/requests/2026-10-16-m02-futures-rule-clarification/replay_examples.py'
bash -c 'ulimit -v 2000000; .venv/bin/python docs/requests/2026-10-16-m02-futures-rule-clarification/verify_candidate.py'
```

本轮没有新增数据库查询、共享库写入／DDL、策略／目标重算、真实账户初始化、生产操作或远端推送。仅提交本任务源码、测试与请求交付；其他会话改动保留。
