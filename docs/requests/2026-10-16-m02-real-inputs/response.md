# M02真实输入补证回函（禁用交付）

本轮完成可取得资料的补证、67合约完整映射和禁用候选。**新增29个合约规格量纲核对，EC2611.INE独立生命周期已补齐；19个DCE量纲和24个当前费率单位/有效链仍缺，候选不可启用。** 全程保留原67行权重及零权重PB，未截取已知费用子集。

授权采用authorization.json，input-scope.json中旧draft/pending标记是发函前快照，原件未改。办公室补证提交`bc2f5d0`已消费；本轮证据、候选及验证本地提交为`cfcf0df`（`docs: reconcile M02 real-input evidence and disabled futures candidate`）；本回函提交号补列另作文档提交，未推送。

## 1. 已取得的事实与来源

- **29/48量纲缺项取得官方规格佐证**：SHFE官网24个清单品种（含5个INE品种）、GFEX官网5个品种。逐个核对交易单位或指数乘数、报价单位、CNY维度与10-15内部乘数相符，见[official-dimensions.json](official-dimensions.json)。加上既有19个CZCE量纲，现共48/67有规格维度依据。当前取得不能证明历史当时可知、未来D仍有效，实际报价源单位声明也尚未验收。
- **EC2611独立生命周期补齐**：[官方10-15合约参数文件](https://www.shfe.com.cn/data/busiparamdata/future/ContractBaseInfo20261015.dat)给出上市日2026-05-26、到期/交割日2026-11-30，文件内部更新时点2026-10-15 16:20:06（北京时间），实际取得2026-10-16 09:18:44.971942 CST。原始SHA为`f193b82a69d676b1be8b89fbbdd977e8391f997d64fc6b859eda998b8600b36d`。原行EXCHANGEID为SHFE，是交叉发布页面字段；原生身份仍为EC2611.INE。原文参数按日全所发布，本次下载一次文件（80,724字节），只采用EC一行补缺。原有66条生命周期未替换。审查补证见[EC日期推导依据](ec-lifecycle-basis.json)：归档[官方EC产品页](https://www.shfe.com.cn/products/futures/index_f/ec_f/)的`currinstrumentprop-f-component`中，`type_select == 'ec'`专用表将`OPENDATE`标为“上市日”、`EXPIREDATE`标为“到期日”、`ENDDELIVDATE`标为“交割日”；同页`pageList[0]`（`data_standard=2026-05-11`）明确`DeliveryPeriod=同最后交易日`。因此最后交易日由`ENDDELIVDATE=20261130`及该规则推出，不从英文`EXPIREDATE`猜测。页面原文SHA为`3ba4a95de04fbcbbc139a6c96a44873c06f94ae43961febbce3c4fa575277e5f`，实际取得2026-10-16 09:15:15.600422 CST。另核验11月30日为当月最后一个自然日且为周一，内部sfe当日开市标志仅作佐证，不能认证INE完整未来会话。规则原文为“合约交割月份最后一个开展期货交易的周一（上海国际能源交易中心可以根据国家法定节假日等调整最后交易日）”；官方参数未含节假日调整，未来D仍需复核公告和日历。此次只复用归档，未新增获取，不扩大未来适用或执行授权。
- **24费缺项未隐式补值**：复用办公室当次raw，19CZCE/5DCE四费数值均与M01一致，但fee_scale仍NULL。旧官方JM、FG、SA公告只提供有限品种/时点语义，没有完整证据覆盖10-15或未来D，故没有把旧公告填成当前费表。J/LH日内开仓差异与调整政策未证事项逐项保留。
- **DCE源版本差异原样保留**：5个合约M01库声明原SHA为`741336afa6a181254d998f4e50c24a0bf8ea2124a6f0a348db260cbd0fa22106`，办公室本次库声明为`ead55d52c67399c2a555e36217f73a509d66b94fd9804465067b557844f69e8b`。数值、fee_style/fee_scale及fetched_at未变，原因未证；新raw只绑定新声明。办公室没有导出完整原响应重算SHA，本函也未把“库声明SHA”冒充已验证原始响应字节。两条链见[contract-evidence.json](contract-evidence.json)。

[原文归档索引](source-archives.json)同时保存原文SHA与gzip文件SHA，gzip是字节无损压缩；[新取数收据](acquisitions.jsonl)保留每次URL、时刻、结果和实际取得的原字节SHA。[复用来源索引](reused-sources.json)保留办公室原取得时间，复制时间另列，不替换成新的规则发布时间。产品页有明确发布日期时单独登记；没有的保持null，未从HTTP Last-Modified或抓取时刻猜生效日。

## 2. 实际访问范围和失败边界

本项目新增公共HTTP原文获取尝试42次：SHFE27、GFEX6、DCE4、CZCE4、INE1。33次返回HTTP200，其中INE直接页面是WAF，明确拒收为业务证据；32份是29个产品页、2份页面引用的官方API脚本和1份EC所在合约参数JSON。另9次未取得业务文件。HTTP尝试时段为2026-10-16约09:13～09:20 CST，逐次时刻、域名、HTTP结果/异常类型见[acquisition-assessment.json](acquisition-assessment.json)。早期HTTPError没有记录数字状态码，明确为null，不猜403或其他代码。

DCE试过官方首页HTTPS、HTTP、有限直连及已知日内规则页；CZCE试过官方首页/中文入口的HTTPS、HTTP及有限直连。失败后停止重试。GFEX HTTPS失败后官方HTTP成功；INE直接页WAF后采用SHFE官方交叉发布入口，未登录或绕供应商权限。网页检索仅用于定位链接，缓存结果未冒充本轮原始响应。未启动浏览器，未访问付费或登录数据源。

复用范围包括：M01的67快照/67结算参数/19CZCE量纲/66生命周期、办公室10-16限定24费raw和91日日历、办公室既有两费用公告原文以及SHFE/INE节假日/时段原文。本项目数据库调用为0，没有重复原13条查询；办公室本轮只读SELECT计数和回滚收据保留在[office/response.md](office/response.md)和[office/evidence-readonly.json](office/evidence-readonly.json)。

## 3. 禁用配置、时点与模型差异

[account.disabled.json](account.disabled.json)显式`enabled=false/runtime_ready=false/executable=false`，仍沿用NAV100,000,000 CNY、单位净值1、nearest_integer_ties_even及既有现金/融资规则。原账户标识/起点/版本、费用政策等未批准项继续未填；原10-15参数窗口没有延长。

[67行证据矩阵](contract-evidence.json)保留每份来源、已知字段及逐合约missing_fields。[model-and-input-gaps.md](model-and-input-gaps.md)列明四费映射、日内开仓政策、经纪商费用/保证金加收、滑点、最低费/取整/优惠、未计项、下单最小量/限额及实际报价缺项。交易所标准、内部快照、实际经纪商要求分开；不默认免费或研究4bp。没有证据支持的经济有效起止和原始发布时间保持null。

[calendar.disabled.json](calendar.disabled.json)覆盖2026-09-01～11-30的91自然日，内部sfe开市标志59个；这不是五所完整会话认证。已复用SHFE中秋/国庆休市安排、INE9-21通知和日盘/夜盘时段原文，区分节前无夜盘、节后日盘先开和当晚恢复。品种/假期与前晚、跨午夜的交易日映射尚未形成可执行区间，`complete=false`。D尚未选择，逐日execution/settlement均为空，不伪造未来D及其后两个观察日。

## 4. 1亿元整数尺度准备检查

仅使用冻结的10-15**收盘参考价**、原生权重和内部乘数/同日保证金；未模拟成交、未生成收益或账本。见[逐合约整数计算](integer-preparation.csv)及[汇总](preparation-check.json)。

|指标|结果|
|---|---:|
|原生gross|2.2617946883905694524|
|整数参考gross|2.2598081|
|整数参考净权重|−0.00009540|
|权重绝对偏差之和|0.0215810422474054896（2.1581个百分点）|
|最大单约绝对偏差|0.004031719587148965（0.40317个百分点）|
|交易所保证金参考额|22,048,152.60 CNY（22.0481526% NAV）|
|未知费用/加收前参考余量|77,951,847.40 CNY|

**AU2612.SHF原非零目标对应−0.4511772143手，被既定规则取整为0**；最大单约偏差即来自AU。PB原生零权重保留。没有扩大本金、删合约或重新归一化目标。`funding_ready=false`：19个DCE量纲、有效保证金/经纪商加收、费用、实际执行价和下单限制未齐，此参考余量不能证明真实资金约束通过，也不能承诺整数账户精确复制原策略。

## 5. 原生日更目标可用性

[native-target-inventory.json](native-target-inventory.json)记录实际存在的9-10、9-14目标和已捕获9-16目标，以及67行10-15固定归档复验目标。前三份为68行旧产物，不冒充本批67行新输入。10-15复验目标的真实发布为20:06:21 CST，不能倒填日盘前。未见已验收的10-16或未来每日目标；文件mtime不当作可用时点。

如另行接受新增当前目标计算范围，唯一建议入口是已修正零权重投影的`forward_inputs.m01 --as-of ... --production-observation ... --output-dir <new-dir>`；所需原生R150/T+1/252波动率版本、真实生产来源观察、五所实际日线覆盖和900日暖机输入，以及2GB/单线程约束均已列在清单。本轮没有运行或重新拉取900日历史。

## 6. 验证与交付边界

[validation.json](validation.json)：**533项离线证据断言通过**，包括全部原文/压缩SHA、67行原权重、29个乘数维度匹配、EC原始行、24单位继续为空、DCE双来源链、逐合约整数算术、91日日历完整性和禁用状态。Ruff检查通过。没有改生产模型代码，因此没有重跑旧99项测试。

只验证本次数据的复核命令（不联网/查库/初始化）：

```sh
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python docs/requests/2026-10-16-m02-real-inputs/verify_delivery.py'
```

新资料缺项及失败访问已具体列出；汇总方可据此继续官方原文补证和统一经济政策提案。本次未付费、未共享库写入/DDL、未重算策略/目标、未初始化任何账本（含合成账本）、未部署/定时器/交易收益通知或远端推送。其他会话改动保持原样。
