# 办公室回函：M02 既有来源、字段和有限只读补证

2026-10-16，data_manager → multi_asset_backtest_db。**本函办公室范围已完成；真实执行输入仍未齐备，候选保持禁用。** 已按authorization.json受理，冻结input-scope.json中的旧draft/pending标记不再是授权状态；原件未改。范围为887只股票、67合约，日历2026-09-01～11-30；没有承接旧138合约研究窗或暂停的历史股票状态任务。

证据入口：[接单及12份输入SHA核对](acceptance.json)、[只读查询和收据](evidence-readonly.json)、[67合约逐项映射](futures-source-map.json)、[25份原资料/代码SHA](source-index.json)、[禁用候选](office-input-candidate.json)。没有新取公开网页、付费源调用、业务写库、DDL、目标/策略计算、账本初始化或生产动作。

## 1. 本次实际取得与复用范围

- 复用M01的67条10-15内部快照、67条交易所结算参数、19条郑商所单位资料、66条生命周期；42只HK主档、FX旧值和EC查询零命中也复用，不再重查。M01最终归档时刻为2026-10-15 14:37:20 UTC；各查询真实取得时刻完整保存在映射文件的`m01_read_receipts`，不能以本次索引时间替代。
- 新增一次限定表名的字段查询；随后一次只读事务执行24合约10-15原文行、91日日历、指定12种来源目录查询。首次事务因源SHA差异在落文件前退出，回滚关闭；为保存证据又执行同样三项只读查询。总计7条业务/目录SELECT（不含SET/SHOW），不是7次采源。最终三项为24/91/21行；目录21行含重复映射，对应12种relation，不能算21独立来源。
- 查询设20秒超时、8MB work_mem、REPEATABLE READ READ ONLY，结束回滚关闭；首次字段查询15秒超时、只读并回滚。最终实际取得时刻见每项`acquired_at`，约2026-10-16 09:15 CST。股票887券当前行情/开盘状态、完整事件覆盖未重查，归股票项目；官方规则/产品资料归各源项目。

## 2. 24个费用单位缺失合约：已有数值，没有新增单位认证

入口为`exchange_data.futures_settle_param`，责任方data_manager。键为`exchange + trade_date + contract_id`；字段为`open_fee/offset_fee/short_open_fee/short_offset_fee/fee_style/fee_scale/source_url/source_sha256/fetched_at/raw`。本次仅查2026-10-15、冻结清单的24合约。

|范围|本次现存原文|已确认的字段映射|仍缺什么|
|---|---|---|---|
|19个CZCE|13条“绝对值”、6条“比例值”，24缺项中的这19条fee_scale全NULL|`raw.交易手续费`投影到open/offset/short_open；`raw.日内平今仓交易手续费`投影到short_offset，旧版兼容列为平今仓手续费|绝对值的计价单位及比例值分母不能单靠标签推定；四字段仍须对照适用的官方费用说明|
|I2701/J2701/JD2612/JM2701/LH2701.DCE|5条均“比例值”，fee_scale全NULL|raw的openFee/offsetFee/shortOpenFee/shortOffsetFee直接对应四费；原响应无业务日期回显，raw保留_requested_trade_date及_response_has_date=false|比例分母、日内开仓适用条件及当前有效规则仍须官方依据|

对应解析器为[czce.py](../../scripts/exchange_settle/czce.py)、[dce.py](../../scripts/exchange_settle/dce.py)，本次只读取并登记SHA。`short_open/short_offset`在此表示日内开/平字段，不可按英文short误读为空头方向。CZCE的short_open是同一“交易手续费”列的投影，并不是已证明的独立日内开仓追溯收费政策。

**源版本差异须独立保留。** CZCE19条仍绑定M01的`cb86fd21…`；DCE5条当前`source_sha256=ead55d52c67399c2a555e36217f73a509d66b94fd9804465067b557844f69e8b`，M01冻结为`741336afa6a181254d998f4e50c24a0bf8ea2124a6f0a348db260cbd0fa22106`。DCE四费、fee_style、fee_scale及fetched_at未变。原因本轮未证，不据此声称业务值修订或源正确性；新读raw只绑定当前库声明的SHA，不挂到M01旧SHA。逐行比较在`source_version_comparisons`；本轮没有导出/重新散列完整交易所响应，所以这些是库记录中的源SHA，不能冒充已对原始响应字节复核。

完整67合约原M01有22个YUAN_PER_LOT、16个PERMILLE、5个PER_TEN_THOUSAND及24个未知；本轮不提升任何未知单位。郑商所19个短码连接复用M01既有规范化匹配，不推广为跨十年的通用截码规则。

## 3. 48合约量纲、EC生命周期与67合约时点

|既有入口/责任方|精确字段及本清单覆盖|能够证明及限制|
|---|---|---|
|`public.futures_contract_info` / market-monitor|交易日期、合约代码、交易所代码、合约乘数、最小价格、最后日期、多头/空头保证金率、四费、updated_at；M01为67/67条10-15快照|有内部参数数值，无独立报价币种/价格单位/乘数单位/有效区间；导入器trade_date缺省取运行日，不是交易所盘中生效日|
|`exchange_data.czce_futures_reference_record_latest` / data_manager|publication_date、source_fetched_at、contract_multiplier、multiplier_unit、minimum_price_tick、price_tick_unit、minimum_tick_value、tick_value_unit、currency、listed_date、last_trade_date、expiry_at、source_url/source_sha256；19/19 CZCE|单位和量纲可追溯，样本发布日期10-15、source_fetched_at为18:11:14 CST；不自动证明未来D的适用性。其余48合约的量纲缺项仍交期货项目补|
|`exchange_data.czce_futures_reference_capture` / data_manager|publication_date、source_fetched_at、source_url、source_sha256、availability、response_bytes|原文定位入口；本轮只定位列，不重新导出全响应。latest按每个发布日期取最新可用版本，非历史当时可见版本|
|`public.futures_contracts_all` / market-monitor|symbol、listing_date、last_trade_date、created_at、updated_at；M01为66/67|EC2611.INE仍独立零命中，已有内部乘数50、最后日期2026-11-30不能替代独立上市/最后交易证据，也不能误报为缺乘数|

