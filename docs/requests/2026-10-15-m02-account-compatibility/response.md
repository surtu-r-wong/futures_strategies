# M02 回函：逐合约费用适配已完成，真实候选仍未就绪

代码、测试、67合约候选及合成账本本地提交：`2096def`（`feat: add dated contract fees and M02 offline accounting evidence`）。本回函另作文档提交；未推送。

已实现 `contract-fees-v1` 的显式元/手、千分比、万分比换算、逐合约有效日期和来源、FIFO日期持仓、开仓/隔日平仓/平今/反手分拆，以及交易所费用、经纪商附加费、滑点、开仓调整的独立对账。`open_fee` 同时用于多空普通开仓；`short_open_fee` 仅映射日内开仓。经纪商口径、单位和政策来源未知时拒绝配置。字段映射、条件化日内开仓政策及实现边界详见 [fee-model.md](fee-model.md)。

旧 `cost_bps` 公式保留为独立 `uniform-bps-v1`，与新配置互斥。账户配置及引擎指纹不可变；旧状态没有开仓日期证据时不得直接接入新模型，必须另行批准迁移/新账户版本。没有迁移旧账或拼接历史收益。ReturnFeed schema保持原样，新旧模型有各自独立策略version和fee_model说明。

候选：[account.candidate.json](candidate/account.candidate.json)、[逐合约来源](candidate/contract-provenance.json)、[manifest](candidate/manifest.json)。完整保留67个原生目标，包括PB零权重，未截取43合约重新归一化，未再次加杠杆或套入汇总样例0.4权重。67个乘数及同日保证金交叉核验一致，多空保证金均相同。郑商所三位/四位代码已正确规范化，保留66条独立生命周期，唯一缺项为EC2611.INE；EC乘数50、保证金17%已保留。

候选沿用汇总方已准备的1亿元观察尺度、初始单位净值1、整数手数 `nearest_integer_ties_even` 与既有现金/资金规则，来源摘要见 [accounting-candidate-extract.json](accounting-candidate-extract.json)。这不是用户资产配置，也不重新要求用户选择资本。所有乘数/保证金/费率窗口只覆盖2026-10-15，未外推。费用单位22个元/手、16个千分比、5个万分比；24个未知、48个报价币种/维度假设、经纪商费用/滑点/日内开仓政策等继续阻塞。完整精确字段、来源及拟核验范围见 [input-checklist.md](input-checklist.md)。首个实际执行区间、账户起点和版本仍未批准。

合成证据（不是027实际净收益）：

- [多日账本](synthetic-evidence/synthetic-ledger.json)：初态、每日NAV/费用、成交分拆、持仓批次和来源。
- [ReturnFeed](synthetic-evidence/synthetic-returns.json)：两个独立版本序列，每个2日；现有schema校验通过。
- [手算预期](synthetic-evidence/hand-calculated-expected.json)、[重放manifest](synthetic-evidence/manifest.json)：输入配置/日历/目标/报价与完整packets均在两个模型子目录内。临时SQLite已销毁，JSON账本可重放。

|合成版本|10-13费用|10-13 NAV|10-14费用|10-14 NAV|
|---|---:|---:|---:|---:|
|contract-fees-v1|50|11,700|102.5|12,727.5|
|uniform-bps-v1，10bp、整数手数|20|11,730|33.72|12,826.28|

两者均使用虚构本金10,000、A乘数10/B乘数5；第一日持仓A+15/B−5，价差收益1,750。新模型首日 `(15+5)*(2+0.5)=50`；次日隔夜盈亏350、平A15/B5并开B13，费用 `20*(3+0.5)+13*(2+0.5)=102.5`，当日价差收益780。旧模型次日反手总名义额33,720，费用33.72。均未重复应用目标杠杆。

同日平今由纯费用函数独立合成验证：昨日2手、今日再开1手，减至0.5手，FIFO平昨费7元、平今费3.75元、开仓调整−0.5元，合计10.25元；之后反手为−1手，剩余平今费用3.25元、新开费用2.5元。多空方向各验证一次。日结账户接口仍是每日一次日盘执行，未扩展为盘中/夜盘撮合系统。

从仓库根目录重放到新目录（仅临时合成账户）：

```sh
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m forward_account.fee_demo --output-dir /tmp/m02-fee-replay-review'
```

目录必须尚不存在。经济结果固定，实际生成时间及与之相关的发布散列会随重放改变。若代码有变，引擎指纹和收益版本也相应变化。新旧输入可分别按子目录config/calendar/interval通过原CLI在临时测试目录重放。

验证：账户旧模型27项、新费用16项、输入适配27项、候选映射2项、基础分钟账户27项，共 **99 passed**，详见 [pytest-results.txt](pytest-results.txt)。覆盖费率单位换算、方向、隔日/平今/反手、比例开仓调整使用原开仓价、有效日期切换与过期拒绝、未知单位/经纪商/政策拒绝、费用后保证金不足不提交、日期证据与规则散列不匹配拒绝、重复日结不重扣、尾部修订不累计扣费、浮点持仓变化和新旧Feed版本隔离。Ruff检查通过；本地来源文件SHA-256与候选manifest一致。测试范围不等于全仓测试。合成CSV沿用现有生成器CRLF，收据散列保留原字节；差异检查采用`cr-at-eol`识别正常CSV行尾。

本次未重跑策略、未取新数据库/网络/供应商数据、未写共享库、未初始化或推进真实账户、未写正式`returns/latest.json`、未部署或发交易通知。仅初始化过临时合成测试SQLite，测试目录生命周期结束即删除。其他人的未提交修改保持原样。

向汇总会话 `01a13f0e-e2ab-7c83-ad9b-bbd87800b49f` 的主动回通知已尝试，但本地MCP传输连接失败；本回函与证据不依赖通知成功，汇总方可直接读取。该失败不是审批拒绝，也不影响本地交付。
