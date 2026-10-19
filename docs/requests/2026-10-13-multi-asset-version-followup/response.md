# 回复：027 修订后产物与合约单位资料

2026-10-13，futures_strategies → multi_asset_backtest_db。

已按 [补充问询](request.md) 核对本机及项目文档登记的 WSL2 运行主机。**找到两份修订后的生产 027 目标及配置；同配置的独立研究历史暂无可提供的现成归档。合约单位方面有既有元数据读取入口、分钟 bundle 和反推审计资料，但没有核实覆盖所问 138 个合约的正式单位清单。**

本次只读检查既有文件和代码，未运行策略、重跑历史、查写共享库、安装依赖或改生产作业。仅新增本回复，原盘点回函保持不变。

## 1. 027：修订后的目标与配置已找到

位置在 WSL2，连接入口为 `ssh -p 2223 ghls@100.120.152.1`。准确仓库根目录是：

```text
/home/ghls/claude-code/futures_strategies
```

注意同机另有 `/home/ghls/futures_strategies`，是另一份旧 checkout；本节产物在带 `claude-code` 的路径下，不可混用。

### 已有文件、版本与覆盖

以下路径相对于上述远端仓库根目录；CSV 与同日期 JSON 成对使用。

| 信号日期 | 已有目标文件 | 配套配置 | 核实结果 |
| --- | --- | --- | --- |
| 2026-09-15 | `output/targets/citic027_20260915_next_targets.csv` | `output/targets/citic027_20260915_config.json` | 68 行，CSV 所有 `signal_date` 均为 09-15；最后一次对应运行于 09-16 17:12 完成，日志记录 commit `55114632`、退出码 0 |
| 2026-09-16 | `output/targets/citic027_20260916_next_targets.csv` | `output/targets/citic027_20260916_config.json` | 68 行，CSV 所有 `signal_date` 均为 09-16；对应运行于 09-17 14:53 完成，日志记录 commit `55114632`、退出码 0 |

运行时间来自同目录 `citic027_daily.log`，日志带 `+08:00`；文件修改时间与完成记录一致。09-15 曾在 commit `2ad1ee5f` 下先跑一次，随后同名前缀重跑覆盖；**现在文件对应最后一次 `55114632`，不能据较早日志给它标版本**。提交号是生成日志的记录，不是 JSON 内置的完整源码快照/dirty-state 证明；本次未重做历史运行验收。

两份 JSON 均显式记录：

```text
factor_kind=basis_momentum
window=150
min_observations=1
smoothing=1
t1_leg=top_two_by_oi
normalise_by_gap=false
return_basis=close_to_close
execution_lag=1
cadence=daily
restrict_to_named=false
lookback_days=900
target_vol=0.15
vol_window=252
max_gross_leverage=4.0
```

已核实 `vol_window_days=252`、`vol_window_min_products=61`、`index_days=599`、`products=68`。`index_days` 是日跑内部波动估计/研究窗口的诊断计数，**不是已落盘的 599 日持仓或收益文件**，不据此推导精确历史起点。

两份 CSV 均已包含修订后的四种代码列：`order_code / code_exchange / code_qmt / code_ctp`，以及 `contract, direction, close, multiplier, raw_weight, vol_scale, target_weight, notional, lots`。其 `signal_date` 是信号截止日，生成时刻在次日；这两份是目标快照，不是连续历史或成交记录。本次核对到该目录最新为 09-16 信号，不据此判断其他生产运行是否暂停。

### 文件指纹

本次直接读取远端四个小文件计算 SHA-256，供取用时核对；未复制或改变原文件。

| 文件名 | SHA-256 |
| --- | --- |
| `citic027_20260915_next_targets.csv` | `fe2422dfcd339570bac3ace57612b6ee232499ce6708cc00824ec6d41a126831` |
| `citic027_20260915_config.json` | `a8a6b22949038148be22ecbda2d5f64490e82f5ff9bea511e3479e3b2780e627` |
| `citic027_20260916_next_targets.csv` | `1ad34fe5614ac40b2cf6a6392019529af79316a1c14cd977db5d2c0e7058489d` |
| `citic027_20260916_config.json` | `2bff02dfb2743c39cfaf6f46493d80b673d2d0d45932b39805683eb2b35cd23e` |

### 研究历史：暂无符合本问询版本的现成归档

**生产 R=150、`close_to_close`、`execution_lag=1` 的独立逐日研究历史：本次核查暂无可提供的现成归档。** 已检查本机既有 `output/citic`、`output/targets`，以及远端上述仓库、旧 checkout、`/home/ghls/fs-bmom`、`/home/ghls/carry_citic_20260903` 等已知研究目录中的相关文件；未把未定位的其他主机/离线备份断言为不存在。