五所本批结算参数fetched_at分别为10-15 CST：CZCE 17:40:45、DCE 17:41:13、SHFE 17:40:33、INE 17:40:37、GFEX 17:40:29。这些是本库采集时刻；原始精确发布时间和有效起止尚未被证明，映射中保持null。67合约属于上述五所，本函不将其他未入清单交易所的旧证据算入覆盖。

已有[9-21两公告原文及收据](../../findings/2026-09-20-stock-exchange-direct-sources/followup-rule-evidence/README.md)可作有限语义证据：DCE焦煤2025-02变更样本明确成交金额分母；CZCE玻璃/纯碱2026-06样本显示同一公告的保证金结算时点与手续费夜盘时点不同。原HTML、文本、取得时间和SHA均在source-index引用目录，不外推成67合约10-15或未来D有效政策。经纪商加成、最低费、取整、滑点、保证金适用账户和未计项由消费方另行落实，不默认0。

## 4. 股票、FX、日历、状态及事件入口

|类别|已有来源/责任方与字段|已知覆盖与不能证明的内容|
|---|---|---|
|股票主档|stock_selector维护`stock_selector.stock_meta`：ts_code/market/exchange/list_date/delist_date/updated_at|M01核42只HK主档；表无逐券currency、board_lot、price_tick、报价股数、申报最小量/增量、零股规则。887券规则缺口不能用市场后缀或“统一100股”补齐|
|资产侧元数据|market-monitor的`public.assets`，M01查询symbol、currency/quote_currency、board_lot/lot_size、quote_unit、tick_size|仅42HK及EC候选别名零命中；不把这个有限查询说成全库或887券都不存在资料|
|HKD/CNY|market-monitor的`public.fx_rate_daily`：currency_code/rate_date/rate_to_cny/source/created_at/updated_at|目录当前PAUSED，M01最新2026-03-04、source=manual；rate_to_cny是向CNY的方向，旧值不能当当前执行或估值价。M01 HKD EDB名称检索零命中不证明所有可能交叉源都不可用；本轮未重复搜FX全表|
|日历|market-monitor的`public.trading_calendar`：calendar_date/sfe/hkxe/created_at/updated_at/deleted_at|本次91自然日无缺日期、无NULL/软删除；sfe=true 59、hkxe=true 63。原始官方发布时间未知，只有内部记录时间；没有逐市场开闭市、休息、半日、夜盘归属及临时停市完整会话。sfe是既有股票/国内期货代码采用的标志，不是六所逐日独立认证|
|日价|stock_selector的`stock_daily_price`：ts_code/trade_date/open/close/available_at/data_source/updated_at，另有复权字段|原始日open/close与复权字段分开；日价不是执行成交或开盘可买卖证据。此次未重查887券价格覆盖|
|状态|stock_selector的`stock_status`：ts_code/trade_date/is_st/is_suspended/limit_up/limit_down/is_new/updated_at|没有盘中观测时刻、开盘buyable/sellable或源版本凭证字段；日终涨跌停不可替代开盘状态。目录adapter描述也是登记文字，不冒充当前实现验收|
|公司行动|stock_selector的`stock_dividend_event`：event_stage、proposal_ann_date、implementation_ann_date、record_date、ex_date、available_date、data_source、source_event_id/source_payload；`stock_dividend_implementation`：wind_code、batch_id/batch_observed_at/batch_status、query_year/query_period/query_sectorid、exrights_exdividend_date、dividend_payment_date、sharedividends_proportion/shareincrease_proportion|已有入口；旧输入检查仅600000.SH样本，不是887券完整事件覆盖。不得把未查询/未见记录解释为空事件；配股、拆并股、退市等完整范围及现金流对账仍需消费方/股票源项目确认|

股票旧结构证据路径见source-index的`input-adapter-acceptance/current/source-inspection.json`（原查询10-15 18:42 CST）；本轮仅复用其结构和有限性，不扩大样本。股票项目负责官方CN/HK会话、逐券规则、当前免费FX及状态补证；期货项目负责官方合约/费用/EC生命周期。办公室已告知两方可复用结果，无重复官网请求。

## 5. 交付状态与后续边界

`office-input-candidate.json`为证据引用与缺项清单，`enabled=false/executable=false`，不是可执行账户配置；D、有效起止均null。官方补证后应把已证明项更新到各源项目候选并重新验收，不能把本函的缺项快照当作对后续官方证据的否定。

本函没有从数值大小推单位、没有把内部快照或取得时间当原始发布时间、没有向未来填充10-15参数、没有生成未来行情/状态观察。DCE源SHA变化已明确交期货和汇总方，尚无证据解释时保留双链路，不自行修库。办公室本轮可以交付的来源说明和补证已完成；剩余规则证明及经济政策依协调分工办理。