已有 [09-16 收益口径裁决](../../research/2026-09-16-citic-return-basis-ruling.md) 及 [026 探针记录](../../plans/2026-09-15-cicsf026-time-series-momentum-probe.md) 记载四条指数换时序重跑，但该组 027 研究点为 **R=200**，不能代替生产 **R=150** 历史。本地旧 `cicsf027_best*`、`cicsf027_replica*` 文件也不因代码默认已修改便取得新版本身份。

继续保留本机 09-14 目标及配置的旧样本身份。新增发现的两份远端目标可以作为修订后版本样本；本次按要求不为缺失历史启动重跑。

## 2. 合约单位：可复用的既有资料与身份

### 2.1 商品合约元数据读取入口：`public.futures_contract_info`

本仓分钟链读取的合约乘数并非来自 `public.assets`。现有入口是 [common/minute/pg_source.py](../../../common/minute/pg_source.py) 的 `_METADATA_QUERY` 和 `PublicMinuteSource.resolve_metadata_multiplier`：

```sql
SELECT "交易日期", "合约乘数"
FROM public.futures_contract_info
WHERE "合约代码" = %s
  AND "交易日期" <= %s
ORDER BY "交易日期" DESC, "合约乘数" ASC NULLS LAST;
```

这是既有读取方式说明，本次没有执行查询。其取值身份为**合约元数据**；是否得到分钟价格区间交叉验证需另外看 resolution，不能把“取自表”直接标作“已经核验”。同一最新日期乘数冲突、非正或非整数时，现有解析器会拒绝。

代码先通过同文件 `_minute_contract` 将日线代码映射成元数据/分钟使用的 `minute_symbol`：通常去掉交易所后缀，郑商所另走 `czce_minute_symbol(contract, trade_date)`。因此 `public.assets` 直接匹配不到，不等于这张表也没有；但本次未拿到所问 138 个合约的逐项清单，也未核实该表对它们的实际覆盖，交由已受理目录问询的办公室继续定位。

既有历史记录称该表自 2025-12-22 起有快照，见 [股指运行手册](../../operations/guosen-open-momentum-runbook.md)。这是历史证据，不保证每个商品合约/每个日期都有记录，也不是法律生效日完整的交易所规则档案；按交易日期取不晚于时点的记录，不能用当前快照无条件回填历史。

### 2.2 已有商品分钟 bundle：远端真实存在，但截止 2026-01-30

在第 1 节同一远端仓库下，已找到并读取两个现有 manifest：

| 目录 | manifest 声明区间 | 版本 |
| --- | --- | --- |
| `output/commodity-panel-v1/` | 2012-01-04..2026-01-30 | `provenance.git_head=496df6d4203d1aa52d9b33b198c7da6bdd79d6e9` |
| `output/commodity-panel-w2526/` | 2025-01-01..2026-01-30 | 同上 |

两处均有 `bars.parquet / dominants.parquet / roll_fills.parquet / universes.parquet / manifest.json`。按 [bundle 契约](../../operations/commodity-panel-bundle.md) 和 [面板代码](../../../common/commodity/panel.py)，`bars` 中已有 `trade_date, product, contract, multiplier, pricing_basis`，可按具体合约和历史交易日复用已有计算值；这些值同时服务原始价格与五分钟成交价换算。

`commodity-panel-v1/manifest.json` 记录：

- `bars.parquet` SHA-256：`d885db24abb22e2ccec615bb37fa6be70529aafba885928a3e7f4a960c1baf10`。
- `inputs.multiplier_resolutions_sha256`：`c54397b21adfb07543db71ebe937ac3bd82c44249f2b3448ce2a664e5bfac7f4`。
- `session_rules_reliable_end=2026-01-30`。

以上是**manifest 内记录的摘要和范围**，本次未重算整个 Parquet 的 hash 或逐行验收；当前本机与该远端所试解释器均缺 `pyarrow`，因此只完成文件存在性及 manifest 核对，字段定义取自既有契约。未为此安装依赖。

**这两份 bundle 不覆盖所问 2026-05～08。** 其中也许包含后来仍交易的某些合约，但本次未核实与 138 合约的交集，更不能把同品种早期乘数无条件向后沿用。且 `multiplier_resolutions_sha256` 是证据摘要，不是逐合约来源明细；仅见 `bars.multiplier` 无法判断该行究竟来自元数据、日线反推还是分钟反推。两目录下未发现可直接读取的逐条 multiplier-resolution 清单，不能据 bundle 名称把所有数升级为正式交易所单位。

### 2.3 已有反推和交叉验证资料

| 现有位置 | 方法/已有内容 | 应保留的身份 |
| --- | --- | --- |
| [cta_carry/targets.py](../../../cta_carry/targets.py) 的 `infer_product_multipliers` | 按品种取近期成交记录 `turnover/(volume×close)` 中位数，用于目标整手估算 | **品种级估计值**，不是逐合约官方规则；源码实际取最后 60 条有成交 bar，并非去重后的 60 个交易日 |
| [citic_index/universe.py](../../../citic_index/universe.py) 的 `_daily_multipliers` | 品种级日线成交额/成交量/收盘价比值的因果扩展中位数，供沉淀资金和 027 目标换算 | **估计值**；修订后 027 目标中的 multiplier 也不会因 lag 修复而变成正式元数据 |
| [common/minute/pg_source.py](../../../common/minute/pg_source.py) 的 `_daily_multiplier_candidates / resolve_metadata_multiplier` | 缺元数据时用日线 high/low、turnover、volume 约束整数候选；条件允许时再由分钟价域佐证 | `source=daily_turnover`，需同时保留是否经过交叉验证；`daily_turnover_unchecked` 不等于已验证 |
| [common/minute/bars.py](../../../common/minute/bars.py) 的 `MultiplierResolution`、`infer_contract_multiplier / validate_metadata_multiplier` | 保存 multiplier、source、sample_rows、pass_rate、sample_dates、样本范围、evidence 摘要、resolution_path 等；分钟推断要求唯一合格整数候选 | `inferred` 仍是**反推值**；`metadata` 是来源类型，校验结果须单独看；不能统称正式单位 |
| [cta_carry/minute_backtest.py](../../../cta_carry/minute_backtest.py) 的 `executions` 导出 | 有 `multiplier / multiplier_source / multiplier_pass_rate / multiplier_sample_rows`，可追踪执行时实际采用的乘数及验证证据 | 已有回测审计结构；本次没有新建或补齐执行产物，亦未证实存在覆盖 138 合约的这类明细 |

相关实测说明可直接复用：

- [乘数通过率门槛及反推缺陷记录](../../research/2026-08-25-multiplier-pass-rate-threshold.md)：解释收盘价比值舍入为何会把原油乘数推成相邻整数，以及为什么“候选推断”和“验证已有候选”是两种检验。
- [分钟与日线执行比较](../../research/2026-08-26-minute-vs-daily-execution.md)：七窗口中 29.8% 乘数解析未经分钟 bar 佐证（历史统计）；不能把所有分钟回测值一律标作双来源验证。
- [郑商所分钟成交额问题](../../research/2026-08-25-czce-minute-turnover-is-synthetic.md) 与 [定价基准配置](../../../config/carry_minute_pricing_basis.csv)：郑商所归档分钟 `amount` 是合成值，部分时期甚至缺乘数；不能拿该列反推正式单位。已有路径使用日线依据及 `ohlc_typical`，不将其伪称真实 VWAP。

另有 [index_open_momentum/pg_source.py](../../../index_open_momentum/pg_source.py) 的 `CFFEX_MULTIPLIERS`：IF/IH=300、IC/IM=200，源码登记为 2026-08-27 与合约元数据核对的股指事实。但它只覆盖股指，不解决这次 Carry 商品合约缺项。

### 2.4 正式交易单位及单位换算清单：暂无

本次核查**暂无可直接提供、覆盖这 138 个商品合约且逐项绑定交易所规则版本/生效日的正式交易单位、报价单位与换算清单**。上述“元数据入口”“已用乘数”“经价域佐证的估计”分别保留原身份，不补造吨/手、克/手等法定单位说明，也不把同品种所有历史合约视为永远同一乘数。

已有分钟导入脚本 [transform.awk](../../../scripts/futures_minute/transform.awk) 只是字段映射和清洗，没有通用的元/万元、吨/克换算规则，不能当作一张合约单位字典。代码还专门限制日线候选的合约生命周期，避免老燃料油与重挂后乘数跨代混用，见 `_DAILY_MULTIPLIER_PRODUCT_QUERY`。

## 3. 接入边界确认

理解并接受本次说明：下游会构建模拟成交后持仓、NAV、费用及资金账，**真实经纪商持仓不是该回测的前置条件**。下游现行 10% 商品保证金可保留为显式研究参数，不标成历史经纪商或交易所实际保证金；它与合约乘数/报价单位是不同数据项，本函不替它补造事实依据。

本次提供既有位置、版本、覆盖及缺失结论，由 multi_asset_backtest_db 与办公室去重、适配。未修改策略、生产作业、原盘点回复或共享库，也未启动新的全历史产出。
